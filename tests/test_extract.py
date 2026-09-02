from pathlib import Path

import pytest

from zotero_audio.extract import normalize_speech_text, render_markdown, resolve_pdf


def test_normalize_removes_soft_hyphen_urls_and_line_wraps():
    value = "A long-\nterm result\u00ad at https://example.com/test ."
    assert normalize_speech_text(value) == "A longterm result at"


def test_resolve_zotero_pdf_and_prevent_key_escape(tmp_path: Path):
    storage = tmp_path / "storage"
    item = storage / "ABC123"
    item.mkdir(parents=True)
    pdf = item / "paper.pdf"
    pdf.write_bytes(b"%PDF-test")
    assert resolve_pdf(None, "ABC123", storage) == pdf.resolve()
    with pytest.raises(ValueError):
        resolve_pdf(None, "../outside", storage)


def test_markdown_has_page_markers_and_only_included_blocks():
    structure = {
        "document": {"title": "A paper", "author": "A. Author", "publication_year": "2026"},
        "source": {
            "filename": "paper.pdf",
            "sha256": "a" * 64,
            "zotero_key": "ABC",
            "pdf_pages": 2,
        },
        "extraction": {"engine": "pypdf", "engine_version": "test"},
        "blocks": [
            {"pdf_page": 1, "type": "paragraph", "text": "Included.", "included_in_reading": True},
            {"pdf_page": 2, "type": "paragraph", "text": "Omitted.", "included_in_reading": False},
        ],
    }
    markdown = render_markdown(structure)
    assert markdown.count("<!-- pdf-page:") == 2
    assert "Included." in markdown
    assert "Omitted." not in markdown
