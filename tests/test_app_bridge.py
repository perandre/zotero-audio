from __future__ import annotations

import io
import json
import threading
from types import SimpleNamespace
from urllib.error import HTTPError, URLError

import pytest

from zotero_audio.app_bridge import CloudBridge, NoRedirect
from zotero_audio.app_state import Store


@pytest.fixture
def bridge(tmp_path):
    store = Store(tmp_path)
    worker = SimpleNamespace(stop=threading.Event(), current_job_id=None)
    bridge = CloudBridge(store, worker)
    bridge.config = lambda: {"url": "http://127.0.0.1:8797", "bridge_token": "x" * 64}
    return bridge


def error(code):
    return HTTPError("http://127.0.0.1:8797/api/bridge/jobs/test", code, "Cloud error", {}, io.BytesIO())


def queued(bridge, *, job_id="job-one"):
    bridge.store.put_article({"id": "PAPER1", "title": "A complete research article title"})
    return bridge.store.create_job({"action": "full", "scope": "one", "article_id": "PAPER1"}, job_id=job_id,
                                   remote={"lease_token": "test-lease", "worker_id": bridge.worker_id})


def test_progress_and_cancellation_use_the_cloud_contract(bridge):
    job = queued(bridge)
    bridge.store.update_job(job["id"], status="running", progress=57)
    sent = []
    bridge.request = lambda method, path, body=None: sent.append(body) or {"job": {"status": "cancel_requested"}}
    bridge.sync_jobs()
    assert sent[0]["progress"] == .57
    assert bridge.store.job(job["id"])["status"] == "cancel_requested"
    bridge.sync_jobs()
    assert sent[1]["status"] == "running"
    assert sent[1]["lease_seconds"] == 300


@pytest.mark.parametrize("exception", [error(503), URLError("network down"), TimeoutError()])
def test_transient_network_failure_keeps_work_running_and_unacknowledged(bridge, exception):
    job = queued(bridge)
    bridge.store.update_job(job["id"], status="running", progress=30)
    def fail(*args, **kwargs):
        raise exception
    bridge.request = fail
    bridge.sync_jobs()
    current = bridge.store.job(job["id"])
    assert current["status"] == "running"
    assert not current["remote"].get("acknowledged")


def test_only_explicit_lost_lease_cancels_active_processing(bridge):
    job = queued(bridge)
    bridge.store.update_job(job["id"], status="running")
    def lost(*args, **kwargs):
        raise error(409)
    bridge.request = lost
    bridge.sync_jobs()
    current = bridge.store.job(job["id"])
    assert current["status"] == "cancel_requested"
    assert current["remote"]["acknowledged"] is True
    assert current["remote"]["lease_lost"] is True


def test_lost_completion_response_does_not_erase_completed_result(bridge):
    job = queued(bridge)
    bridge.store.update_job(job["id"], status="completed", progress=100, message="Audio ready with two warnings")
    def closed(*args, **kwargs):
        raise error(409)
    bridge.request = closed
    bridge.sync_jobs()
    current = bridge.store.job(job["id"])
    assert current["status"] == "completed"
    assert current["message"] == "Audio ready with two warnings"
    assert current["remote"]["acknowledged"] is True


def test_reclaimed_job_updates_existing_local_lease_without_duplicate_work(bridge):
    job = queued(bridge)
    bridge.store.update_job(job["id"], status="completed", progress=100)
    bridge._adopt_claim({**job, "lease_token": "replacement-lease", "status": "running"})
    assert len(bridge.store.jobs()["jobs"]) == 1
    local = bridge.store.job(job["id"])
    assert local["status"] == "completed"
    assert local["remote"]["lease_token"] == "replacement-lease"
    assert local["remote"]["acknowledged"] is False


def test_missing_article_fails_remote_job_with_full_title_and_releases_pending_handoff(bridge):
    claim = {"id": "missing-job", "action": "full", "scope": "one", "article_id": "MISSING",
             "title": "The complete title of the missing paper", "lease_token": "lease"}
    bridge.store.set_state("cloud_pending_claim", claim)
    sent = []
    bridge.request = lambda method, path, body=None: sent.append(body) or {"job": {"status": "failed"}}
    bridge._adopt_claim(claim)
    assert sent[0]["status"] == "failed"
    assert claim["title"] in sent[0]["message"]
    assert "Sync Zotero" in sent[0]["message"]
    assert bridge.store.state("cloud_pending_claim") is None


def test_missing_article_handoff_survives_failed_terminal_ack(bridge):
    claim = {"id": "missing-job", "action": "full", "scope": "one", "article_id": "MISSING",
             "title": "The missing paper", "lease_token": "lease"}
    bridge.store.set_state("cloud_pending_claim", claim)
    def fail(*args, **kwargs):
        raise error(503)
    bridge.request = fail
    with pytest.raises(HTTPError):
        bridge._adopt_claim(claim)
    assert bridge.store.state("cloud_pending_claim")["id"] == claim["id"]


def test_settings_seed_only_unconfigured_cloud_and_preserve_cloud_timestamp(bridge):
    bridge.store.update_settings({"opening_sound": "none", "icloud_folder": "/tmp/Private listening"}, timestamp="2025-01-01T00:00:00.000Z")
    calls = []
    def request(method, path, data=None):
        calls.append((method, data))
        return {"configured": False, "updated_at": None} if method == "GET" else {**data, "configured": True, "updated_at": "2026-01-01T00:00:00.000Z"}
    bridge.request = request
    bridge.sync_settings()
    assert calls[1][1]["icloud_folder"] == "/tmp/Private listening"
    assert "updated_at" not in calls[1][1] and "configured" not in calls[1][1]
    assert bridge.store.settings()["updated_at"] == "2026-01-01T00:00:00.000Z"
    calls.clear()
    bridge.request = lambda method, path, data=None: calls.append(method) or {"settings": {"configured": True, "updated_at": "2027-01-01T00:00:00.000Z", "opening_sound": "typing"}}
    bridge.sync_settings()
    assert calls == ["GET"]
    assert bridge.store.settings()["opening_sound"] == "typing"


def test_private_markdown_and_review_sync_without_audio_paths_and_only_on_change(bridge, tmp_path):
    markdown, review = tmp_path / "article.md", tmp_path / "ai-review.md"
    markdown.write_text("# Private company research\n\nEvidence.")
    review.write_text("# Findings\n\nA minor citation warning.")
    bridge.store.put_article({"id": "PRIVATE1", "title": "Private company research", "authors": ["Full Author"], "year": "2025",
                             "source_url": None, "license_status": "private", "markdown": str(markdown), "review": str(review),
                             "editions": {"full": {"audio": "/private/article.m4a", "audio_url": "file:///private/article.m4a"}}})
    calls = []
    bridge.request = lambda method, path, body=None: calls.append((path, body)) or {"article": body["article"]}
    assert bridge.sync_articles() == 1
    value = calls[0][1]
    assert value["article"]["year"] == 2025
    assert value["article"]["source_url"] == ""
    assert value["markdown"] == markdown.read_text()
    assert value["review"] == review.read_text()
    assert "/private/article.m4a" not in json.dumps(value)
    assert bridge.sync_articles() == 0
    review.write_text("# Findings\n\nUpdated audio review.")
    assert bridge.sync_articles() == 1
    assert len(calls) == 2
    assert calls[-1][1]["review"] == review.read_text()


def test_bad_article_does_not_prevent_other_articles_from_syncing(bridge, tmp_path):
    for key in ["GOOD", "BAD"]:
        bridge.store.put_article({"id": key, "title": key + " complete paper title", "source_url": None})
    calls = []
    def request(method, path, data=None):
        calls.append(data["article"]["id"])
        if data["article"]["id"] == "BAD":
            raise error(400)
        return {"article": data["article"]}
    bridge.request = request
    assert bridge.sync_articles() == 1
    assert set(calls) == {"GOOD", "BAD"}
    assert bridge.store.state("cloud_article_errors")["BAD"]["title"] == "BAD complete paper title"
    bridge.sync_articles()
    assert calls.count("BAD") == 1  # unchanged permanent rejection backs off


def test_lease_renewal_has_its_own_thread_and_does_not_wait_for_uploads(bridge):
    started = threading.Event()
    bridge.sync_jobs = lambda: started.set() or bridge.worker.stop.set()
    bridge.start()
    assert started.wait(1)
    bridge.thread.join(1)
    bridge.lease_thread.join(1)
    assert bridge.lease_thread.name == "cloud-leases"
    assert bridge.RENEW_SECONDS == 15
    assert bridge.POLL_SECONDS == 60


def test_transport_retries_idempotent_errors_without_leaking_credentials(bridge):
    calls = []
    class Opener:
        def open(self, request, timeout):
            calls.append(request)
            if len(calls) == 1:
                raise error(503)
            return io.BytesIO(b'{"ok":true}')
    bridge._opener = Opener()
    bridge.worker.stop = SimpleNamespace(wait=lambda seconds: False)
    assert bridge.request("POST", "/api/bridge/heartbeat", {"worker_id": "test"}) == {"ok": True}
    assert len(calls) == 2
    assert calls[0].data == calls[1].data
    assert calls[0].get_header("User-agent").startswith("OneMorePaper/")
    assert NoRedirect().redirect_request(None, None, 302, "", {}, "https://other.example") is None


@pytest.mark.parametrize("url", ["http://example.com", "ftp://localhost", "https://owner:secret@example.com", "https://example.com/?secret=yes"])
def test_bridge_configuration_rejects_insecure_or_credential_bearing_origins(bridge, url):
    bridge.config = lambda: {"url": url, "bridge_token": "x" * 64}
    with pytest.raises(ValueError, match="HTTPS origin"):
        bridge.request("GET", "/api/bridge/settings")
