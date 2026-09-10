"""Loopback dashboard/API. It serves only registered artifacts, never arbitrary files."""
from __future__ import annotations

import fcntl
import json
import mimetypes
import os
import re
import secrets
import signal
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlsplit

from .app_bridge import CloudBridge, refresh_edited_markdown
from .app_library import artifact_path, import_existing, refresh_zotero, visible_article
from .app_state import Store, TERMINAL, now
from .app_worker import LocalWorker


def status(store: Store, worker=None):
    articles = store.all_articles()
    jobs = store.jobs()["jobs"]
    with store.db() as db:
        pending = db.execute("SELECT kind,count(*) count FROM deliveries WHERE status!='completed' GROUP BY kind").fetchall()
    counts = {"articles": len(articles), "total": len(articles), "markdown_ready": sum(a.get("markdown_status") == "ready" for a in articles),
              "audio_ready": sum(a.get("audio_status") == "ready" for a in articles), "warnings": sum(bool(a.get("warnings")) for a in articles),
              "queued": sum(j["status"] == "queued" for j in jobs), "running": sum(j["status"] == "running" for j in jobs)}
    return {"worker": {"online": worker is not None, "last_seen": now() if worker else store.state("last_seen"),
                       "current_job_id": getattr(worker, "current_job_id", None)}, "counts": counts,
            "cloud": store.state("cloud", {"online": False, "configured": False}), "cloud_lease": store.state("cloud_lease", {}), "zotero": store.state("zotero", {}),
            "deliveries": {r["kind"]: r["count"] for r in pending}, "capabilities": {"local_files": True, "authenticated": True, "remote": False},
            "version": "0.2.0"}


def public_job(job):
    return {k: v for k, v in job.items() if k not in {"remote"}}


def make_handler(store: Store, worker, port: int):
    asset_root = Path(__file__).resolve().parents[2] / "cloud/public"
    allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
    allowed_origins = {"http://" + host for host in allowed_hosts}

    class Handler(BaseHTTPRequestHandler):
        server_version = "OneMorePaper/0.2"

        def log_message(self, format, *args):
            if args and str(args[1] if len(args) > 1 else "").startswith("5"):
                store.event("http_error", message=str(args[0])[:200])

        def send_json(self, value, code=200, *, headers=None):
            payload = json.dumps(value, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            for name, value in (headers or {}).items():
                self.send_header(name, value)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(payload)

        def authorize(self, mutation=False):
            if self.headers.get("Host") not in allowed_hosts:
                raise PermissionError("This service accepts loopback hostnames only")
            origin = self.headers.get("Origin")
            if origin and origin not in allowed_origins:
                raise PermissionError("Cross-origin access is not allowed")
            if mutation:
                if self.headers.get_content_type().lower() != "application/json":
                    raise ValueError("Send application/json")
                if not origin:
                    token = store.state("local_api_token")
                    if not isinstance(token, str) or not token or not secrets.compare_digest(self.headers.get("Authorization", ""), "Bearer " + token):
                        raise PermissionError("Use the CLI, dashboard, or a local API credential")

        def body(self):
            if self.headers.get("Transfer-Encoding"):
                raise ValueError("Send a JSON body with Content-Length; chunked requests are not supported")
            if len(self.headers.get_all("Content-Length", [])) > 1:
                raise ValueError("Send exactly one Content-Length header")
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 <= length <= 1_048_576:
                raise ValueError("Request body exceeds 1 MB")
            value = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(value, dict):
                raise ValueError("Request body must be an object")
            return value

        def dispatch(self):
            try:
                method = "GET" if self.command == "HEAD" else self.command
                mutation = method in {"POST", "PATCH"}
                self.authorize(mutation)
                parsed = urlsplit(self.path)
                path, query = unquote(parsed.path), parse_qs(parsed.query)
                first = lambda key, default="": query.get(key, [default])[0]
                body = self.body() if mutation else {}
                if path == "/api/status" and method == "GET":
                    return self.send_json(status(store, worker))
                if path == "/api/capabilities" and method == "GET":
                    return self.send_json({"local_files": True, "authenticated": True, "remote": False, "cancel_jobs": True, "retry_jobs": True})
                if path == "/api/library" and method == "GET":
                    result = store.library(first("q"), int(first("limit", "100")), int(first("offset", "0")))
                    result["items"] = [visible_article(a) for a in result["items"]]
                    return self.send_json(result)
                if path == "/api/search" and method == "GET":
                    return self.send_json(store.search(first("q")))
                if path == "/api/settings":
                    if method == "GET":
                        return self.send_json(store.settings())
                    if method == "PATCH":
                        return self.send_json(store.update_settings(body))
                if path == "/api/jobs":
                    if method == "GET":
                        return self.send_json({"jobs": [public_job(j) for j in store.jobs()["jobs"]]})
                    if method == "POST":
                        return self.send_json({"job": public_job(store.create_job(body))}, 201)
                match = re.fullmatch(r"/api/jobs/([^/]+)(?:/(cancel|retry))?", path)
                if match:
                    job_id, action = match.groups()
                    if method == "GET" and not action:
                        return self.send_json({"job": public_job(store.job(job_id))})
                    if method == "POST" and action == "cancel":
                        return self.send_json({"job": public_job(store.cancel(job_id))})
                    if method == "POST" and action == "retry":
                        old = store.job(job_id)
                        if old["status"] not in {"failed", "cancelled"}:
                            return self.send_json({"job": public_job(old)})
                        request = {key: old[key] for key in ("action", "scope", "article_id", "qa")}
                        request.update(force=False, idempotency_key=f"retry:{job_id}")
                        return self.send_json({"job": public_job(store.create_job(request))}, 201)
                match = re.fullmatch(r"/api/articles/([^/]+)(?:/(markdown|review|audio|open))?", path)
                if match:
                    article_id, action = match.groups()
                    article = store.article(article_id)
                    if method == "GET" and not action:
                        return self.send_json(visible_article(article))
                    if method == "GET" and action in {"markdown", "review", "audio"}:
                        target = artifact_path(article, action, first("edition", "full"))
                        return self.send_file(target, download=first("download") == "1")
                    if method == "POST" and action == "open":
                        kind = body.get("artifact", "markdown")
                        if kind not in {"markdown", "audio", "review"}:
                            raise ValueError("Choose Markdown, audio or review")
                        target = artifact_path(article, kind, body.get("edition", "full"))
                        subprocess.run(["/usr/bin/open", *(["-R"] if body.get("reveal") else []), str(target)], check=True)
                        return self.send_json({"opened": True, "title": article["title"], "artifact": kind})
                if method == "GET":
                    if path in {"/", "/login"} or path.startswith("/articles/"):
                        target = asset_root / "index.html"
                    else:
                        target = (asset_root / path.lstrip("/")).resolve()
                        if not target.is_relative_to(asset_root.resolve()):
                            raise PermissionError("Invalid asset path")
                    if target.is_file():
                        return self.send_file(target)
                return self.send_json({"error": {"code": "not_found", "message": "Not found"}}, 404)
            except (KeyError, FileNotFoundError) as exc:
                return self.send_json({"error": {"code": "not_found", "message": str(exc).strip("'")}}, 404)
            except (ValueError, TypeError) as exc:
                return self.send_json({"error": {"code": "invalid_request", "message": str(exc)}}, 400)
            except PermissionError as exc:
                return self.send_json({"error": {"code": "forbidden", "message": str(exc)}}, 403)
            except (BrokenPipeError, ConnectionResetError):
                return
            except Exception as exc:
                store.event("http_error", message=str(exc), type=type(exc).__name__)
                return self.send_json({"error": {"code": "internal_error", "message": str(exc)}}, 500)

        def send_file(self, path: Path, *, download=False):
            # Hold one opened revision while serving it. Generated Markdown
            # and AAC can be atomically replaced by the worker concurrently.
            with path.open("rb") as stream:
                return self.send_stream(path, stream, download=download)

        def send_stream(self, path, stream, *, download=False):
            size = os.fstat(stream.fileno()).st_size
            start, end, partial = 0, size - 1, False
            header = self.headers.get("Range")
            if header:
                match = re.fullmatch(r"bytes=(\d*)-(\d*)", header)
                if not match or (not match[1] and not match[2]):
                    return self.send_json({"error": {"code": "invalid_range", "message": "Invalid byte range"}}, 416,
                                          headers={"Content-Range": f"bytes */{size}"})
                if match[1]:
                    start = int(match[1])
                    end = min(int(match[2]) if match[2] else size - 1, size - 1)
                else:
                    start, end = max(0, size - int(match[2])), size - 1
                if start >= size or start > end:
                    return self.send_json({"error": {"code": "invalid_range", "message": "Byte range is outside this file"}}, 416,
                                          headers={"Content-Range": f"bytes */{size}"})
                partial = True
            self.send_response(206 if partial else 200)
            content_type = {".md": "text/markdown; charset=utf-8", ".m4a": "audio/mp4", ".js": "text/javascript; charset=utf-8"}.get(path.suffix, mimetypes.guess_type(path.name)[0] or "application/octet-stream")
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(max(0, end - start + 1)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; media-src 'self' https:; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'")
            if partial:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            if download:
                self.send_header("Content-Disposition", "attachment; filename*=UTF-8''" + quote(path.name))
            self.end_headers()
            if self.command == "HEAD":
                return
            stream.seek(start)
            remaining = end - start + 1
            while remaining > 0:
                chunk = stream.read(min(65536, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)

        do_GET = dispatch
        do_HEAD = dispatch
        do_POST = dispatch
        do_PATCH = dispatch

    return Handler


def serve(store: Store, port=8765):
    from .runtime import configure_tool_path
    configure_tool_path()
    store.root.mkdir(parents=True, exist_ok=True)
    with (store.root / "daemon.lock").open("a+") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("The local worker is already running") from exc
        # The old scheduler and this daemon must not both run generation/publication.
        legacy_path = store.runtime / "full-library/automatic-sync.lock"
        legacy_path.parent.mkdir(exist_ok=True, parents=True)
        with legacy_path.open("a+") as legacy_lock:
            try:
                fcntl.flock(legacy_lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise RuntimeError("The previous audio batch is still running. Wait for it before starting the new worker.") from exc
            if not store.state("local_api_token"):
                store.set_state("local_api_token", secrets.token_urlsafe(32))
            worker = LocalWorker(store)
            httpd = ThreadingHTTPServer(("127.0.0.1", port), make_handler(store, worker, port))
            httpd.daemon_threads = True
            store.set_state("daemon", {"pid": os.getpid(), "port": port, "started_at": now()})
            def maintenance():
                while not worker.stop.is_set():
                    try:
                        refresh_edited_markdown(store)
                    except Exception as exc:
                        store.event("maintenance_error", message=str(exc))
                    store.set_state("last_seen", now())
                    worker.stop.wait(15)
            def shutdown(signum, frame):
                worker.stop.set()
                threading.Thread(target=httpd.shutdown, daemon=True).start()
            signal.signal(signal.SIGTERM, shutdown)
            signal.signal(signal.SIGINT, shutdown)
            worker.start()
            CloudBridge(store, worker).start()
            threading.Thread(target=maintenance, name="catalog-maintenance", daemon=True).start()
            print(f"1 More Paper is running at http://127.0.0.1:{port}", flush=True)
            try:
                httpd.serve_forever(poll_interval=0.25)
            finally:
                worker.stop.set()
                httpd.server_close()
                for thread in worker.threads:
                    # Keep the exclusive lock until the current atomic stage
                    # finishes. A replacement worker must not race FFmpeg.
                    while thread.is_alive():
                        thread.join(timeout=0.5)
                store.set_state("daemon", {})
