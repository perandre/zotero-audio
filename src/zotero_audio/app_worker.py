"""One local generation worker and independent, retryable artifact delivery."""
from __future__ import annotations

import json
import os
import re
import shutil
import threading
import time
import traceback
import uuid
from pathlib import Path

from .app_library import artifact_path, import_existing, migrate_catalog_markdown, read_json, refresh_zotero
from .article_files import research_markdown_filename, review_markdown_path
from .app_state import Store, TERMINAL, now
from .util import sha256_file


class Cancelled(Exception):
    pass


class WorkerStopping(Cancelled):
    """A process restart resumes work; it is different from user cancellation."""


class BackendPool:
    """Lazy providers keep the model warm and let third-party packages register TTS.

    Entry point group: zotero_audio.tts. A provider exports a zero-argument factory
    returning the existing SpeechBackend protocol. No cloud fallback is implicit.
    """
    def __init__(self):
        self.providers = {}
        self.loaded = {}

    def get(self, name="kokoro"):
        if name not in self.loaded:
            if name == "kokoro":
                from .audio import MlxKokoroBackend
                self.loaded[name] = MlxKokoroBackend()
            else:
                from importlib.metadata import entry_points
                candidates = [p for p in entry_points(group="zotero_audio.tts") if p.name == name]
                if len(candidates) != 1:
                    raise ValueError(f"Speech provider {name!r} is not installed")
                self.loaded[name] = candidates[0].load()()
        return self.loaded[name]


def readable_filename(article: dict, edition: str, suffix: str = ".m4a") -> str:
    title = re.sub(r'[\x00-\x1f/:\\]', " — ", article["title"]).strip(" .")
    prefix = f"{article['year']} — " if article.get("year") else ""
    authors = [str(a).strip() for a in article.get("authors", []) if str(a).strip()]
    surname = authors[0].split()[-1][:40] if authors else ""
    author = f" — {surname}" if surname else ""
    tail = f"{author} — {edition.capitalize()}{suffix}"
    base = prefix + title
    while len((base + tail).encode("utf-8")) > 235:
        base = base[:-1]
    return base + tail


def _merge_warnings(*groups):
    """Preserve evidence from independent stages without repeating it."""
    seen, warnings = set(), []
    for group in groups:
        for warning in group or []:
            key = json.dumps(warning, sort_keys=True, ensure_ascii=False)
            if key not in seen:
                seen.add(key)
                warnings.append(warning)
    return warnings


def _publication_status(article, default):
    statuses = {record.get("publication_status") for record in article.get("editions", {}).values()}
    if "retry_pending" in statuses:
        return "retry_pending"
    if "pending" in statuses:
        return "pending"
    if "published" in statuses:
        return "published"
    return default


class LocalWorker:
    def __init__(self, store: Store):
        self.store = store
        self.stop = threading.Event()
        self.backends = BackendPool()
        self.current_job_id = None
        self.last_activity = now()
        self.threads = []

    def start(self):
        from .app_zotero import ZoteroMonitor
        self.store.recover()
        migrate_catalog_markdown(self.store)
        monitor = ZoteroMonitor(self.store, self.stop)
        for target, name in ((self.run, "generation"), (self.deliver, "delivery"), (monitor.run, "zotero-discovery")):
            thread = threading.Thread(target=target, name=name, daemon=True)
            thread.start()
            self.threads.append(thread)

    def run(self):
        while not self.stop.is_set():
            job = self.store.claim_job()
            if not job:
                self.stop.wait(0.5)
                continue
            self.current_job_id = job["id"]
            self.last_activity = now()
            try:
                self.process(job)
            except WorkerStopping:
                self.store.update_job(job["id"], status="queued", stage="queued", message="Paused for worker restart; cached work will resume")
            except Cancelled:
                self.store.update_job(job["id"], status="cancelled", stage="cancelled", message="Cancelled; completed files and cached speech are preserved")
            except Exception as exc:
                self.store.update_job(job["id"], status="failed", stage="failed", message=str(exc))
                self.store.event("job_failed", job_id=job["id"], title=job["title"], error=type(exc).__name__, message=str(exc))
                self._error_log(job["id"])
            finally:
                self.current_job_id = None
                self.last_activity = now()

    def _error_log(self, job_id):
        log = self.store.root / "errors"
        log.mkdir(exist_ok=True)
        (log / f"{job_id}.log").write_text(traceback.format_exc(), encoding="utf-8")

    def snapshot(self, source: Path, *, expected_hash: str | None = None, name: str | None = None) -> Path:
        """Freeze a delivery revision. APFS clones are cheap and safe for later edits."""
        content_hash = expected_hash or sha256_file(source)
        target = self.store.root / "artifacts" / content_hash / (name or source.name)
        if target.is_file():
            return target
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = target.with_name("." + target.name + "." + uuid.uuid4().hex + ".copying")
        try:
            cloned = False
            if os.uname().sysname == "Darwin":
                import ctypes
                library = ctypes.CDLL(None, use_errno=True)
                clone = library.clonefile
                clone.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_int]
                clone.restype = ctypes.c_int
                cloned = clone(os.fsencode(source), os.fsencode(temporary), 0) == 0
            if not cloned:
                shutil.copyfile(source, temporary)
            if sha256_file(temporary) != content_hash:
                raise RuntimeError("Artifact changed while preparing delivery; retry from the saved generation")
            temporary.chmod(0o600)
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
        return target

    def check_cancel(self, job):
        if self.stop.is_set():
            raise WorkerStopping()
        if self.store.job(job["id"])["status"] in {"cancel_requested", "cancelled"}:
            raise Cancelled()

    def process(self, job):
        from .generation import process_article
        if job["action"] == "sync":
            self.store.update_job(job["id"], stage="reading_zotero", message="Reading saved Zotero PDFs through the local GET API")
            imported = import_existing(self.store)
            result = refresh_zotero(self.store)
            self.store.update_job(job["id"], status="completed", stage="completed", progress=100,
                                  message=f"Imported {imported} existing articles. " + (f"Found {result['count']} saved PDFs in Zotero." if result["online"] else result["message"]))
            return
        if job["scope"] in {"new", "all"}:
            self.store.update_job(job["id"], stage="reading_zotero", message="Refreshing saved Zotero PDFs")
            refresh_zotero(self.store)
        articles = [self.store.article(job["article_id"])] if job["scope"] == "one" else self.store.all_articles()
        if job.get("automatic") and articles[0].get("in_zotero") is False:
            self.store.update_job(job["id"], status="cancelled", stage="cancelled", message="The PDF is no longer available in Zotero; existing output is preserved")
            return
        if job["scope"] != "one":
            articles = [a for a in articles if a.get("in_zotero") is not False]
        if job["scope"] == "new":
            if job["action"] == "markdown":
                articles = [a for a in articles if not a.get("markdown") or not Path(a["markdown"]).is_file()]
            else:
                wanted = {"full", "brief"} if job["action"] == "both" else {job["action"]}
                articles = [a for a in articles if any(not a.get("editions", {}).get(e, {}).get("audio") or not Path(a["editions"][e]["audio"]).is_file() for e in wanted)]
        outcomes = []
        for index, original_article in enumerate(articles):
            self.check_cancel(job)
            article_id = original_article["id"]
            self.store.update_job(job["id"], title=original_article["title"], article_id_current=article_id,
                                  progress=round(100 * index / len(articles)), message=f"Article {index + 1} of {len(articles)}")
            settings = self.store.settings()
            def progress(event):
                self.check_cancel(job)
                self.last_activity = now()
                stage = event.get("stage", "processing")
                detail = f"{event.get('completed', 0)} / {event['total']} segments" if event.get("total") else stage.replace("_", " ")
                self.store.update_job(job["id"], stage=stage, title=original_article["title"], edition=event.get("edition"), message=f"Article {index + 1}/{len(articles)} · {detail}")
                if stage == "markdown_ready":
                    markdown_path = Path(event["markdown"])
                    generation = read_json(markdown_path.parent / "generation.json")
                    markdown = markdown_path.read_text(encoding="utf-8")
                    with self.store.edit_article(article_id, markdown=markdown) as current:
                        current.update(markdown=event["markdown"], markdown_sha256=event["markdown_sha256"], markdown_status="ready",
                                       qa_status=event.get("qa_status", "unchecked"),
                                       review=str(review_markdown_path(markdown_path.parent, current, migrate=True)), managed=True)
                        current["artifacts"] = {**current.get("artifacts", {}), "markdown": True, "review": True}
                        current["warnings"] = _merge_warnings(generation.get("warnings"), current.get("delivery_warnings"))
                    self.queue_backup(current, Path(event["markdown"]), "Markdown", settings)
            def ready(record):
                display = self.store.article(article_id)
                edition = record["edition"]
                # The generator's cache path can change on the next run. Delivery
                # and listening use an immutable, readable revision instead.
                record["audio"] = str(self.snapshot(Path(record["audio"]), expected_hash=record.get("audio_sha256"), name=readable_filename(display, edition)))
                if record.get("markdown") and Path(record["markdown"]).is_file():
                    record["markdown"] = str(self.snapshot(Path(record["markdown"]), name=research_markdown_filename(display)))
                publishing = bool(settings["auto_publish"] and record.get("public_eligible") and record.get("selected"))
                private = not record.get("public_eligible")
                destination = (str(Path(settings["icloud_folder"]) / readable_filename(display, edition))
                               if private and settings.get("icloud_folder") else None)
                with self.store.edit_article(article_id) as current:
                    previous = current.setdefault("editions", {}).get(edition, {})
                    already_published = (publishing and previous.get("audio_sha256") == record.get("audio_sha256")
                                         and previous.get("publication_status") == "published")
                    record["publication_status"] = ("published" if already_published else "pending" if publishing
                        else "private" if private else "not_requested" if not settings["auto_publish"] else "not_selected")
                    if already_published and previous.get("audio_url"):
                        record["audio_url"] = previous["audio_url"]
                    current["editions"][edition] = record
                    current["audio_status"] = "ready"
                    current.setdefault("artifacts", {})["audio"] = True
                    current["publication_status"] = _publication_status(current, record["publication_status"])
                    if private:
                        current["icloud_status"] = "pending" if destination else "not_configured"
                if publishing and not already_published:
                    self.store.enqueue_delivery("publish", article_id, {"record": record})
                elif destination:
                    self.store.enqueue_delivery("icloud", article_id, {"source": record["audio"], "target": destination, "sha256": record.get("audio_sha256")})
                self.queue_backup(current, Path(record["audio"]), edition, settings)
                self.store.event("audio_ready", job_id=job["id"], title=current["title"], edition=edition)
            try:
                if original_article.get("source_changed") and not job.get("force"):
                    raise ValueError("The source PDF changed. Existing Markdown is preserved; choose Regenerate to replace it explicitly.")
                result = process_article(Path(original_article["bundle"]), mode=job["action"], pdf=Path(original_article["source_path"]) if original_article.get("source_path") else None,
                    zotero_key=article_id, metadata={**original_article.get("metadata", {}), "podcast_selected": settings["auto_publish"]}, backend_factory=lambda: self.backends.get(settings["tts_model"]),
                    qa=job["qa"], force=job["force"], opening_sound=settings["opening_sound"] == "typing", closing_sound=settings["closing_sound"] == "typing",
                    spoken_intro=settings["spoken_intro"], full_voice=settings["full_voice"], brief_voice=settings["brief_voice"], speed=settings["speed"],
                    progress=progress, on_edition_ready=ready)
                with self.store.edit_article(article_id) as current:
                    # Discovery can notice a replacement PDF during synthesis.
                    # Preserve its hash and warning when finishing older work.
                    source_changed = current.get("source_sha256") not in {None, original_article.get("source_sha256"), result["source_sha256"]}
                    current.update(managed=True, source_changed=source_changed,
                                   source_sha256=current["source_sha256"] if source_changed else result["source_sha256"],
                                   warnings=_merge_warnings(result.get("warnings"), current.get("delivery_warnings")),
                                   license_status="open" if result.get("public_eligible") else "private",
                                   qa_status="warnings" if result.get("warnings") else ("passed" if job["qa"] else "unchecked"), review=result["ai_review"])
                    for edition, record in result.get("editions", {}).items():
                        # Retain delivery status only for this exact audio revision.
                        existing = current.setdefault("editions", {}).get(edition, {})
                        same_audio = record.get("audio_sha256") and existing.get("audio_sha256") == record["audio_sha256"]
                        retained = {key: existing[key] for key in ("publication_status", "audio_url") if same_audio and key in existing}
                        current["editions"][edition] = {**record, **retained}
                    current["publication_status"] = _publication_status(current, current.get("publication_status", "not_requested"))
                outcomes.append({"id": article_id, "title": current["title"], "status": result["status"], "warnings": len(current["warnings"])})
            except Cancelled:
                raise
            except Exception as exc:
                with self.store.edit_article(article_id) as current:
                    current["warnings"] = _merge_warnings(current.get("warnings"), [{"stage": "generation", "message": str(exc)}])
                    current["qa_status"] = "warnings"
                outcomes.append({"id": article_id, "title": current["title"], "status": "failed", "error": str(exc)})
                self._error_log(job["id"])
                self.store.event("article_failed", job_id=job["id"], title=current["title"], message=str(exc))
        failed = sum(r["status"] == "failed" for r in outcomes)
        warnings = sum(r.get("warnings", 0) for r in outcomes)
        message = f"{len(outcomes) - failed} articles ready"
        if not outcomes:
            message = "Everything requested is already available"
        if failed:
            message += f"; {failed} need attention"
        if warnings:
            message += f"; {warnings} warnings"
        message += ". Delivery continues independently."
        self.store.update_job(job["id"], status="failed" if outcomes and failed == len(outcomes) else "completed", stage="completed", progress=100, message=message, results=outcomes)

    def queue_backup(self, article, source: Path, edition: str, settings):
        if not settings.get("backup_enabled") or not settings.get("backup_folder"):
            return
        try:
            content_hash = sha256_file(source)
            source = self.snapshot(source, expected_hash=content_hash)
            target = Path(settings["backup_folder"]) / readable_filename(article, edition, "") / content_hash[:16] / source.name
            with self.store.edit_article(article["id"]) as current:
                current["backup_status"] = "pending"
            self.store.enqueue_delivery("backup", article["id"], {"source": str(source), "target": str(target), "sha256": content_hash})
        except Exception as exc:
            with self.store.edit_article(article["id"]) as current:
                current["backup_status"] = "failed"
                warning = {"stage": "backup", "message": f"Backup could not be prepared: {exc}. Generation remains available."}
                current["delivery_warnings"] = _merge_warnings(current.get("delivery_warnings"), [warning])
                current["warnings"] = _merge_warnings(current.get("warnings"), [warning])
            self.store.event("backup_failed", title=current["title"], message=str(exc))

    def deliver(self):
        while not self.stop.is_set():
            with self.store.db() as db:
                row = db.execute("""SELECT * FROM deliveries WHERE status='pending' AND next_attempt <= ?
                    ORDER BY CASE kind WHEN 'publish' THEN 0 WHEN 'icloud' THEN 1 ELSE 2 END,updated_at LIMIT 1""", (time.time(),)).fetchone()
                if row:
                    db.execute("UPDATE deliveries SET status='running',updated_at=? WHERE id=?", (now(), row["id"]))
            if not row:
                self.stop.wait(1)
                continue
            try:
                payload = json.loads(row["data"])
                if row["kind"] == "publish":
                    self.publish(row["article_id"], payload["record"])
                else:
                    self.copy_artifact(payload)
                    with self.store.edit_article(row["article_id"]) as current:
                        current[row["kind"] + "_status"] = "copied"
                with self.store.db() as db:
                    db.execute("UPDATE deliveries SET status='completed',error=NULL,updated_at=? WHERE id=?", (now(), row["id"]))
            except Exception as exc:
                attempts = row["attempts"] + 1
                self._error_log("delivery-" + row["id"])
                with self.store.db() as db:
                    db.execute("UPDATE deliveries SET status='pending',attempts=?,error=?,next_attempt=?,updated_at=? WHERE id=?",
                               (attempts, str(exc), time.time() + min(3600, 15 * 2 ** min(attempts, 8)), now(), row["id"]))
                with self.store.edit_article(row["article_id"]) as current:
                    if row["kind"] == "publish":
                        record = payload.get("record", {})
                        edition = current.get("editions", {}).get(record.get("edition"), {})
                        if edition.get("audio_sha256") == record.get("audio_sha256"):
                            edition["publication_status"] = "retry_pending"
                            current["publication_status"] = _publication_status(current, "retry_pending")
                    else:
                        current[row["kind"] + "_status"] = "retry_pending"
                self.store.event("delivery_retry", kind=row["kind"], title=current["title"], message=str(exc), attempts=attempts)

    @staticmethod
    def copy_artifact(payload):
        source, target = Path(payload["source"]), Path(payload["target"])
        if not source.is_file():
            raise FileNotFoundError("The generated source file is not available")
        if source.resolve() == target.resolve():
            return
        # Atomic local export; do not poll iCloud network synchronization.
        target.parent.mkdir(parents=True, exist_ok=True)
        content_hash = payload.get("sha256") or sha256_file(source)
        original_target = target
        collision = 0
        while target.exists():
            if target.stat().st_size == source.stat().st_size and sha256_file(target) == content_hash:
                return
            # Keep collisions/revisions instead of overwriting a user's other file.
            collision += 1
            ending = "" if collision == 1 else f"-{collision}"
            target = original_target.with_name(original_target.stem + " — " + content_hash[:8] + ending + original_target.suffix)
        temporary = target.with_name("." + target.name + ".copying")
        try:
            shutil.copyfile(source, temporary)
            if sha256_file(temporary) != content_hash:
                raise RuntimeError("Export checksum did not match")
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)

    def publish(self, article_id, record):
        from .generation import publish_finished_episode
        from .podcast import load_podcast_config
        from .publish_sync import sync_public
        config_path = self.store.runtime / "podcast.toml"
        if not config_path.is_file():
            raise RuntimeError("Podcast publishing is not configured")
        config = load_podcast_config(config_path)
        publication = publish_finished_episode(record, config)
        published = next(iter(publication.get("public_editions", [])), {})
        if publication.get("published") and published.get("audio_url"):
            sync_public(config, [published])
        elif not publication.get("published"):
            with self.store.edit_article(article_id) as current:
                edition = current.get("editions", {}).get(record.get("edition"), {})
                if edition.get("audio_sha256") == record.get("audio_sha256"):
                    edition["publication_status"] = publication.get("reason", "not_published")
                    current["publication_status"] = _publication_status(current, edition["publication_status"])
            return
        else:
            raise RuntimeError("The publisher did not return a published episode")
        edition = record["edition"]
        with self.store.edit_article(article_id) as current:
            latest = current.get("editions", {}).get(edition, {})
            if latest.get("audio_sha256") == record.get("audio_sha256"):
                latest.update(audio_url=published["audio_url"], publication_status="published")
                current["publication_status"] = _publication_status(current, "published")
        self.store.event("episode_published", title=current["title"], edition=edition)
