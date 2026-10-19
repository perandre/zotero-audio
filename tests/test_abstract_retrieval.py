import io
import json
import socket
from urllib.error import HTTPError, URLError

import pytest

from zotero_audio import abstract_retrieval as retrieval
from zotero_audio import app_research as research
from zotero_audio.app_state import Store


DATA = {"title": "Synthetic organizational study", "DOI": "10.1234/example"}


def raw(value):
    return json.dumps(value).encode()


def test_crossref_miss_falls_back_to_reconstructed_openalex_abstract():
    urls = []
    def fetch(url):
        urls.append(url)
        if "crossref" in url:
            return raw({"message": {"DOI": DATA["DOI"], "title": [DATA["title"]]}})
        return raw({"doi": "https://doi.org/" + DATA["DOI"], "title": DATA["title"], "abstract_inverted_index": {"Source": [0], "abstract.": [1]}})
    result = retrieval.retrieve(DATA, fetch=fetch)
    assert result["abstract"] == "Source abstract."
    assert result["source"] == "openalex"
    assert [a["status"] for a in result["attempts"]] == ["not_found", "found"]
    assert len(urls) == 2


def test_no_doi_search_rejects_other_title_and_uses_semantic_scholar():
    def fetch(url):
        if "openalex" in url:
            return raw({"results": [{"title": "Unrelated ceramics", "abstract_inverted_index": {"Wrong.": [0]}}]})
        return raw({"data": [{"title": DATA["title"], "abstract": "The correct abstract."}]})
    result = retrieval.retrieve({"title": DATA["title"]}, fetch=fetch)
    assert result["abstract"] == "The correct abstract."
    assert result["source"] == "semantic-scholar"


def test_mismatched_dois_and_provider_errors_do_not_create_false_abstracts():
    def fetch(url):
        if "crossref" in url:
            return raw({"message": {"DOI": "10.1234/wrong", "title": [DATA["title"]], "abstract": "Wrong paper."}})
        if "openalex" in url:
            raise URLError("Temporary outage")
        return b""
    result = retrieval.retrieve(DATA, fetch=fetch)
    assert not result["abstract"]
    assert result["status"] == "retry_pending"
    assert result["attempts"][1]["error"] == "URLError"


def test_datacite_and_arxiv_sources_are_matched():
    def fetch(url):
        if "datacite" in url:
            return raw({"data": {"id": DATA["DOI"], "attributes": {"titles": [{"title": DATA["title"]}], "descriptions": [{"descriptionType": "Abstract", "description": "DataCite abstract."}]}}})
        return b""
    assert retrieval.retrieve(DATA, fetch=fetch)["source"] == "datacite"
    atom = b'<feed xmlns="http://www.w3.org/2005/Atom"><entry><id>https://arxiv.org/abs/2601.12345v2</id><title>Synthetic organizational study</title><summary>arXiv abstract.</summary></entry></feed>'
    result = retrieval.retrieve({"title": DATA["title"], "url": "https://arxiv.org/abs/2601.12345"}, fetch=lambda _: atom)
    assert result["abstract"] == "arXiv abstract."
    assert result["source"] == "arxiv"


@pytest.mark.parametrize("body", [
    '<meta name="citation_abstract" content="An actual publisher abstract.">',
    '<section id="abstract"><h2>Abstract</h2><p>An actual publisher abstract.</p></section><p>Unrelated text.</p>',
    '<script type="application/ld+json">{"@type":"ScholarlyArticle","headline":"Synthetic organizational study","abstract":"An actual publisher abstract."}</script>',
])
def test_publisher_requires_identity_and_only_extracts_abstract(body):
    header = '<meta name="citation_title" content="Synthetic organizational study"><meta name="citation_doi" content="10.1234/example">'
    assert retrieval.publisher_abstract((header + body).encode(), DATA) == "An actual publisher abstract."
    assert retrieval.publisher_abstract((header.replace("10.1234/example", "10.1234/wrong") + body).encode(), DATA) == ""
    assert retrieval.publisher_abstract(body.encode(), DATA) == ""


def test_publisher_rejects_truncated_description():
    page = b'<meta name="citation_title" content="Synthetic organizational study"><meta name="citation_abstract" content="Truncated...">'
    assert retrieval.publisher_abstract(page, DATA) == ""


@pytest.mark.parametrize("index", [{"gap": [1]}, {"a": [0], "b": [0]}, {"a": [-1]}, {"a": [True]}, {"a": [30000]}])
def test_invalid_inverted_indices_are_rejected(index):
    assert retrieval.inverted_abstract(index) == ""


def test_http_retries_server_failure_and_bounds_response(monkeypatch):
    monkeypatch.setattr(retrieval, "public_url", lambda url: url)
    retrieval._NEXT.clear()
    calls = []
    def opener(request, timeout):
        calls.append(request.full_url)
        if len(calls) == 1:
            raise HTTPError(request.full_url, 503, "unavailable", {}, None)
        return io.BytesIO(b"{}")
    assert retrieval.get("https://example.org/source", opener=opener, sleep=lambda _: None) == b"{}"
    assert len(calls) == 2
    retrieval._NEXT.clear()
    with pytest.raises(ValueError, match="size limit"):
        retrieval.get("https://example.org/source", opener=lambda *a, **k: io.BytesIO(b"x" * (retrieval.MAX_BYTES + 1)), sleep=lambda _: None)


def test_rate_limit_honors_cooldown_without_hammering(monkeypatch):
    monkeypatch.setattr(retrieval, "public_url", lambda url: url)
    retrieval._NEXT.clear()
    calls = []
    def opener(request, timeout):
        calls.append(1)
        raise HTTPError(request.full_url, 429, "rate limited", {"Retry-After": "120"}, None)
    with pytest.raises(HTTPError):
        retrieval.get("https://example.org/rate", opener=opener, sleep=lambda _: None)
    with pytest.raises(URLError, match="cooldown"):
        retrieval.get("https://example.org/rate", opener=opener, sleep=lambda _: None)
    assert len(calls) == 1
    retrieval._NEXT.clear()


def test_public_url_rejects_local_sources_and_redirects(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [(2, 1, 6, "", ("127.0.0.1", 80))])
    with pytest.raises(ValueError):
        retrieval.public_url("http://localhost/private")
    with pytest.raises(ValueError):
        retrieval.PublicRedirect().redirect_request(None, None, 302, "", {}, "http://localhost/private")


def test_enrichment_handles_markdown_only_no_doi_and_preserves_full_abstract(tmp_path, monkeypatch):
    store = Store(tmp_path)
    full = tmp_path / "papers" / "full.md"
    full.parent.mkdir()
    full.write_text("# Full research\nAn existing introduction.")
    store.put_article({"id": "ATTACH", "title": DATA["title"], "markdown": str(full), "metadata": {"parent_key": "REF"}})
    record = {"key": "REF", "library_id": "users/0", "data": {"title": DATA["title"]}}
    research.prepare(store, [record])
    assert research.rows(store)[0]["coverage"] == "selected_markdown"
    abstract = "Long source abstract. " * 500
    calls = []
    def retrieve(data, **kwargs):
        calls.append(1)
        return {"abstract": abstract, "source": "semantic-scholar", "resource": "https://api.semanticscholar.org/example", "status": "found", "attempts": []}
    monkeypatch.setattr(retrieval, "retrieve", retrieve)
    result = research.enrich(store, [record], progress=lambda _: None)
    assert result["found"] == 1
    saved = research.retrieved_abstract(store, record)
    assert saved["abstract"] == abstract
    record["data"].pop("abstractNote")
    record["data"].pop("_research_coverage")
    research.prepare(store, [record])
    assert research.rows(store)[0]["coverage"] == "retrieved_abstract"
    with store.db() as db:
        content = json.loads(db.execute("SELECT input FROM research_items").fetchone()[0])
    assert content["truncated"]
    assert len(content["abstract"]) == 8000
    research.enrich(store, [record], progress=lambda _: None)
    assert calls == [1]


def test_old_crossref_miss_does_not_prevent_other_sources_and_failures_retry(tmp_path, monkeypatch):
    store = Store(tmp_path)
    record = {"key": "REF", "library_id": "users/0", "data": dict(DATA)}
    research.prepare(store, [record])
    import time
    fingerprint = research.digest({"doi": DATA["DOI"], "title": DATA["title"], "policy": 1})
    with store.db() as db:
        db.execute("INSERT INTO research_abstract_cache VALUES (?,?,?)", (fingerprint, "", time.time()))
    calls = []
    def retrieve(data, **kwargs):
        calls.append(kwargs)
        return {"abstract": "", "status": "retry_pending", "attempts": [{"source": "openalex", "status": "unavailable"}]}
    monkeypatch.setattr(retrieval, "retrieve", retrieve)
    assert research.enrich(store, [record], progress=lambda _: None)["retry_pending"] == 1
    assert research.retrieved_abstract(store, record)["status"] == "retry_pending"
    research.enrich(store, [record], progress=lambda _: None)
    assert calls == [{"skip_crossref": True}, {"skip_crossref": True}]


def test_new_abstract_files_contain_full_source_text_and_preserve_edits(tmp_path):
    from pathlib import Path
    from zotero_audio.library_export import export_library
    store = Store(tmp_path)
    record = {"key": "REF", "library_id": "users/0", "data": dict(DATA)}
    research.prepare(store, [record])
    abstract = "An actual source abstract. " * 400
    result = {"abstract": abstract.strip(), "source": "openalex", "resource": "https://api.openalex.org/works/example", "status": "found", "attempts": []}
    with store.db() as db:
        db.execute("INSERT INTO research_abstract_retrievals VALUES (?,?,0)", (research.abstract_fingerprint(record), json.dumps(result)))
    exported = export_library(store, records=[record])
    assert exported["abstract_files"] == 1
    directory = Path(exported["abstract_directory"])
    abstract_path = next(directory.glob("*.md"))
    content = abstract_path.read_text()
    assert abstract.strip() in content
    assert 'type: "Research Abstract"' in content
    assert 'https://api.openalex.org/works/example' in content
    assert "No sourced abstract" not in content
    abstract_path.write_text("Manual edit")
    for _ in range(2):
        assert export_library(store, records=[record])["preserved_abstract_edits"] == [DATA["title"]]
        assert abstract_path.read_text() == "Manual edit"


def test_group_local_abstract_does_not_leak_into_personal_record(tmp_path):
    store = Store(tmp_path)
    store.put_article({"id": "g123:ATTACH", "title": DATA["title"], "metadata": {"parent_key": "REF", "abstract": "Group abstract."}})
    records = [{"key": "REF", "library_id": lib, "data": {"title": DATA["title"]}} for lib in ["users/0", "groups/123"]]
    research.prepare(store, records)
    coverages = {r["library_id"]: r["coverage"] for r in research.rows(store)}
    assert coverages == {"users/0": "title_only", "groups/123": "cached_abstract"}
