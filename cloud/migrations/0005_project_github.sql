-- Direct GitHub documents and small durable commit receipts. No Mac edit queue.
ALTER TABLE project_documents ADD COLUMN source_blob_sha TEXT;
CREATE TABLE project_github_documents (
  path TEXT PRIMARY KEY, blob_sha TEXT NOT NULL, title TEXT NOT NULL,
  text TEXT NOT NULL, revision TEXT NOT NULL, bytes INTEGER NOT NULL,
  synced_at TEXT NOT NULL
);
CREATE TABLE project_github_changes (
  id TEXT PRIMARY KEY, path TEXT NOT NULL, request_hash TEXT NOT NULL,
  revision TEXT NOT NULL, base_commit TEXT, commit_sha TEXT,
  status TEXT NOT NULL DEFAULT 'preparing', result TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
