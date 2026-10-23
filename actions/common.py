"""
common.py — Actions 侧共享库
单一通道设计：所有 D1 读写 / R2 上传 / 缓存清理都走 Worker internal API，
Actions 只需要 ADMIN_TOKEN + WORKER_URL 两个 Secret。
"""
import hashlib
import json
import os
import re
import socket
import time
import urllib.parse as urlparse
from datetime import datetime, timezone, timedelta

import requests

WORKER_URL = os.environ["WORKER_URL"].rstrip("/")
ADMIN_TOKEN = os.environ["ADMIN_TOKEN"]
HEADERS = {"Authorization": f"Bearer {ADMIN_TOKEN}", "Content-Type": "application/json"}

# ---------- 时间工具（全部 UTC，D1 默认 datetime('now') 即 UTC）----------
def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

def hours_ago(h: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=h)).strftime("%Y-%m-%d %H:%M:%S")

def days_ago(d: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=d)).strftime("%Y-%m-%d %H:%M:%S")

def iso_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

# ---------- Worker internal API ----------
def api(path: str, payload=None, timeout=120, raw_body: bytes = None):
    url = f"{WORKER_URL}{path}"
    if raw_body is not None:
        resp = requests.post(url, headers={"Authorization": f"Bearer {ADMIN_TOKEN}"},
                             data=raw_body, timeout=timeout)
    else:
        resp = requests.post(url, headers=HEADERS, json=payload, timeout=timeout)
    resp.raise_for_status()
    return resp.json()

def d1_query(sql: str, params=None) -> list[dict]:
    """查询，返回行列表"""
    r = api("/api/internal/d1-query", {"sql": sql, "params": params or []})
    return r.get("rows", [])

def d1_exec(sql: str, params=None) -> None:
    api("/api/internal/d1", {"sql": sql, "params": params or []})

def d1_batch(statements: list[dict]) -> None:
    """批量写。D1 batch 单请求上限 5000 条语句，这里按 900 分批。"""
    for i in range(0, len(statements), 900):
        api("/api/internal/d1", {"statements": statements[i:i + 900]})

def upload_r2(key: str, data: bytes) -> None:
    """上传 R2 并自动 purge 对应 URL 缓存"""
    api(f"/api/internal/upload?key={key}", raw_body=data)

def trigger_workflow(name: str) -> None:
    api("/api/trigger", {"workflow": name})

# ---------- 运行记录 ----------
def start_run(kind: str) -> str:
    import random
    run_id = f"{kind}-{int(time.time())}-{random.randrange(1000, 9999)}"
    d1_exec("INSERT INTO runs (id, kind, status) VALUES (?, ?, 'running')", [run_id, kind])
    return run_id

def finish_run(run_id: str, summary: dict) -> None:
    d1_exec("UPDATE runs SET status='done', summary_json=? WHERE id=?",
            [json.dumps(summary, ensure_ascii=False), run_id])

# ---------- URL 归一化指纹（iptv-hub 思路：去重/状态机主键）----------
_SKIP_QS_KEYS = {"token", "expires", "sign", "timestamp", "ts", "_", "w"}

def normalize_url(url: str) -> str:
    try:
        p = urlparse.urlsplit(url.strip())
    except Exception:
        return url.strip()
    scheme = (p.scheme or "http").lower()
    host = (p.hostname or "").lower().rstrip(".")
    port = p.port
    if (scheme == "http" and port == 80) or (scheme == "https" and port == 443):
        port = None
    path = p.path.rstrip("/")
    qs = sorted(f"{k}={v}" for k, v in urlparse.parse_qsl(p.query)
                if k.lower() not in _SKIP_QS_KEYS)
    return f"{scheme}://{host}" + (f":{port}" if port else "") + path + \
           (("?" + "&".join(qs)) if qs else "")

def fingerprint(url: str) -> str:
    return hashlib.sha1(normalize_url(url).encode()).hexdigest()

# ---------- 名称归一化（EPG 匹配用，Phase 3 起效）----------
_CN_NUM = {"一": "1", "二": "2", "三": "3", "四": "4", "五": "5",
           "六": "6", "七": "7", "八": "8", "九": "9", "十": "10"}
_CCTV_RE = re.compile(r"^cctv[-_ ]*(\d+)(\+|plus|k|4k|8k)?", re.I)
_CCTV_CN_RE = re.compile(r"^(?:央视|中央|cctv)([一二三四五六七八九十\d]+)(\+|plus|4k|8k|台)?", re.I)

def canonical_name(name: str) -> str:
    """频道主名归一化：去空白/全半角统一/CCTV 规整/卫视规整"""
    n = name.strip().replace(" ", "").replace("\u3000", "")
    n = re.sub(r"[·•∙]", "", n)
    m = _CCTV_RE.match(n)
    if m:
        num = m.group(1)
        suffix = (m.group(2) or "").lower()
        if suffix in ("+", "plus"):
            return f"CCTV{num}+"
        if suffix in ("k", "4k"):
            return f"CCTV{num}4K"
        if suffix == "8k":
            return f"CCTV{num}8K"
        return f"CCTV{num}"
    m = _CCTV_CN_RE.match(n)
    if m:
        num = m.group(1)
        if num in _CN_NUM:
            num = _CN_NUM[num]
        suffix = (m.group(2) or "").lower()
        if suffix in ("+", "plus"):
            return f"CCTV{num}+"
        if suffix in ("k", "4k"):
            return f"CCTV{num}4K"
        if suffix == "台":
            return f"CCTV{num}"
        return f"CCTV{num}"
    # 卫视归一化：XX卫视HD/高清 → XX卫视
    n = re.sub(r"(高清|HD|超清|FHD|4K|8K|标清|SD)$", "", n, flags=re.I)
    return n

# ---------- 内网地址守卫（测速 SSRF 防护，照 iptv-hub netguard 思想）----------
def is_private_host(host: str) -> bool:
    host = host.lower().rstrip(".")
    if host in ("localhost",) or host.endswith(".local"):
        return True
    try:
        ip = socket.gethostbyname(host)
    except Exception:
        return False  # 解析不了按私有处理
    if ip.startswith(("127.", "10.", "192.168.", "169.254.")) or ip.startswith("0."):
        return True
    if ip.startswith("172."):
        try:
            second = int(ip.split(".")[1])
            if 16 <= second <= 31:
                return True
        except Exception:
            pass
    if ":" in ip:
        if ip == "::1":
            return True
        low = ip.split(":")[0].lower()
        if low.startswith(("fe80", "fc", "fd")):
            return True
    return False
