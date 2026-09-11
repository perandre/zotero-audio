from __future__ import annotations

import json
import math
import re
import unicodedata
from collections import Counter
from importlib import metadata
from pathlib import Path
from typing import Any

from pypdf import PdfReader

from .article_files import research_markdown_path
from .util import atomic_write_json, atomic_write_text, filename_part, json_digest, sha256_file, sha256_text
from .zotero import merge_document_metadata
from .zotero_local import zotero_attachment_path
from .literature import REPORT_SUMMARIES, is_report


DEFAULT_ZOTERO_STORAGE = Path.home() / "Zotero" / "storage"
REFERENCE_HEADINGS = {"references", "bibliography", "literature cited", "works cited"}
SEMANTIC_HEADINGS = {
    "abstract", "summary", "introduction", "background", "literature review",
    "materials and methods", "methods", "methodology", "results", "findings",
    "discussion", "limitations", "conclusion", "conclusions", "data availability",
    "acknowledgments", "acknowledgements",
} | REFERENCE_HEADINGS | set(REPORT_SUMMARIES) | {"contents", "table of contents", "about this report", "recommendations", "appendix", "appendices", "endnotes"}
YEAR_RE = re.compile(r"\b(?:19|20)\d{2}\b")
NUMBERED_HEADING_RE = re.compile(r"^(\d+(?:\.\d+){0,5})[.)]\s+(.+)$")
REFERENCE_ENTRY_RE = re.compile(r"^\d+[.]\s+[A-ZÀ-ÖØ-Þ][.]\s+")
EMAIL_RE = re.compile(r"\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b[.,;:]?")
WEB_RE = re.compile(r"(?i)\b(?:https?://|www\.)\S+")
CITATION_RE = re.compile(
    r"(?:\((?:refs?[.]?\s*)?\d{1,3}(?:\s*[,;–-]\s*\d{1,3})*\)|"
    r"\[(?:\d{1,3}(?:\s*[,;–-]\s*\d{1,3})*)\])"
)
FURNITURE_RE = re.compile(
    r"(?i)^(?:author affiliations?|author contributions?|the authors? declare|"
    r"copyright\s+©?|to whom correspondence|this article contains supporting information|"
    r"published by|downloaded from)\b"
)


def resolve_pdf(pdf: str | None, zotero_key: str | None, zotero_storage: Path) -> Path:
    if bool(pdf) == bool(zotero_key):
        raise ValueError("Specify exactly one of --pdf and --zotero-key")
    if pdf:
        path = Path(pdf).expanduser().resolve()
    else:
        key = str(zotero_key)
        path = zotero_attachment_path(key, zotero_storage)
    if not path.is_file():
        raise FileNotFoundError(f"PDF does not exist: {path}")
    if path.suffix.lower() != ".pdf":
        raise ValueError(f"Input is not a PDF: {path}")
    return path


def _package_version(name: str) -> str:
    try:
        return metadata.version(name)
    except metadata.PackageNotFoundError:
        return "unknown"


def normalize_speech_text(value: str) -> str:
    text = unicodedata.normalize("NFKC", value)
    # Only a discretionary soft hyphen proves a typesetting word break.
    # Keep real hyphens, citations, URLs, and all other article content.
    text = re.sub(r"\u00ad\s*\n\s*", "", text)
    text = text.replace("\u00a0", " ").replace("\u00ad", "")
    text = re.sub(r"(?<=\w)-[ \t]*\n\s*(?=\w)", "-", text)
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"\s+([,.;:!?])", r"\1", text)
    text = re.sub(r"\(\s+", "(", text)
    text = re.sub(r"\s+\)", ")", text)
    return text.strip()


def _edge_key(line: str) -> str:
    return re.sub(r"\d+", "#", normalize_speech_text(line).casefold())


def _running_edge_keys(page_texts: list[str]) -> set[str]:
    counts: Counter[str] = Counter()
    for text in page_texts:
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        for line in set(lines[:2] + lines[-2:]):
            key = _edge_key(line)
            if 2 <= len(key) <= 160:
                counts[key] += 1
    threshold = max(3, math.ceil(len(page_texts) * 0.4))
    return {line for line, count in counts.items() if count >= threshold}


def _numbered_heading_parts(text: str) -> list[str]:
    """Split `2.1. Heading. Body` without guessing at unnumbered prose."""
    match = NUMBERED_HEADING_RE.match(text)
    if not match:
        return [text]
    number, remainder = match.groups()
    inline = re.match(r"^(.{2,100}?)[.]\s+([A-ZÀ-ÖØ-Þ].+)$", remainder)
    if not inline:
        return [text]
    heading, body = inline.groups()
    if len(heading.split()) > 14:
        return [text]
    return [f"{number}. {heading}", body]


def _paragraphs(text: str, omitted_edges: set[str]) -> tuple[list[str], list[dict[str, str]]]:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    omissions: list[dict[str, str]] = []
    groups: list[str] = []
    current: list[str] = []
    footnote = False

    def flush() -> None:
        if not current:
            return
        joined = ""
        for value in current:
            if joined.endswith("\u00ad"):
                joined = joined[:-1] + value
            elif joined.endswith("-"):
                joined += value
            else:
                joined += (" " if joined else "") + value
        cleaned = normalize_speech_text(joined)
        if cleaned:
            groups.extend(_numbered_heading_parts(cleaned))
        current.clear()

    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            flush()
            footnote = False
            continue
        if stripped and _edge_key(stripped) in omitted_edges:
            flush()
            omissions.append({"text": normalize_speech_text(stripped), "reason": "repeated-page-edge"})
            continue
        if stripped[:1] in {"*", "†", "‡", "§", "¶"}:
            flush()
            footnote = True
        if footnote:
            omissions.append({"text": normalize_speech_text(stripped), "reason": "footnote"})
            if stripped.endswith((".", "!", "?")):
                footnote = False
            continue
        cleaned_line = normalize_speech_text(stripped)
        if not cleaned_line:
            continue
        if FURNITURE_RE.match(cleaned_line):
            flush()
            omissions.append({"text": cleaned_line, "reason": "publisher-or-contact-furniture"})
            continue
        if (not current and _heading_level(cleaned_line) is not None) or cleaned_line.casefold().rstrip(":") == "significance":
            flush()
            groups.extend(_numbered_heading_parts(cleaned_line))
            continue
        current.append(stripped)
    flush()
    return [value for value in groups if value], omissions


def _heading_level(text: str) -> int | None:
    marked = re.match(r"^(#{2,6})\s+", text)
    if marked:
        return len(marked.group(1))
    lowered = text.casefold().rstrip(":")
    if lowered in SEMANTIC_HEADINGS:
        return 2
    match = NUMBERED_HEADING_RE.match(text)
    if match and len(text) <= 140 and int(match.group(1).split(".")[0]) <= 30:
        return min(6, len(match.group(1).split(".")) + 1)
    return None


def _metadata_value(reader: PdfReader, name: str) -> str | None:
    try:
        value = getattr(reader.metadata, name, None) if reader.metadata else None
    except (TypeError, ValueError):
        # A malformed PDF creation/modification date must not prevent us from
        # reading otherwise valid article text and bibliographic metadata.
        return None
    if not value:
        return None
    return normalize_speech_text(str(value)) or None


def _infer_abstract(first_page: str) -> tuple[str | None, str | None]:
    """Return only abstracts with a visible, conservative PDF boundary."""
    # Poppler can put the adjacent keyword column between the spaced heading
    # and abstract. It keeps a blank line at that column's end.
    first_page = re.sub(r"(?m)^A B S T R A C T\s*$", "Abstract", first_page)
    # Some publisher PDFs flatten the title block, article information, and
    # abstract heading onto one line. Restore a boundary only for the visibly
    # typeset all-caps marker; ordinary mentions of "abstract" are left alone.
    first_page = re.sub(
        r"(?<![A-Za-z])(?:A\s+B\s+S\s+T\s+R\s+A\s+C\s+T|ABSTRACT)(?![A-Za-z])",
        "\nAbstract\n",
        first_page,
    )
    first_page = re.sub(r"(?ms)(^Abstract\n\s*)Keywords:[^\n]*\n.*?\n\s*\n", r"\1", first_page)
    first_page = re.sub(r"\u00ad\s*\n\s*", "", first_page)
    lines = [normalize_speech_text(line) for line in first_page.splitlines()]
    lines = [line for line in lines if line]
    start: int | None = None
    source: str | None = None
    for index, line in enumerate(lines):
        if line.casefold().rstrip(":") == "abstract":
            start, source = index + 1, "pdf-explicit-heading"
            break
    if start is None:
        for index, line in enumerate(lines):
            if line.casefold().startswith("edited by "):
                start, source = index + 1, "pdf-editorial-front-matter"
                break
    # Several ACM/arXiv and journal layouts visibly place the abstract
    # paragraph between the author block and publication furniture without
    # printing an ``Abstract`` heading. Restrict this fallback to recognizable
    # publisher front matter and stop at its fixed bibliographic markers; it
    # must never turn an arbitrary opening paragraph into a Brief.
    if start is None and any(
        line.casefold() == "article"
        or re.search(r"(?i)\barxiv:\s*\d|\bccs concepts\b|\bacm reference format\b", line)
        for line in lines
    ):
        stop_markers = (
            r"(?i)^ccs concepts\b",
            r"(?i)^additional key words and phrases\b",
            r"(?i)^acm reference format\b",
            r"(?i)^authors['’] addresses\b",
            r"(?i)^permission to make digital copies\b",
            r"(?i)^\d+\s+[A-Z][^.!?]{8,},\s",
        )
        for index, line in enumerate(lines):
            if len(line.split()) < 6 or not re.match(r"[A-Z][a-z]", line):
                continue
            if any(re.search(pattern, line) for pattern in stop_markers):
                continue
            candidate: list[str] = []
            for following in lines[index:]:
                if any(re.search(pattern, following) for pattern in stop_markers):
                    break
                if FURNITURE_RE.match(following):
                    break
                candidate.append(following)
            words = " ".join(candidate).split()
            if 40 <= len(words) <= 500 and re.search(r"[.!?]", " ".join(candidate)):
                start, source = index, "pdf-implicit-front-matter"
                break
    if start is None:
        return None, None

    collected: list[str] = []

    def boundary_prefix(line: str) -> tuple[str | None, bool]:
        """Split a flattened keyword/section boundary from abstract prose."""
        keyword = re.search(r"(?i)(?<!\w)(?:keywords?|key\s+words?)\s*:?(?=\s|$)", line)
        if keyword and (keyword.start() == 0 or line[:keyword.start()].rstrip().endswith((".", ";", ":"))):
            prefix = line[:keyword.start()].strip()
            return (prefix or None), True
        introduction = re.search(
            r"(?i)(?<!\w)(?:\d+(?:\.\d+)*[.)]?\s+)?introduction\b", line
        )
        if introduction and (introduction.start() == 0 or line[:introduction.start()].rstrip().endswith((".", ";", ":"))):
            prefix = line[:introduction.start()].strip()
            return (prefix or None), True
        return None, False

    for line in lines[start:]:
        lowered = line.casefold().rstrip(":")
        if (
            lowered in {"keywords", "key words", "introduction", "significance"}
            or line.count("|") >= 2
            or (collected and _heading_level(line) is not None)
            or (
                source == "pdf-implicit-front-matter"
                and re.match(
                    r"(?i)^(?:ccs concepts|additional key words and phrases|acm reference format|"
                    r"authors['’] addresses|permission to make digital copies)\b",
                    line,
                )
            )
        ):
            break
        prefix, is_boundary = boundary_prefix(line)
        if is_boundary:
            if prefix:
                collected.append(prefix)
            break
        if FURNITURE_RE.match(line):
            break
        collected.append(line)
    abstract = normalize_speech_text(" ".join(collected))
    words = abstract.split()
    if not 40 <= len(words) <= 500 or not re.search(r"[.!?]", abstract):
        return None, None
    return abstract, source


def _front_matter_replaced_with_abstract(first_page: str, abstract: str, source: str | None) -> str:
    if not source:
        return first_page
    first_page = re.sub(
        r"(?<![A-Za-z])(?:A\s+B\s+S\s+T\s+R\s+A\s+C\s+T|ABSTRACT)(?![A-Za-z])",
        "\nAbstract\n",
        first_page,
    )
    lines = first_page.replace("\r\n", "\n").replace("\r", "\n").splitlines()
    start = None
    for index, line in enumerate(lines):
        clean = normalize_speech_text(line).casefold().rstrip(":")
        if (source == "pdf-explicit-heading" and clean.replace(" ", "") == "abstract") or (
            source == "pdf-editorial-front-matter" and clean.startswith("edited by ")
        ):
            start = index
            break
    if start is None and source == "pdf-implicit-front-matter":
        first_word = normalize_speech_text(abstract).split(" ", 1)[0].casefold()
        for index, line in enumerate(lines):
            if normalize_speech_text(line).casefold().startswith(first_word):
                start = index
                break
    if start is None:
        return first_page
    end = None
    keep_boundary = False
    for index in range(start + 1, len(lines)):
        clean = normalize_speech_text(lines[index])
        lowered = clean.casefold().rstrip(":")
        if lines[index].count("|") >= 2:
            end = index + 1
            break
        if lowered == "introduction" or NUMBERED_HEADING_RE.match(clean):
            end = index
            keep_boundary = True
            break
    if end is None:
        return first_page
    tail_at = end if not keep_boundary else end
    return f"Abstract\n\n{abstract}\n\n" + "\n".join(lines[tail_at:])


def _inferred_license(page_texts: list[str]) -> tuple[str | None, str | None]:
    raw = re.sub(r"\s+", " ", "\n".join(page_texts)).casefold()
    text = re.sub(r"\s+", " ", normalize_speech_text("\n".join(page_texts))).casefold()
    if re.search(
        r"creative commons attribution(?: international)? license\s*4\.0"
        r"|\bcc\s*[- ]?by\s*4\.0\b"
        r"|creativecommons\.org/licenses/by/4\.0",
        raw + " " + text,
    ):
        return "https://creativecommons.org/licenses/by/4.0/", "pdf-text"
    if re.search(r"\bcc0(?:\s+1\.0)?\b", raw + " " + text):
        return "https://creativecommons.org/publicdomain/zero/1.0/", "pdf-text"
    return None, None


def extract_pdf(pdf: Path, *, zotero_key: str | None, include_references: bool,
                metadata: dict[str, Any] | None = None) -> dict[str, Any]:
    reader = PdfReader(str(pdf), strict=False)
    if reader.is_encrypted and reader.decrypt("") == 0:
        raise ValueError(f"PDF is encrypted and cannot be read: {pdf}")

    raw_pages: list[str] = []
    extraction_errors: list[dict[str, Any]] = []
    from .layout import extract_layout
    report = is_report(metadata or {})
    raw_pages, layout_records = extract_layout(pdf, report=report)
    engine, engine_version = "pymupdf4llm-layout-original-spans", _package_version("pymupdf4llm")
    for page_number, records in enumerate(layout_records, 1):
        if records[-1]["raw_text"]:
            extraction_errors.append({"pdf_page": page_number, "error": "unassigned-layout-text"})
    if not any(page.strip() for page in raw_pages):
        raise RuntimeError(
            "No extractable text was found. The PDF may require OCR; use an OCR/Marker preprocessing path."
        )

    title = _metadata_value(reader, "title")
    if not title or title.casefold() in {"untitled", "document"}:
        title = normalize_speech_text(pdf.stem)
    author = _metadata_value(reader, "author")
    # Preserve all bibliographic signals available in the PDF metadata. Zotero
    # parent-item enrichment can replace these later without losing provenance.
    metadata_fields = {}
    if reader.metadata:
        for raw_key, raw_value in reader.metadata.items():
            key = str(raw_key).lstrip("/").casefold()
            if key in {"subject", "keywords", "creator", "producer", "doi", "url", "rights", "journal", "publisher", "language", "abstract"} and raw_value:
                metadata_fields[key] = normalize_speech_text(str(raw_value))
    for field in ("subject", "keywords", "creator", "producer", "doi", "url", "rights", "journal", "publisher", "language", "abstract"):
        value = _metadata_value(reader, field)
        if value:
            metadata_fields[field] = value
    publication_year = None
    publication_year_source = None
    for source_name, value in (
        ("filename", pdf.name),
        ("pdf-metadata", _metadata_value(reader, "creation_date")),
        ("first-page-text", raw_pages[0][:2000] if raw_pages else ""),
    ):
        years = YEAR_RE.findall(str(value)) if value else []
        if years:
            publication_year = years[-1]
            publication_year_source = source_name
            break

    edge_keys = _running_edge_keys(raw_pages)
    inferred_abstract, abstract_source = (None, None) if report else _infer_abstract(raw_pages[0])
    if not report and not inferred_abstract:
        # A few publisher PDFs expose the abstract in the logical PDF text
        # order but their layout extractor places the adjacent article-info
        # column before it. Use the page's native text as a narrow fallback
        # for Brief detection while retaining layout spans for the article.
        native_first_page = reader.pages[0].extract_text() or ""
        inferred_abstract, abstract_source = _infer_abstract(native_first_page)
    processed_pages = list(raw_pages)
    if inferred_abstract:
        processed_pages[0] = _front_matter_replaced_with_abstract(
            raw_pages[0], inferred_abstract, abstract_source
        )
    pages: list[dict[str, Any]] = []
    blocks: list[dict[str, Any]] = []
    references_started = False
    title_key = normalize_speech_text(title).casefold()
    editorial_front_matter = abstract_source == "pdf-editorial-front-matter"
    for page_number, raw_text in enumerate(raw_pages, start=1):
        paragraphs, omissions = _paragraphs(processed_pages[page_number - 1], edge_keys)
        page_block_ids: list[str] = []
        suppress_remainder = False
        suppress_until_heading = editorial_front_matter and page_number == 2
        for index, text in enumerate(paragraphs, start=1):
            block_id = f"p{page_number:04d}-b{index:04d}"
            lowered = text.casefold().rstrip(":")
            heading_level = _heading_level(text)
            if report and NUMBERED_HEADING_RE.match(text):
                # Numbered key findings are prose unless the layout classifier
                # marked them as headings (with an explicit Markdown prefix).
                heading_level = None
            if heading_level:
                text = re.sub(r"^#{2,6}\s+", "", text)
                lowered = text.casefold().rstrip(":")
            included = True
            omission_reason = None
            if heading_level:
                suppress_until_heading = False
            if lowered in REFERENCE_HEADINGS or (report and lowered == "endnotes") or REFERENCE_ENTRY_RE.match(text):
                references_started = True
            if references_started and not include_references:
                included = False
                omission_reason = "reference-section"
            elif suppress_until_heading:
                included = False
                omission_reason = "detached-page-fragment"
            elif page_number == 1 and lowered == "significance" and editorial_front_matter:
                suppress_remainder = True
                included = False
                omission_reason = "duplicate-editorial-front-matter"
            elif suppress_remainder:
                included = False
                omission_reason = "duplicate-editorial-front-matter"
            elif report and re.fullmatch(r"\d{1,4}", text):
                included = False
                omission_reason = "standalone-page-or-chapter-number"
            elif re.match(r"(?i)^(?:table|fig(?:ure)?)\s*\d+[.:]", text):
                included = False
                omission_reason = "table-or-figure-caption"
            elif re.match(r"(?i)^(?:this (?:table|figure)|sample:|se in parentheses)", text):
                included = False
                omission_reason = "table-or-figure-note"
            elif len(re.findall(r"\d", text)) >= max(12, len(text) // 5):
                included = False
                omission_reason = "probable-table-grid"
            elif page_number == 1 and text.casefold() == title_key:
                included = False
                omission_reason = "duplicate-document-title"
            record: dict[str, Any] = {
                "id": block_id,
                "pdf_page": page_number,
                "reading_order": len(blocks) + 1,
                "type": "heading" if heading_level else "paragraph",
                "text": text,
                "text_sha256": sha256_text(text),
                "included_in_reading": included,
            }
            if heading_level:
                record["heading_level"] = heading_level
            if omission_reason:
                record["omission_reason"] = omission_reason
            blocks.append(record)
            page_block_ids.append(block_id)
        pages.append(
            {
                "pdf_page": page_number,
                "raw_text_sha256": sha256_text(raw_text),
                "block_ids": page_block_ids,
                "omissions": omissions,
                "layout": layout_records[page_number - 1] if layout_records else [],
            }
        )

    document = {
        "title": title,
        "author": author,
        "publication_year": publication_year,
        "publication_year_source": publication_year_source,
        "metadata": metadata_fields,
    }
    if inferred_abstract and not metadata_fields.get("abstract"):
        document["abstract"] = inferred_abstract
        document["abstract_source"] = abstract_source
    rights, rights_source = _inferred_license(["\n".join(r["raw_text"] for r in records) for records in layout_records])
    if rights and not metadata_fields.get("rights"):
        document["rights"] = rights
        document["rights_source"] = rights_source
    document = merge_document_metadata(document, metadata)
    if inferred_abstract:
        document["abstract"] = inferred_abstract
        document["abstract_source"] = abstract_source

    # Join continuations across layout boxes/columns/pages, retaining both IDs.
    previous = None
    for block in blocks:
        if not block["included_in_reading"]:
            continue
        if (previous and previous["type"] == block["type"] == "paragraph"
                and (not re.search(r"[.!?][\"”’)]?$", previous["text"])
                     or previous["text"].count("(") > previous["text"].count(")"))
                and re.match(r"[a-z\d]", block["text"])):
            previous["text"] += " " + block["text"]
            previous["text_sha256"] = sha256_text(previous["text"])
            previous.setdefault("source_block_ids", [previous["id"]]).append(block["id"])
            previous.setdefault("pdf_pages", [previous["pdf_page"]]).append(block["pdf_page"])
            block["included_in_reading"] = False
            block["omission_reason"] = "continuation-merged"
        else:
            previous = block

    expected_sections = [number for records in layout_records for number in records[-1].get("expected_sections", [])]
    actual_sections = [match.group(1) for block in blocks if block["included_in_reading"] and block["type"] == "heading"
                       if (match := re.match(r"^(\d+(?:\.\d+)*)", block["text"]))]
    if len(expected_sections) >= 3 and actual_sections != expected_sections:
        extraction_errors.append({"error": "heading-order-mismatch", "expected": expected_sections, "actual": actual_sections})
    source_sha = sha256_file(pdf)
    structure: dict[str, Any] = {
        "schema": "zotero-audio-structure/v1",
        "document": document,
        "source": {
            "filename": pdf.name,
            "path": str(pdf),
            "sha256": source_sha,
            "zotero_key": zotero_key,
            "pdf_pages": len(raw_pages),
        },
        "extraction": {
            "engine": engine,
            "engine_version": engine_version,
            "algorithm": "layout-original-spans-report-v1" if report else "layout-original-spans-v4",
            "include_references": include_references,
            "errors": extraction_errors,
        },
        "pages": pages,
        "blocks": blocks,
    }
    structure["structure_sha256"] = json_digest(structure)
    return structure


def render_markdown(structure: dict[str, Any]) -> str:
    document = structure["document"]
    source = structure["source"]
    frontmatter = [
        "---",
        "schema: zotero-audio-markdown/v1",
        f"title: {json.dumps(document['title'], ensure_ascii=False)}",
        f"author: {json.dumps(document.get('author'), ensure_ascii=False)}",
        f"authors: {json.dumps(document.get('authors', []), ensure_ascii=False)}",
        f"publication_year: {json.dumps(document.get('publication_year'))}",
        f"publication_date: {json.dumps(document.get('publication_date'), ensure_ascii=False)}",
        f"journal: {json.dumps(document.get('journal'), ensure_ascii=False)}",
        f"publisher: {json.dumps(document.get('publisher'), ensure_ascii=False)}",
        f"university: {json.dumps(document.get('university'), ensure_ascii=False)}",
        *[f"{key}: {json.dumps(document.get(key), ensure_ascii=False)}" for key in
          ("item_type", "literature_type", "institution", "report_type", "report_number", "series_title", "evidence")],
        f"doi: {json.dumps(document.get('doi'), ensure_ascii=False)}",
        f"url: {json.dumps(document.get('url'), ensure_ascii=False)}",
        f"rights: {json.dumps(document.get('rights'), ensure_ascii=False)}",
        f"language: {json.dumps(document.get('language'), ensure_ascii=False)}",
        f"zotero_key: {json.dumps(source.get('zotero_key'))}",
        f"source_file: {json.dumps(source['filename'], ensure_ascii=False)}",
        f"source_sha256: {json.dumps(source['sha256'])}",
        f"pdf_pages: {source['pdf_pages']}",
        f"extractor: {json.dumps(structure['extraction']['engine'])}",
        f"extractor_version: {json.dumps(structure['extraction']['engine_version'])}",
        "---",
        "",
        f"# {document['title']}",
    ]
    by_page: dict[int, list[dict[str, Any]]] = {}
    for block in structure["blocks"]:
        if block["included_in_reading"]:
            by_page.setdefault(block["pdf_page"], []).append(block)
    parts = ["\n".join(frontmatter)]
    for page_number in range(1, source["pdf_pages"] + 1):
        parts.append(f"<!-- pdf-page: {page_number} -->")
        for block in by_page.get(page_number, []):
            if block["type"] == "heading":
                parts.append(f"{'#' * block['heading_level']} {block['text']}")
            else:
                parts.append(block["text"])
    return "\n\n".join(part for part in parts if part.strip()) + "\n"


def bundle_name(structure: dict[str, Any]) -> str:
    document = structure["document"]
    source = structure["source"]
    prefix = f"{document['publication_year']} - " if document.get("publication_year") else ""
    identity = source.get("zotero_key") or source["sha256"][:12]
    return filename_part(f"{prefix}{document['title']} [{identity}]")


def render_research_markdown(structure: dict[str, Any]) -> str:
    """Retain research material that is deliberately omitted from narration.

    The legacy renderer describes a reading. This renderer also includes
    references, figure/table captions and grids, and extracted footnotes.
    Source page markers remain available to readers and retrieval tools.
    """
    research = {**structure, "blocks": []}
    redundant = {"continuation-merged", "duplicate-document-title", "duplicate-editorial-front-matter",
                 "standalone-page-or-chapter-number"}
    for block in structure.get("blocks", []):
        research["blocks"].append({**block, "included_in_reading": block.get("omission_reason") not in redundant})
    for page in structure.get("pages", []):
        for ordinal, omission in enumerate(page.get("omissions", []), 1):
            if omission.get("reason") not in {"footnote", "publisher-or-contact-furniture"}:
                continue
            research["blocks"].append({"id": f"p{page['pdf_page']:04d}-note{ordinal:04d}",
                "pdf_page": page["pdf_page"], "type": "paragraph", "text": omission["text"],
                "included_in_reading": True})
    return render_markdown(research).replace("schema: zotero-audio-markdown/v1",
                                             "schema: zotero-audio-research-markdown/v1", 1)


def write_extraction(bundle: Path, structure: dict[str, Any], markdown: str) -> Path:
    markdown_path = research_markdown_path(bundle, structure.get("document", {}), migrate=True)
    atomic_write_text(markdown_path, markdown)
    atomic_write_json(bundle / "structure.json", structure)
    return markdown_path
