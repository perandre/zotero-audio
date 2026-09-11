"""Small durable control store shared by the CLI, local UI and cloud bridge.

Generated documents live outside Git. SQLite records work and delivery separately:
an unavailable upload or iCloud destination never rolls back completed generation.
"""
from __future__ import annotations

import hashlib
import copy
import json
import os
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_SETTINGS = {
    "qa_enabled": True,
    "opening_sound": "typing",
    "closing_sound": "none",
    "spoken_intro": True,
    "full_voice": "am_michael",
    "brief_voice": "af_heart",
    "speed": 1.0,
    "tts_model": "kokoro",
    "auto_publish": True,
    "auto_generate": "markdown",
    "icloud_folder": "",
    "backup_enabled": False,
    "backup_folder": "",
}
TERMINAL = {"completed", "failed", "cancelled"}
ACTIONS = {"markdown", "full", "brief", "both", "sync"}


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def runtime_root() -> Path:
    return Path(os.environ.get("ZOTERO_AUDIO_RUNTIME", str(Path.home() / "Sites/zotero-audio-runtime"))).expanduser().resolve()


def validate_settings(value: dict) -> dict:
    if not isinstance(value, dict):
        raise ValueError("Settings must be an object")
    result = {}
    for key, val in value.items():
        if key == "updated_at":
            continue
        if key not in DEFAULT_SETTINGS:
            raise ValueError(f"Unknown setting: {key}")
        default = DEFAULT_SETTINGS[key]
        if isinstance(default, bool):
            if not isinstance(val, bool):
                raise ValueError(f"{key} must be true or false")
        elif key == "speed":
            if isinstance(val, bool) or not isinstance(val, (int, float)) or not 0.5 <= val <= 2:
                raise ValueError("Speed must be between 0.5 and 2")
        elif not isinstance(val, str) or len(val) > 4096:
            raise ValueError(f"{key} must be text")
        if key in {"opening_sound", "closing_sound"} and val not in {"none", "typing"}:
            raise ValueError("Sound must be none or typing")
        if key == "auto_generate" and val not in {"off", "markdown", "full", "brief", "both"}:
            raise ValueError("Automatic generation must be off, markdown, full, brief or both")
        if key in {"icloud_folder", "backup_folder"} and val:
            path = Path(val).expanduser()
            if not path.is_absolute():
                raise ValueError("Choose an absolute folder path")
            val = str(path)
        result[key] = val
    return result


class Store:
    def __init__(self, root: Path | None = None):
        self.runtime = (root or runtime_root()).resolve()
        self.root = self.runtime / "control"
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = self.root / "library.sqlite3"
        self._lock = threading.RLock()
        self.zotero_lock = threading.RLock()
        with self.db() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS articles (
                    id TEXT PRIMARY KEY, title TEXT NOT NULL, data TEXT NOT NULL,
                    updated_at TEXT NOT NULL, cloud_hash TEXT DEFAULT ''
                );
                CREATE VIRTUAL TABLE IF NOT EXISTS article_search USING fts5(
                    id UNINDEXED, title, body, tokenize='unicode61'
                );
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY, status TEXT NOT NULL, data TEXT NOT NULL,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS jobs_status ON jobs(status, created_at);
                CREATE TABLE IF NOT EXISTS deliveries (
                    id TEXT PRIMARY KEY, kind TEXT NOT NULL, article_id TEXT NOT NULL,
                    status TEXT NOT NULL, data TEXT NOT NULL, attempts INTEGER DEFAULT 0,
                    error TEXT, next_attempt REAL DEFAULT 0, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS settings (id INTEGER PRIMARY KEY CHECK(id=1), data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            """)
        self.path.chmod(0o600)
        if self.state("initialized") is None:
            settings = dict(DEFAULT_SETTINGS)
            config = self.runtime / "podcast.toml"
            if config.is_file():
                import tomllib
                try:
                    settings["icloud_folder"] = tomllib.loads(config.read_text()).get("paths", {}).get("private_root", "")
                except (ValueError, OSError):
                    pass
            self.update_settings(settings)
            self.set_state("initialized", True)
        self._migrate_search_index()

    def _migrate_search_index(self):
        """Update legacy search snippets without changing the research files."""
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            version = db.execute("SELECT value FROM state WHERE key='search_index_version'").fetchone()
            if version and json.loads(version[0]) == 2:
                return
            rows = db.execute("SELECT a.data,s.body FROM articles a LEFT JOIN article_search s ON s.id=a.id").fetchall()
            for row in rows:
                article = json.loads(row["data"])
                self._write_article(db, article, article, markdown=row["body"] or "")
            db.execute("INSERT OR REPLACE INTO state VALUES ('search_index_version','2')")

    @contextmanager
    def db(self):
        with self._lock:
            connection = sqlite3.connect(self.path, timeout=30)
            connection.row_factory = sqlite3.Row
            try:
                yield connection
                connection.commit()
            except Exception:
                connection.rollback()
                raise
            finally:
                connection.close()

    def state(self, key: str, default=None):
        with self.db() as db:
            row = db.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set_state(self, key: str, value):
        with self.db() as db:
            db.execute("INSERT OR REPLACE INTO state VALUES (?,?)", (key, json.dumps(value)))

    def settings(self):
        with self.db() as db:
            row = db.execute("SELECT data FROM settings WHERE id=1").fetchone()
        return {**DEFAULT_SETTINGS, **(json.loads(row[0]) if row else {})}

    def update_settings(self, values: dict, *, timestamp: str | None = None):
        values = validate_settings(values)
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT data FROM settings WHERE id=1").fetchone()
            updated = {**DEFAULT_SETTINGS, **(json.loads(row[0]) if row else {}), **values, "updated_at": timestamp or now()}
            db.execute("INSERT OR REPLACE INTO settings VALUES (1,?)", (json.dumps(updated),))
        return updated

    def article(self, article_id: str) -> dict:
        with self.db() as db:
            row = db.execute("SELECT data FROM articles WHERE id=?", (article_id,)).fetchone()
        if not row:
            raise KeyError("Article not found")
        return json.loads(row[0])

    def put_article(self, article: dict, *, markdown: str | None = None):
        """Merge supplied fields atomically; use edit_article for nested edits."""
        article = dict(article)
        if not article.get("id") or not article.get("title"):
            raise ValueError("Every article requires an ID and full title")
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT data FROM articles WHERE id=?", (article["id"],)).fetchone()
            previous = json.loads(row[0]) if row else {}
            return self._write_article(db, previous, article, markdown=markdown)

    @contextmanager
    def edit_article(self, article_id: str, *, markdown: str | None = None):
        """Read, modify and save one article under a single write transaction.

        Keep filesystem snapshots, network calls and other Store write calls
        outside this block. The transaction serializes other connections and
        processes; the RLock also protects callers sharing this Store instance.
        Exceptions roll back both the article and its search-index update.
        """
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT data FROM articles WHERE id=?", (article_id,)).fetchone()
            if not row:
                raise KeyError("Article not found")
            previous = json.loads(row[0])
            edited = copy.deepcopy(previous)
            yield edited
            if edited.get("id") != article_id:
                raise ValueError("An article edit cannot change its ID")
            edited.update(self._write_article(db, previous, edited, markdown=markdown))

    @staticmethod
    def _write_article(db, previous: dict, article: dict, *, markdown: str | None = None):
        if not article.get("id") or not article.get("title"):
            raise ValueError("Every article requires an ID and full title")
        combined = {**previous, **article}
        # Merely viewing or scanning the library must not trigger another upload.
        compare = lambda d: {k: v for k, v in d.items() if k != "updated_at"}
        if compare(combined) != compare(previous):
            combined["updated_at"] = now()
        else:
            combined["updated_at"] = previous.get("updated_at", now())
        db.execute("""INSERT INTO articles(id,title,data,updated_at) VALUES(?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET title=excluded.title,data=excluded.data,updated_at=excluded.updated_at""",
            (combined["id"], combined["title"], json.dumps(combined, ensure_ascii=False), combined["updated_at"]))
        if markdown is not None or not previous or previous.get("title") != combined.get("title") or previous.get("authors") != combined.get("authors"):
            if markdown is None:
                old_search = db.execute("SELECT body FROM article_search WHERE id=?", (combined["id"],)).fetchone()
                markdown = old_search[0] if old_search else ""
            db.execute("DELETE FROM article_search WHERE id=?", (combined["id"],))
            search_title = combined["title"] + " " + " ".join(combined.get("authors") or [])
            import re
            search_body = re.sub(r"\A---\s*\n.*?\n---\s*\n", "", markdown or "", count=1, flags=re.S)
            search_body = re.sub(r"<!--.*?-->", "", search_body, flags=re.S)
            db.execute("INSERT INTO article_search(id,title,body) VALUES(?,?,?)", (combined["id"], search_title, search_body))
        return combined

    def library(self, q: str = "", limit: int = 100, offset: int = 0):
        limit = max(1, min(int(limit), 500))
        offset = max(0, int(offset))
        import re
        words = re.findall(r"\w+", q, re.UNICODE)[:32]
        if words:
            expression = " OR ".join('"' + word + '"' for word in words)
            clause = "WHERE title LIKE ? OR json_extract(data,'$.authors') LIKE ? OR id IN (SELECT id FROM article_search WHERE article_search MATCH ?)"
            params = ("%" + q + "%", "%" + q + "%", expression)
        else:
            clause, params = ("WHERE title LIKE ?", ("%" + q + "%",)) if q else ("", ())
        with self.db() as db:
            total = db.execute(f"SELECT count(*) FROM articles {clause}", params).fetchone()[0]
            rows = db.execute(f"SELECT data FROM articles {clause} ORDER BY title COLLATE NOCASE LIMIT ? OFFSET ?", (*params, limit, offset)).fetchall()
        return {"items": [json.loads(r[0]) for r in rows], "total": total}

    def all_articles(self):
        with self.db() as db:
            return [json.loads(r[0]) for r in db.execute("SELECT data FROM articles ORDER BY title COLLATE NOCASE")]

    def search(self, query: str, limit: int = 20):
        import re
        words = re.findall(r"\w+", query, re.UNICODE)[:32]
        if not words:
            return {"results": []}
        expression = " OR ".join('"' + word + '"' for word in words)
        with self.db() as db:
            rows = db.execute("""SELECT a.data, snippet(article_search,2,'','', ' … ',48) snippet
                FROM article_search JOIN articles a ON a.id=article_search.id
                WHERE article_search MATCH ? ORDER BY bm25(article_search,0,5,1) LIMIT ?""", (expression, max(1, min(limit, 100)))).fetchall()
        return {"results": [{"id": d["id"], "title": d["title"], "authors": d.get("authors", []), "year": d.get("year"),
                             "url": f"/articles/{d['id']}", "snippet": row["snippet"]} for row in rows for d in [json.loads(row["data"])]]}

    def create_job(self, request: dict, *, job_id: str | None = None, remote: dict | None = None, automatic=False):
        action, scope = request.get("action"), request.get("scope", "one")
        if action not in ACTIONS or scope not in {"new", "all", "one"}:
            raise ValueError("Choose markdown, full, brief, both or sync and a valid scope")
        for flag in ("qa", "force"):
            if flag in request and not isinstance(request[flag], bool):
                raise ValueError(f"{flag} must be true or false")
        article_id = request.get("article_id")
        title = self.article(article_id)["title"] if scope == "one" and action != "sync" else f"{action.capitalize()} · {scope} articles"
        if scope == "one" and action != "sync" and not article_id:
            raise ValueError("Select an article")
        idem = request.get("idempotency_key")
        if idem is not None and (not isinstance(idem, str) or not 1 <= len(idem) <= 200):
            raise ValueError("Idempotency key must be 1–200 characters")
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            if automatic:
                # Serialize with CLI/cloud queue inserts, including requests
                # created by another Store connection during a Zotero scan.
                prior_request = db.execute("""SELECT 1 FROM jobs
                    WHERE json_extract(data,'$.action') != 'sync' AND
                    (json_extract(data,'$.article_id') = ? OR
                     (json_extract(data,'$.scope') != 'one' AND status IN ('queued','running','cancel_requested')))
                    LIMIT 1""", (article_id,)).fetchone()
                if prior_request:
                    return None
            if idem:
                for row in db.execute("SELECT data FROM jobs"):
                    old = json.loads(row[0])
                    if old.get("idempotency_key") == idem:
                        expected = {"action": action, "scope": scope, "article_id": article_id,
                                    "force": request.get("force", False), "qa": request.get("qa", self.settings()["qa_enabled"])}
                        if any(old.get(k) != v for k, v in expected.items()):
                            raise ValueError("This idempotency key was already used for a different request")
                        return old
            timestamp = now()
            job = {"id": job_id or uuid.uuid4().hex, "action": action, "scope": scope, "article_id": article_id,
                   "qa": request.get("qa", self.settings()["qa_enabled"]), "force": request.get("force", False),
                   "idempotency_key": idem, "title": title, "status": "queued", "stage": "queued", "progress": 0,
                   "message": "Waiting for the local worker", "created_at": timestamp, "updated_at": timestamp,
                   "remote": remote}
            if automatic:
                job["automatic"] = True
            prior = db.execute("SELECT data FROM jobs WHERE id=?", (job["id"],)).fetchone()
            if prior:
                return json.loads(prior[0])
            db.execute("INSERT INTO jobs VALUES(?,?,?,?,?)", (job["id"], job["status"], json.dumps(job), timestamp, timestamp))
        self.event("job_queued", job_id=job["id"], title=title, action=action)
        return job

    def job(self, job_id: str):
        with self.db() as db:
            row = db.execute("SELECT data FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            raise KeyError("Job not found")
        return json.loads(row[0])

    def jobs(self, limit=100):
        with self.db() as db:
            return {"jobs": [json.loads(r[0]) for r in db.execute("SELECT data FROM jobs ORDER BY created_at DESC LIMIT ?", (min(max(int(limit), 1), 500),))]}

    def update_job(self, job_id: str, **updates):
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT data FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not row:
                raise KeyError("Job not found")
            job = json.loads(row[0])
            # Progress callbacks must never erase a user's cancellation request.
            if job["status"] == "cancel_requested" and updates.get("status") not in TERMINAL:
                updates["status"] = "cancel_requested"
            job.update(updates)
            job["updated_at"] = now()
            db.execute("UPDATE jobs SET status=?,data=?,updated_at=? WHERE id=?", (job["status"], json.dumps(job), job["updated_at"], job_id))
        return job

    def claim_job(self):
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT data FROM jobs WHERE status='queued' ORDER BY created_at LIMIT 1").fetchone()
            if not row:
                return None
            job = json.loads(row[0])
            job.update(status="running", stage="starting", updated_at=now())
            db.execute("UPDATE jobs SET status=?,data=?,updated_at=? WHERE id=?", (job["status"], json.dumps(job), job["updated_at"], job["id"]))
        return job

    def cancel(self, job_id: str):
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT data FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not row:
                raise KeyError("Job not found")
            job = json.loads(row[0])
            if job["status"] in TERMINAL:
                return job
            job.update(status="cancelled" if job["status"] == "queued" else "cancel_requested", updated_at=now(), message="Cancellation requested; cached work will be kept")
            db.execute("UPDATE jobs SET status=?,data=?,updated_at=? WHERE id=?", (job["status"], json.dumps(job), job["updated_at"], job_id))
        return job

    def recover(self):
        with self.db() as db:
            active = [json.loads(r[0]) for r in db.execute("SELECT data FROM jobs WHERE status IN ('running','cancel_requested')")]
        for job in active:
            if job["status"] == "running":
                self.update_job(job["id"], status="queued", stage="queued", message="Resuming after worker restart")
            elif job["status"] == "cancel_requested":
                self.update_job(job["id"], status="cancelled")
        with self.db() as db:
            db.execute("UPDATE deliveries SET status='pending' WHERE status='running'")

    def enqueue_delivery(self, kind: str, article_id: str, data: dict):
        delivery_id = digest({"kind": kind, "article_id": article_id, "data": data})
        with self.db() as db:
            db.execute("INSERT OR IGNORE INTO deliveries(id,kind,article_id,status,data,updated_at) VALUES(?,?,?,'pending',?,?)", (delivery_id, kind, article_id, json.dumps(data), now()))
        return delivery_id

    def event(self, event: str, **fields):
        # Events contain summaries/provenance, never source text or credentials.
        entry = {"schema": "one-more-paper-event/v1", "time": now(), "event": event, **fields}
        with self._lock:
            with (self.root / "events.jsonl").open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
