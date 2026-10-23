"""
fetch_epg.py — EPG 多源合并模块（Phase 4，照 laobaiepg 思路：Actions 抓取 → 合并 → KV 分发）
- 多源并发下载（支持 .xml / .gz / HTTP gzip）
- 频道白名单：只保留 D1 channels 表里 tvg_id 能匹配上的频道（无白名单时全量兜底）
- tvg-id 优先匹配，canonical_name/display-name 归一化兜底
- 同频道跨源合并，按 (channel, start, title) 去重
- 合并结果上传 publish/epg.xml（Worker 自动 purge 缓存）
- 写 runs 记录（kind='epg'）
"""
import gzip
import io
import json
import os
import re
import sys
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
import yaml

sys.path.insert(0, os.path.dirname(__file__))
from common import (d1_query, start_run, finish_run, upload_r2, utc_now,
                    canonical_name)

CONFIG_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "config", "sources.yaml")
with open(CONFIG_PATH, encoding="utf-8") as f:
    CFG = yaml.safe_load(f)

EPG_CFG = CFG.get("epg", {})
DEFAULT_SOURCES = [
    "https://live.fanmingming.com/e.xml",
    "https://epg.112114.xyz/pp.xml",
]
SOURCES = EPG_CFG.get("sources") or DEFAULT_SOURCES
MAX_EPG_BYTES = 64 * 1024 * 1024
REQUEST_TIMEOUT = 60
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")


def download_one(url: str) -> bytes:
    """下载一个 EPG 源，自动解 gzip（文件后缀 .gz 或 content-encoding: gzip）"""
    headers = {"User-Agent": USER_AGENT, "Accept": "*/*"}
    resp = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT, stream=True)
    resp.raise_for_status()
    chunks = []
    total = 0
    for chunk in resp.iter_content(1024 * 256):
        chunks.append(chunk)
        total += len(chunk)
        if total > MAX_EPG_BYTES:
            raise RuntimeError(f"{url}: oversize >{MAX_EPG_BYTES}")
    data = b"".join(chunks)
    if url.endswith(".gz") or resp.headers.get("Content-Encoding", "").lower() == "gzip" \
            or data[:2] == b"\x1f\x8b":
        data = gzip.decompress(data)
    return data


def parse_tv(xml_bytes: bytes) -> dict:
    """解析 XMLTV：返回 {channel_id: {'names': set(), 'programmes': {(start,title): (stop, desc)}}}"""
    out = {}
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        # 有些源头尾有非法字符，尝试清理后重试
        text = xml_bytes.decode("utf-8", "ignore")
        text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", text)
        root = ET.fromstring(text.encode("utf-8"))
    for ch in root.iter("channel"):
        cid = (ch.get("id") or "").strip()
        if not cid:
            continue
        entry = out.setdefault(cid, {"names": set(), "programmes": {}})
        for dn in ch.iter("display-name"):
            if dn.text:
                entry["names"].add(dn.text.strip())
    for pr in root.iter("programme"):
        cid = (pr.get("channel") or "").strip()
        start = (pr.get("start") or "").strip()
        if not cid or not start or cid not in out:
            continue
        title_el = pr.find("title")
        title = (title_el.text or "").strip() if title_el is not None else ""
        if not title:
            continue
        stop = (pr.get("stop") or "").strip()
        desc_el = pr.find("desc")
        desc = (desc_el.text or "").strip() if desc_el is not None else ""
        out[cid]["programmes"][(start, title)] = (stop, desc)
    return out


def build_whitelist() -> set:
    """从 D1 channels 表拿 tvg_id 集合 + canonical_name 集合，作为过滤白名单"""
    tvg_ids, names = set(), set()
    try:
        rows = d1_query("SELECT tvg_id, canonical_name FROM channels")
        for r in rows:
            tid = (r.get("tvg_id") or "").strip()
            if tid:
                tvg_ids.add(tid)
            nm = (r.get("canonical_name") or "").strip()
            if nm:
                names.add(nm)
                names.add(canonical_name(nm))
    except Exception:
        pass
    return tvg_ids, names


def normalize_epg_id(cid: str) -> str:
    """EPG 频道 id 归一化：CCTV-1 → CCTV1、去空格、常见后缀剥离"""
    n = cid.strip().replace(" ", "")
    n = re.sub(r"^CCTV[-_ ]*(\d+)", r"CCTV\1", n, flags=re.I)
    n = re.sub(r"(高清|HD|4K|8K|标清|SD)$", "", n, flags=re.I)
    return n


def match_channel(cid: str, names: set, tvg_ids: set, chan_names: set,
                  norm_cache: dict) -> bool:
    """判断 EPG 频道是否命中白名单：tvg-id 精确/归一化匹配 → display-name 匹配"""
    if not tvg_ids and not chan_names:
        return True  # 白名单拿不到（D1 空）就全量
    if cid in tvg_ids:
        return True
    nc = norm_cache.setdefault(cid, normalize_epg_id(cid))
    if nc in tvg_ids:
        return True
    if nc in chan_names:
        return True
    for nm in names:
        if nm in chan_names or canonical_name(nm) in chan_names or \
                norm_cache.setdefault(nm, normalize_epg_id(nm)) in tvg_ids:
            return True
    return False


def main():
    run_id = start_run("epg")
    summary = {"sources": [], "total_channels": 0, "total_programmes": 0,
               "filtered_channels": 0, "skipped_sources": []}

    tvg_ids, chan_names = build_whitelist()
    norm_cache = {}

    with ThreadPoolExecutor(max_workers=min(4, len(SOURCES))) as pool:
        futures = {pool.submit(download_one, u): u for u in SOURCES}
        merged = {}
        for fut in as_completed(futures):
            url = futures[fut]
            try:
                data = fut.result()
                parsed = parse_tv(data)
                kept = 0
                for cid, info in parsed.items():
                    if not match_channel(cid, info["names"], tvg_ids, chan_names, norm_cache):
                        continue
                    target = merged.setdefault(cid, {"names": set(), "programmes": {}})
                    target["names"].update(info["names"])
                    target["programmes"].update(info["programmes"])
                    kept += 1
                summary["sources"].append({"url": url, "channels": len(parsed),
                                           "kept": kept})
            except Exception as e:
                summary["skipped_sources"].append({"url": url, "error": str(e)[:200]})

    if not merged:
        finish_run(run_id, summary)
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        sys.exit(0)  # 全挂也不阻断 workflow，保留旧 epg.xml

    # ---------- 生成 XMLTV ----------
    ns = ""
    tv = ET.Element("tv", {"generator-info-name": "iptv-cf"})
    for cid in sorted(merged):
        info = merged[cid]
        ch = ET.SubElement(tv, "channel", {"id": cid})
        for nm in sorted(info["names"])[:3]:
            dn = ET.SubElement(ch, "display-name", {"lang": "zh"})
            dn.text = nm
        for (start, title), (stop, desc) in info["programmes"].items():
            pr = ET.SubElement(tv, "programme", {"channel": cid, "start": start, "stop": stop})
            t = ET.SubElement(pr, "title", {"lang": "zh"})
            t.text = title
            if desc:
                d = ET.SubElement(pr, "desc", {"lang": "zh"})
                d.text = desc
    tree = ET.ElementTree(tv)
    buf = io.BytesIO()
    tree.write(buf, encoding="utf-8", xml_declaration=True)
    epg_bytes = buf.getvalue()

    upload_r2("publish/epg.xml", epg_bytes)

    summary["total_channels"] = len(merged)
    summary["total_programmes"] = sum(len(v["programmes"]) for v in merged.values())
    summary["size_bytes"] = len(epg_bytes)
    summary["generated_at"] = utc_now()
    finish_run(run_id, summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
