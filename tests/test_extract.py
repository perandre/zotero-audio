from pathlib import Path

import pytest

from zotero_audio.extract import _infer_abstract, _paragraphs, normalize_speech_text, render_markdown, resolve_pdf


def test_normalize_preserves_article_content_and_real_hyphens():
    value = "A long-\nterm result\u00ad at https://example.com/test www.example.org me@example.org."
    assert normalize_speech_text(value) == "A long-term result at https://example.com/test www.example.org me@example.org."
    assert normalize_speech_text("Prior work (1, 2) agrees [3], but the year (2024) remains.") == (
        "Prior work (1, 2) agrees [3], but the year (2024) remains."
    )
    assert normalize_speech_text("organi\u00ad\nsations and com\u00ad\nplexities") == "organisations and complexities"


def test_wrapped_sentences_stay_in_the_same_paragraph():
    paragraphs, _ = _paragraphs("First sentence.\nSecond sentence with organi\u00ad\nsations.\n\nNext paragraph.", set())
    assert paragraphs == ["First sentence. Second sentence with organisations.", "Next paragraph."]


def test_semantic_lines_split_numbered_heading_and_remove_contact_furniture():
    text = """1. Materials and Methods
The first sentence wraps
onto another line. A new paragraph starts here.
Author affiliations: Example University
"""
    paragraphs, omissions = _paragraphs(text, set())
    assert paragraphs == [
        "1. Materials and Methods",
        "The first sentence wraps onto another line. A new paragraph starts here.",
    ]
    assert omissions[-1]["reason"] == "publisher-or-contact-furniture"


def test_abstract_inference_requires_visible_boundaries():
    abstract, source = _infer_abstract(
        "Edited by An Editor\n"
        "This study reports a careful result across a large representative sample. "
        "It explains the method, evidence, limitations, and implications in enough detail to be useful. "
        "The results are robust across all of the planned sensitivity analyses and support the conclusion. "
        "We also describe why the evidence matters for future work and where caution is required.\n"
        "topic one | topic two | topic three\nIntroduction\nBody"
    )
    assert source == "pdf-editorial-front-matter"
    assert abstract and abstract.startswith("This study reports")


def test_spaced_abstract_heading_skips_adjacent_keywords_and_repairs_soft_wraps():
    body = (
        "This study reports a careful result across a large representative sample of organi\u00ad\nsations. "
        "It explains the method, evidence, limitations, and implications in enough detail to be useful. "
        "The results are robust across all of the planned sensitivity analyses and support the conclusion."
    )
    abstract, source = _infer_abstract("A B S T R A C T\n\nKeywords:\nAI\nTechnology\n\n" + body + "\n\n1. Introduction\nBody")
    assert abstract == body.replace("\u00ad\n", "")
    assert source == "pdf-explicit-heading"


def test_abstract_inference_stops_at_inline_keywords_and_introduction_boundary():
    body = (
        "This study reports a careful result across a large representative sample. "
        "It explains the method, evidence, limitations, and implications in enough detail to be useful. "
        "The results are robust across all of the planned sensitivity analyses and support the conclusion."
    )
    abstract, source = _infer_abstract(
        "Abstract\n" + body + "\nKeywords Artificial intelligence · Responsible AI 1 Introduction\nBody"
    )
    assert abstract == body
    assert source == "pdf-explicit-heading"


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
    assert "authors: []" in markdown


def test_inline_spaced_abstract_marker_is_treated_as_a_visible_boundary():
    body = (
        "This study reports a careful result across a large representative sample. "
        "It explains the method, evidence, limitations, and implications in enough detail to be useful. "
        "The results are robust across all of the planned sensitivity analyses and support the conclusion."
    )
    abstract, source = _infer_abstract(
        "Title and authors A R T I C L E I N F O A B S T R A C T " + body + " Keywords: AI"
    )
    assert abstract == body
    assert source == "pdf-explicit-heading"


def test_publisher_front_matter_without_abstract_heading_is_bounded():
    abstract = (
        "Artificial intelligence is increasingly used in software engineering workflows. "
        "This study examines the psychological costs of adoption through interviews with software professionals. "
        "The findings identify accountability anxiety, identity disruption, meaning erosion, and uncertainty distress. "
        "The study contributes a human-centered account of organizational AI transition."
    )
    page = (
        "The Paper Title\nAuthors Name, University\n" + abstract +
        "\nCCS Concepts: Artificial intelligence.\nACM Reference Format:\n1 Introduction"
    )
    found, source = _infer_abstract(page)
    assert found == abstract
    assert source == "pdf-implicit-front-matter"


@pytest.mark.parametrize('notice', [
    'This work is licensed under a Creative Commons Attribution 4.0 International License.',
    'This work is licensed under a Creative Commons Attribution 4.0 Interna-\ntional License.',
    'Creative Commons Attribution International License 4.0',
    'CC BY 4.0',
])
def test_explicit_cc_by_notice_variants_are_recognized(notice):
    from zotero_audio.extract import _inferred_license
    assert _inferred_license([notice]) == ('https://creativecommons.org/licenses/by/4.0/', 'pdf-text')


@pytest.mark.parametrize('notice', [
    'Creative Commons Attribution-NonCommercial 4.0 International License',
    'Creative Commons Attribution-NoDerivatives 4.0 International License',
    'Creative Commons Attribution-ShareAlike 4.0 International License',
    'Creative Commons Attribution 3.0 International License',
    'All rights reserved',
])
def test_license_detection_does_not_relax_the_allowlist(notice):
    from zotero_audio.extract import _inferred_license
    assert _inferred_license([notice]) == (None, None)


def test_cached_source_license_requires_matching_pdf_bytes(tmp_path):
    import pymupdf
    from zotero_audio.extract import source_pdf_rights
    from zotero_audio.util import sha256_file
    pdf = tmp_path / 'Synthetic license.pdf'
    with pymupdf.open() as doc:
        page = doc.new_page()
        page.insert_text((50, 50), 'This work is licensed under a Creative Commons Attribution 4.0 International License.', fontsize=9)
        doc.save(pdf)
    assert source_pdf_rights(pdf, sha256_file(pdf))[0] == 'https://creativecommons.org/licenses/by/4.0/'
    assert source_pdf_rights(pdf, '0' * 64) == (None, None)
