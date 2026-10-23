"""
speed_test.py — 测速模块
两段式稳态速率算法照 iptv-hub 实测调优：
  512KB 预热段丢弃（CDN 边缘预缓冲会骗人）→ 1MB 采样段 → 8s 首窗口
  → 未过预热且均速 <256KB/s → 判定慢启动 → 延长到 12s
  稳态样本 <32KB → 退回整段平均（防噪声）
状态机：fail>=3 剔除 → 14 天复活 → 复活过且仍失效 90 天 purge
"""
import asyncio
import json
import os
import re
import sys
import time
import aiohttp
import yaml

sys.path.insert(0, os.path.dirname(__file__))
from common import (d1_query, d1_batch, start_run, finish_run, utc_now,
                    hours_ago, days_ago, fingerprint, is_private_host)

CONFIG_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "config", "sources.yaml")
with open(CONFIG_PATH, encoding="utf-8") as f:
    CFG = yaml.safe_load(f)

S = CFG["speed"]
SM = CFG["statemachine"]
WARMUP = S["warmup_bytes"]            # 512KB
SAMPLE = S["sample_bytes"]            # 1MB
TIMEOUT = S["timeout"]                # 8s
STALL_TIMEOUT = S["stall_timeout"]    # 12s
MIN_STEADY = S["min_steady_bytes"]    # 32KB
SLOW_KBPS = S["slow_window_kbps"]     # 256
MAX_REDIRECTS = S["max_redirects"]
CONCURRENCY = S["concurrency"]
MAX_ENTRIES = S["max_entries_per_run"]

DEFAULT_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
AD_KEYWORDS = ["advertisement", "purevideoad", "youku_ad", "cacheplayvideo"]


def pending_entries() -> list[dict]:
    """五档预算查询（照 iptv-hub auto_budget 分档，自用规模直接全取）"""
    sql = """
    SELECT e.id, e.fingerprint, e.url, e.headers_json
    FROM entries e
    WHERE e.active=1 AND e.status NOT IN ('purged','unsupported')
      AND (
        e.last_check_at = ''                                        -- 未测：立即
        OR (e.fail_count > 0 AND e.last_fail_at <= ?)               -- 连续失败 20h
        OR (e.status='error' AND e.last_fail_at <= ?)               -- 状态级失效
        OR (e.status='ok' AND e.last_check_at <= ?)                 -- ok 72h 复检
      )
    ORDER BY CASE WHEN e.fail_count > 0 THEN 0 ELSE 1 END, e.last_check_at
    LIMIT ?
    """
    return d1_query(sql, [hours_ago(SM["fail_retest_hours"]),
                          hours_ago(SM["fail_retest_hours"]),
                          hours_ago(SM["retest_ok_hours"]),
                          MAX_ENTRIES])


class TargetNotMeasurable(Exception):
    """内网/解析失败：跳过不计入失效"""


async def open_url(session: aiohttp.ClientSession, url: str, headers: dict):
    """手动逐跳跟随 + 内网守卫（照 iptv-hub：关自动跳转防 SSRF）"""
    current = url
    for _ in range(MAX_REDIRECTS + 1):
        host = (__import__("urllib.parse", fromlist=["urlsplit"]).urlsplit(current).hostname or "")
        if is_private_host(host):
            raise TargetNotMeasurable(f"private host: {host}")
        resp = await session.get(current, headers=headers, allow_redirects=False,
                                 ssl=False)
        if resp.status in (301, 302, 303, 307, 308):
            loc = resp.headers.get("Location", "")
            if not loc:
                return resp
            resp.release()
            current = __import__("urllib.parse", fromlist=["urljoin"]).urljoin(current, loc)
            continue
        return resp
    raise TargetNotMeasurable("too many redirects")


async def measure_one(session: aiohttp.ClientSession, url: str, headers: dict) -> dict:
    """两段式稳态测速，返回结果 dict"""
    start = time.perf_counter()
    latency, total, status = None, 0, 0
    warmup_at, extended, deadline = None, False, TIMEOUT
    http_error, read_error = "", ""

    def out_of_time() -> bool:
        return (time.perf_counter() - start) >= deadline

    def try_extend() -> bool:
        nonlocal extended, deadline
        if extended:
            return False
        if total < 32768 or (total < WARMUP and
                             total / 1024 / max(time.perf_counter() - start, 0.001) < SLOW_KBPS):
            extended = True
            deadline = max(deadline, STALL_TIMEOUT)
            return True
        return False

    urls = [url]
    # HLS：先拿 m3u8 分片列表，最多取前 3 个分片连续读（照 iptv-hub 多分片采样）
    playlist_info = None
    try:
        async with await open_url(session, url, headers) as r0:
            if r0.status < 400:
                body = await r0.content.read(128 * 1024)
                text = body.decode("utf-8", "ignore")
                if "#EXT-X-STREAM-INF" in text:
                    playlist_info = parse_master(text)
                elif "#EXTINF" in text:
                    segs = re.findall(r"^(?!#)(\S+)$", text, re.M)
                    base = url.rsplit("/", 1)[0] + "/"
                    urls += [segs[i] if segs[i].startswith("http") else base + segs[i]
                             for i in range(min(3, len(segs)))]
                    if not re.search(r"#EXT-X-ENDLIST", text):
                        pass  # 直播流无 ENDLIST 正常
                if AD_KEYWORDS and any(k in text.lower() for k in AD_KEYWORDS):
                    return {"ok": 0, "error": "ad-loop", "resolution": "",
                            "speed_kbps": 0, "avg_kbps": 0, "latency_ms": 0,
                            "http_status": r0.status, "duration_ms": 0}
            else:
                return {"ok": 0, "error": f"HTTP {r0.status}", "resolution": "",
                        "speed_kbps": 0, "avg_kbps": 0, "latency_ms": 0,
                        "http_status": r0.status, "duration_ms": 0}
    except TargetNotMeasurable as e:
        raise
    except Exception as e:
        return {"ok": 0, "error": f"{type(e).__name__}: {e}"[:80], "resolution": "",
                "speed_kbps": 0, "avg_kbps": 0, "latency_ms": 0,
                "http_status": 0, "duration_ms": 0}

    for u in urls:
        if total >= SAMPLE or (out_of_time() and not try_extend()):
            break
        try:
            resp = await open_url(session, u, headers)
            if resp.status >= 400:
                http_error = f"HTTP {resp.status}"
                status = resp.status
                await resp.release()
                break
            status = resp.status
            async for chunk, _ in resp.content.iter_chunks():
                data = chunk
                if latency is None:
                    latency = int((time.perf_counter() - start) * 1000)
                total += len(data)
                if warmup_at is None and total >= WARMUP:
                    warmup_at = time.perf_counter() - start
                if out_of_time():
                    if try_extend():
                        continue
                    break
                if total >= SAMPLE:
                    break
            await resp.release()
        except TargetNotMeasurable:
            if total > 0:
                break
            raise
        except Exception as e:
            read_error = f"{type(e).__name__}: {e}"[:80]
            break

    duration_ms = int((time.perf_counter() - start) * 1000)
    if total <= 0:
        return {"ok": 0, "error": http_error or read_error or "no-data",
                "resolution": "", "speed_kbps": 0, "avg_kbps": 0,
                "latency_ms": latency or 0, "http_status": status,
                "duration_ms": duration_ms}

    elapsed = max(time.perf_counter() - start, 0.001)
    avg_kbps = total / 1024 / elapsed
    if warmup_at is not None and (total - WARMUP) >= MIN_STEADY and \
            (elapsed - warmup_at) >= 0.1:
        steady_kbps = (total - WARMUP) / 1024 / (elapsed - warmup_at)
    else:
        steady_kbps = avg_kbps  # 稳态样本不足退回整段平均

    resolution = (playlist_info or {}).get("resolution", "")
    return {"ok": 1, "error": "", "resolution": resolution,
            "speed_kbps": round(steady_kbps, 1), "avg_kbps": round(avg_kbps, 1),
            "latency_ms": latency or 0, "http_status": status,
            "duration_ms": duration_ms}


def parse_master(text: str) -> dict:
    """解析 EXT-X-STREAM-INF 多码率子流，取最高 BANDWIDTH 的 RESOLUTION"""
    best = {"bandwidth": 0, "resolution": ""}
    for m in re.finditer(r"#EXT-X-STREAM-INF:([^\n]+)", text):
        attrs = dict(re.findall(r'([\w-]+)="?([^",]+)"?', m.group(1)))
        try:
            bw = int(attrs.get("BANDWIDTH", 0))
        except Exception:
            bw = 0
        if bw >= best["bandwidth"]:
            best = {"bandwidth": bw,
                    "resolution": attrs.get("RESOLUTION", "")}
    return best


def apply_statemachine(results: list[dict], entry_ids: list[int], id_map: dict) -> list[dict]:
    """把测速结果翻译成 entries/stream_health 的批量更新 + 复活/purge"""
    stmts = []
    for r in results:
        eid = r["entry_id"]
        fp = r["fingerprint"]
        if r["ok"]:
            stmts.append({
                "sql": """UPDATE entries SET status='ok', fail_count=0, last_check_at=?,
                          active=1, updated_at=datetime('now') WHERE id=?""",
                "params": [utc_now(), eid]})
            stmts.append({
                "sql": """INSERT INTO stream_health (fingerprint, ok_count, fail_count,
                            last_ok_at, last_speed_kbps, best_speed_kbps, last_resolution, status)
                          VALUES (?, 1, 0, ?, ?, ?, ?, 'ok')
                          ON CONFLICT(fingerprint) DO UPDATE SET
                            ok_count=ok_count+1, fail_count=0, last_ok_at=excluded.last_ok_at,
                            last_speed_kbps=excluded.last_speed_kbps,
                            best_speed_kbps=MAX(best_speed_kbps, excluded.best_speed_kbps),
                            last_resolution=excluded.last_resolution, status='ok'""",
                "params": [fp, utc_now(), r["speed_kbps"], r["speed_kbps"],
                           r["resolution"]]})
        else:
            stmts.append({
                "sql": """UPDATE entries SET fail_count=fail_count+1, last_fail_at=?,
                          last_check_at=?,
                          active=CASE WHEN fail_count+1 >= ? THEN 0 ELSE active END,
                          status=CASE WHEN fail_count+1 >= ? THEN 'error' ELSE status END,
                          updated_at=datetime('now') WHERE id=?""",
                "params": [utc_now(), utc_now(), SM["prune_max_fail"],
                           SM["prune_max_fail"], eid]})
            stmts.append({
                "sql": """INSERT INTO stream_health (fingerprint, ok_count, fail_count,
                            last_fail_at, status)
                          VALUES (?, 0, 1, ?, 'error')
                          ON CONFLICT(fingerprint) DO UPDATE SET
                            fail_count=fail_count+1, last_fail_at=excluded.last_fail_at,
                            status='error'""",
                "params": [fp, utc_now()]})
        stmts.append({
            "sql": """INSERT INTO speed_tests (entry_id, fingerprint, ok, http_status,
                        speed_kbps, avg_kbps, latency_ms, resolution, error, duration_ms)
                      VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            "params": [eid, fp, r["ok"], r["http_status"], r["speed_kbps"],
                       r["avg_kbps"], r["latency_ms"], r["resolution"],
                       r["error"], r["duration_ms"]]})
    return stmts


def revival_and_purge() -> dict:
    """复活冷却期满线路 + 季度清理（照 iptv-hub revive_expired/purge_revived_streams）"""
    out = {"revived": 0, "purged": 0}
    # 复活：剔除（active=0）且 fail_count>=阈值 且 last_fail_at 超过 14 天
    rows = d1_query("""SELECT id FROM entries WHERE active=0 AND fail_count>=?
                       AND last_fail_at<>'' AND last_fail_at<=?""",
                    [SM["prune_max_fail"], days_ago(SM["retest_after_days"])])
    if rows:
        ids = [r["id"] for r in rows]
        d1_batch([{"sql": """UPDATE entries SET active=1, fail_count=0,
                             revive_count=revive_count+1, status='ok',
                             last_check_at='', updated_at=datetime('now') WHERE id=?""",
                   "params": [i]} for i in ids])
        out["revived"] = len(ids)
    # 季度清理：复活过且仍失效 → purged（永不复活）
    rows = d1_query("""SELECT id FROM entries WHERE revive_count>=? AND active=0
                       AND status='error' AND last_fail_at<>'' AND last_fail_at<=?""",
                    [SM["purge_min_revive"], days_ago(SM["purge_interval_days"])])
    if rows:
        ids = [r["id"] for r in rows]
        d1_batch([{"sql": "UPDATE entries SET status='purged', active=0, updated_at=datetime('now') WHERE id=?",
                   "params": [i]} for i in ids])
        out["purged"] = len(ids)
    return out


async def main():
    run_id = start_run("speed")
    entries = pending_entries()
    summary = {"pending": len(entries), "tested": 0, "ok": 0, "fail": 0,
               "skipped": 0, "revived": 0, "purged": 0}

    sem = asyncio.Semaphore(CONCURRENCY)
    connector = aiohttp.TCPConnector(limit=CONCURRENCY + 4, ssl=False)

    async with aiohttp.ClientSession(connector=connector) as session:
        results = []
        for i in range(0, len(entries), CONCURRENCY):
            batch = entries[i:i + CONCURRENCY]
            done = await asyncio.gather(*[measure_entry(session, sem, e) for e in batch])
            results.extend(done)

    tested = [r for r in results if not r.get("skipped")]
    skipped = [r for r in results if r.get("skipped")]
    summary.update({"tested": len(tested), "skipped": len(skipped),
                    "ok": sum(1 for r in tested if r["ok"]),
                    "fail": sum(1 for r in tested if not r["ok"])})

    if tested:
        stmts = apply_statemachine(tested, [t["entry_id"] for t in tested], {})
        d1_batch(stmts)
    rp = revival_and_purge()
    summary.update(rp)

    # 信誉分更新（源级：可用率）
    d1_batch([{
        "sql": """UPDATE sources SET reputation=
                    ROUND(0.5 + 0.5 * (SELECT COALESCE(AVG(ok),0) FROM speed_tests
                     WHERE speed_tests.ts >= datetime('now','-3 days')
                       AND speed_tests.entry_id IN
                        (SELECT id FROM entries WHERE source_id=sources.id)), 3),
                  updated_at=datetime('now')"""}])

    finish_run(run_id, summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))


async def measure_entry(session: aiohttp.ClientSession, sem: asyncio.Semaphore, entry: dict) -> dict:
    async with sem:
        headers = {"User-Agent": DEFAULT_UA, "Accept": "*/*", "Connection": "close"}
        extra = json.loads(entry.get("headers_json") or "{}")
        headers.update(extra)
        try:
            result = await measure_one(session, entry["url"], headers)
        except TargetNotMeasurable:
            return {"entry_id": entry["id"], "fingerprint": entry["fingerprint"],
                    "ok": 0, "error": "not-measurable", "resolution": "",
                    "speed_kbps": 0, "avg_kbps": 0, "latency_ms": 0,
                    "http_status": 0, "duration_ms": 0, "skipped": True}
        result.update({"entry_id": entry["id"], "fingerprint": entry["fingerprint"]})
        return result


if __name__ == "__main__":
    asyncio.run(main())
