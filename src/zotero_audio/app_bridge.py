"""Outbound-only synchronization, with lease renewal independent of file delivery.

A network outage never cancels local generation. Only the server's explicit lost
lease response stops a cloud-owned job. Completed uploads and terminal updates
are retried safely; private audio and arbitrary local paths never cross the API.
"""
from __future__ import annotations

import json
import math
import threading
import time
import uuid
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .app_library import read_json, visible_article
from .app_state import DEFAULT_SETTINGS, Store, TERMINAL, digest, now
from .util import sha256_file


class NoRedirect(HTTPRedirectHandler):
    """Never forward the bridge bearer credential to a redirect destination."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class CloudBridge:
    # Forces one upload after the R2 object-key naming migration.
    SYNC_SCHEMA_VERSION = 3
    POLL_SECONDS = 60
    LEASE_SECONDS = 300
    RENEW_SECONDS = 15

    def __init__(self, store: Store, worker):
        self.store, self.worker = store, worker
        self.worker_id = store.state("worker_id") or uuid.uuid4().hex
        store.set_state("worker_id", self.worker_id)
        self.last_settings_check = 0.0
        self.thread = None
        self.lease_thread = None
        self._lease_lock = threading.Lock()
        self._opener = build_opener(NoRedirect())

    def config(self):
        return read_json(self.store.root / "cloud.json")

    def request(self, method, path, data=None):
        config = self.config()
        base = config.get("url", "").rstrip("/")
        parts = urlsplit(base)
        allowed = parts.scheme == "https" or (parts.scheme == "http" and parts.hostname in {"127.0.0.1", "localhost"})
        if not parts.hostname or not allowed or parts.username or parts.password or parts.query or parts.fragment or parts.path:
            raise ValueError("Cloud URL must be an HTTPS origin, or a localhost HTTP origin for development")
        if not config.get("bridge_token"):
            raise ValueError("Cloud bridge token is not configured")
        if not path.startswith("/api/bridge/") or path.startswith("//"):
            raise ValueError("Only bridge API paths are allowed")
        encoded = json.dumps(data, ensure_ascii=False).encode() if data is not None else None
        # All bridge mutations are idempotent: claims return the worker's existing
        # live lease, file uploads use content hashes, and updates replace state.
        for attempt in range(3):
            request = Request(base + path, data=encoded, headers={
                "Authorization": "Bearer " + config["bridge_token"],
                "Content-Type": "application/json",
                "User-Agent": "OneMorePaper/0.2 (macOS library bridge)",
            }, method=method)
            try:
                with self._opener.open(request, timeout=10) as response:
                    raw = response.read(1_100_001)
                    if len(raw) > 1_100_000:
                        raise ValueError("Cloud response exceeded the bridge limit")
                    return json.loads(raw) if raw else {}
            except HTTPError as exc:
                if exc.code not in {408, 429, 500, 502, 503, 504} or attempt == 2:
                    raise
                delay = min(5, max(1, int(exc.headers.get("Retry-After", "1")))) if str(exc.headers.get("Retry-After", "1")).isdigit() else 1
            except (URLError, TimeoutError, ConnectionError):
                if attempt == 2:
                    raise
                delay = attempt + 1
            if self.worker.stop.wait(delay):
                raise RuntimeError("Bridge is stopping")

    def start(self):
        if self.thread and self.thread.is_alive():
            return
        self.thread = threading.Thread(target=self.run, name="cloud-bridge", daemon=True)
        self.lease_thread = threading.Thread(target=self.renew_loop, name="cloud-leases", daemon=True)
        self.lease_thread.start()
        self.thread.start()

    def _record_error(self, exc, *, state="cloud"):
        message = f"Cloud returned HTTP {exc.code}; synchronization will retry" if isinstance(exc, HTTPError) else "Cloud is unavailable; local processing continues and synchronization will retry"
        self.store.set_state(state, {"online": False, "last_attempt": now(), "message": message, "url": self.config().get("url")})

    def run(self):
        while not self.worker.stop.is_set():
            if not self.config():
                self.worker.stop.wait(5)
                continue
            try:
                self.cycle()
                self.store.set_state("cloud", {"online": True, "last_sync": now(), "url": self.config().get("url"), "sync_warnings": list(self.store.state("cloud_article_errors", {}).values())})
            except Exception as exc:
                self._record_error(exc)
            self.worker.stop.wait(self.POLL_SECONDS)

    def renew_loop(self):
        while not self.worker.stop.is_set():
            if self.config():
                try:
                    self.sync_jobs()
                except Exception as exc:
                    self._record_error(exc, state="cloud_lease")
            self.worker.stop.wait(self.RENEW_SECONDS)

    def _remote_jobs(self):
        # Active remote jobs must not disappear behind a limit of recent local jobs.
        with self.store.db() as db:
            rows = db.execute("SELECT data FROM jobs WHERE json_extract(data,'$.remote') IS NOT NULL AND COALESCE(json_extract(data,'$.remote.acknowledged'),0)=0").fetchall()
        return [json.loads(row[0]) for row in rows]

    def sync_jobs(self):
        if not self._lease_lock.acquire(blocking=False):
            return
        try:
            had_error = False
            for snapshot in self._remote_jobs():
                job = self.store.job(snapshot["id"])
                remote = job.get("remote") or {}
                if remote.get("acknowledged"):
                    continue
                status = job["status"] if job["status"] in TERMINAL else "running"
                value = float(job.get("progress", 0))
                progress = max(0, min(1, value / 100)) if math.isfinite(value) else 0
                fields = {"worker_id": self.worker_id, "lease_token": remote["lease_token"],
                          "lease_seconds": self.LEASE_SECONDS, "status": status, "progress": progress,
                          **{key: job[key] for key in ("stage", "message", "title") if key in job}}
                try:
                    response = self.request("PATCH", f"/api/bridge/jobs/{quote(job['id'], safe='')}", fields)
                except HTTPError as exc:
                    if exc.code == 409:
                        if job["status"] not in TERMINAL:
                            self.store.cancel(job["id"])
                            self.store.update_job(job["id"], remote={**remote, "acknowledged": True, "lease_lost": True},
                                                  message="Cloud lease was lost; completed files and cached work are preserved")
                        else:
                            # The server may have accepted completion before its
                            # response was lost. Preserve the completed result.
                            self.store.update_job(job["id"], remote={**remote, "acknowledged": True})
                        continue
                    self._record_error(exc, state="cloud_lease")
                    had_error = True
                    continue
                except (URLError, TimeoutError, ConnectionError):
                    self._record_error(RuntimeError("Cloud unavailable"), state="cloud_lease")
                    had_error = True
                    continue
                remote_job = response.get("job", {})
                # Preserve a local cancellation request without sending an unsupported
                # status. The generation worker acknowledges cancelled at a checkpoint.
                if remote_job.get("status") in {"cancel_requested", "cancelled"} and job["status"] not in TERMINAL:
                    self.store.cancel(job["id"])
                if job["status"] in TERMINAL:
                    self.store.update_job(job["id"], remote={**remote, "acknowledged": True})
            if not had_error:
                self.store.set_state("cloud_lease", {"online": True, "last_sync": now()})
        finally:
            self._lease_lock.release()

    def cycle(self):
        self.sync_jobs()
        self.request("POST", "/api/bridge/heartbeat", {"worker_id": self.worker_id, "title": "This Mac", "current_job_id": self.worker.current_job_id})
        if time.monotonic() - self.last_settings_check > 60:
            self.sync_settings()
            self.last_settings_check = time.monotonic()
        pending = self.store.state("cloud_pending_claim")
        if pending:
            self._adopt_claim(pending)
        if not self._remote_jobs() and not self.store.state("cloud_pending_claim"):
            response = self.request("POST", "/api/bridge/jobs/claim", {"worker_id": self.worker_id, "lease_seconds": self.LEASE_SECONDS})
            if response.get("job"):
                # Persist before handing work to the generation thread. A process
                # restart between receipt and local queue insertion must be harmless.
                self.store.set_state("cloud_pending_claim", response["job"])
                self._adopt_claim(response["job"])
        self.sync_articles()
        from .app_projects import ProjectDocuments
        try:
            ProjectDocuments(self.store).sync(self)
        except Exception:
            self.store.set_state("project_sync_error", {"at": now(), "message": "PhD project sync needs attention; article processing continues."})
        else:
            self.store.set_state("project_sync_error", None)

    def _adopt_claim(self, job):
        with self._lease_lock:
            return self._adopt_claim_locked(job)

    def _adopt_claim_locked(self, job):
        remote = {"lease_token": job["lease_token"], "worker_id": self.worker_id, "acknowledged": False}
        request = {key: job[key] for key in ("action", "scope", "article_id", "qa", "force") if key in job and job[key] is not None}
        try:
            local = self.store.create_job(request, job_id=job["id"], remote=remote)
            # Store.create_job is idempotent and deliberately returns an existing
            # record. Renew its cloud lease, rather than keeping a stale token.
            self.store.update_job(local["id"], remote=remote)
            if job.get("status") == "cancel_requested":
                self.store.cancel(local["id"])
        except (KeyError, ValueError) as exc:
            title = job.get("title") or "Selected Zotero article"
            message = (f"{title}: this article is not available in this Mac's library. Sync Zotero on the Mac, then retry this job."
                       if isinstance(exc, KeyError) else f"{title}: the requested processing options are not supported on this Mac.")
            try:
                self.request("PATCH", f"/api/bridge/jobs/{quote(job['id'], safe='')}", {
                    "worker_id": self.worker_id, "lease_token": job["lease_token"], "status": "failed",
                    "stage": "failed", "progress": 0, "title": title, "message": message,
                })
            except HTTPError as error:
                if error.code != 409:
                    raise
            self.store.event("cloud_job_rejected", job_id=job["id"], title=title, message=message)
        self.store.set_state("cloud_pending_claim", None)

    def sync_settings(self):
        response = self.request("GET", "/api/bridge/settings")
        remote = response.get("settings", response)
        local = self.store.settings()
        values = lambda value: {key: value[key] for key in DEFAULT_SETTINGS if key in value}
        if not remote.get("configured", True) or not remote.get("updated_at") or local.get("updated_at", "") > remote["updated_at"]:
            response = self.request("PATCH", "/api/bridge/settings", values(local))
            remote = response.get("settings", response)
            if remote.get("updated_at"):
                self.store.update_settings(values(remote), timestamp=remote["updated_at"])
        elif remote["updated_at"] > local.get("updated_at", ""):
            self.store.update_settings(values(remote), timestamp=remote["updated_at"])

    @staticmethod
    def _file_signature(path):
        if not path:
            return None
        try:
            value = Path(path).stat()
            return [str(path), value.st_mtime_ns, value.st_size]
        except OSError:
            return None

    @staticmethod
    def _cloud_article(article):
        value = visible_article(article, local=False)
        value["source_url"] = value.get("source_url") or ""
        value["authors"] = value.get("authors") or []
        if isinstance(value["authors"], str):
            value["authors"] = [value["authors"]]
        try:
            year = int(value.get("year"))
            value["year"] = year if 1 <= year <= 3000 else None
        except (ValueError, TypeError):
            value["year"] = None
        if not value.get("audio_url"):
            value.pop("audio_url", None)
        # Only episode URLs already published by the licensing-aware delivery path
        # should cross into cloud playback. All actual audio remains local here.
        for record in value.get("editions", {}).values():
            if not str(record.get("audio_url", "")).startswith("https://"):
                record.pop("audio_url", None)
        return value

    def sync_articles(self, limit=10):
        with self.store.db() as db:
            rows = db.execute("SELECT id,data,cloud_hash FROM articles ORDER BY updated_at DESC").fetchall()
        signatures = self.store.state("cloud_article_signatures", {})
        failures = self.store.state("cloud_article_errors", {})
        sent = 0
        for row in rows:
            article = json.loads(row["data"])
            visible = self._cloud_article(article)
            signature = digest({"article": visible, "markdown": self._file_signature(article.get("markdown")), "review": self._file_signature(article.get("review"))})
            if row["cloud_hash"] and signatures.get(row["id"]) == signature:
                continue
            previous_error = failures.get(row["id"], {})
            if previous_error.get("signature") == signature and previous_error.get("next_attempt", 0) > time.time():
                continue
            payload = {"article": visible}
            try:
                for key in ("markdown", "review"):
                    path = Path(article[key]) if article.get(key) else None
                    if path and path.is_file():
                        payload[key] = path.read_text(encoding="utf-8")
                fingerprint = digest({"sync_schema": self.SYNC_SCHEMA_VERSION, "payload": payload})
                if row["cloud_hash"] != fingerprint:
                    self.request("PUT", f"/api/bridge/articles/{quote(row['id'], safe='')}", payload)
                    with self.store.db() as db:
                        db.execute("UPDATE articles SET cloud_hash=? WHERE id=? AND data=?", (fingerprint, row["id"], row["data"]))
                    sent += 1
                signatures[row["id"]] = signature
                failures.pop(row["id"], None)
            except (HTTPError, OSError, UnicodeError) as exc:
                code = exc.code if isinstance(exc, HTTPError) else None
                failures[row["id"]] = {"title": article["title"], "signature": signature, "code": code,
                    "message": f"{article['title']}: cloud sync needs attention" + (f" (HTTP {code})" if code else ""),
                    "next_attempt": time.time() + (3600 if code in {400, 413} else 60)}
                if code in {401, 403, 429}:
                    break
            if sent >= limit or self.worker.stop.is_set():
                break
        self.store.set_state("cloud_article_signatures", signatures)
        self.store.set_state("cloud_article_errors", failures)
        return sent

def refresh_edited_markdown(store: Store):
    """Pick up user/agent edits for local and remote search without rewriting them."""
    for article in store.all_articles():
        path = Path(article["markdown"]) if article.get("markdown") else None
        if not path or not path.is_file():
            continue
        signature = [path.stat().st_mtime_ns, path.stat().st_size]
        if signature == article.get("markdown_stat"):
            continue
        import hashlib
        content = path.read_bytes()
        if signature != [path.stat().st_mtime_ns, path.stat().st_size]:
            continue
        current_hash = hashlib.sha256(content).hexdigest()
        with store.edit_article(article["id"], markdown=content.decode("utf-8")) as current:
            if current.get("markdown") != str(path):
                continue
            old_hash = current.get("markdown_sha256")
            current["markdown_stat"] = signature
            current["markdown_sha256"] = current_hash
            if old_hash and current_hash != old_hash:
                current["qa_status"] = "unchecked"
                current["warnings"] = [{"stage": "markdown", "message": "Markdown was edited. Existing audio is unchanged; regenerate an edition to hear the changes."}]
