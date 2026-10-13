"""Source-grounded episode description text and its provenance."""
from __future__ import annotations

import html
import json
import re
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from typing import Any, Callable

MAX_ABSTRACT_CHARS = 24_000
MAX_CROSSREF_BYTES = 512_000
CROSSREF_TIMEOUT_SECONDS = 4


class _TextOnly(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.hidden = 0

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag.casefold() in {"script", "style"}:
            self.hidden += 1

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() in {"script", "style"} and self.hidden:
            self.hidden -= 1

    def handle_data(self, data: str) -> None:
        if not self.hidden:
            self.parts.append(data)


def normalize_abstract(value: Any) -> str:
    """Convert a metadata abstract to bounded plain text without rewriting it."""
    if not isinstance(value, str):
        return ""
    if len(value) > MAX_ABSTRACT_CHARS * 4:
        return ""
    parser = _TextOnly()
    try:
        parser.feed(value)
        value = " ".join(parser.parts)
    except Exception:
        value = re.sub(r"<[^>]{0,500}>", " ", value)
    value = html.unescape(value)
    value = re.sub(r"\s+", " ", value).strip()
    if len(value) > MAX_ABSTRACT_CHARS:
        return ""
    value = re.sub(r"^abstract\s*[:.—–-]\s*", "", value, flags=re.I)
    return value.strip()


def doi_abstract(doi: Any) -> str:
    """Fetch Crossref's publisher/depositor metadata for a DOI, with strict bounds."""
    doi = str(doi or "").strip()
    doi = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", doi, flags=re.I)
    if not re.fullmatch(r"10\.\d{4,9}/[-._;()/:A-Z0-9]+", doi, flags=re.I):
        return ""
    url = "https://api.crossref.org/works/" + urllib.parse.quote(doi, safe="/()")
    request = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "1MorePaper/1.0 (episode descriptions)"})
    raw = _bounded_get(request)
    if raw:
        try:
            payload = json.loads(raw)
            if not isinstance(payload, dict):
                payload = {}
            message = payload.get("message", {})
            if not isinstance(message, dict):
                message = {}
            returned_doi = str(message.get("DOI") or "").strip()
            abstract = message.get("abstract", "") if not returned_doi or returned_doi.casefold() == doi.casefold() else ""
            text = normalize_abstract(abstract)
            if text:
                return text
        except (ValueError, TypeError):
            pass

    # DataCite is a fixed, unauthenticated public DOI metadata endpoint.
    datacite_url = "https://api.datacite.org/dois/" + urllib.parse.quote(doi, safe="/()")
    datacite_raw = _bounded_get(urllib.request.Request(datacite_url, headers={"Accept": "application/vnd.api+json"}))
    if datacite_raw:
        try:
            payload = json.loads(datacite_raw)
            if not isinstance(payload, dict):
                payload = {}
            item_data = payload.get("data", {})
            if not isinstance(item_data, dict):
                item_data = {}
            returned_doi = str(item_data.get("id") or "").strip()
            if returned_doi and returned_doi.casefold() != doi.casefold():
                item_data = {}
            attributes = item_data.get("attributes", {})
            descriptions = attributes.get("descriptions", []) if isinstance(attributes, dict) else []
            for item in descriptions if isinstance(descriptions, list) else []:
                if not isinstance(item, dict):
                    continue
                if str(item.get("descriptionType", "")).casefold() == "abstract":
                    text = normalize_abstract(item.get("description"))
                    if text:
                        return text
        except (ValueError, TypeError, AttributeError):
            pass

    # arXiv DOIs encode the arXiv identifier and expose the abstract through
    # their documented Atom API. Query only a validated identifier on that host.
    match = re.fullmatch(r"10\.48550/arxiv\.(\d{4}\.\d{4,5}(?:v\d+)?)", doi, flags=re.I)
    if match:
        arxiv_id = match.group(1)
        arxiv_url = "https://export.arxiv.org/api/query?" + urllib.parse.urlencode({"id_list": arxiv_id, "max_results": 1})
        arxiv_raw = _bounded_get(urllib.request.Request(arxiv_url, headers={"Accept": "application/atom+xml"}))
        if arxiv_raw:
            try:
                root = ET.fromstring(arxiv_raw)
                entry = root.find("{http://www.w3.org/2005/Atom}entry")
                entry_id = entry.findtext("{http://www.w3.org/2005/Atom}id", "") if entry is not None else ""
                if re.search(rf"/abs/{re.escape(arxiv_id)}(?:v\d+)?$", entry_id):
                    summary = entry.find("{http://www.w3.org/2005/Atom}summary") if entry is not None else None
                    text = normalize_abstract(summary.text if summary is not None else "")
                    if text:
                        return text
            except Exception:
                pass
    return ""


def _bounded_get(request: urllib.request.Request) -> bytes:
    try:
        with urllib.request.urlopen(request, timeout=CROSSREF_TIMEOUT_SECONDS) as response:
            if response.status != 200:
                return b""
            raw = response.read(MAX_CROSSREF_BYTES + 1)
        return raw if len(raw) <= MAX_CROSSREF_BYTES else b""
    except Exception:
        # Missing or unavailable external metadata must never block publication.
        return b""


def _pdf_abstract(structure: dict[str, Any], document: dict[str, Any]) -> str:
    structure_document = structure.get("document", {}) if isinstance(structure, dict) else {}
    source = str(structure_document.get("abstract_source") or document.get("abstract_source") or "")
    blocks = structure.get("blocks", []) if isinstance(structure, dict) else []
    active = False
    parts: list[str] = []
    for block in blocks:
        text = str(block.get("text", block.get("source_text", ""))).strip()
        if str(block.get("role", "")).casefold() == "abstract" and text:
            parts.append(text)
            if sum(map(len, parts)) > MAX_ABSTRACT_CHARS:
                return ""
            continue
        lowered = text.casefold().rstrip(".: ")
        if block.get("type") == "heading":
            if lowered == "abstract":
                active = True
                continue
            if active:
                break
        if not active:
            # Some PDFs flatten the label and first sentence into one paragraph.
            inline = re.match(r"(?i)^abstract\s*[:.—–-]\s*(.+)$", text)
            if inline:
                active = True
                text = inline.group(1).strip()
            else:
                continue
        if re.match(r"(?i)^(?:keywords?|index terms|ccs concepts|received|revised|accepted|published online)\s*[:—–-]", text):
            break
        if block.get("included_in_reading", True) and text:
            parts.append(text)
            if sum(map(len, parts)) > MAX_ABSTRACT_CHARS:
                return ""
    block_abstract = normalize_abstract(" ".join(parts))
    if block_abstract:
        return block_abstract
    abstract = normalize_abstract(structure_document.get("abstract") or document.get("abstract"))
    return abstract if source.startswith("pdf-") or source == "preserved-extraction-metadata" else ""


def select_description_abstract(document: dict[str, Any], *, metadata: dict[str, Any] | None = None,
                                structure: dict[str, Any] | None = None,
                                cached: dict[str, Any] | None = None,
                                source_sha256: str | None = None,
                                fetch_doi: Callable[[Any], str] | None = None) -> dict[str, str]:
    """Choose one saved description abstract; narration inputs are untouched."""
    metadata = metadata or {}
    structure = structure or {}
    cached = cached or {}
    zotero = normalize_abstract(metadata.get("abstract"))
    cache_matches = bool(source_sha256 and cached.get("source_sha256") == source_sha256)
    if cache_matches and "description_abstract" in cached:
        cached_text = normalize_abstract(cached.get("description_abstract"))
        generated_text = normalize_abstract(cached.get("generated_description_abstract"))
        source = str(cached.get("description_abstract_source") or "preserved")
        # Explicit description edits and older saved values remain authoritative.
        if ("generated_description_abstract" not in cached or cached_text != generated_text):
            return {"description_abstract": cached_text, "description_abstract_source": source}
        if source == "zotero" and cached_text:
            return {"description_abstract": cached_text, "description_abstract_source": source}
        # A matching saved result also records that external lookup was already
        # attempted, including a missing DOI abstract.
        if source in {"doi-metadata", "pdf-extraction", "unavailable"}:
            if zotero:
                return {"description_abstract": zotero, "description_abstract_source": "zotero"}
            return {"description_abstract": cached_text, "description_abstract_source": source}

    if zotero:
        return {"description_abstract": zotero, "description_abstract_source": "zotero"}

    doi_text = (fetch_doi or doi_abstract)(document.get("doi") or metadata.get("doi"))
    if doi_text:
        return {"description_abstract": doi_text, "description_abstract_source": "doi-metadata"}

    structure_document = structure.get("document", {}) if isinstance(structure, dict) else {}
    extracted_source = str(structure_document.get("abstract_source") or document.get("abstract_source") or "")
    extracted = _pdf_abstract(structure, document)
    if extracted:
        return {"description_abstract": extracted, "description_abstract_source": "pdf-extraction"}
    return {"description_abstract": "", "description_abstract_source": "unavailable"}


def edition_description(edition: str, brief_contents: str | None = None) -> str:
    if edition == "brief":
        if not brief_contents:
            return "This is a Brief audio edition."
        contents = brief_contents
        return f"This Brief reads {contents} in the authors' own words."
    return "This Full Reading presents the paper's text as an audio edition."
