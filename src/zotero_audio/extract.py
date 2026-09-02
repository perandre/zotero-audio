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

from .util import atomic_write_json, atomic_write_text, filename_part, json_digest, sha256_file, sha256_text


DEFAULT_ZOTERO_STORAGE = Path.home() / "Zotero" / "storage"
REFERENCE_HEADINGS = {"references", "bibliography", "literature cited", "works cited"}
YEAR_RE = re.compile(r"\b(?:19|20)\d{2}\b")
NUMBERED_HEADING_RE = re.compile(r"^(\d+(?:\.\d+){0,5})[.)]?\s+(.+)$")


def resolve_pdf(pdf: str | None, zotero_key: str | None, zotero_storage: Path) -> Path:
    if bool(pdf) == bool(zotero_key):
        raise ValueError("Specify exactly one of --pdf and --zotero-key")
    if pdf:
        path = Path(pdf).expanduser().resolve()
    else:
        root = zotero_storage.expanduser().resolve()
        key = str(zotero_key)
        key_dir = (root / key).resolve()
        if key_dir.parent != root:
            raise ValueError("A Zotero key must be a single directory name")
        candidates = sorted(key_dir.rglob("*.pdf")) if key_dir.exists() else []
        if not candidates:
            raise FileNotFoundError(f"No PDF found for Zotero key {key} in {key_dir}")
        if len(candidates) > 1:
            raise RuntimeError(
                f"Multiple PDF attachments found for {key}: "
                + ", ".join(item.name for item in candidates)
                + ". Use --pdf to choose one."
            )
        path = candidates[0].resolve()
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
    text = text.replace("\u00a0", " ").replace("\u00ad", "")
    text = re.sub(r"(?<=\w)-\s*\n\s*(?=\w)", "", text)
    text = re.sub(r"\bhttps?://\S+", "", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\s+([,.;:!?])", r"\1", text)
    text = re.sub(r"\(\s+", "(", text)
    text = re.sub(r"\s+\)", ")", text)
    return text.strip(" \t\n.,")


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


def _paragraphs(text: str, omitted_edges: set[str]) -> tuple[list[str], list[dict[str, str]]]:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    kept_lines: list[str] = []
    omissions: list[dict[str, str]] = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped and _edge_key(stripped) in omitted_edges:
            omissions.append({"text": normalize_speech_text(stripped), "reason": "repeated-page-edge"})
            continue
        kept_lines.append(line)

    groups = re.split(r"\n\s*\n+", "\n".join(kept_lines))
    results: list[str] = []
    for group in groups:
        lines = [line.strip() for line in group.splitlines() if line.strip()]
        if not lines:
            continue
        joined = ""
        for line in lines:
            if joined.endswith("-") and line[:1].islower():
                joined = joined[:-1] + line
            else:
                joined += (" " if joined else "") + line
        cleaned = normalize_speech_text(joined)
        if cleaned:
            results.append(cleaned)
    return results, omissions


def _heading_level(text: str) -> int | None:
    lowered = text.casefold().rstrip(":")
    if lowered in {"abstract", "introduction", "conclusion", "conclusions"} | REFERENCE_HEADINGS:
        return 2
    match = NUMBERED_HEADING_RE.match(text)
    if match and len(text) <= 140:
        return min(6, len(match.group(1).split(".")) + 1)
    if len(text) <= 80 and not re.search(r"[.!?]$", text) and (
        text.isupper() or text.istitle()
    ):
        return 2
    return None


def _metadata_value(reader: PdfReader, name: str) -> str | None:
    value = getattr(reader.metadata, name, None) if reader.metadata else None
    if not value:
        return None
    return normalize_speech_text(str(value)) or None


def extract_pdf(pdf: Path, *, zotero_key: str | None, include_references: bool) -> dict[str, Any]:
    reader = PdfReader(str(pdf), strict=False)
    if reader.is_encrypted and reader.decrypt("") == 0:
        raise ValueError(f"PDF is encrypted and cannot be read: {pdf}")

    raw_pages: list[str] = []
    extraction_errors: list[dict[str, Any]] = []
    for page_number, page in enumerate(reader.pages, start=1):
        try:
            raw_pages.append(page.extract_text() or "")
        except Exception as exc:
            raw_pages.append("")
            extraction_errors.append({"pdf_page": page_number, "error": type(exc).__name__})
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
    pages: list[dict[str, Any]] = []
    blocks: list[dict[str, Any]] = []
    references_started = False
    title_key = normalize_speech_text(title).casefold()
    for page_number, raw_text in enumerate(raw_pages, start=1):
        paragraphs, omissions = _paragraphs(raw_text, edge_keys)
        page_block_ids: list[str] = []
        for index, text in enumerate(paragraphs, start=1):
            block_id = f"p{page_number:04d}-b{index:04d}"
            lowered = text.casefold().rstrip(":")
            heading_level = _heading_level(text)
            included = True
            omission_reason = None
            if lowered in REFERENCE_HEADINGS:
                references_started = True
            if references_started and not include_references:
                included = False
                omission_reason = "reference-section"
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
            }
        )

    source_sha = sha256_file(pdf)
    structure: dict[str, Any] = {
        "schema": "zotero-audio-structure/v1",
        "document": {
            "title": title,
            "author": author,
            "publication_year": publication_year,
            "publication_year_source": publication_year_source,
            "metadata": metadata_fields,
        },
        "source": {
            "filename": pdf.name,
            "path": str(pdf),
            "sha256": source_sha,
            "zotero_key": zotero_key,
            "pdf_pages": len(raw_pages),
        },
        "extraction": {
            "engine": "pypdf",
            "engine_version": _package_version("pypdf"),
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
        f"publication_year: {json.dumps(document.get('publication_year'))}",
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


def write_extraction(bundle: Path, structure: dict[str, Any], markdown: str) -> None:
    atomic_write_text(bundle / "article.md", markdown)
    atomic_write_json(bundle / "structure.json", structure)
