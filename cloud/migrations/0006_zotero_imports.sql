-- Cloud reference imports are independent of the Mac generation queue.
CREATE TABLE zotero_import_requests (
  id TEXT PRIMARY KEY, request_hash TEXT NOT NULL, library_id TEXT NOT NULL,
  identity TEXT, result TEXT, created_at TEXT NOT NULL
);
CREATE INDEX zotero_import_requests_created ON zotero_import_requests(created_at);
CREATE TABLE zotero_import_items (
  library_id TEXT NOT NULL, identity TEXT NOT NULL, item_key TEXT NOT NULL,
  payload TEXT NOT NULL, creator_request_id TEXT,
  PRIMARY KEY(library_id, identity)
);
CREATE TABLE zotero_api_state (
  library_id TEXT PRIMARY KEY, retry_at INTEGER NOT NULL
);
