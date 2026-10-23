"""
aggregate.py — 聚合导出模块
归一化 → 频道实体 → 多线路合并 → 按稳态速率排序 → 多格式导出 → 上传 R2 → purge
"""
import json
import os
import re
import sys
from datetime import datetime, timezone

import yaml

sys.path.insert(0, os.path.dirname(__file__))
from common import (d1_query, d1_batch, start_run, finish_run, upload_r2,
                    utc_now, canonical_name, fingerprint)

CONFIG_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "config", "sources.yaml")
with open(CONFIG_PATH, encoding="utf-8") as f:
    CFG = yaml.safe_load(f)

AG = CFG["aggregate"]
MAX_LINES = AG["max_lines_per_channel"]
MAX_LINES_CCTV = AG["max_lines_cctv"]

# 别名库：先内置常见，后续可把 iptv-api alias.txt（2798 行）导入 D1 kv_config 扩展
EXTRA_ALIASES = {
    "CCTV1": ["CCTV-1", "CCTV 1", "cctv1", "CCTV1综合", "CCTV-1综合", "中央1"],
    "CCTV2": ["CCTV-2", "cctv2", "CCTV2财经", "CCTV-2财经"],
    "CCTV5": ["CCTV-5", "cctv5", "CCTV5体育", "CCTV-5体育"],
    "CCTV5+": ["CCTV-5+", "CCTV5PLUS", "CCTV5＋", "cctv5plus"],
    "CCTV13": ["CCTV-13", "cctv13", "CCTV13新闻", "CCTV-13新闻"],
}


def alias_to_primary(name: str) -> str:
    n = canonical_name(name)
    for primary, aliases in EXTRA_ALIASES.items():
        if n in aliases:
            return primary
    return n


def active_entries() -> list[dict]:
    """活跃条目 + 健康排序（照 iptv-hub 导出查询：隐藏失效，按速率排序）"""
    sql = """
    SELECT e.id, e.url, e.name_raw, e.tvg_id, e.tvg_logo, e.group_title,
           e.headers_json, e.attrs_json, e.region,
           COALESCE(h.last_speed_kbps, 0) AS speed_kbps,
           COALESCE(h.last_resolution, '') AS resolution
    FROM entries e
    LEFT JOIN stream_health h ON h.fingerprint = e.fingerprint
    WHERE e.active=1 AND e.status='ok'
      AND COALESCE(h.fail_count, 0) < 3
    ORDER BY COALESCE(h.last_speed_kbps, 0) DESC
    """
    return d1_query(sql)


def build_channels(entries: list[dict]) -> dict:
    """归一化聚合：primary_name -> {meta, lines:[...]}"""
    channels = {}
    for e in entries:
        primary = alias_to_primary(e["name_raw"])
        if not primary:
            continue
        ch = channels.setdefault(primary, {"lines": [], "tvg_id": "", "logo": "",
                                           "groups": {}})
        if e["tvg_id"] and not ch["tvg_id"]:
            ch["tvg_id"] = e["tvg_id"]
        if e["tvg_logo"] and not ch["logo"]:
            ch["logo"] = e["tvg_logo"]
        grp = e["group_title"] or "未分组"
        ch["groups"][grp] = ch["groups"].get(grp, 0) + 1
        ch["lines"].append(e)
    # 每条频道内按稳态速率降序（导出取前 N 就是最快的线路）
    for ch in channels.values():
        ch["lines"].sort(key=lambda x: x.get("speed_kbps") or 0, reverse=True)
    return channels


def line_limit(primary: str) -> int:
    if primary.startswith("CCTV"):
        return MAX_LINES_CCTV
    return MAX_LINES


def gen_m3u(channels: dict, name: str = "IPTV") -> str:
    out = ["#EXTM3U"]
    for primary, ch in sorted(channels.items()):
        for i, e in enumerate(ch["lines"][:line_limit(primary)]):
            grp = e["group_title"] or "未分组"
            display = f"{primary}" if len(ch["lines"][:line_limit(primary)]) <= 1 \
                else f"{primary} 线路{i + 1}"
            attrs = f'tvg-id="{ch["tvg_id"] or e["tvg_id"]}" '
            attrs += f'tvg-name="{primary}" '
            attrs += f'tvg-logo="{ch["logo"] or e["tvg_logo"]}" '
            attrs += f'group-title="{grp}"'
            a = json.loads(e["attrs_json"] or "{}")
            if a.get("catchup"):
                attrs += f' catchup="{a["catchup"]}"'
                if a.get("catchup_source"):
                    attrs += f' catchup-source="{a["catchup_source"]}"'
                if a.get("catchup_days"):
                    attrs += f' catchup-days="{a["catchup_days"]}"'
            out.append(f"#EXTINF:-1 {attrs},{display}")
            h = json.loads(e["headers_json"] or "{}")
            if h.get("User-Agent"):
                out.append(f'#EXTVLCOPT:http-user-agent={h["User-Agent"]}')
            if h.get("Referer"):
                out.append(f'#EXTVLCOPT:http-referrer={h["Referer"]}')
            out.append(e["url"])
    return "\n".join(out) + "\n"


def gen_txt(channels: dict) -> str:
    """TVBox/DIYP 分组格式"""
    out = []
    for primary, ch in sorted(channels.items()):
        grp = (ch["lines"][0]["group_title"] if ch["lines"] else "") or "未分组"
        out.append(f"{grp},#genre#")
        for i, e in enumerate(ch["lines"][:line_limit(primary)]):
            display = primary if len(ch["lines"][:line_limit(primary)]) <= 1 \
                else f"{primary} 线路{i + 1}"
            out.append(f"{display},{e['url']}")
        out.append("")
    return "\n".join(out)


def gen_json(channels: dict) -> str:
    data = []
    for primary, ch in sorted(channels.items()):
        data.append({
            "name": primary,
            "tvg_id": ch["tvg_id"],
            "logo": ch["logo"],
            "group": (ch["lines"][0]["group_title"] if ch["lines"] else ""),
            "urls": [e["url"] for e in ch["lines"][:line_limit(primary)]],
        })
    return json.dumps(data, ensure_ascii=False, indent=2)


def sync_channel_table(channels: dict) -> None:
    """频道实体落 D1（canonical_name 唯一）+ channel_entries 重建"""
    stmts = []
    for primary, ch in channels.items():
        stmts.append({
            "sql": """INSERT INTO channels (canonical_name, tvg_id, logo, category, aliases_json)
                      VALUES (?, ?, ?, ?, '[]')
                      ON CONFLICT(canonical_name) DO UPDATE SET
                        tvg_id=CASE WHEN channels.tvg_id='' THEN excluded.tvg_id ELSE channels.tvg_id END,
                        logo=CASE WHEN channels.logo='' THEN excluded.logo ELSE channels.logo END""",
            "params": [primary, ch["tvg_id"], ch["logo"], ""]})
    d1_batch(stmts)
    # 重建映射
    chan_ids = {r["canonical_name"]: r["id"]
                for r in d1_query("SELECT id, canonical_name FROM channels")}
    pairs = []
    for primary, ch in channels.items():
        cid = chan_ids.get(primary)
        if not cid:
            continue
        for e in ch["lines"]:
            pairs.append((cid, e["id"]))
    if pairs:
        d1_batch([{"sql": "DELETE FROM channel_entries"}, *[
            {"sql": "INSERT OR IGNORE INTO channel_entries (channel_id, entry_id) VALUES (?, ?)",
             "params": [c, e]} for c, e in pairs]])


def main():
    run_id = start_run("aggregate")
    entries = active_entries()
    channels = build_channels(entries)
    sync_channel_table(channels)

    m3u = gen_m3u(channels)
    txt = gen_txt(channels)
    js = gen_json(channels)

    upload_r2("publish/iptv.m3u", m3u.encode())
    upload_r2("publish/iptv.txt", txt.encode())
    upload_r2("publish/iptv.json", js.encode())

    # 快照（7 天滚动，回滚用）
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    upload_r2(f"snapshots/{today}/iptv.m3u", m3u.encode())
    upload_r2(f"snapshots/{today}/iptv.txt", txt.encode())

    total_lines = sum(len(ch["lines"]) for ch in channels.values())
    summary = {"channels": len(channels), "total_lines": total_lines,
               "generated_at": utc_now()}

    # diff：对比昨日 channels 计数（简单版：看 runs 历史）
    prev = d1_query("""SELECT summary_json FROM runs WHERE kind='aggregate'
                       AND status='done' AND id<>? ORDER BY ts DESC LIMIT 1""", [run_id])
    if prev:
        try:
            prev_s = json.loads(prev[0]["summary_json"])
            summary["diff_channels"] = summary["channels"] - prev_s.get("channels", 0)
        except Exception:
            pass

    finish_run(run_id, summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
