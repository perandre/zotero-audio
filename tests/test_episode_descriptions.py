import json
from urllib.error import URLError

from zotero_audio import episode_descriptions as descriptions
from zotero_audio.podcast import _show_notes


def test_zotero_abstract_wins_and_is_plain_text_without_touching_document():
    document = {"abstract": "PDF wording", "doi": "10.1234/example"}
    selected = descriptions.select_description_abstract(
        document,
        metadata={"abstract": "<jats:p>Authors &amp; methods.</jats:p>"},
        structure={"document": {"abstract": "PDF wording", "abstract_source": "pdf-inline"}},
    )
    assert selected == {
        "description_abstract": "Authors & methods.",
        "description_abstract_source": "zotero",
    }
    assert document["abstract"] == "PDF wording"


def test_cached_abstract_prevents_doi_fetch_and_preserves_user_edit(monkeypatch):
    def fail(*args, **kwargs):
        raise AssertionError("cached description should avoid a network request")

    monkeypatch.setattr(descriptions.urllib.request, "urlopen", fail)
    selected = descriptions.select_description_abstract(
        {"doi": "10.1234/example"}, cached={
            "description_abstract": "An edited description.",
            "description_abstract_source": "zotero",
            "source_sha256": "abc",
        }, source_sha256="abc",
    )
    assert selected == {
        "description_abstract": "An edited description.",
        "description_abstract_source": "zotero",
    }


def test_cached_abstract_from_another_pdf_is_not_reused(monkeypatch):
    monkeypatch.setattr(descriptions, "doi_abstract", lambda doi: "New publisher abstract.")
    selected = descriptions.select_description_abstract(
        {"doi": "10.1234/example"}, cached={
            "description_abstract": "Old paper's text.",
            "generated_description_abstract": "Old paper's text.",
            "description_abstract_source": "doi-metadata",
            "source_sha256": "old",
        }, source_sha256="new",
    )
    assert selected == {"description_abstract": "New publisher abstract.", "description_abstract_source": "doi-metadata"}


def test_crossref_metadata_is_bounded_plain_text_and_used_before_pdf(monkeypatch):
    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self, size):
            assert size == descriptions.MAX_CROSSREF_BYTES + 1
            return json.dumps({"message": {"abstract": "<jats:p>Deposited &amp; useful.</jats:p>"}}).encode()

    seen = {}

    def fake_open(request, timeout):
        seen["url"] = request.full_url
        seen["timeout"] = timeout
        return Response()

    monkeypatch.setattr(descriptions.urllib.request, "urlopen", fake_open)
    selected = descriptions.select_description_abstract(
        {"doi": "10.1234/example"},
        structure={"document": {"abstract": "Extracted PDF.", "abstract_source": "pdf-inline"}},
    )
    assert selected == {
        "description_abstract": "Deposited & useful.",
        "description_abstract_source": "doi-metadata",
    }
    assert seen == {
        "url": "https://api.crossref.org/works/10.1234/example",
        "timeout": descriptions.CROSSREF_TIMEOUT_SECONDS,
    }


def test_datacite_and_arxiv_metadata_fallbacks_are_identity_checked(monkeypatch):
    class Response:
        def __init__(self, body, status=200):
            self.body = body
            self.status = status

        def __enter__(self): return self
        def __exit__(self, *args): return False
        def read(self, size): return self.body

    urls = []
    def datacite(request, timeout):
        urls.append(request.full_url)
        if "crossref.org" in request.full_url:
            return Response(b'{"message":{"DOI":"10.1234/item"}}')
        return Response(json.dumps({"data": {"id": "10.1234/item", "attributes": {
            "descriptions": [{"descriptionType": "Other", "description": "Ignore."},
                             {"descriptionType": "Abstract", "description": "Deposited abstract."}]}}}).encode())
    monkeypatch.setattr(descriptions.urllib.request, "urlopen", datacite)
    assert descriptions.doi_abstract("10.1234/item") == "Deposited abstract."
    assert urls[1] == "https://api.datacite.org/dois/10.1234/item"

    urls.clear()
    atom = (b'<feed xmlns="http://www.w3.org/2005/Atom"><entry>'
            b'<id>https://arxiv.org/abs/2401.12345v1</id><summary>arXiv abstract.</summary>'
            b'</entry></feed>')
    def arxiv(request, timeout):
        urls.append(request.full_url)
        if "export.arxiv.org" in request.full_url:
            return Response(atom)
        return Response(b"{}", 404)
    monkeypatch.setattr(descriptions.urllib.request, "urlopen", arxiv)
    assert descriptions.doi_abstract("10.48550/arXiv.2401.12345v1") == "arXiv abstract."
    assert "export.arxiv.org/api/query" in urls[-1]


def test_external_metadata_failures_are_bounded_and_nonfatal(monkeypatch):
    calls = []
    def timeout(request, timeout):
        calls.append((request.full_url, timeout))
        raise URLError("offline")
    monkeypatch.setattr(descriptions.urllib.request, "urlopen", timeout)
    assert descriptions.doi_abstract("10.1234/example") == ""
    assert calls and all(timeout == descriptions.CROSSREF_TIMEOUT_SECONDS for _, timeout in calls)

    class Oversized:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def read(self, size): return b"x" * size
    monkeypatch.setattr(descriptions.urllib.request, "urlopen", lambda *a, **k: Oversized())
    assert descriptions.doi_abstract("10.1234/example") == ""

    class Malformed:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def read(self, size): return b"not json"
    monkeypatch.setattr(descriptions.urllib.request, "urlopen", lambda *a, **k: Malformed())
    assert descriptions.doi_abstract("10.1234/example") == ""


def test_abstract_with_cosmetic_prefix_and_overbound_text_is_not_truncated():
    assert descriptions.normalize_abstract("Abstract: A complete, short abstract.") == "A complete, short abstract."
    assert descriptions.normalize_abstract("z" * (descriptions.MAX_ABSTRACT_CHARS + 1)) == ""


def test_pdf_abstract_fallback_is_source_marked_and_show_notes_are_factual():
    selected = descriptions.select_description_abstract(
        {"abstract": "Extracted abstract", "abstract_source": "pdf-inline"},
    )
    assert selected["description_abstract"] == "Extracted abstract"
    assert selected["description_abstract_source"] == "pdf-extraction"

    notes = _show_notes(
        {"title": "Paper", "journal": "Journal", "publication_year": 2026},
        ["Ada Author"], "brief", {}, description_abstract=selected["description_abstract"],
        brief_contents="the authors' abstract",
    )
    assert notes.startswith("Extracted abstract\n\nThis Brief reads the authors' abstract in the authors' own words.")
    assert "Narration: Read verbatim by an AI-generated voice." in notes
    assert "Authors: Ada Author" in notes


def test_pdf_blocks_are_used_only_as_complete_bounded_abstracts():
    selected = descriptions.select_description_abstract(
        {"doi": ""}, structure={"blocks": [
            {"type": "heading", "text": "Abstract"},
            {"type": "paragraph", "text": "The authors report a result."},
            {"type": "heading", "text": "Keywords"},
            {"type": "paragraph", "text": "Should not be included."},
        ]},
    )
    assert selected["description_abstract"] == "The authors report a result."
    assert selected["description_abstract_source"] == "pdf-extraction"
    assert descriptions.normalize_abstract("x" * (descriptions.MAX_ABSTRACT_CHARS + 1)) == ""


def test_rss_html_marks_shared_abstract_and_keeps_custom_notes_unclassified():
    from zotero_audio.podcast import _show_notes_html

    generated = "Source abstract.\n\nThis is a Full Reading audio edition."
    html = _show_notes_html(generated, description_abstract="Source abstract.",
                            description_source="zotero", generated_show_notes=generated)
    assert '<p class="episode-abstract" data-source="zotero">Source abstract.</p>' in html
    custom = _show_notes_html("My own edited notes.", description_abstract="Source abstract.",
                              description_source="zotero", generated_show_notes="Old generated notes.")
    assert "episode-abstract" not in custom
    assert "My own edited notes." in custom
