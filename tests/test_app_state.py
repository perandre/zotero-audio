from concurrent.futures import ThreadPoolExecutor

import pytest

from zotero_audio.app_state import Store


@pytest.fixture
def store(tmp_path):
    store = Store(tmp_path)
    store.put_article({"id": "ARTICLE1", "title": "How a company adopted artificial intelligence", "authors": ["Anna Author"]},
                      markdown="The company increased productivity through careful training.")
    return store


def test_only_one_worker_claims_a_persisted_job(store):
    queued = store.create_job({"action": "markdown", "scope": "one", "article_id": "ARTICLE1"})
    other = Store(store.runtime)
    with ThreadPoolExecutor(max_workers=2) as pool:
        claims = list(pool.map(lambda database: database.claim_job(), [store, other]))
    claimed = [job for job in claims if job]
    assert len(claimed) == 1
    assert claimed[0]["id"] == queued["id"]
    assert Store(store.runtime).job(queued["id"])["status"] == "running"


def test_legacy_search_index_migrates_without_changing_artifacts_or_metadata(store):
    before = store.article("ARTICLE1")
    markdown = "---\nschema: old-schema\ntitle: A paper\n---\n\nUseful research evidence about adoption."
    with store.db() as db:
        db.execute("UPDATE article_search SET body=? WHERE id='ARTICLE1'", (markdown,))
        db.execute("DELETE FROM state WHERE key='search_index_version'")
    migrated = Store(store.runtime)
    assert migrated.article("ARTICLE1") == before
    result = migrated.search("Anna")["results"][0]
    assert "Useful research evidence" in result["snippet"]
    assert "old-schema" not in result["snippet"]


def test_restart_recovers_work_and_preserves_cancellation_and_delivery(store):
    interrupted = store.create_job({"action": "full", "scope": "one", "article_id": "ARTICLE1"})
    store.claim_job()
    cancelled = store.create_job({"action": "brief", "scope": "one", "article_id": "ARTICLE1"})
    store.claim_job()
    store.cancel(cancelled["id"])
    # A late progress message must not erase the cancellation request.
    store.update_job(cancelled["id"], status="running", stage="synthesizing", progress=40)
    delivery = store.enqueue_delivery("icloud", "ARTICLE1", {"source": "/private/generated.m4a"})
    with store.db() as db:
        db.execute("UPDATE deliveries SET status='running' WHERE id=?", (delivery,))
    reopened = Store(store.runtime)
    reopened.recover()
    assert reopened.job(interrupted["id"])["status"] == "queued"
    assert reopened.job(cancelled["id"])["status"] == "cancelled"
    with reopened.db() as db:
        assert db.execute("SELECT status FROM deliveries WHERE id=?", (delivery,)).fetchone()[0] == "pending"
    assert reopened.claim_job()["id"] == interrupted["id"]


def test_restart_recovers_unfinished_jobs_older_than_500_newer_jobs(store):
    interrupted = store.create_job({"action": "full", "scope": "one", "article_id": "ARTICLE1"})
    store.claim_job()
    # Simulate an established history without invoking unrelated processing.
    for index in range(501):
        store.create_job({"action": "markdown", "scope": "one", "article_id": "ARTICLE1"}, job_id=f"later-{index}")
    store.recover()
    assert store.job(interrupted["id"])["status"] == "queued"


def test_idempotency_prevents_duplicate_jobs_across_connections(store):
    request = {"action": "markdown", "scope": "one", "article_id": "ARTICLE1", "idempotency_key": "intended-operation"}
    other = Store(store.runtime)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda database: database.create_job(request), [store, other]))
    assert results[0]["id"] == results[1]["id"]
    assert len(store.jobs()["jobs"]) == 1
    assert results[0]["title"] == "How a company adopted artificial intelligence"


def test_idempotency_key_cannot_silently_represent_a_different_action(store):
    store.create_job({"action": "markdown", "scope": "one", "article_id": "ARTICLE1", "idempotency_key": "same-key"})
    with pytest.raises(ValueError, match="(?i)idempotency|different|conflict"):
        store.create_job({"action": "full", "scope": "one", "article_id": "ARTICLE1", "idempotency_key": "same-key"})


def test_search_survives_status_updates_and_includes_changed_authors(store):
    before = store.article("ARTICLE1")
    store.put_article({**before, "audio_status": "ready"})
    assert store.search("productivity")["results"][0]["id"] == "ARTICLE1"
    store.put_article({**before, "title": "AI adoption lessons", "authors": ["Beatrice Researcher"]})
    assert store.search("Beatrice")["results"][0]["title"] == "AI adoption lessons"
    assert store.search("productivity")["results"][0]["id"] == "ARTICLE1"


def test_invalid_flags_do_not_queue_accidental_work(store):
    for request in ({"action": "delete", "scope": "all"}, {"action": "markdown", "scope": "all", "force": "false"},
                    {"action": "full", "scope": "one", "article_id": "ARTICLE1", "qa": 0}):
        with pytest.raises(ValueError):
            store.create_job(request)
    assert not store.jobs()["jobs"]


def test_atomic_article_edits_preserve_both_connections_updates(store):
    other = Store(store.runtime)

    def update_pair(pair):
        database, edition = pair
        for _ in range(25):
            with database.edit_article("ARTICLE1") as article:
                state = article.setdefault("editions", {}).setdefault(edition, {"updates": 0})
                state["updates"] += 1
                state["publication_status"] = "published"
                state["audio_url"] = f"https://podcast.example.org/{edition}.m4a"

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(update_pair, [(store, "brief"), (other, "full")]))
    article = store.article("ARTICLE1")
    assert article["editions"]["brief"]["updates"] == 25
    assert article["editions"]["full"]["updates"] == 25
    assert store.search("productivity")["results"][0]["id"] == "ARTICLE1"


def test_atomic_article_edit_rolls_back_data_and_search_on_failure(store):
    original = store.article("ARTICLE1")
    with pytest.raises(RuntimeError):
        with store.edit_article("ARTICLE1", markdown="Replacement evidence that must roll back") as article:
            article["title"] = "Uncommitted title"
            raise RuntimeError("Interrupted before commit")
    assert store.article("ARTICLE1") == original
    assert store.search("productivity")["results"][0]["id"] == "ARTICLE1"
    assert not store.search("Replacement")["results"]
