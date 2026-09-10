CREATE TABLE IF NOT EXISTS articles (
  id TEXT PRIMARY KEY, title TEXT NOT NULL, authors TEXT NOT NULL DEFAULT '[]',
  year INTEGER, source_url TEXT NOT NULL DEFAULT '', license_status TEXT NOT NULL DEFAULT 'unknown',
  markdown_status TEXT NOT NULL DEFAULT 'pending', audio_status TEXT NOT NULL DEFAULT 'pending',
  qa_status TEXT NOT NULL DEFAULT 'unchecked', warnings TEXT NOT NULL DEFAULT '[]',
  metadata TEXT NOT NULL DEFAULT '{}', markdown_key TEXT, markdown_hash TEXT,
  review_key TEXT, review_hash TEXT, content_bytes INTEGER NOT NULL DEFAULT 0,
  updated_at TEXT NOT NULL
);
CREATE INDEX articles_updated ON articles(updated_at DESC);
CREATE INDEX articles_markdown_status ON articles(markdown_status);
CREATE VIRTUAL TABLE article_search USING fts5(article_id UNINDEXED, title, body, tokenize='unicode61');
CREATE TABLE IF NOT EXISTS jobs (
  id TEXT PRIMARY KEY, action TEXT NOT NULL, scope TEXT NOT NULL, article_id TEXT,
  title TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'queued', stage TEXT NOT NULL DEFAULT 'queued',
  progress REAL NOT NULL DEFAULT 0, message TEXT NOT NULL DEFAULT '', options TEXT NOT NULL DEFAULT '{}',
  idempotency_key TEXT UNIQUE, worker_id TEXT, lease_token TEXT, lease_expires TEXT,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE INDEX jobs_claim ON jobs(status, created_at);
CREATE INDEX jobs_leases ON jobs(status, lease_expires);
CREATE INDEX jobs_recent ON jobs(updated_at DESC);
CREATE TABLE IF NOT EXISTS worker_status (
  worker_id TEXT PRIMARY KEY, title TEXT NOT NULL DEFAULT 'Mac', current_job_id TEXT,
  version TEXT, last_seen TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS settings (id INTEGER PRIMARY KEY CHECK(id=1), value TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS budget (id INTEGER PRIMARY KEY CHECK(id=1), content_bytes INTEGER NOT NULL DEFAULT 0, articles INTEGER NOT NULL DEFAULT 0);
INSERT OR IGNORE INTO budget(id) VALUES(1);
CREATE TABLE IF NOT EXISTS daily_usage (day TEXT PRIMARY KEY, uploads INTEGER NOT NULL DEFAULT 0);
