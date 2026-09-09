from pathlib import Path

import pytest

from zotero_audio.cli import build_parser
from zotero_audio.extract import _paragraphs
from zotero_audio.literature import brief_blocks, evidence_from_extra, is_report
from zotero_audio.podcast import (
    _show_notes, build_intro, build_local_podcast, content_quality_gate,
    create_edition_plan, extract_brief,
)
from zotero_audio.segment import create_speech_plan
from zotero_audio.util import atomic_write_json, json_digest


def block(text, *, heading=False, page=2, level=2, included=True, **extra):
    return {"id": f"p{page}-{text[:15]}", "text": text, "pdf_page": page,
            "type": "heading" if heading else "paragraph", "heading_level": level,
            "included_in_reading": included, **extra}


def report(*blocks):
    structure = {
        "source": {"sha256": "a" * 64, "zotero_key": "REPORT01"},
        "document": {"title": "AI adoption", "item_type": "report", "institution": "Example Institute",
                     "publication_date": "2026-01", "publication_year": "2026"},
        "blocks": list(blocks),
    }
    structure["structure_sha256"] = json_digest(structure)
    return structure


def summary_report():
    return report(block("1. Executive summary", heading=True),
                  block("Survey participants reported benefits, with important limitations.",
                        source_block_ids=["p2-start", "p3-end"], pdf_pages=[2, 3]),
                  block("Methods", heading=True, page=4),
                  block("The sample was recruited online.", page=4))


def test_report_type_is_explicit_and_can_be_overridden():
    assert is_report({"item_type": "report"})
    assert not is_report({"publisher": "McKinsey"})
    assert not is_report({"item_type": "report", "literature_type": "academic"})
    args = build_parser().parse_args(["prepare", "--pdf", "report.pdf", "--literature-type", "report"])
    assert args.literature_type == "report"


def test_report_brief_uses_source_sections_and_preserves_cross_page_provenance():
    structure = summary_report()
    # A Zotero abstract must not override the report's source summary.
    structure["document"]["abstract"] = "An unrelated catalog description."
    source = create_speech_plan(structure)
    plan = create_edition_plan(source, structure, "brief")
    assert plan["brief_contents"] == "the publisher's executive summary"
    assert "the report" in plan["segments"][0]["text"]
    assert "Issued by Example Institute in January 2026" in plan["segments"][0]["text"]
    body = [s for s in plan["segments"] if s["kind"] == "body"]
    assert [s["text"] for s in body] == [structure["blocks"][1]["text"]]
    assert body[0]["pdf_pages"] == [2, 3]
    assert body[0]["source_block_ids"] == ["p2-start", "p3-end"]
    qa = content_quality_gate(structure, source, edition="brief")
    assert qa["status"] == "pass"  # Issuer is sufficient when there are no named authors.
    structure["extraction"] = {"errors": [{"pdf_page": 3, "error": "unassigned-layout-text"}]}
    assert content_quality_gate(structure, source, edition="brief")["status"] == "needs_review"


def test_report_brief_keeps_subsections_and_stops_at_excluded_references():
    structure = report(block("Executive summary", heading=True), block("First finding."),
                       block("Adoption", heading=True, level=3), block("Second finding."),
                       block("A repeated pull quote.", included=False),
                       block("References", heading=True, included=False), block("A reference.", included=False))
    brief = extract_brief(structure)
    assert [b["text"] for b in brief_blocks(brief)] == ["First finding.", "Adoption", "Second finding."]


@pytest.mark.parametrize("blocks,reason", [
    ([block("Executive summary", heading=True), block("Unbounded prose.")], "report-summary-unbounded"),
    ([block("Executive summary", heading=True), block("Next heading", heading=True)], "report-summary-not-detected"),
    ([block("Executive summary", heading=True), block("First summary."), block("Methods", heading=True),
      block("Executive summary", heading=True), block("Second summary."), block("Conclusion", heading=True)], "report-summary-ambiguous"),
    ([block("Executive summary", heading=True), block("word " * 1501), block("Methods", heading=True)], "report-summary-too-long"),
])
def test_report_brief_fails_closed(blocks, reason):
    brief = extract_brief(report(*blocks))
    assert not brief["available"] and brief["reason"] == reason


def test_short_key_findings_can_replace_an_overlong_executive_summary():
    structure = report(block("Executive summary", heading=True), block("word " * 1501),
                       block("Key findings", heading=True), block("One complete finding."),
                       block("Methods", heading=True))
    brief = extract_brief(structure)
    assert brief["brief_contents"] == "the publisher's key findings"
    assert [b["text"] for b in brief_blocks(brief)] == ["One complete finding."]


def test_contents_entries_are_never_selected_as_a_report_summary():
    structure = report(block("Contents", heading=True, page=1),
                       block("Key insights", heading=True, page=1), block("Chapter 1: Adoption 4", page=1),
                       block("Appendix", heading=True, page=1),
                       block("Key insights", heading=True, page=3), block("The real finding.", page=3),
                       block("Methods", heading=True, page=4))
    assert [b["text"] for b in brief_blocks(extract_brief(structure))] == ["The real finding."]


def test_repeated_summary_heading_on_following_pages_is_a_continuation():
    structure = report(block("Introduction Key findings", heading=True, page=5, level=4),
                       block("First finding.", page=5),
                       block("Introduction Key findings", heading=True, page=6, level=4),
                       block("Second finding.", page=6), block("Main report", heading=True, page=7, level=3))
    brief = extract_brief(structure)
    assert brief["brief_contents"] == "the publisher's key findings"
    assert [b["text"] for b in brief_blocks(brief)] == ["First finding.", "Second finding."]


def test_report_headings_survive_layout_markers():
    paragraphs, _ = _paragraphs("## Executive summary\n\nProse.\n\n## Business adoption\n\nMore prose.", set())
    assert paragraphs == ["## Executive summary", "Prose.", "## Business adoption", "More prose."]


def test_evidence_notes_do_not_invent_peer_review_or_funding():
    evidence = evidence_from_extra("Evidence sample: 200 respondents\nEvidence claim types: survey observations and forecasts\nUnrelated: private note")
    assert evidence == {"sample": "200 respondents", "claim_types": "survey observations and forecasts", "source": "zotero-extra"}
    structure = summary_report()
    structure["document"]["evidence"] = evidence
    notes = _show_notes(structure["document"], [], "brief", {"allowed": False})
    assert "Literature type: Report" in notes
    assert "Peer review: Not stated" in notes
    assert "Evidence sample: 200 respondents" in notes
    assert "Evidence funding" not in notes


def test_report_build_stays_private_without_rights(tmp_path: Path):
    bundle = tmp_path / "bundle"
    structure = summary_report()
    source = create_speech_plan(structure)
    atomic_write_json(bundle / "structure.json", structure)
    atomic_write_json(bundle / "speech-plan.json", source)
    result = build_local_podcast(bundle, tmp_path / "private", selected=True, publishing_enabled=True,
                                 edition="brief", dry_run=True)
    assert result["content_qa"]["status"] == "pass"
    assert result["published"] is False
    assert result["state"] != "public_ready"
    assert result["editions"]["brief"]["status"] == "planned"


def test_full_report_intro_retains_authors_issuer_and_series():
    text = build_intro("full", "Adoption", ["Ada Smith"], literature_type="report", institution="Example Institute",
                       series_title="Technology Outlook", report_number="12", publication_date="2026-04-05")
    assert "by Ada Smith" in text
    assert "Issued by Example Institute on April 5, 2026" in text
    assert "Report series: Technology Outlook. Report number: 12." in text
