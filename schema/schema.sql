-- ai-capital-flows 账本 schema v2
CREATE TABLE IF NOT EXISTS entities (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  name_zh TEXT,
  grp TEXT NOT NULL,
  ticker TEXT,
  cik INTEGER,
  country TEXT,
  aliases TEXT NOT NULL DEFAULT '[]',
  pooled INTEGER NOT NULL DEFAULT 0,
  auto_added INTEGER NOT NULL DEFAULT 0
);

-- 采集到的文章 / 文件。tier: 1=SEC 文件，2=官方新闻室/通讯社，3=媒体
CREATE TABLE IF NOT EXISTS articles (
  id TEXT PRIMARY KEY,
  tier INTEGER NOT NULL,
  source_kind TEXT NOT NULL,        -- edgar | edgar_fts | newsroom | media_rss | gnews
  source_name TEXT NOT NULL,
  url TEXT NOT NULL,
  orig_url TEXT,                    -- gnews 解码后的原文地址
  publisher TEXT,
  title TEXT NOT NULL,
  published_at TEXT,
  fetched_at TEXT NOT NULL DEFAULT (datetime('now')),
  text TEXT NOT NULL DEFAULT '',
  has_body INTEGER NOT NULL DEFAULT 0,
  entity_hits TEXT NOT NULL DEFAULT '[]',
  score INTEGER NOT NULL DEFAULT 0,
  prefilter_hit INTEGER NOT NULL DEFAULT 0,
  edgar_form TEXT,
  edgar_items TEXT,
  edgar_entity TEXT,
  status TEXT NOT NULL DEFAULT 'new',   -- new | extracted | skipped | error
  note TEXT
);
CREATE INDEX IF NOT EXISTS idx_articles_status ON articles(status, prefilter_hit);
CREATE INDEX IF NOT EXISTS idx_articles_pub ON articles(published_at);

-- 模型逐篇抽出的原始事件（审计用，不直接展示）
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  article_id TEXT NOT NULL,
  edge_id INTEGER,
  kind TEXT NOT NULL,
  from_entity TEXT,
  to_entity TEXT,
  amount_usd REAL,
  amount_text TEXT,
  instrument TEXT,
  term TEXT,
  capacity TEXT,
  announced TEXT,
  maturity_date TEXT,
  period TEXT,
  status TEXT,
  confidence REAL,
  title_zh TEXT,
  title_en TEXT,
  summary_zh TEXT,
  quote TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- 合并后的边（页面展示）。tier: confirmed | reported | candidate
CREATE TABLE IF NOT EXISTS edges (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  kind TEXT NOT NULL,               -- order | investment | debt | lease | guarantee | vendor_financing
  from_entity TEXT NOT NULL,
  to_entity TEXT NOT NULL,
  amount_usd REAL,
  amount_text TEXT,
  instrument TEXT,
  term TEXT,
  capacity TEXT,
  announced TEXT,
  maturity_date TEXT,
  status TEXT NOT NULL DEFAULT 'announced',   -- rumored | announced | signed | completed | amended | cancelled
  tier TEXT NOT NULL DEFAULT 'candidate',
  confidence REAL NOT NULL DEFAULT 0,
  title_zh TEXT,
  title_en TEXT,
  summary_zh TEXT,
  first_seen TEXT NOT NULL DEFAULT (datetime('now')),
  last_seen TEXT NOT NULL DEFAULT (datetime('now')),
  superseded_by INTEGER,
  seed INTEGER NOT NULL DEFAULT 0,
  distinct_deal INTEGER NOT NULL DEFAULT 0,   -- 人工认定独立交易，不参与合并 / 去重 / 聚类
  review TEXT NOT NULL DEFAULT 'pending',     -- pending | confirmed | corrected | deleted
  review_note TEXT
);
CREATE INDEX IF NOT EXISTS idx_edges_key ON edges(kind, from_entity, to_entity);

CREATE TABLE IF NOT EXISTS edge_sources (
  edge_id INTEGER NOT NULL,
  article_id TEXT,
  url TEXT NOT NULL,
  title TEXT,
  publisher TEXT,
  date TEXT,
  tier INTEGER NOT NULL,
  quote TEXT,
  PRIMARY KEY (edge_id, url)
);

CREATE TABLE IF NOT EXISTS snapshots (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  entity_id TEXT NOT NULL,
  metric TEXT NOT NULL,             -- capex | rpo | backlog | total_debt | compute_commitments | other
  period TEXT,
  value_usd REAL,
  value_text TEXT,
  summary_zh TEXT,
  article_id TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  UNIQUE(entity_id, metric, period)
);

CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  started_at TEXT NOT NULL DEFAULT (datetime('now')),
  finished_at TEXT,
  stage TEXT NOT NULL,
  stats TEXT,
  ok INTEGER,
  error TEXT
);

CREATE TABLE IF NOT EXISTS source_status (
  name TEXT PRIMARY KEY,
  tier INTEGER,
  last_run TEXT,
  ok INTEGER,
  fetched INTEGER,
  added INTEGER,
  error TEXT
);

CREATE TABLE IF NOT EXISTS extraction_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id INTEGER,
  article_ids TEXT,
  model TEXT,
  raw_output TEXT,
  cost_usd REAL,
  duration_ms INTEGER,
  ok INTEGER NOT NULL DEFAULT 1,
  error TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
