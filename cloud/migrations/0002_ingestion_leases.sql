CREATE TABLE article_sync_leases (
  article_id TEXT PRIMARY KEY, lease_token TEXT NOT NULL, expires_at TEXT NOT NULL
);
