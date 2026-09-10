-- Private project workspace, separate from article generation/publication.
CREATE TABLE projects (
  id TEXT PRIMARY KEY, title TEXT NOT NULL, worker_id TEXT NOT NULL,
  synced_at TEXT NOT NULL, warnings TEXT NOT NULL DEFAULT '[]'
);
CREATE TABLE project_documents (
  project TEXT NOT NULL REFERENCES projects(id), path TEXT NOT NULL,
  title TEXT NOT NULL, text TEXT NOT NULL, revision TEXT NOT NULL,
  format TEXT NOT NULL, source_modified_at TEXT NOT NULL,
  synced_at TEXT NOT NULL, bytes INTEGER NOT NULL,
  PRIMARY KEY(project, path)
);
CREATE VIRTUAL TABLE project_search USING fts5(project UNINDEXED, path, title, text, tokenize='unicode61');
CREATE TABLE project_changes (
  id TEXT PRIMARY KEY, project TEXT NOT NULL REFERENCES projects(id),
  path TEXT NOT NULL, text TEXT NOT NULL, expected_revision TEXT, request_hash TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'queued', result TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE INDEX project_changes_pending ON project_changes(project, status, created_at);
