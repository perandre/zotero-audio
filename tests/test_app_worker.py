import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from zotero_audio import generation
from zotero_audio.app_state import Store
from zotero_audio.app_worker import Cancelled, LocalWorker, readable_filename
from zotero_audio.extract import render_research_markdown
from zotero_audio.util import atomic_write_json, json_digest, sha256_file


@pytest.fixture
def article_store(tmp_path):
    store = Store(tmp_path / "runtime")
    bundle = store.runtime / "library" / "ARTICLE1"
    bundle.mkdir(parents=True)
    structure = {"document": {"title": "How a company successfully adopted AI", "authors": ["Anna Author"], "publication_year": "2025"},
        "source": {"filename": "paper.pdf", "path": str(bundle / "paper.pdf"), "sha256": "a" * 64, "zotero_key": "ARTICLE1", "pdf_pages": 1},
        "extraction": {"engine": "fixture", "engine_version": "1", "include_references": True, "errors": []},
        "pages": [], "blocks": [
            {"id": "b1", "type": "heading", "heading_level": 2, "text": "Introduction", "pdf_page": 1, "included_in_reading": True},
            {"id": "b2", "type": "paragraph", "text": "The company reduced customer waiting time through careful AI adoption [7].", "pdf_page": 1, "included_in_reading": True},
            {"id": "b3", "type": "heading", "heading_level": 2, "text": "Conclusion", "pdf_page": 1, "included_in_reading": True},
            {"id": "b4", "type": "paragraph", "text": "Careful staff training helped improve the outcome.", "pdf_page": 1, "included_in_reading": True}]}
    structure["structure_sha256"] = json_digest(structure)
    atomic_write_json(bundle / "structure.json", structure)
    markdown = render_research_markdown(structure)
    (bundle / "article.md").write_text(markdown)
    article = {"id": "ARTICLE1", "title": structure["document"]["title"], "authors": ["Anna Author"], "year": "2025",
               "bundle": str(bundle), "metadata": structure["document"], "markdown": str(bundle / "article.md"),
               "source_sha256": "a" * 64, "editions": {}, "artifacts": {"markdown": True, "audio": False}, "warnings": []}
    store.put_article(article, markdown=markdown)
    return store, article


def claim(store, action="markdown", **options):
    queued = store.create_job({"action": action, "scope": "one", "article_id": "ARTICLE1", **options})
    assert store.claim_job()["id"] == queued["id"]
    return store.job(queued["id"])


def deliveries(store):
    with store.db() as db:
        return [{**dict(row), "payload": json.loads(row["data"])} for row in db.execute("SELECT * FROM deliveries ORDER BY updated_at")]


def test_real_markdown_only_flow_updates_catalog_search_and_review_without_tts(article_store, monkeypatch):
    store, article = article_store
    markdown_path = Path(article["markdown"])
    manual = markdown_path.read_text().replace("Careful staff", "Thoughtful staff")
    markdown_path.write_text(manual)
    worker = LocalWorker(store)
    monkeypatch.setattr(worker.backends, "get", lambda *args: pytest.fail("Markdown must not load a speech model"))
    job = claim(store)
    worker.process(job)
    assert store.job(job["id"])["status"] == "completed"
    updated = store.article(article["id"])
    assert updated["markdown_status"] == "ready"
    assert Path(updated["review"]).is_file()
    assert markdown_path.read_text() == manual
    assert store.search("Thoughtful")["results"][0]["title"] == article["title"]
    assert not updated["editions"]
    assert not deliveries(store)


def test_whole_zotero_run_keeps_historical_documents_searchable_without_processing_them(article_store, monkeypatch):
    from zotero_audio import app_worker
    store, article = article_store
    store.put_article({**article, "in_zotero": False})
    monkeypatch.setattr(app_worker, "refresh_zotero", lambda store: {"online": True})
    monkeypatch.setattr(generation, "process_article", lambda *a, **kw: pytest.fail("Historical documents are not in a whole-Zotero run"))
    job = store.create_job({"action": "markdown", "scope": "all"})
    LocalWorker(store).process(job)
    assert store.job(job["id"])["status"] == "completed"
    assert store.search("careful")["results"][0]["title"] == article["title"]


@pytest.mark.parametrize("private", [False, True])
def test_edition_callback_exposes_audio_and_queues_delivery_before_next_edition(article_store, monkeypatch, private):
    store, article = article_store
    store.update_settings({"icloud_folder": str(store.runtime / "icloud")})
    seen = []

    def generate(bundle, **kwargs):
        kwargs["progress"]({"stage": "markdown_ready", "title": article["title"], "markdown": article["markdown"],
                            "markdown_sha256": sha256_file(Path(article["markdown"])), "qa_status": "skipped"})
        records = {}
        for edition in ("brief", "full"):
            if edition == "full":
                current = store.article(article["id"])
                assert current["editions"]["brief"]["audio"]
                queued = deliveries(store)
                assert len(queued) == 1
                assert queued[0]["kind"] == ("icloud" if private else "publish")
                assert store.job(job["id"])["status"] == "running"
                assert not (store.runtime / "icloud").exists()
            path = bundle / f"{edition}.m4a"
            path.write_bytes(f"finished {edition}".encode())
            record = {"edition": edition, "title": article["title"], "audio": str(path), "audio_sha256": sha256_file(path),
                      "public_eligible": not private, "selected": True, "status": "ready"}
            records[edition] = record
            kwargs["on_edition_ready"](record)
            seen.append(edition)
        return {"source_sha256": "a" * 64, "warnings": [], "ai_review": str(bundle / "ai-review.md"),
                "editions": records, "status": "ready"}

    monkeypatch.setattr(generation, "process_article", generate)
    job = claim(store, "both", qa=False)
    LocalWorker(store).process(job)
    assert seen == ["brief", "full"]
    assert len(deliveries(store)) == 2
    assert store.job(job["id"])["status"] == "completed"


def test_changed_pdf_preserves_manual_markdown_until_explicit_regeneration(article_store, monkeypatch):
    store, article = article_store
    before = Path(article["markdown"]).read_bytes()
    store.put_article({**article, "source_changed": True})
    monkeypatch.setattr(generation, "process_article", lambda *a, **kw: pytest.fail("Changed source requires explicit regeneration"))
    job = claim(store)
    LocalWorker(store).process(job)
    assert store.job(job["id"])["status"] == "failed"
    assert "Regenerate" in store.job(job["id"])["results"][0]["error"]
    assert Path(article["markdown"]).read_bytes() == before


def test_backup_is_queued_without_delaying_markdown_and_failure_retries_independently(article_store, monkeypatch):
    store, article = article_store
    blocked_destination = store.runtime / "not-a-folder"
    blocked_destination.write_text("An existing user file")
    store.update_settings({"backup_enabled": True, "backup_folder": str(blocked_destination)})
    worker = LocalWorker(store)
    job = claim(store)
    worker.process(job)
    assert store.job(job["id"])["status"] == "completed"
    assert deliveries(store)[0]["status"] == "pending"
    assert store.article(article["id"])["backup_status"] == "pending"

    def fail_copy(payload):
        worker.stop.set()
        raise OSError("Backup volume unavailable")

    monkeypatch.setattr(worker, "copy_artifact", fail_copy)
    worker.deliver()
    queued = deliveries(store)[0]
    assert queued["status"] == "pending" and queued["attempts"] == 1
    assert "Backup volume unavailable" in queued["error"]
    assert store.job(job["id"])["status"] == "completed"
    assert Path(article["markdown"]).is_file()


@pytest.mark.parametrize("replacement_mode", ["atomic", "in_place"])
def test_pending_backup_preserves_original_revision_after_regeneration(article_store, replacement_mode):
    store, article = article_store
    store.update_settings({"backup_enabled": True, "backup_folder": str(store.runtime / "backups")})
    worker = LocalWorker(store)
    source = Path(article["markdown"])
    first_revision = source.read_bytes()
    worker.queue_backup(article, source, "Markdown", store.settings())
    # The generator/editor installs a later file through atomic replacement.
    if replacement_mode == "atomic":
        replacement = source.with_suffix(".new")
        replacement.write_text("A new, deliberately edited revision")
        replacement.replace(source)
    else:
        source.write_text("A new, deliberately edited revision")
    payload = deliveries(store)[0]["payload"]
    worker.copy_artifact(payload)
    assert Path(payload["target"]).read_bytes() == first_revision
    assert source.read_text() == "A new, deliberately edited revision"


def test_optional_backup_snapshot_failure_does_not_fail_finished_markdown(article_store, monkeypatch):
    store, article = article_store
    store.update_settings({"backup_enabled": True, "backup_folder": str(store.runtime / "backups")})
    worker = LocalWorker(store)

    def unavailable_snapshot(*args, **kwargs):
        raise OSError("Backup snapshot area is unavailable")

    monkeypatch.setattr(worker, "snapshot", unavailable_snapshot)
    job = claim(store)
    worker.process(job)
    assert store.job(job["id"])["status"] == "completed"
    assert store.article(article["id"])["markdown_status"] == "ready"
    assert store.article(article["id"]).get("backup_status") in {"failed", "retry_pending", "snapshot_failed"}


def test_worker_honors_cancelled_state_even_if_job_was_claimed_during_cancellation(article_store):
    store, _ = article_store
    worker = LocalWorker(store)
    job = claim(store)
    # This is the durable state left if cancel() read a queued job immediately
    # before another connection claimed it. The active worker must stop.
    store.update_job(job["id"], status="cancelled")
    with pytest.raises(Cancelled):
        worker.check_cancel(job)


def test_publisher_envelope_uploads_ready_assets_without_synthesis(article_store, monkeypatch):
    import zotero_audio.podcast as podcast
    import zotero_audio.publish_sync as publish_sync
    import zotero_audio.audio as audio
    store, article = article_store
    (store.runtime / "podcast.toml").write_text("# fixture; parsed by injected config loader\n")
    path = Path(article["bundle"]) / "full.m4a"
    path.write_bytes(b"already completed AAC fixture")
    record = {"edition": "full", "title": article["title"], "audio": str(path), "audio_sha256": sha256_file(path)}
    store.put_article({**article, "editions": {"full": record}})
    config = SimpleNamespace()
    monkeypatch.setattr(podcast, "load_podcast_config", lambda path: config)
    monkeypatch.setattr(generation, "process_article", lambda *a, **kw: pytest.fail("Publishing must never generate"))
    monkeypatch.setattr(audio, "assemble_m4a", lambda *a, **kw: pytest.fail("Publishing must never encode"))
    published = {**record, "audio_url": "https://podcast.example.org/finished.m4a"}
    monkeypatch.setattr(generation, "publish_finished_episode", lambda input_record, settings: {
        "published": True, "feed_changed": True, "public_editions": [published]})
    uploaded = []
    monkeypatch.setattr(publish_sync, "sync_public", lambda settings, records: uploaded.extend(records))
    LocalWorker(store).publish(article["id"], record)
    assert uploaded == [published]
    assert store.article(article["id"])["editions"]["full"]["publication_status"] == "published"
    assert sha256_file(path) == record["audio_sha256"]


def test_private_audio_filename_is_readable_and_path_safe():
    article = {"id": "PRIVATEKEY987", "title": "AI / success: evidence\\results\nfrom companies", "authors": ["Anna Author"], "year": "2025"}
    name = readable_filename(article, "full")
    assert name.endswith(".m4a")
    assert "AI" in name and "companies" in name and "Author" in name and "2025" in name
    assert "PRIVATEKEY987" not in name
    assert not any(char in name for char in "/\\\n")
    assert len(readable_filename({**article, "title": "Norsk forskning ø " * 100}, "full").encode()) <= 235


def test_blank_and_unusually_long_author_names_do_not_break_filename_creation():
    import subprocess
    import sys
    article = {"title": "A useful article", "authors": ["", "Anna Author"], "year": "2025"}
    assert readable_filename(article, "full").endswith(".m4a")
    # A malformed long surname previously caused an infinite truncation loop.
    # Bound this subprocess so a regression cannot hang the whole test suite.
    code = ("from zotero_audio.app_worker import readable_filename; "
            "name=readable_filename({'title':'A useful article','authors':['ø'*300]},'full'); "
            "assert len(name.encode()) <= 235; assert name.endswith('.m4a')")
    subprocess.run([sys.executable, "-c", code], check=True, timeout=3)


def test_new_edition_keeps_publication_status_completed_during_snapshot(article_store, monkeypatch):
    store, article = article_store
    worker = LocalWorker(store)
    original_snapshot = worker.snapshot

    def snapshot_with_delivery_completion(source, **kwargs):
        if source.name == "full.m4a":
            latest = store.article(article["id"])
            latest["editions"]["brief"].update(publication_status="published", audio_url="https://podcast.example.org/brief.m4a")
            store.put_article(latest)
        return original_snapshot(source, **kwargs)

    monkeypatch.setattr(worker, "snapshot", snapshot_with_delivery_completion)

    def generate(bundle, **kwargs):
        records = {}
        for edition in ("brief", "full"):
            source = bundle / f"{edition}.m4a"
            source.write_bytes(f"Final {edition}".encode())
            record = {"title": article["title"], "edition": edition, "audio": str(source), "audio_sha256": sha256_file(source),
                      "public_eligible": True, "selected": True, "status": "ready"}
            records[edition] = record
            kwargs["on_edition_ready"](record)
        return {"source_sha256": "a" * 64, "warnings": [], "ai_review": str(bundle / "ai-review.md"),
                "editions": records, "status": "ready"}

    monkeypatch.setattr(generation, "process_article", generate)
    job = claim(store, "both")
    worker.process(job)
    brief = store.article(article["id"])["editions"]["brief"]
    assert brief["publication_status"] == "published"
    assert brief["audio_url"] == "https://podcast.example.org/brief.m4a"


def test_export_collision_does_not_overwrite_either_existing_user_file(tmp_path):
    source = tmp_path / "source.m4a"
    source.write_bytes(b"newly produced edition")
    destination = tmp_path / "iCloud" / "A readable article title.m4a"
    destination.parent.mkdir()
    destination.write_bytes(b"existing personal recording")
    conflict = destination.with_name(destination.stem + " — " + sha256_file(source)[:8] + destination.suffix)
    conflict.write_bytes(b"another existing personal recording")
    LocalWorker.copy_artifact({"source": str(source), "target": str(destination), "sha256": sha256_file(source)})
    assert destination.read_bytes() == b"existing personal recording"
    assert conflict.read_bytes() == b"another existing personal recording"
    assert any(file.read_bytes() == source.read_bytes() for file in destination.parent.iterdir())
