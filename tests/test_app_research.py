import io
import json
from urllib.error import HTTPError

import pytest

from zotero_audio import app_research as research
from zotero_audio.app_state import Store


def response(label="good"):
    answers = {
        "fit": {"type": "choice", "choice": label, "confidence": 0.8,
                "probabilities": {s: float(s == label) for s in research.LABELS}},
        "contribution": {"type": "choice", "choice": "map", "confidence": 1,
                         "probabilities": {s: float(s == "map") for s in research.QUESTIONS["contribution"]["criteria"]}},
        **{s: {"type": "noul", "noul": 0.5} for s in ("rq1", "rq2", "rq3")},
    }
    return {"model": research.MODEL, "answers": answers, "usage": {"input_tokens": 100}, "request_id": "synthetic-request"}


def item(key="A", abstract="Synthetic abstract"):
    return {"key": key, "data": {"title": "Synthetic organizational study", "itemType": "journalArticle", "abstractNote": abstract}}


class API:
    def __init__(self, items):
        self.items = items

    def _get(self, path):
        start = int(path.split("start=")[1].split("&")[0])
        return self.items[start:start + 100]


def test_paginated_parent_discovery():
    records = [item(str(i)) for i in range(205)]
    records += [{"key": "note", "data": {"itemType": "note", "title": "A note"}}]
    found = research.discover(API(records))
    assert len(found) == 205
    assert found[-1]["key"] == "204"


def test_input_coverage_and_private_url_removal():
    value = research.article_input({"title": "A study", "abstractNote": "<p>Agentic work &amp; governance https://example.invalid/?private=secret</p>"})
    assert value["coverage"] == "title_abstract"
    assert "private" not in value["abstract"]
    assert "&" in value["abstract"]
    assert research.article_input({"title": "A study"})["coverage"] == "title_only"
    excerpt = research.article_input({"title": "A study"}, "# Introduction\nWork\n# Conclusion\nUseful\n# References\nExcluded")
    assert excerpt["coverage"] == "selected_markdown"
    assert "Excluded" not in excerpt["excerpt"]


def test_resume_duplicate_cache_and_changed_input(tmp_path, monkeypatch):
    store = Store(tmp_path)
    monkeypatch.setenv("TYPESAFE_API_KEY", "synthetic-test-key")
    calls = []

    def classify(value, key):
        calls.append(value)
        return research.validate_response(response())

    api = API([item("A"), item("B")])
    result = research.run(store, api=api, classifier=classify, progress=lambda _: None)
    assert result["statuses"]["completed"] == 2
    assert result["input_tokens"] == 100
    assert len(calls) == 1
    research.run(store, api=api, classifier=classify, progress=lambda _: None)
    assert len(calls) == 1
    api.items[0]["data"]["abstractNote"] = "Changed synthetic abstract"
    research.run(store, api=api, classifier=classify, progress=lambda _: None)
    assert len(calls) == 2
    with store.db() as db:
        assert db.execute("SELECT COUNT(*) FROM research_results").fetchone()[0] == 2
        assert db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0


def test_failure_is_preserved_until_explicit_retry(tmp_path, monkeypatch):
    store = Store(tmp_path)
    monkeypatch.setenv("TYPESAFE_API_KEY", "synthetic-test-key")
    calls = []

    def fail(value, key):
        calls.append(1)
        raise RuntimeError("Do not print secret input")

    result = research.run(store, api=API([item()]), classifier=fail, progress=lambda s: calls.append(s))
    assert result["statuses"]["failed"] == 1
    assert not any("secret input" in str(s) for s in calls)
    research.run(store, api=API([item()]), classifier=fail, progress=lambda _: None)
    assert calls.count(1) == 1
    research.run(store, api=API([item()]), classifier=lambda v, k: research.validate_response(response()), retry_failed=True, progress=lambda _: None)
    assert research.summary(store)["statuses"]["completed"] == 1


def test_interrupted_work_and_manual_override(tmp_path, monkeypatch):
    store = Store(tmp_path)
    monkeypatch.setenv("TYPESAFE_API_KEY", "synthetic-test-key")
    research.prepare(store, research.discover(API([item()])))
    with store.db() as db:
        db.execute("UPDATE research_items SET status='running'")
        db.execute("INSERT INTO research_overrides VALUES (?,?,?,?,?)", ("users/0", "A", research.profile_hash(), "excellent", "synthetic-date"))
    research.run(store, api=API([item()]), classifier=lambda v, k: research.validate_response(response()), progress=lambda _: None)
    row = research.rows(store)[0]
    assert row["label"] == "excellent"
    assert row["model_label"] == "good"
    assert research.summary(store)["statuses"]["completed"] == 1


def test_validation_rejects_partial_invalid_or_different_model():
    valid = response()
    assert research.validate_response(valid)["request_id"] == "synthetic-request"
    for change in (lambda r: r.update(model="other"),
                   lambda r: r["answers"].pop("rq1"),
                   lambda r: r["answers"]["fit"].update(confidence=float("nan")),
                   lambda r: r["answers"]["fit"].update(probabilities={"poor": 1})):
        value = response()
        change(value)
        with pytest.raises(ValueError, match="invalid classification"):
            research.validate_response(value)


def test_retry_and_no_upstream_error_body_in_diagnostics():
    requests = []
    sleeps = []

    def opener(request, timeout):
        requests.append(request)
        if len(requests) == 1:
            raise HTTPError(request.full_url, 429, "limited", {"Retry-After": "2"}, io.BytesIO(b"private echoed input"))
        return io.BytesIO(json.dumps(response()).encode())

    research.classify({"title": "Synthetic title"}, "synthetic-test-key", opener=opener, sleep=sleeps.append)
    assert sleeps == [2]
    assert len(requests) == 2

    def unauthorized(request, timeout):
        raise HTTPError(request.full_url, 401, "secret echo", {}, io.BytesIO(b"private echoed input"))

    with pytest.raises(RuntimeError, match="^Typesafe HTTP 401$"):
        research.classify({}, "synthetic-test-key", opener=unauthorized)


def test_private_key_file_and_csv_formula_safety(tmp_path, monkeypatch):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    store = Store(tmp_path)
    key_path = store.root / "typesafe-api-key.txt"
    key_path.write_text("synthetic-test-key")
    key_path.chmod(0o644)
    with pytest.raises(ValueError, match="private"):
        research.api_key(store)
    key_path.chmod(0o600)
    assert research.api_key(store) == "synthetic-test-key"
    record = item()
    record["data"]["title"] = "=synthetic formula"
    research.prepare(store, research.discover(API([record])))
    path = tmp_path / "results.csv"
    research.export_csv(store, path)
    assert "'=synthetic formula" in path.read_text()


def test_api_auth_failure_stops_new_requests(tmp_path, monkeypatch):
    store = Store(tmp_path)
    monkeypatch.setenv("TYPESAFE_API_KEY", "synthetic-test-key")
    calls = []

    def fail(value, key):
        calls.append(1)
        raise RuntimeError("Typesafe HTTP 401")

    records = [item(str(i), f"Synthetic abstract {i}") for i in range(20)]
    with pytest.raises(RuntimeError, match="401"):
        research.run(store, api=API(records), workers=1, classifier=fail, progress=lambda _: None)
    assert len(calls) == 1
    assert research.summary(store)["statuses"]["pending"] == 19


def test_research_cli_works_offline_without_daemon(tmp_path, monkeypatch, capsys):
    from zotero_audio import app_cli
    store = Store(tmp_path)
    research.prepare(store, research.discover(API([item()])))
    monkeypatch.setattr(app_cli, "Store", lambda: store)
    monkeypatch.setattr(app_cli, "ensure_daemon", lambda _: pytest.fail("Must not start generation"))
    assert app_cli.main(["research-fit", "status", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["total"] == 1
    assert app_cli.main(["research-fit", "override", "--item-key", "A", "--label", "excellent", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["model_result_preserved"]
    assert app_cli.main(["research-fit", "list", "--label", "excellent", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["items"][0]["title"] == "Synthetic organizational study"


def test_crossref_matching_and_enrichment_cache(tmp_path):
    payload = {"message": {"DOI": "10.1234/synthetic", "title": ["Synthetic organizational study"], "abstract": "<p>A synthetic abstract about organizational AI.</p>"}}
    opener = lambda request, timeout: io.BytesIO(json.dumps(payload).encode())
    assert "organizational AI" in research.crossref_abstract("10.1234/synthetic", "Synthetic organizational study", opener=opener)
    assert research.crossref_abstract("10.1234/synthetic", "Unrelated ceramic materials", opener=opener) == ""
    assert research.crossref_abstract("10.1234/different", "Synthetic organizational study", opener=opener) == ""
    store = Store(tmp_path)
    record = item(abstract="")
    record["data"]["DOI"] = "10.1234/synthetic"
    records = research.discover(API([record]))
    research.prepare(store, records)
    calls = []

    def fetch(doi, title):
        calls.append(doi)
        return "Synthetic abstract"

    research.enrich(store, records, fetch=fetch, progress=lambda _: None)
    assert research.rows(store)[0]["coverage"] == "crossref_abstract"
    records[0]["data"].pop("abstractNote")
    records[0]["data"].pop("_research_coverage")
    research.prepare(store, records)
    assert research.rows(store)[0]["coverage"] == "crossref_abstract"
    research.enrich(store, records, fetch=fetch, progress=lambda _: None)
    assert calls == ["10.1234/synthetic"]
