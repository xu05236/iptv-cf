"""
fetch_sources.py — 采集模块
多源并发抓取 + 条件请求（ETag/Last-Modified）+ 五格式解析 + upsert 进 D1
"""
import asyncio
import json
import re
import sys
import os
import yaml
import aiohttp

sys.path.insert(0, os.path.dirname(__file__))
from common import (d1_query, d1_batch, d1_exec, start_run, finish_run,
                    utc_now, fingerprint, normalize_url, hours_ago)

CONFIG_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "config", "sources.yaml")
with open(CONFIG_PATH, encoding="utf-8") as f:
    CFG = yaml.safe_load(f)

MAX_SOURCE_BYTES = 16 * 1024 * 1024
SYNC_CONCURRENCY = 6
REQUEST_TIMEOUT = 45
PROXY = os.environ.get("SOURCE_PROXY", "").strip()
PROXY_HOSTS = set(h.strip().lower().rstrip(".")
                  for h in os.environ.get("SOURCE_PROXY_HOSTS", "").split(",") if h.strip())

USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")


def detect_format(content: bytes, url: str = "") -> str:
    head = content[:2048].decode("utf-8", "ignore").lstrip()
    low = head.lower()
    if low.startswith("#extm3u"):
        return "m3u"
    if b"<hwctc" in content[:4096].lower():
        return "hwctc"
    if low.startswith("<?xml") or low.startswith("<"):
        return "hwctc"
    if low.startswith("{"):
        try:
            j = json.loads(head)
        except Exception:
            return "unknown"
        if isinstance(j, dict) and "lives" in j:
            return "tvbox"
        if isinstance(j, dict) and "channels" in j:
            return "json"
        return "json"
    if low.startswith("["):
        return "json"
    # 文本：包含 #genre# 或 名称,url 行 → txt
    if "#genre#" in low or re.search(r"^[^,\n]+[,，]\s*https?://", head, re.M):
        return "txt"
    return "unknown"


def parse_extinf_attrs(line: str) -> tuple[dict, str]:
    """解析 #EXTINF 的属性与显示名，返回 (attrs, display_name)"""
    attrs = {}
    m = re.match(r'#EXTINF:[^,]*,(.*)$', line, re.S)
    display = m.group(1).strip() if m else ""
    for k, v in re.findall(r'([\w-]+)="([^"]*)"', line):
        attrs[k.lower()] = v
    return attrs, display


def parse_m3u(content: bytes) -> list[dict]:
    entries, attrs, display = [], {}, ""
    extopts = {}
    for raw in content.decode("utf-8", "ignore").splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#EXTINF:"):
            attrs, display = parse_extinf_attrs(line)
            extopts = {}
        elif line.startswith("#EXTVLCOPT:"):
            opt = line[len("#EXTVLCOPT:"):].strip()
            if "=" in opt:
                k, v = opt.split("=", 1)
                k = k.strip().lower()
                if k == "http-user-agent":
                    extopts["user-agent"] = v.strip()
                elif k in ("http-referrer", "referer"):
                    extopts["referer"] = v.strip()
        elif line.startswith("#") or line.startswith("{"):
            continue
        elif "://" in line and display:
            headers = {}
            if extopts.get("user-agent"):
                headers["User-Agent"] = extopts["user-agent"]
            if extopts.get("referer"):
                headers["Referer"] = extopts["referer"]
            entries.append({
                "url": line.strip(),
                "name": display or attrs.get("tvg-name", ""),
                "tvg_id": attrs.get("tvg-id", ""),
                "tvg_logo": attrs.get("tvg-logo", ""),
                "group_title": attrs.get("group-title", ""),
                "headers": headers,
                "catchup": attrs.get("catchup", ""),
                "catchup_source": attrs.get("catchup-source", ""),
                "catchup_days": attrs.get("catchup-days", ""),
            })
            display = ""
    return entries


def parse_txt(content: bytes) -> list[dict]:
    entries, group = [], ""
    for raw in content.decode("utf-8", "ignore").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "#genre#" in line:
            group = line.split(",")[0].replace("#genre#", "").strip()
            continue
        # 名称,url 或 名称，url（全角逗号）
        parts = re.split(r"[,，]", line, maxsplit=1)
        if len(parts) == 2 and "://" in parts[1]:
            entries.append({
                "url": parts[1].strip(),
                "name": parts[0].strip(),
                "tvg_id": "", "tvg_logo": "",
                "group_title": group or "",
                "headers": {}, "catchup": "", "catchup_source": "", "catchup_days": "",
            })
    return entries


def parse_json(content: bytes) -> list[dict]:
    try:
        j = json.loads(content.decode("utf-8", "ignore"))
    except Exception:
        return []
    entries = []
    chans = j.get("channels", j) if isinstance(j, dict) else j
    if isinstance(chans, list):
        for c in chans:
            if not isinstance(c, dict):
                continue
            url = c.get("url") or (c.get("urls") or [None])[0]
            if not url:
                continue
            entries.append({
                "url": url, "name": str(c.get("name", "")),
                "tvg_id": str(c.get("tvg_id", c.get("epg_id", "")) or ""),
                "tvg_logo": str(c.get("tvg_logo", c.get("logo", "")) or ""),
                "group_title": str(c.get("group", c.get("group_title", "")) or ""),
                "headers": {}, "catchup": "", "catchup_source": "", "catchup_days": "",
            })
    return entries


def parse_tvbox(content: bytes) -> tuple[list[dict], list[str]]:
    """TVBox 配置：返回 (自身直接条目, lives[].url 子地址)"""
    try:
        j = json.loads(content.decode("utf-8", "ignore"))
    except Exception:
        return [], []
    entries, sub_urls = [], []
    for live in j.get("lives", []) or []:
        group = live.get("group", live.get("name", "")) or ""
        for c in live.get("channels", []) or []:
            urls = c.get("urls") or ([c.get("url")] if c.get("url") else [])
            for u in urls:
                if not u:
                    continue
                entries.append({
                    "url": u, "name": str(c.get("name", "")),
                    "tvg_id": "", "tvg_logo": "", "group_title": group,
                    "headers": {}, "catchup": "", "catchup_source": "", "catchup_days": "",
                })
    for live in j.get("lives", []) or []:
        u = live.get("url")
        if u and "://" in u:
            sub_urls.append(u)
    return entries, sub_urls


def parse_hwctc(content: bytes) -> list[dict]:
    """华为 HWCTC XML：正则抓 ChannelItem 的 name/url"""
    text = content.decode("utf-8", "ignore")
    entries = []
    for m in re.finditer(r"<(?:Channel|ChannelItem|Service)[^>]*>", text, re.I):
        tag = m.group(0)
        name = re.search(r'(?:Name|ChannelName|name)="([^"]*)"', tag, re.I)
        url = re.search(r'(?:URL|MediaURL|url|PlayUrl)="([^"]*)"', tag, re.I)
        if name and url and "://" in url.group(1):
            entries.append({
                "url": url.group(1), "name": name.group(1),
                "tvg_id": "", "tvg_logo": "", "group_title": "",
                "headers": {}, "catchup": "", "catchup_source": "", "catchup_days": "",
            })
    if not entries:
        # 降级：任意 中文名 + URL 成对出现
        for m in re.finditer(r'([\u4e00-\u9fff][\u4e00-\u9fff\w\-]{1,30})["\s>]+(https?://[^\s"<]+)', text):
            entries.append({
                "url": m.group(2), "name": m.group(1),
                "tvg_id": "", "tvg_logo": "", "group_title": "",
                "headers": {}, "catchup": "", "catchup_source": "", "catchup_days": "",
            })
    return entries


def parse_entries(fmt: str, content: bytes):
    if fmt == "m3u":
        return parse_m3u(content), []
    if fmt == "txt":
        return parse_txt(content), []
    if fmt == "json":
        return parse_json(content), []
    if fmt == "tvbox":
        return parse_tvbox(content)
    if fmt == "hwctc":
        return parse_hwctc(content), []
    return [], []


def host_needs_proxy(url: str) -> bool:
    host = urlparse_host(url)
    return host in PROXY_HOSTS or any(host.endswith("." + h) for h in PROXY_HOSTS)


def urlparse_host(url: str) -> str:
    from urllib.parse import urlsplit
    return (urlsplit(url).hostname or "").lower().rstrip(".")


async def fetch_source(session: aiohttp.ClientSession, src: dict, sem: asyncio.Semaphore):
    """返回 (src, entries, status, etag, last_modified, error)"""
    async with sem:
        url, fmt = src["url"], src.get("type") or "auto"
        headers = {"User-Agent": USER_AGENT}
        headers.update(json.loads(src.get("headers_json") or "{}"))
        if src.get("etag"):
            headers["If-None-Match"] = src["etag"]
        if src.get("last_modified"):
            headers["If-Modified-Since"] = src["last_modified"]
        proxy = PROXY if (PROXY and host_needs_proxy(url)) else None
        try:
            timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)
            async with session.get(url, headers=headers, proxy=proxy, timeout=timeout) as resp:
                if resp.status == 304:
                    return src, None, "not-modified", "", "", ""
                if resp.status >= 400:
                    return src, [], "error", "", "", f"HTTP {resp.status}"
                body = await resp.content.read(MAX_SOURCE_BYTES + 1)
                if len(body) > MAX_SOURCE_BYTES:
                    return src, [], "error", "", "", "oversize"
                etag = resp.headers.get("ETag", "")
                lm = resp.headers.get("Last-Modified", "")
                if fmt == "auto":
                    fmt = detect_format(body, url)
                entries, sub_urls = parse_entries(fmt, body)
                if fmt == "tvbox":
                    src["sub_urls"] = sub_urls
                return src, entries, "ok", etag, lm, ""
        except Exception as e:
            return src, [], "error", "", "", f"{type(e).__name__}: {e}"[:200]


def build_statements(src: dict, entries: list[dict]) -> list[dict]:
    """生成 upsert 语句：新条目 INSERT，已有条目 UPDATE 元数据（源里消失的 active=0）"""
    stmts = []
    seen = set()
    for e in entries:
        fp = fingerprint(e["url"])
        if fp in seen:
            continue
        seen.add(fp)
        headers = json.dumps(e["headers"] or {}, ensure_ascii=False)
        attrs = json.dumps({"catchup": e.get("catchup", ""),
                            "catchup_source": e.get("catchup_source", ""),
                            "catchup_days": e.get("catchup_days", "")}, ensure_ascii=False)
        region = "domestic" if ".cn" in urlparse_host(e["url"]) or re.search(
            r"[\u4e00-\u9fff]", e.get("name", "")) else ""
        stmts.append({
            "sql": """INSERT INTO entries (source_id, fingerprint, url, name_raw, tvg_id, tvg_logo,
                        group_title, headers_json, attrs_json, region, last_check_at)
                      VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '')
                      ON CONFLICT(source_id, fingerprint) DO UPDATE SET
                        url=excluded.url, name_raw=excluded.name_raw,
                        tvg_id=excluded.tvg_id, tvg_logo=excluded.tvg_logo,
                        group_title=excluded.group_title, headers_json=excluded.headers_json,
                        attrs_json=excluded.attrs_json, region=excluded.region,
                        updated_at=datetime('now')""",
            "params": [src["id"], fp, e["url"], e["name"], e["tvg_id"], e["tvg_logo"],
                       e["group_title"], headers, attrs, region],
        })
    # 源里已消失的条目：active=0（不带 fail_count 条件，不进复活通道——照 iptv-hub）
    stmts.append({
        "sql": """UPDATE entries SET active=0, updated_at=datetime('now')
                  WHERE source_id=? AND active=1
                    AND fingerprint NOT IN (SELECT value FROM json_each(?))""",
        "params": [src["id"], json.dumps(list(seen) or [""])],
    })
    return stmts


async def main():
    run_id = start_run("fetch")
    # 仓库 YAML 源 + D1 里后台添加的源
    yaml_sources = {f"yaml:{s['name']}": dict(s) for s in CFG.get("sources", [])}
    db_sources = d1_query("SELECT id, type, url, name, headers_json, proxy_host, etag, "
                          "last_modified FROM sources WHERE enabled=1")
    sources = []
    for s in db_sources:
        s["proxy_host"] = bool(s.get("proxy_host"))
        sources.append(s)
    db_ids = {s["id"] for s in db_sources}
    for sid, s in yaml_sources.items():
        if sid not in db_ids:
            # yaml 源必须先落库，否则 etag/UPDATE 无处持久化，每轮都全量重抓
            d1_exec("""INSERT OR IGNORE INTO sources (id, type, url, name, headers_json, enabled)
                       VALUES (?, ?, ?, ?, '{}', 1)""",
                    [sid, s.get("type", "auto"), s["url"], s.get("name", "")])
        sources.append({
            "id": sid, "type": s.get("type", "auto"), "url": s["url"],
            "name": s.get("name", ""), "headers_json": "{}",
            "proxy_host": bool(s.get("proxy_host")), "etag": "", "last_modified": "",
        })

    connector = aiohttp.TCPConnector(limit=SYNC_CONCURRENCY * 2, ssl=False)
    sem = asyncio.Semaphore(SYNC_CONCURRENCY)
    summary = {"total_sources": len(sources), "fetched": 0, "not_modified": 0,
               "failed": 0, "new_entries": 0, "details": []}

    async with aiohttp.ClientSession(connector=connector) as session:
        for batch_start in range(0, len(sources), SYNC_CONCURRENCY):
            batch = sources[batch_start:batch_start + SYNC_CONCURRENCY]
            results = await asyncio.gather(
                *[fetch_source(session, s, sem) for s in batch], return_exceptions=True)
            for r in results:
                if isinstance(r, Exception):
                    summary["failed"] += 1
                    continue
                src, entries, status, etag, lm, error = r
                if status == "not-modified":
                    summary["not_modified"] += 1
                    d1_exec("UPDATE sources SET last_checked_at=? WHERE id=?",
                            [utc_now(), src["id"]])
                    continue
                if status == "error":
                    summary["failed"] += 1
                    summary["details"].append({"id": src["id"], "error": error})
                    d1_exec("""UPDATE sources SET status='error', error_message=?,
                               last_checked_at=?, updated_at=datetime('now') WHERE id=?""",
                            [error, utc_now(), src["id"]])
                    continue
                summary["fetched"] += 1
                if entries:
                    stmts = build_statements(src, entries)
                    d1_batch(stmts)
                # 更新源状态
                d1_exec("""UPDATE sources SET status='ok', etag=?, last_modified=?,
                           last_fetched_at=?, last_checked_at=?, entry_count=?,
                           error_message='', updated_at=datetime('now') WHERE id=?""",
                        [etag, lm, utc_now(), utc_now(), len(entries) if entries else 0, src["id"]])
                # TVBox 子地址：登记为子源（下次采集跟进）
                for sub in (src.get("sub_urls") or []):
                    slug = f"tvbox:{src['id']}:{fingerprint(sub)[:10]}"
                    d1_exec("""INSERT OR IGNORE INTO sources (id, type, url, name, enabled)
                               VALUES (?, 'm3u', ?, 'tvbox-child', 1)""", [slug, sub])

    finish_run(run_id, summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
