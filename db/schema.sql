-- ============================================================
-- IPTV-CF D1 Schema
-- 状态机参数照 iptv-hub：fail>=3 剔除 → 14 天复活 → 90 天 purge
-- ============================================================

-- 数据源
CREATE TABLE IF NOT EXISTS sources (
  id TEXT PRIMARY KEY,                          -- slug
  type TEXT NOT NULL DEFAULT 'm3u',             -- m3u/txt/json/tvbox/hwctc/jsp
  url TEXT NOT NULL,
  name TEXT NOT NULL DEFAULT '',
  headers_json TEXT NOT NULL DEFAULT '{}',      -- 抓取时附加请求头
  proxy_host INTEGER NOT NULL DEFAULT 0,        -- 1=抓取时走代理（测速永不走代理）
  enabled INTEGER NOT NULL DEFAULT 1,
  status TEXT NOT NULL DEFAULT 'ok',            -- ok/error/unsupported
  etag TEXT NOT NULL DEFAULT '',
  last_modified TEXT NOT NULL DEFAULT '',
  last_fetched_at TEXT NOT NULL DEFAULT '',
  last_checked_at TEXT NOT NULL DEFAULT '',
  error_message TEXT NOT NULL DEFAULT '',
  entry_count INTEGER NOT NULL DEFAULT 0,
  reputation REAL NOT NULL DEFAULT 0.5,         -- 源级信誉分 0~1
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- 线路条目（状态机核心）
CREATE TABLE IF NOT EXISTS entries (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  source_id TEXT NOT NULL REFERENCES sources(id),
  fingerprint TEXT NOT NULL,                    -- URL 归一化指纹（sha1），去重/状态机主键
  url TEXT NOT NULL,
  name_raw TEXT NOT NULL,
  tvg_id TEXT NOT NULL DEFAULT '',
  tvg_logo TEXT NOT NULL DEFAULT '',
  group_title TEXT NOT NULL DEFAULT '',
  headers_json TEXT NOT NULL DEFAULT '{}',      -- EXTVLCOPT:http-user-agent / referrer 继承
  attrs_json TEXT NOT NULL DEFAULT '{}',        -- catchup 等播放属性
  region TEXT NOT NULL DEFAULT '',              -- domestic/international
  status TEXT NOT NULL DEFAULT 'ok',            -- ok/error/unsupported/purged
  fail_count INTEGER NOT NULL DEFAULT 0,        -- 连续失败次数（成功即清零）
  last_fail_at TEXT NOT NULL DEFAULT '',
  revive_count INTEGER NOT NULL DEFAULT 0,      -- 复活次数（季度清理依据）
  active INTEGER NOT NULL DEFAULT 1,            -- 0=已剔除（墓碑，14天后可复活）
  last_check_at TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now')),
  UNIQUE(source_id, fingerprint)
);
CREATE INDEX IF NOT EXISTS idx_entries_fp ON entries(fingerprint);
CREATE INDEX IF NOT EXISTS idx_entries_pending ON entries(status, fail_count, last_fail_at, last_check_at, active);
CREATE INDEX IF NOT EXISTS idx_entries_source ON entries(source_id);

-- 频道实体（归一化主名）
CREATE TABLE IF NOT EXISTS channels (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  canonical_name TEXT NOT NULL UNIQUE,          -- 归一化后的主名
  tvg_id TEXT NOT NULL DEFAULT '',
  logo TEXT NOT NULL DEFAULT '',
  category TEXT NOT NULL DEFAULT '',
  aliases_json TEXT NOT NULL DEFAULT '[]',
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- 频道 <-> 线路 多对多
CREATE TABLE IF NOT EXISTS channel_entries (
  channel_id INTEGER NOT NULL REFERENCES channels(id),
  entry_id INTEGER NOT NULL REFERENCES entries(id),
  PRIMARY KEY (channel_id, entry_id)
);

-- 订阅（导出出口）
CREATE TABLE IF NOT EXISTS subscriptions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  filters_json TEXT NOT NULL DEFAULT '{}',
  enabled INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS sub_channels (
  subscription_id INTEGER NOT NULL REFERENCES subscriptions(id),
  channel_id INTEGER NOT NULL REFERENCES channels(id),
  entry_limit INTEGER NOT NULL DEFAULT 5,       -- 每频道最多导出条数
  sort_key TEXT NOT NULL DEFAULT 'speed',       -- speed/latency
  PRIMARY KEY (subscription_id, channel_id)
);

-- 测速记录（落库即历史）
CREATE TABLE IF NOT EXISTS speed_tests (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  entry_id INTEGER NOT NULL REFERENCES entries(id),
  fingerprint TEXT NOT NULL,
  ts TEXT NOT NULL DEFAULT (datetime('now')),
  ok INTEGER NOT NULL DEFAULT 0,
  http_status INTEGER NOT NULL DEFAULT 0,
  speed_kbps REAL NOT NULL DEFAULT 0,           -- 稳态速率（跳过 512KB 预热段）
  avg_kbps REAL NOT NULL DEFAULT 0,             -- 整段平均（诊断用）
  latency_ms INTEGER NOT NULL DEFAULT 0,        -- 首字节
  resolution TEXT NOT NULL DEFAULT '',
  region TEXT NOT NULL DEFAULT '',
  error TEXT NOT NULL DEFAULT '',
  duration_ms INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_st_ts ON speed_tests(ts);
CREATE INDEX IF NOT EXISTS idx_st_entry ON speed_tests(entry_id, ts);

-- 按指纹的健康汇总（导出排序、预算统计都走它）
CREATE TABLE IF NOT EXISTS stream_health (
  fingerprint TEXT PRIMARY KEY,
  ok_count INTEGER NOT NULL DEFAULT 0,
  fail_count INTEGER NOT NULL DEFAULT 0,
  last_ok_at TEXT NOT NULL DEFAULT '',
  last_fail_at TEXT NOT NULL DEFAULT '',
  best_speed_kbps REAL NOT NULL DEFAULT 0,
  last_speed_kbps REAL NOT NULL DEFAULT 0,
  last_resolution TEXT NOT NULL DEFAULT '',
  revive_count INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'ok'
);

-- 运行记录（日志索引：run_id → log_path/摘要）
CREATE TABLE IF NOT EXISTS runs (
  id TEXT PRIMARY KEY,
  kind TEXT NOT NULL,                           -- fetch/speed/aggregate/epg
  ts TEXT NOT NULL DEFAULT (datetime('now')),
  status TEXT NOT NULL DEFAULT 'running',
  summary_json TEXT NOT NULL DEFAULT '{}',
  log_path TEXT NOT NULL DEFAULT ''
);

-- 众包纠错
CREATE TABLE IF NOT EXISTS feedback (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  entry_id INTEGER NOT NULL REFERENCES entries(id),
  ts TEXT NOT NULL DEFAULT (datetime('now')),
  kind TEXT NOT NULL DEFAULT 'broken',          -- broken/wrong_channel/ad
  note TEXT NOT NULL DEFAULT ''
);

-- 热配置（等价 KV 的 D1 备份）
CREATE TABLE IF NOT EXISTS kv_config (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
