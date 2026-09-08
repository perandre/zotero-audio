"""Use layout classification for ordering, but original PDF spans for wording."""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any


def line_text(line: dict[str, Any]) -> str:
    result = ""
    previous = None
    for span in line["spans"]:
        text = span["text"]
        if previous and result and text and not result[-1].isspace() and not text[0].isspace():
            if span["bbox"][0] - previous["bbox"][2] > .5:
                result += " "
        result += text
        previous = span
    return result.rstrip()


def reading_order(boxes: list[dict], width: float) -> list[dict]:
    """Read each column completely between full-width layout elements."""
    def column_order(group):
        return sorted(group, key=lambda b: (b["x0"] >= width / 2, b["y0"], b["x0"]))
    wide = sorted([b for b in boxes if b["x1"] - b["x0"] > width * .58], key=lambda b: b["y0"])
    narrow = [b for b in boxes if b not in wide]
    ordered = []
    for barrier in wide:
        before = [b for b in narrow if (b["y0"] + b["y1"]) / 2 < barrier["y0"]]
        ordered.extend(column_order(before))
        narrow = [b for b in narrow if b not in before]
        ordered.append(barrier)
    ordered.extend(column_order(narrow))
    return ordered


def extract_layout(pdf: Path) -> tuple[list[str], list[list[dict[str, Any]]]]:
    import pymupdf
    import pymupdf4llm

    layout = json.loads(pymupdf4llm.to_json(str(pdf), use_ocr=False))
    pages, provenance = [], []
    with pymupdf.open(pdf) as document:
        for page_info in layout["pages"]:
            page = document[page_info["page_number"] - 1]
            lines = [line for block in page.get_text("dict")["blocks"]
                     for line in block.get("lines", [])]
            assigned = set()
            parts, records = [], []
            for box in reading_order(page_info["boxes"], page.rect.width):
                rect = pymupdf.Rect(box["x0"], box["y0"], box["x1"], box["y1"])
                selected = []
                for index, line in enumerate(lines):
                    x0, y0, x1, y1 = line["bbox"]
                    if index not in assigned and rect.contains(pymupdf.Point((x0+x1)/2, (y0+y1)/2)):
                        selected.append(line)
                        assigned.add(index)
                selected.sort(key=lambda line: (round(line["bbox"][1], 1), line["bbox"][0]))
                rendered_lines = []
                for line in selected:
                    value = line_text(line)
                    is_heading = (re.match(r"^\d+(?:\.\d+)*\.\s+", value)
                                  and any(span["flags"] & 18 for span in line["spans"]))
                    rendered_lines.append(("\n" + value + "\n") if is_heading else value)
                raw = "\n".join(rendered_lines).strip()
                kind = box["boxclass"]
                # Running heads are occasionally labelled text by the layout model.
                edge = rect.y1 < page.rect.height * .06 or rect.y0 > page.rect.height * .94
                omitted = kind in {"table", "picture", "caption", "page-header", "page-footer", "footnote"} or edge
                # Publisher/contact matter is identifiable on the opening page.
                if page.number == 0 and re.match(r"(?i)^(?:\*\s*Corresponding author|E-mail addresses|https://doi.org|Received \d|Available online|\d{4}-\d{4}/)", raw):
                    omitted = True
                    kind = "publisher-furniture"
                records.append({"bbox": list(rect), "type": kind, "raw_text": raw,
                                "included": not omitted, "table": box.get("table")})
                if raw and not omitted:
                    parts.append(raw)
            # Do not silently lose text that the layout model failed to classify.
            missing = [line_text(line) for index, line in enumerate(lines)
                       if index not in assigned and line_text(line).strip()
                       and line["bbox"][1] >= page.rect.height * .06
                       and line["bbox"][3] <= page.rect.height * .94]
            section_numbers = []
            for level, title, number in layout.get("toc", []):
                match = re.match(r"^(\d+(?:\.\d+)*)(?:\s|\.\s)", title)
                if level > 1 and number == page.number + 1 and match:
                    section_numbers.append(match.group(1))
            records.append({"type": "unassigned", "raw_text": "\n".join(missing), "included": False,
                            "expected_sections": section_numbers})
            # A discretionary hyphen also identifies a wrapped word between boxes.
            pages.append(re.sub(r"\u00ad\s*\n\s*", "", "\n\n".join(parts)))
            provenance.append(records)
    return pages, provenance
