"""Explicit literature types and source-preserving report Brief selection."""
from __future__ import annotations

import re
from typing import Any


REPORT_SUMMARIES = ("executive summary", "key findings", "key takeaways", "key insights", "in brief", "overview")
EVIDENCE_FIELDS = {
    "methodology": "Evidence methodology",
    "sample": "Evidence sample",
    "data_dates": "Evidence data dates",
    "funding": "Evidence funding",
    "claim_types": "Evidence claim types",
    "peer_review": "Peer review",
}


def is_report(document: dict[str, Any]) -> bool:
    explicit = document.get("literature_type")
    return explicit == "report" if explicit else document.get("item_type") == "report"


def report_organisation(document: dict[str, Any]) -> str:
    return str(document.get("institution") or document.get("publisher") or "").strip()


def evidence_from_extra(extra: str) -> dict[str, str]:
    """Capture only explicitly labelled notes; do not infer evidence quality."""
    result = {}
    for field, label in EVIDENCE_FIELDS.items():
        values = re.findall(rf"(?im)^\s*{re.escape(label)}\s*:\s*([^\r\n]+)", extra)
        if values:
            result[field] = "\n".join(dict.fromkeys(value.strip() for value in values))
    if result:
        result["source"] = "zotero-extra"
    return result


def report_heading(text: str) -> str:
    text = re.sub(r"^\s*(?:\d+(?:\.\d+)*[.)]?\s+|[IVX]+[.)]\s+)", "", text)
    name = re.split(r"\s+[|—–]\s+", text, maxsplit=1)[0].strip().casefold().rstrip(".:")
    # Some publishers combine a chapter running head and the summary heading.
    if name.startswith("introduction ") and name.removeprefix("introduction ") in REPORT_SUMMARIES:
        name = name.removeprefix("introduction ")
    return name


def brief_sections(brief: dict[str, Any]) -> list[dict[str, Any]]:
    if "sections" in brief:
        return brief["sections"]
    return [{"heading": heading, "blocks": brief.get(key, [])}
            for heading, key in (("Abstract", "abstract"), ("Authors’ conclusion", "conclusion"))
            if brief.get(key)]


def brief_blocks(brief: dict[str, Any]) -> list[dict[str, Any]]:
    return [block for section in brief_sections(brief) for block in section["blocks"]]


def extract_report_brief(structure: dict[str, Any], max_seconds: float) -> dict[str, Any]:
    blocks = structure.get("blocks", [])
    candidates: dict[str, list[list[dict[str, Any]]]] = {name: [] for name in REPORT_SUMMARIES}
    unbounded = False
    contents_pages = {block.get("pdf_page") for block in blocks if block.get("type") == "heading"
                      and report_heading(str(block.get("text", ""))) in {"contents", "table of contents"}}
    continued_headings: set[int] = set()
    for index, heading in enumerate(blocks):
        if index in continued_headings or heading.get("pdf_page") in contents_pages:
            continue
        name = report_heading(str(heading.get("text", "")))
        if heading.get("type") != "heading" or not heading.get("included_in_reading", True) or name not in candidates:
            continue
        level = int(heading.get("heading_level", 2))
        section = []
        bounded = False
        last_page = heading.get("pdf_page")
        for next_index, block in enumerate(blocks[index + 1:], index + 1):
            if block.get("type") == "heading" and report_heading(str(block.get("text", ""))) in {
                "case studies", "real-world case studies", "methodology", "foreword", "references", "endnotes",
            }:
                bounded = True
                break
            # Even an omitted heading (e.g. references) is a section boundary.
            if block.get("type") == "heading" and int(block.get("heading_level", 2)) <= level:
                page = block.get("pdf_page")
                if (report_heading(str(block.get("text", ""))) == name
                        and isinstance(page, int) and isinstance(last_page, int)
                        and page > heading.get("pdf_page", page) and page <= last_page + 1):
                    continued_headings.add(next_index)
                    last_page = page
                    continue
                bounded = True
                break
            if block.get("included_in_reading", True):
                section.append(block)
                last_page = block.get("pdf_page", last_page)
        if not bounded:
            unbounded = True
            continue
        prose = [b for b in section if b.get("type") != "heading" and str(b.get("text", "")).strip()]
        # A contents entry or empty visual panel is not a usable summary.
        if prose:
            candidates[name].append(section)

    reason = "report-summary-unbounded" if unbounded else "report-summary-not-detected"
    for name in REPORT_SUMMARIES:
        matches = candidates[name]
        if len(matches) > 1:
            reason = "report-summary-ambiguous"
            continue
        if not matches:
            continue
        section = matches[0]
        word_count = sum(len(str(block.get("text", "")).split()) for block in section)
        if word_count / 150 * 60 > max_seconds:
            reason = "report-summary-too-long"
            continue
        return {"available": True, "abstract": [], "conclusion": [],
                "sections": [{"heading": name.capitalize(), "blocks": section}],
                "brief_contents": f"the publisher's {name}"}
    return {"available": False, "reason": reason, "abstract": [], "conclusion": [], "sections": []}
