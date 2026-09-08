"""Private-first podcast editions, sidecars, RSS, and local publication.

No network requests occur here. ``LocalPublisher`` builds a verified local
mirror suitable for later upload by a credentialled object-storage adapter.
"""
from __future__ import annotations

import html
import hashlib
import mimetypes
import re
import shutil
import urllib.parse
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import format_datetime, parsedate_to_datetime
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Protocol

from . import __version__
from .audio import SpeechBackend, assemble_m4a, synthesize_plan, validate_wav
from .segment import chunk_text, create_speech_plan
from .util import atomic_write_json, atomic_write_text, json_digest, load_json, sha256_file, sha256_text
from .zotero import (
    license_record_from_metadata,
    load_bundle_metadata,
    merge_document_metadata,
    normalize_publication_date,
)

EDITION_BRIEF = "brief"
EDITION_FULL = "full"
EDITIONS = (EDITION_BRIEF, EDITION_FULL)
TRUE_PEAK_CEILING_DBTP = -1.0
ITUNES_NS = "http://www.itunes.com/dtds/podcast-1.0.dtd"
PODCAST_NS = "https://podcastindex.org/namespace/1.0"
ATOM_NS = "http://www.w3.org/2005/Atom"
CONTENT_NS = "http://purl.org/rss/1.0/modules/content/"
REMOTE_HEALTH_HEADERS = {"User-Agent": "OpenPaperAudioHealth/1.0"}
for _prefix, _ns in (("itunes", ITUNES_NS), ("podcast", PODCAST_NS), ("atom", ATOM_NS), ("content", CONTENT_NS)):
    ET.register_namespace(_prefix, _ns)

LICENSES = {
    "https://creativecommons.org/licenses/by/4.0/": ("CC-BY-4.0", "Creative Commons Attribution 4.0 International"),
    "https://creativecommons.org/publicdomain/zero/1.0/": ("CC0-1.0", "CC0 1.0 Universal"),
    "https://creativecommons.org/publicdomain/mark/1.0/": ("PDM-1.0", "Public Domain Mark 1.0"),
}
LICENSE_ALIASES = {
    "cc by 4.0": "https://creativecommons.org/licenses/by/4.0/",
    "cc0": "https://creativecommons.org/publicdomain/zero/1.0/",
    "cc0 1.0": "https://creativecommons.org/publicdomain/zero/1.0/",
    "public domain mark 1.0": "https://creativecommons.org/publicdomain/mark/1.0/",
}
HTTP_URL_RE = re.compile(r"https?://[^\s<]+", re.IGNORECASE)


@dataclass(frozen=True)
class ShowConfig:
    title: str
    description: str
    slug: str
    guid: str


@dataclass(frozen=True)
class PodcastConfig:
    private_root: Path
    state_root: Path
    public_root: Path | None = None
    base_url: str = "https://podcast.example.invalid"
    site_title: str = "Open Paper Audio"
    site_description: str = "Careful audio editions of openly licensed research papers."
    language: str = "en"
    author: str = "Open Paper Audio"
    owner_name: str = "Open Paper Audio"
    owner_email: str = "podcast@example.invalid"
    category: str = "Science"
    copyright: str = "Open Paper Audio"
    publishing_enabled: bool = False
    dry_run: bool = True
    r2_bucket: str = ""
    brief_show: ShowConfig = field(default_factory=lambda: ShowConfig(
        "Open Paper Briefs", "Brief editions containing the authors' abstract and, when suitable, conclusion.",
        "brief", "f25aa92b-5bea-51f8-b842-0836db8713ed"))
    full_show: ShowConfig = field(default_factory=lambda: ShowConfig(
        "Open Paper Full Readings", "Full readings of openly licensed research papers.",
        "full", "119266cf-33b9-5795-bcc2-0e81e72291ef"))


def load_podcast_config(path: Path) -> PodcastConfig:
    import tomllib
    with path.expanduser().resolve().open("rb") as stream:
        raw = tomllib.load(stream)
    paths, common, shows = raw.get("paths", {}), raw.get("podcast", {}), raw.get("shows", {})
    public_value = str(paths.get("public_root", "")).strip()
    defaults = PodcastConfig(Path("."), Path("."))
    def show(slug: str, fallback: ShowConfig) -> ShowConfig:
        value = shows.get(slug, {})
        return ShowConfig(str(value.get("title", fallback.title)), str(value.get("description", fallback.description)),
                          slug, str(value.get("guid", fallback.guid)))
    return PodcastConfig(
        private_root=Path(paths["private_root"]).expanduser().resolve(),
        state_root=Path(paths["state_root"]).expanduser().resolve(),
        public_root=Path(public_value).expanduser().resolve() if public_value else None,
        base_url=str(common.get("base_url", defaults.base_url)).rstrip("/"),
        site_title=str(common.get("site_title", defaults.site_title)),
        site_description=str(common.get("site_description", defaults.site_description)),
        language=str(common.get("language", defaults.language)), author=str(common.get("author", defaults.author)),
        owner_name=str(common.get("owner_name", defaults.owner_name)),
        owner_email=str(common.get("owner_email", defaults.owner_email)), category=str(common.get("category", defaults.category)),
        copyright=str(common.get("copyright", defaults.copyright)),
        publishing_enabled=bool(common.get("publishing_enabled", False)), dry_run=bool(common.get("dry_run", True)),
        r2_bucket=str(common.get("r2_bucket", "")).strip(),
        brief_show=show("brief", defaults.brief_show), full_show=show("full", defaults.full_show))


def _surname(name: str) -> str:
    clean = re.sub(r"\s+", " ", name.strip())
    if "," in clean:
        return clean.split(",", 1)[0].strip()
    parts = clean.split()
    if len(parts) > 1 and parts[-2].casefold() in {"da", "de", "del", "der", "di", "la", "le", "van", "von"}:
        return " ".join(parts[-2:])
    return parts[-1] if parts else ""


def episode_title(title: str, authors: Iterable[str] = (), year: str | int | None = None) -> str:
    names = [_surname(str(author)) for author in authors if str(author).strip()]
    label = names[0] if len(names) == 1 else f"{names[0]} & {names[1]}" if len(names) == 2 else f"{names[0]} et al." if names else ""
    suffix = " ".join(part for part in (label, f"({str(year).strip()})" if year else "") if part)
    return f"{title.strip()} — {suffix}" if suffix else title.strip()


def spoken_authors(authors: Iterable[str]) -> str:
    names = [re.sub(r"\s+", " ", str(author).strip()) for author in authors if str(author).strip()]
    if not names:
        return "the authors"
    if len(names) == 1:
        return names[0]
    if len(names) == 2:
        return f"{names[0]} and {names[1]}"
    if len(names) == 3:
        return f"{names[0]}, {names[1]}, and {names[2]}"
    return f"{names[0]} and colleagues"


def _spoken_date(value: str) -> str:
    normalized = normalize_publication_date(value)
    match = re.fullmatch(r"((?:19|20)\d{2})-(\d{2})-(\d{2})", str(normalized))
    if match:
        year, month, day = (int(part) for part in match.groups())
        try:
            parsed = datetime(year, month, day)
        except ValueError:
            return str(normalized)
        return f"{parsed.strftime('%B')} {parsed.day}, {parsed.year}"

    partial = re.fullmatch(r"((?:19|20)\d{2})-(\d{2})", str(normalized))
    if partial:
        year, month = int(partial.group(1)), int(partial.group(2))
        if 1 <= month <= 12:
            return f"{datetime(year, month, 1).strftime('%B')} {year}"
    return str(normalized).strip()


def _date_preposition(value: str) -> str:
    normalized = normalize_publication_date(value)
    match = re.fullmatch(r"((?:19|20)\d{2})-(\d{2})-(\d{2})", str(normalized))
    if not match:
        return "in"
    year, month, day = (int(part) for part in match.groups())
    try:
        datetime(year, month, day)
    except ValueError:
        return "in"
    return "on"


def build_intro(edition: str, title: str, authors: Iterable[str], *, journal: str | None = None,
                university: str | None = None, publication_date: str | None = None,
                year: str | int | None = None) -> str:
    if edition not in EDITIONS:
        raise ValueError(f"unknown edition: {edition}")
    kind = "a brief of " if edition == EDITION_BRIEF else ""
    affiliation = f", from {university.strip()}" if university and university.strip() else ""
    parts = [f'You’re listening to {kind}“{title.strip()},” by {spoken_authors(authors)}{affiliation}.']
    if journal and publication_date:
        parts.append(f"Published in {journal} {_date_preposition(publication_date)} {_spoken_date(publication_date)}.")
    elif journal and year:
        parts.append(f"Published in {journal} in {year}.")
    elif year:
        parts.append(f"Published in {year}.")
    return " ".join(parts)


def extract_brief(structure: dict[str, Any], max_seconds: float = 600.0) -> dict[str, Any]:
    structure_document = structure.get("document", {})
    metadata_abstract = str(structure_document.get("abstract") or structure_document.get("metadata", {}).get("abstract", "")).strip()
    abstract: list[dict[str, Any]] = ([{
        "id": "document-abstract", "type": "body", "role": "abstract", "text": metadata_abstract,
        "pdf_page": 1, "included_in_reading": True,
        "provenance": structure_document.get("abstract_source") or "document-metadata",
    }] if metadata_abstract else [])
    document_abstract = bool(abstract)
    conclusion: list[dict[str, Any]] = []
    section: str | None = None
    for block in structure.get("blocks", []):
        role = str(block.get("role", "")).casefold()
        text = str(block.get("text", block.get("source_text", ""))).strip()
        heading = text.casefold().rstrip(".: ")
        if role == "abstract" and not document_abstract:
            abstract.append(block); continue
        if role in {"conclusion", "conclusions"}:
            conclusion.append(block); continue
        if block.get("type") == "heading":
            section = "abstract" if heading == "abstract" else "conclusion" if heading in {"conclusion", "conclusions"} else None
        elif block.get("included_in_reading", True) and section:
            if section == "conclusion" or not document_abstract:
                (abstract if section == "abstract" else conclusion).append(block)
    if not abstract:
        return {"available": False, "reason": "abstract-not-detected", "abstract": [], "conclusion": []}
    word_count = lambda blocks: sum(len(str(item.get("text", "")).split()) for item in blocks)
    include_conclusion = bool(conclusion) and (word_count(abstract) + word_count(conclusion)) / 150 * 60 <= max_seconds
    return {"available": True, "abstract": abstract, "conclusion": conclusion if include_conclusion else [],
            "brief_contents": "the authors' abstract and conclusion" if include_conclusion else "the authors' abstract"}


def _canonical_license_url(value: str) -> str | None:
    alias = LICENSE_ALIASES.get(value.strip().casefold())
    if alias:
        return alias
    parsed = urllib.parse.urlsplit(value.strip())
    normalized = f"https://creativecommons.org{parsed.path.rstrip('/')}/" if parsed.netloc.casefold() == "creativecommons.org" else ""
    return normalized if normalized in LICENSES else None


def resolve_license(record: dict[str, Any] | None, *, source_sha256: str | None = None) -> dict[str, Any]:
    if not record:
        return {"status": "unresolved", "reason": "missing-license-record", "allowed": False}
    if not source_sha256 or record.get("source_sha256") != source_sha256:
        return {"status": "conflict", "reason": "license-source-mismatch", "allowed": False}
    canonical = _canonical_license_url(str(record.get("license_url") or record.get("license") or ""))
    if record.get("embargoed"):
        return {"status": "denied", "reason": "source-is-embargoed", "allowed": False}
    if record.get("conflict"):
        return {"status": "conflict", "reason": "conflicting-license-evidence", "allowed": False}
    evidence, read_url = record.get("evidence_url") or record.get("evidence_sha256"), record.get("read_url") or record.get("source_url")
    if not canonical or not evidence or not read_url:
        return {"status": "denied", "reason": "not-allowlisted-or-unverified", "allowed": False}
    identifier, name = LICENSES[canonical]
    return {"status": "allowed", "reason": None, "allowed": True, "id": identifier, "name": name,
            "license_url": canonical, "episode_license_url": "https://creativecommons.org/licenses/by/4.0/",
            "evidence": evidence, "read_url": str(read_url), "content_version": record.get("content_version")}


def publication_state(*, selected: bool, private_ready: bool, license_record: dict[str, Any] | None,
                      source_sha256: str) -> str:
    if not private_ready:
        return "needs_review"
    if not selected:
        return "not_selected"
    result = resolve_license(license_record, source_sha256=source_sha256)
    return "public_ready" if result["allowed"] else "needs_review" if result["status"] == "conflict" else "private_only"


def vtt_timestamp(seconds: float) -> str:
    if seconds < 0:
        raise ValueError("VTT timestamps cannot be negative")
    ms = round(seconds * 1000); hours, ms = divmod(ms, 3_600_000); minutes, ms = divmod(ms, 60_000); seconds, ms = divmod(ms, 1000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}.{ms:03d}"


def build_markdown_transcript(plan: dict[str, Any], title: str) -> str:
    """Render source paragraphs, never arbitrary timed-caption windows."""
    paragraphs: list[str] = []
    previous = None
    for segment in plan["segments"]:
        kind = segment["kind"]
        ids = tuple(segment.get("source_block_ids", []))
        key = (kind, segment.get("section"), ids)
        text = segment["text"]
        if paragraphs and key == previous and kind != "heading" and (ids or kind in {"intro", "closing"}):
            paragraphs[-1] += " " + text
        else:
            paragraphs.append(text)
        previous = key
    return f"# {title}\n\n" + "\n\n".join(paragraphs) + "\n"


def build_transcript(cues: Iterable[dict[str, Any]], destination: Path | None = None) -> str:
    lines = ["WEBVTT", "Kind: captions", "Language: en", ""]
    for index, cue in enumerate(cues, 1):
        start, end = float(cue["start"]), float(cue["end"])
        if end <= start:
            raise ValueError(f"transcript cue {index} has no positive duration")
        text = re.sub(r"\s+", " ", str(cue.get("text", ""))).strip().replace("-->", "→")
        lines.extend((str(index), f"{vtt_timestamp(start)} --> {vtt_timestamp(end)}", text, ""))
    result = "\n".join(lines)
    if destination:
        atomic_write_text(destination, result)
    return result


def build_transcript_html(cues: Iterable[dict[str, Any]], title: str, destination: Path | None = None,
                          attribution: str = "") -> str:
    body = "\n".join(f'<p data-start="{float(c["start"]):.3f}"><a href="audio.m4a#t={float(c["start"]):.3f}">{vtt_timestamp(float(c["start"]))}</a> {html.escape(str(c["text"]))}</p>' for c in cues)
    result = ("<!doctype html><html lang=\"en\"><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
              f"<title>{html.escape(title)}</title><style>body{{max-width:48rem;margin:4rem auto;padding:0 1.25rem;background:#f2efe6;color:#16212b;font:18px/1.6 Georgia,serif}}a{{color:#2f5bd3}}</style>"
              f"<main><h1>{html.escape(title)}</h1><small>{html.escape(attribution)}</small>{body}</main></html>\n")
    if destination:
        atomic_write_text(destination, result)
    return result


def chapter_json(chapters: Iterable[dict[str, Any]], destination: Path | None = None) -> dict[str, Any]:
    values, previous = [], -1.0
    for chapter in chapters:
        start = round(float(chapter["start"]), 3)
        if start < 0 or start <= previous:
            raise ValueError("chapter starts must be non-negative and strictly increasing")
        values.append({"startTime": start, "title": str(chapter["title"]).strip()}); previous = start
    result = {"version": "1.2.0", "chapters": values}
    if destination:
        atomic_write_json(destination, result)
    return result


def loudness_pass(measurement: dict[str, Any]) -> bool:
    loudness, peak = measurement.get("integrated_lufs"), measurement.get("true_peak_dbtp")
    return (isinstance(loudness, (int, float)) and -17 <= loudness <= -15 and isinstance(peak, (int, float))
            and peak <= TRUE_PEAK_CEILING_DBTP and not measurement.get("clipped_samples") and not measurement.get("silent"))


def content_quality_gate(structure: dict[str, Any], source_plan: dict[str, Any],
                         metadata: dict[str, Any] | None = None) -> dict[str, Any]:
    """Conservative listener-readiness gate for known PDF failure modes."""
    metadata = metadata or {}
    errors: list[str] = []
    warnings: list[str] = []
    if structure.get("extraction", {}).get("errors"):
        errors.append("unresolved-pdf-extraction-errors")
    document = merge_document_metadata(source_plan.get("document", {}), metadata)
    if not str(document.get("title", "")).strip():
        errors.append("missing-title")
    if not _authors(document, metadata):
        errors.append("missing-authors")
    language = str(metadata.get("language") or metadata.get("bibliographic_language") or "en").casefold()
    if language.startswith(("nb", "nn", "no")):
        errors.append("unsupported-publication-quality-language")
    headings = [str(block.get("text", "")) for block in structure.get("blocks", [])
                if block.get("type") == "heading" and block.get("included_in_reading", True)]
    if not headings:
        headings = [str(segment.get("text", "")) for segment in source_plan.get("segments", []) if segment.get("kind") == "heading"]
    meaningful = [heading for heading in headings if heading.casefold().rstrip(".:") not in {"references", "bibliography", "works cited"}]
    if len(meaningful) < 2:
        errors.append("page-flat-or-missing-section-structure")
    spoken = "\n".join(str(segment.get("text", "")) for segment in source_plan.get("segments", [])[1:])
    checks = (
        (r"\bhttps?://|\bwww\.", "raw-url-in-spoken-text"),
        (r"\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b", "email-in-spoken-text"),
        (r"\[(?:\d+[ ,;–-]*){1,8}\]", "numeric-citation-marker-in-spoken-text"),
        (r"\ufffd", "unicode-replacement-character"),
        (r"\b[A-Za-z]{55,}\b", "probable-fused-word"),
    )
    for pattern, code in checks:
        if re.search(pattern, spoken):
            errors.append(code)
    if any(heading.casefold().rstrip(".:") in {"references", "bibliography", "works cited"} for heading in headings):
        errors.append("reference-section-in-spoken-plan")
    if not extract_brief({**structure, "document": document})["available"]:
        warnings.append("brief-unavailable-no-confident-abstract")
    return {"schema": "zotero-audio-content-qa/v1", "status": "pass" if not errors else "needs_review",
            "errors": errors, "warnings": warnings, "heading_count": len(meaningful)}


class Publisher(Protocol):
    def put(self, local_path: Path, remote_key: str, content_type: str) -> str: ...
    def head(self, url: str) -> dict[str, Any]: ...


class LocalPublisher:
    """Immutable local mirror with sidecar HTTP-header requirements."""
    def __init__(self, root: Path, base_url: str) -> None:
        self.root = root.expanduser().resolve(); parsed = urllib.parse.urlsplit(base_url)
        if parsed.scheme != "https" or not parsed.netloc:
            raise ValueError("public base_url must be an absolute HTTPS URL")
        self.base_url = base_url.rstrip("/")

    def _destination(self, key_value: str) -> Path:
        key = PurePosixPath(key_value)
        if key.is_absolute() or ".." in key.parts or not key.parts:
            raise ValueError(f"unsafe publisher key: {key_value}")
        destination = (self.root / Path(*key.parts)).resolve()
        if destination != self.root and self.root not in destination.parents:
            raise ValueError(f"publisher key escapes root: {key_value}")
        return destination

    def put(self, local_path: Path, remote_key: str, content_type: str = "application/octet-stream") -> str:
        destination = self._destination(remote_key); destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists() and sha256_file(destination) != sha256_file(local_path):
            raise RuntimeError(f"immutable publisher collision: {remote_key}")
        if not destination.exists():
            shutil.copyfile(local_path, destination)
        atomic_write_json(destination.with_suffix(destination.suffix + ".headers.json"), {
            "content_type": content_type, "cache_control": "public, max-age=31536000, immutable", "accept_ranges": "bytes"})
        return f"{self.base_url}/{urllib.parse.quote(remote_key, safe='/')}"

    def head(self, url: str) -> dict[str, Any]:
        parsed, base = urllib.parse.urlsplit(url), urllib.parse.urlsplit(self.base_url)
        if (parsed.scheme, parsed.netloc) != (base.scheme, base.netloc):
            raise ValueError("URL is outside the configured publisher")
        relative = parsed.path[len(base.path.rstrip('/')):].lstrip("/") if parsed.path.startswith(base.path.rstrip("/")) else parsed.path.lstrip("/")
        path = self._destination(urllib.parse.unquote(relative))
        if not path.is_file():
            raise FileNotFoundError(path)
        headers_path = path.with_suffix(path.suffix + ".headers.json")
        headers = load_json(headers_path) if headers_path.is_file() else {}
        return {"status": 200, "bytes": path.stat().st_size, "sha256": sha256_file(path),
                "content_type": headers.get("content_type") or mimetypes.guess_type(path.name)[0],
                "accept_ranges": headers.get("accept_ranges") == "bytes"}

    def commit_feed(self, staged: Path, canonical_name: str, snapshot_dir: Path) -> tuple[Path, bool]:
        destination, digest = self._destination(canonical_name), sha256_file(staged)
        if destination.is_file() and sha256_file(destination) == digest:
            return destination, False
        snapshot_dir.mkdir(parents=True, exist_ok=True); snapshot = snapshot_dir / f"feed.{digest[:16]}.xml"
        if not snapshot.exists():
            shutil.copyfile(staged, snapshot)
        destination.parent.mkdir(parents=True, exist_ok=True); temporary = destination.with_suffix(".xml.tmp")
        shutil.copyfile(staged, temporary); temporary.replace(destination)
        atomic_write_json(destination.with_suffix(".xml.headers.json"), {"content_type": "application/rss+xml; charset=utf-8",
                          "cache_control": "public, max-age=300", "accept_ranges": "bytes"})
        return snapshot, True


def content_addressed_key(path: Path, prefix: str = "episodes") -> str:
    return f"{prefix.strip('/')}/{sha256_file(path)}/{path.name}"


def _authors(document: dict[str, Any], metadata: dict[str, Any]) -> list[str]:
    value = metadata.get("authors") or document.get("authors")
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    raw = str(value or document.get("author") or "")
    separator = ";" if ";" in raw else " and " if " and " in raw else None
    return [item.strip() for item in raw.split(separator) if item.strip()] if separator else ([raw.strip()] if raw.strip() else [])


def _new_segment(text: str, ordinal: int, kind: str, section: str, *, ids: list[str] | None = None,
                 pages: list[int] | None = None, pause: int = 240, transformations: list[str] | None = None) -> dict[str, Any]:
    text = text.strip()
    return {"ordinal": ordinal, "kind": kind, "section": section, "text": text, "text_sha256": sha256_text(text),
            "source_block_ids": ids or [], "pdf_pages": pages or [], "pause_after_ms": pause,
            "transformations": transformations or [], "narrator_text": kind in {"ident", "intro", "closing"}}


def create_edition_plan(source_plan: dict[str, Any], structure: dict[str, Any], edition: str, *,
                        metadata: dict[str, Any] | None = None, license_result: dict[str, Any] | None = None,
                        public: bool = False, max_chars: int = 900) -> dict[str, Any]:
    if edition not in EDITIONS:
        raise ValueError(f"unknown edition: {edition}")
    metadata = metadata or {}; document = merge_document_metadata(source_plan.get("document", {}), metadata)
    if str(structure.get("document", {}).get("abstract_source", "")).startswith("pdf-"):
        document["abstract"] = structure["document"]["abstract"]
        document["abstract_source"] = structure["document"]["abstract_source"]
    structure = {**structure, "document": document}
    authors, title = _authors(document, metadata), str(document.get("title") or "Untitled paper")
    brief = extract_brief(structure)
    if edition == EDITION_BRIEF and not brief["available"]:
        raise RuntimeError("Brief unavailable: no confidently bounded author abstract")
    publication_date = document.get("publication_date")
    if str(publication_date or "").strip() == str(document.get("publication_year") or "").strip():
        publication_date = None
    intro = build_intro(edition, title, authors, journal=document.get("journal") or document.get("publication_title"),
                        university=document.get("university"), publication_date=publication_date,
                        year=document.get("publication_year"))
    segments: list[dict[str, Any]] = []
    def append(text: str, kind: str, section: str, ids: list[str] | None = None, pages: list[int] | None = None,
               pause: int = 240, transformations: list[str] | None = None) -> None:
        for value in chunk_text(text, max_chars, preserve_sentences=True):
            segments.append(_new_segment(value, len(segments) + 1, kind, section, ids=ids, pages=pages,
                                         pause=pause, transformations=transformations))
    append(intro, "intro", "Introduction", pause=700)
    if edition == EDITION_FULL:
        # Rebuild boundaries from source blocks, including for older cached plans.
        full_plan = create_speech_plan(structure, max_chars=max_chars) if structure.get("blocks") and structure.get("source") else source_plan
        body = list(full_plan.get("segments", []))
        if body and body[0].get("kind") == "title":
            body = body[1:]
        for original in body:
            segments.append({**original, "ordinal": len(segments) + 1, "section": original.get("section") or title,
                             "transformations": list(original.get("transformations", [])), "narrator_text": False})
    else:
        for heading, blocks in (("Abstract", brief["abstract"]), ("Authors’ conclusion", brief["conclusion"])):
            if not blocks:
                continue
            append(heading + ".", "heading", heading, pause=500)
            for block in blocks:
                append(str(block.get("text", "")), "body", heading,
                       [str(block["id"])] if block.get("id") else [], [int(block.get("pdf_page", 1))])
    result = {"schema": "zotero-audio-narration-plan/v1", "pipeline_version": __version__, "edition": edition,
              "document": document, "source_sha256": source_plan["source_sha256"],
              "structure_sha256": source_plan.get("structure_sha256"), "source_plan_sha256": source_plan["plan_sha256"],
              "segmentation": {"algorithm": "podcast-whole-sentence-v2", "max_chars": max_chars},
              "brief_contents": brief.get("brief_contents") if edition == EDITION_BRIEF else None,
              "segments": segments}
    result["plan_sha256"] = json_digest(result)
    return result


def _seed_source_audio(source_bundle: Path, target_bundle: Path, plan: dict[str, Any], backend: SpeechBackend) -> None:
    if not (source_bundle / "run-manifest.json").is_file():
        return
    source_manifest = load_json(source_bundle / "run-manifest.json")
    config_digest = json_digest(backend.config)
    if source_manifest.get("synthesis_config_sha256") != config_digest:
        return
    records = {record.get("text_sha256"): record for record in source_manifest.get("segments", [])}; seeded = []
    target_dir = target_bundle / "audio" / "segments"; target_dir.mkdir(parents=True, exist_ok=True)
    for segment in plan["segments"]:
        original = records.get(segment["text_sha256"])
        if not original:
            continue
        source = source_bundle / original["path"]
        if not source.is_file() or sha256_file(source) != original.get("sha256"):
            continue
        cache_key = json_digest({"text_sha256": segment["text_sha256"], "synthesis_config_sha256": config_digest})
        destination = target_dir / f"{cache_key}.wav"
        if not destination.exists():
            try: destination.hardlink_to(source)
            except OSError: shutil.copyfile(source, destination)
        seeded.append({"ordinal": segment["ordinal"], "cache_key": cache_key, "text_sha256": segment["text_sha256"],
                       "path": str(destination.relative_to(target_bundle)), "sha256": sha256_file(destination),
                       "chunks": original.get("chunks"), **validate_wav(destination)})
    if seeded:
        atomic_write_json(target_bundle / "run-manifest.json", {"schema": "zotero-audio-run-manifest/v1",
                          "pipeline_version": __version__, "source_sha256": plan["source_sha256"],
                          "plan_sha256": plan["plan_sha256"], "synthesis_config": backend.config,
                          "synthesis_config_sha256": config_digest, "status": "in_progress", "segments": seeded})


def _timing(plan: dict[str, Any], manifest: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], float]:
    records = {int(record["ordinal"]): record for record in manifest.get("segments", [])}; cues, chapters = [], []; cursor = 0.0; last = None
    for segment in plan["segments"]:
        record = records.get(int(segment["ordinal"])); duration = float(record["duration_seconds"]) if record else 0
        if duration <= 0:
            raise RuntimeError(f"missing rendered timing for segment {segment['ordinal']}")
        section = str(segment.get("section") or "Paper")
        if section != last:
            chapters.append({"start": cursor, "title": section}); last = section
        chunks = record.get("chunks") or [{"text": segment["text"], "duration_seconds": duration}]
        for chunk in chunks:
            chunk_duration = float(chunk["duration_seconds"])
            if chunk_duration <= 0:
                raise RuntimeError(f"invalid chunk timing for segment {segment['ordinal']}")
            cues.append({"start": cursor, "end": cursor + chunk_duration, "text": chunk["text"], "section": section})
            cursor += chunk_duration
        cursor += float(segment.get("pause_after_ms", 0)) / 1000
    return cues, chapters, cursor


def _episode_guid(source_sha: str, key: str, edition: str) -> str:
    return f"urn:uuid:{uuid.uuid5(uuid.NAMESPACE_URL, f'zotero-audio:{key}:{source_sha}:{edition}')}"


def _show_notes(document: dict[str, Any], authors: list[str], edition: str, license_result: dict[str, Any]) -> str:
    lines = [str(document.get("abstract") or document.get("metadata", {}).get("abstract") or f"A {edition} audio edition of this paper."),
             "", f"Authors: {', '.join(authors) if authors else 'Not supplied'}"]
    journal = document.get("journal") or document.get("publication_title")
    if journal: lines.append(f"Published in: {journal}")
    if document.get("university"): lines.append(f"Institution: {document['university']}")
    if document.get("publication_date") or document.get("publication_year"):
        lines.append(f"Publication date: {document.get('publication_date') or document.get('publication_year')}")
    if license_result.get("allowed"):
        lines.extend((f"Read the paper: {license_result['read_url']}",
                      f"Source license: {license_result['name']} — {license_result['license_url']}"))
    elif document.get("url") or document.get("doi"):
        lines.append(f"Read the paper: {document.get('url') or 'https://doi.org/' + str(document['doi'])}")
    lines.extend(("", "The authors and publisher do not sponsor or endorse this recording."))
    return "\n".join(lines)


def _safe_url(value: str) -> str | None:
    """Return an http(s) URL suitable for an HTML href, or None."""
    candidate = value.strip()
    parsed = urllib.parse.urlsplit(candidate)
    return candidate if parsed.scheme in {"http", "https"} and parsed.netloc else None


def _show_notes_html(show_notes: str, *, edition: str | None = None) -> str:
    """Render plain-text show notes as escaped paragraphs with safe links."""
    paragraphs: list[str] = []
    for line in show_notes.splitlines():
        if not line.strip():
            continue
        paired = re.match(r"^\s*Paired edition:\s*(https?://\S+)\s*$", line, re.IGNORECASE)
        if paired:
            pair_url = _safe_url(paired.group(1))
            if pair_url:
                pair_title = "Full episode" if edition != EDITION_FULL else "Brief episode"
                paragraphs.append(f'<p><a href="{html.escape(pair_url, quote=True)}">{pair_title}</a></p>')
                continue

        rendered: list[str] = []
        cursor = 0
        for match in HTTP_URL_RE.finditer(line):
            raw_url = match.group(0)
            trailing = ""
            while raw_url and raw_url[-1] in ".,;:!?)]}":
                trailing = raw_url[-1] + trailing
                raw_url = raw_url[:-1]
            rendered.append(html.escape(line[cursor:match.start()]))
            safe_url = _safe_url(raw_url)
            if safe_url:
                rendered.append(f'<a href="{html.escape(safe_url, quote=True)}">{html.escape(raw_url)}</a>')
            else:
                rendered.append(html.escape(raw_url))
            rendered.append(html.escape(trailing))
            cursor = match.end()
        rendered.append(html.escape(line[cursor:]))
        paragraphs.append(f"<p>{''.join(rendered)}</p>")
    return "".join(paragraphs)


def _copy_if_changed(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.is_file() and sha256_file(destination) == sha256_file(source):
        return
    temporary = destination.with_suffix(destination.suffix + ".tmp"); shutil.copyfile(source, temporary); temporary.replace(destination)


def _date(value: str | datetime) -> datetime:
    if isinstance(value, datetime): parsed = value
    else:
        try: parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError: parsed = parsedate_to_datetime(str(value))
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def _rfc2822(value: str | datetime) -> str:
    return format_datetime(_date(value), usegmt=True)


def _duration(seconds: float) -> str:
    total = max(0, round(seconds)); hours, remainder = divmod(total, 3600); minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def build_rss(show: dict[str, Any], episodes: Iterable[dict[str, Any]]) -> str:
    required = ("title", "description", "link", "image_url", "feed_url", "owner_email", "guid")
    missing = [key for key in required if not str(show.get(key, "")).strip()]
    if missing: raise ValueError(f"missing required show metadata: {', '.join(missing)}")
    episode_list = list(episodes)
    if not episode_list: raise ValueError("a public feed requires at least one episode")
    root = ET.Element("rss", {"version": "2.0"}); channel = ET.SubElement(root, "channel")
    for tag, value in (("title", show["title"]), ("link", show["link"]), ("description", show["description"]),
                       ("language", show.get("language", "en")), ("copyright", show.get("copyright", ""))):
        ET.SubElement(channel, tag).text = str(value)
    ET.SubElement(channel, "lastBuildDate").text = _rfc2822(max(_date(item["pub_date"]) for item in episode_list))
    ET.SubElement(channel, f"{{{ATOM_NS}}}link", {"href": str(show["feed_url"]), "rel": "self", "type": "application/rss+xml"})
    ET.SubElement(channel, f"{{{ITUNES_NS}}}author").text = str(show.get("author", show["title"]))
    ET.SubElement(channel, f"{{{ITUNES_NS}}}summary").text = str(show["description"])
    ET.SubElement(channel, f"{{{ITUNES_NS}}}explicit").text = "false"; ET.SubElement(channel, f"{{{ITUNES_NS}}}type").text = "episodic"
    ET.SubElement(channel, f"{{{ITUNES_NS}}}image", {"href": str(show["image_url"])})
    ET.SubElement(channel, f"{{{ITUNES_NS}}}category", {"text": str(show.get("category", "Science"))})
    owner = ET.SubElement(channel, f"{{{ITUNES_NS}}}owner")
    ET.SubElement(owner, f"{{{ITUNES_NS}}}name").text = str(show.get("owner_name", show.get("author", show["title"])))
    ET.SubElement(owner, f"{{{ITUNES_NS}}}email").text = str(show["owner_email"])
    ET.SubElement(channel, f"{{{PODCAST_NS}}}guid").text = str(show["guid"])
    for episode in sorted(episode_list, key=lambda item: _date(item["pub_date"]), reverse=True):
        needed = ("title", "guid", "page_url", "audio_url", "bytes", "pub_date", "duration", "image_url", "transcript_url")
        absent = [key for key in needed if not episode.get(key)]
        if absent: raise ValueError(f"episode {episode.get('guid', '?')} lacks {', '.join(absent)}")
        item = ET.SubElement(channel, "item")
        notes_html = _show_notes_html(str(episode.get("show_notes", "")), edition=episode.get("edition"))
        for tag, value in (("title", episode["title"]), ("link", episode["page_url"]),
                           ("description", notes_html)): ET.SubElement(item, tag).text = str(value)
        ET.SubElement(item, f"{{{CONTENT_NS}}}encoded").text = notes_html
        ET.SubElement(item, "guid", {"isPermaLink": "false"}).text = str(episode["guid"])
        ET.SubElement(item, "pubDate").text = _rfc2822(episode["pub_date"])
        ET.SubElement(item, "enclosure", {"url": str(episode["audio_url"]), "length": str(episode["bytes"]), "type": "audio/mp4"})
        ET.SubElement(item, f"{{{ITUNES_NS}}}duration").text = _duration(float(episode["duration"]))
        ET.SubElement(item, f"{{{ITUNES_NS}}}explicit").text = "false"; ET.SubElement(item, f"{{{ITUNES_NS}}}image", {"href": str(episode["image_url"])})
        ET.SubElement(item, f"{{{PODCAST_NS}}}transcript", {"url": str(episode["transcript_url"]), "type": "text/vtt"})
        if episode.get("transcript_html_url"):
            ET.SubElement(item, f"{{{PODCAST_NS}}}transcript", {"url": str(episode["transcript_html_url"]), "type": "text/html", "rel": "alternate"})
        if episode.get("chapters_url"):
            ET.SubElement(item, f"{{{PODCAST_NS}}}chapters", {"url": str(episode["chapters_url"]), "type": "application/json+chapters"})
        license_tag = ET.SubElement(item, f"{{{PODCAST_NS}}}license", {"url": str(episode["episode_license_url"])}); license_tag.text = "CC-BY-4.0"
    ET.indent(root); return ET.tostring(root, encoding="unicode", xml_declaration=True)


def _artifact_url(publisher: LocalPublisher, path: Path, prefix: str) -> str:
    content_type = {".m4a": "audio/mp4", ".png": "image/png", ".vtt": "text/vtt; charset=utf-8",
                    ".html": "text/html; charset=utf-8", ".json": "application/json; charset=utf-8",
                    ".md": "text/markdown; charset=utf-8"}.get(path.suffix.casefold(), "application/octet-stream")
    url = publisher.put(path, content_addressed_key(path, prefix), content_type); check = publisher.head(url)
    if check["bytes"] != path.stat().st_size or check["sha256"] != sha256_file(path) or not check["accept_ranges"]:
        raise RuntimeError(f"published artifact verification failed: {url}")
    return url


def _show(config: PodcastConfig, show: ShowConfig, image_url: str) -> dict[str, Any]:
    return {"title": show.title, "description": show.description, "link": config.base_url + "/",
            "feed_url": f"{config.base_url}/{show.slug}/feed.xml", "image_url": image_url,
            "language": config.language, "author": config.author, "owner_name": config.owner_name,
            "owner_email": config.owner_email, "category": config.category, "copyright": config.copyright, "guid": show.guid}


def _episode_page(record: dict[str, Any]) -> str:
    notes = _show_notes_html(str(record["show_notes"]), edition=record.get("edition"))
    title = html.escape(str(record["title"]))
    image_url = html.escape(str(record["image_url"]), quote=True)
    audio_url = html.escape(str(record["audio_url"]), quote=True)
    transcript_url = html.escape(str(record["transcript_url"]), quote=True)
    return ("<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
            f"<title>{title}</title><style>"
            ":root{color-scheme:light;--page:#f2efe6;--surface:#faf8f2;--ink:#16212b;--muted:#5c665f;--line:#c9c1b2;--link:#2f5bd3}"
            "*{box-sizing:border-box}body{margin:0;background:var(--page);color:var(--ink);font:18px/1.55 Georgia,serif}"
            ".page-shell{max-width:64rem;margin:0 auto;padding:clamp(2rem,7vw,5rem) clamp(1.25rem,4vw,3rem) 3rem}"
            ".episode{background:var(--surface);border:1px solid var(--line);border-radius:1.25rem;padding:clamp(1.25rem,4vw,3rem);box-shadow:0 1rem 3rem rgba(22,33,43,.07)}"
            ".episode-header{border-bottom:1px solid var(--line);padding-bottom:1.75rem;margin-bottom:2rem}"
            ".eyebrow,.footer-label{color:var(--muted);font:700 .72rem/1.3 Helvetica,sans-serif;letter-spacing:.14em;text-transform:uppercase}"
            "h1{font-size:clamp(2rem,5vw,3.5rem);line-height:1.08;margin:.75rem 0 1.5rem;max-width:18ch}"
            ".cover{display:block;width:min(100%,28rem);height:auto;border:5px solid var(--ink);border-radius:.2rem}"
            "audio{display:block;width:100%;margin:2rem 0 0}a{color:var(--link);text-decoration-thickness:.09em;text-underline-offset:.15em}"
            ".show-notes{max-width:68ch}.show-notes h2{font-size:1.35rem;margin:0 0 1rem}.show-notes p{margin:0 0 1rem}.show-notes p:last-child{margin-bottom:0}"
            ".episode-footer{display:flex;align-items:center;justify-content:space-between;gap:1rem;flex-wrap:wrap;margin-top:2rem;padding:1rem 1.25rem;border:1px solid var(--line);border-radius:1rem;background:var(--surface);box-shadow:0 .5rem 1.5rem rgba(22,33,43,.04)}"
            ".episode-footer p{margin:0}.episode-footer a{font-weight:700}.footer-label{font-size:.65rem;letter-spacing:.1em}"
            "@media(max-width:36rem){.episode-footer{align-items:flex-start;flex-direction:column}}"
            "@media(prefers-reduced-motion:reduce){*{scroll-behavior:auto!important;transition:none!important}}"
            "</style></head><body><main class=\"page-shell\"><article class=\"episode\">"
            f"<header class=\"episode-header\"><div class=\"eyebrow\">Open Paper Audio · {html.escape(str(record['edition']).upper())}</div><h1>{title}</h1>"
            f"<img class=\"cover\" src=\"{image_url}\" alt=\"Cover for {title}\"><audio controls preload=\"metadata\" src=\"{audio_url}\"></audio></header>"
            f"<section class=\"show-notes\" aria-labelledby=\"show-notes-title\"><h2 id=\"show-notes-title\">Show notes</h2>{notes}</section></article>"
            f"<footer class=\"episode-footer\" aria-label=\"Episode resources\"><p class=\"footer-label\">Episode resources</p><a href=\"{transcript_url}\">Timed transcript</a></footer>"
            "</main></body></html>\n")


def _write_site(config: PodcastConfig, manifest: dict[str, Any]) -> None:
    episodes = sorted(manifest.get("episodes", []), key=lambda item: _date(item["pub_date"]), reverse=True)
    cards = "".join(f'<article><img src="{html.escape(item["image_url"])}" alt=""><div><small>{item["edition"].upper()}</small><h2><a href="{html.escape(item["page_url"])}">{html.escape(item["title"])}</a></h2><p>{html.escape(item["author_label"])}</p></div></article>' for item in episodes)
    page = ("<!doctype html><html lang=\"en\"><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
            f"<title>{html.escape(config.site_title)}</title><style>body{{margin:0;background:#f2efe6;color:#16212b;font:18px/1.5 Georgia,serif}}main{{max-width:72rem;margin:auto;padding:6vw}}header{{border-bottom:6px solid #16212b}}article{{display:grid;grid-template-columns:9rem 1fr;gap:1.5rem;padding:1.5rem 0;border-bottom:1px solid #16212b55}}img{{width:100%}}a{{color:inherit;text-decoration-color:#2f5bd3}}small{{font:700 12px Helvetica,sans-serif;letter-spacing:.14em}}</style>"
            f"<main><header><small>OPEN PAPER AUDIO</small><h1>{html.escape(config.site_title)}</h1><p>{html.escape(config.site_description)}</p>"
            '<p><a href="brief/feed.xml">Brief RSS</a> · <a href="full/feed.xml">Full Reading RSS</a></p></header>' + cards + "</main></html>\n")
    atomic_write_text(config.public_root / "index.html", page)  # type: ignore[operator]


def _publish(config: PodcastConfig, paper_guid: str, source_sha: str, private_records: dict[str, dict[str, Any]],
             license_result: dict[str, Any]) -> tuple[list[dict[str, Any]], bool]:
    from .cover import render_cover
    if not config.public_root: raise ValueError("public_root is required when publishing is enabled")
    if config.base_url.endswith(".invalid"): raise ValueError("replace the placeholder base_url before publishing")
    publisher = LocalPublisher(config.public_root, config.base_url); manifest_path = config.state_root / "publication-manifest.json"
    manifest = load_json(manifest_path) if manifest_path.is_file() else {"schema": "zotero-audio-publication/v1", "episodes": []}
    existing = {(item["guid"], item.get("revision")): item for item in manifest.get("episodes", [])}; public_records = []
    published_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    for edition, private in private_records.items():
        prefix = f"episodes/{paper_guid}/{source_sha[:16]}/{edition}"; previous = existing.get((private["guid"], source_sha), {})
        paired_edition = EDITION_FULL if edition == EDITION_BRIEF else EDITION_BRIEF
        has_pair = paired_edition in private_records or any(
            value.get("edition") == paired_edition and value.get("source_sha256") == source_sha
            for value in existing.values())
        pair_url = f"{config.base_url}/papers/{paper_guid}/{paired_edition}/index.html" if has_pair else None
        record = {**private, "revision": source_sha, "pub_date": previous.get("pub_date", published_at),
                  "audio_url": _artifact_url(publisher, Path(private["audio"]), prefix),
                  "image_url": _artifact_url(publisher, Path(private["cover"]), prefix),
                  "transcript_url": _artifact_url(publisher, Path(private["transcript"]), prefix),
                  "transcript_html_url": _artifact_url(publisher, Path(private["transcript_html"]), prefix),
                  "chapters_url": _artifact_url(publisher, Path(private["chapters"]), prefix),
                  "markdown_url": _artifact_url(publisher, Path(private["markdown"]), prefix),
                  "episode_license_url": license_result["episode_license_url"],
                  "page_url": f"{config.base_url}/papers/{paper_guid}/{edition}/index.html", "bytes": Path(private["audio"]).stat().st_size}
        if pair_url:
            record["paired_url"] = pair_url
            record["show_notes"] = record["show_notes"] + f"\nPaired edition: {pair_url}"
        page_stage = config.state_root / "public-staging" / paper_guid / edition / "index.html"; atomic_write_text(page_stage, _episode_page(record))
        _copy_if_changed(page_stage, config.public_root / "papers" / paper_guid / edition / "index.html")
        public_records.append(record); existing[(record["guid"], source_sha)] = record
    manifest["episodes"] = sorted(existing.values(), key=lambda item: (item["guid"], item.get("revision", "")))
    images = {}
    for edition, show_config in ((EDITION_BRIEF, config.brief_show), (EDITION_FULL, config.full_show)):
        stage = config.state_root / "public-staging" / f"{edition}-show-cover.png"
        render_cover(show_config.title, edition=edition, authors=config.author, destination=stage); images[edition] = _artifact_url(publisher, stage, "shows")
    feed_stages: list[tuple[str, Path]] = []
    for edition, show_config in ((EDITION_BRIEF, config.brief_show), (EDITION_FULL, config.full_show)):
        episodes = [item for item in manifest["episodes"] if item["edition"] == edition]
        if not episodes: continue
        stage = config.state_root / "public-staging" / edition / "feed.xml"
        atomic_write_text(stage, build_rss(_show(config, show_config, images[edition]), episodes)); ET.fromstring(stage.read_text(encoding="utf-8"))
        feed_stages.append((edition, stage))
    # Build the site before the canonical feeds; feeds are the final commit.
    _write_site(config, manifest)
    rollback = config.state_root / "feed-transaction-backup"
    rollback.mkdir(parents=True, exist_ok=True)
    backups: dict[str, Path | None] = {}
    for edition, _ in feed_stages:
        canonical = config.public_root / edition / "feed.xml"
        backup = rollback / f"{edition}.xml"
        if canonical.is_file():
            shutil.copyfile(canonical, backup); backups[edition] = backup
        else:
            backups[edition] = None
    changed = False
    try:
        for edition, stage in feed_stages:
            _, feed_changed = publisher.commit_feed(stage, f"{edition}/feed.xml", config.state_root / "feed-snapshots" / edition)
            changed |= feed_changed
    except Exception:
        for edition, backup in backups.items():
            canonical = config.public_root / edition / "feed.xml"
            if backup and backup.is_file():
                canonical.parent.mkdir(parents=True, exist_ok=True); shutil.copyfile(backup, canonical)
            else:
                canonical.unlink(missing_ok=True)
        raise
    atomic_write_json(manifest_path, manifest)
    return public_records, changed


def health_check(config: PodcastConfig, *, remote: bool = False) -> dict[str, Any]:
    """Validate both canonical feeds and every URL they reference.

    Local mode checks the generated object-store mirror and its required HTTP
    header sidecars. Remote mode additionally performs public HEAD and one-byte
    range requests; it is intended for the once-daily unattended check.
    """
    if not config.public_root:
        raise ValueError("public_root is required for podcast health checks")
    publisher = LocalPublisher(config.public_root, config.base_url)
    failures: list[str] = []
    checked = 0
    for edition in EDITIONS:
        feed_path = config.public_root / edition / "feed.xml"
        if not feed_path.is_file():
            failures.append(f"missing {edition} feed")
            continue
        try:
            root = ET.fromstring(feed_path.read_text(encoding="utf-8"))
        except Exception as exc:
            failures.append(f"invalid {edition} feed: {exc}")
            continue
        if remote:
            try:
                import urllib.request
                feed_url = f"{config.base_url}/{edition}/feed.xml"
                feed_request = urllib.request.Request(feed_url, headers=REMOTE_HEALTH_HEADERS)
                with urllib.request.urlopen(feed_request, timeout=20) as response:
                    remote_feed = response.read()
                    if response.status != 200 or not remote_feed:
                        raise RuntimeError(f"feed request returned {response.status}")
                if hashlib.sha256(remote_feed).hexdigest() != sha256_file(feed_path):
                    raise RuntimeError("public feed differs from the committed local feed")
            except Exception as exc:
                failures.append(f"{edition} feed origin: {type(exc).__name__}: {exc}")
        urls = [node.attrib[attribute] for node, attribute in (
            *((node, "url") for node in root.findall(".//enclosure")),
            *((node, "href") for node in root.findall(f".//{{{ITUNES_NS}}}image")),
            *((node, "url") for node in root.findall(f".//{{{PODCAST_NS}}}transcript")),
            *((node, "url") for node in root.findall(f".//{{{PODCAST_NS}}}chapters")),
        ) if node.attrib.get(attribute)]
        for url in sorted(set(urls)):
            try:
                result = publisher.head(url)
                if not result["accept_ranges"] or result["bytes"] <= 0 or not result["content_type"]:
                    raise RuntimeError("missing length, MIME type, or byte-range support")
                if remote:
                    import urllib.request
                    request = urllib.request.Request(url, method="HEAD", headers=REMOTE_HEALTH_HEADERS)
                    with urllib.request.urlopen(request, timeout=20) as response:
                        if response.status != 200:
                            raise RuntimeError(f"HEAD returned {response.status}")
                    request = urllib.request.Request(url, headers={**REMOTE_HEALTH_HEADERS, "Range": "bytes=0-0"})
                    with urllib.request.urlopen(request, timeout=20) as response:
                        if response.status not in (200, 206):
                            raise RuntimeError(f"range request returned {response.status}")
                checked += 1
            except Exception as exc:
                failures.append(f"{url}: {type(exc).__name__}: {exc}")
    result = {"schema": "zotero-audio-health/v1", "status": "pass" if not failures else "fail",
              "checked_resources": checked, "remote": remote, "failures": failures,
              "checked_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")}
    config.state_root.mkdir(parents=True, exist_ok=True)
    atomic_write_json(config.state_root / "health-report.json", result)
    return result


def build_local_podcast(bundle: Path, private_root: Path | None = None, *, backend: SpeechBackend | None = None,
                        config: PodcastConfig | None = None, state_root: Path | None = None,
                        public_root: Path | None = None, base_url: str = "https://podcast.example.invalid",
                        selected: bool = False, license_record: dict[str, Any] | None = None,
                        metadata: dict[str, Any] | None = None, dry_run: bool = True,
                        publishing_enabled: bool = False, edition: str = "both") -> dict[str, Any]:
    """Build distinct Brief/Full editions, archive privately, then optionally publish."""
    from .cover import render_cover
    if edition not in {"both", *EDITIONS}:
        raise ValueError(f"Unknown edition: {edition}")
    bundle = bundle.expanduser().resolve(); source_plan = load_json(bundle / "speech-plan.json")
    structure_path = bundle / "structure.json"; structure = load_json(structure_path) if structure_path.is_file() else {"document": source_plan.get("document", {}), "blocks": []}
    discovered = load_bundle_metadata(bundle)
    metadata = {**discovered, **(metadata or {})}
    document = merge_document_metadata(source_plan.get("document", {}), metadata)
    if str(structure.get("document", {}).get("abstract_source", "")).startswith("pdf-"):
        document["abstract"] = structure["document"]["abstract"]
        document["abstract_source"] = structure["document"]["abstract_source"]
    structure = {**structure, "document": document}
    authors = _authors(document, metadata); year = str(document.get("publication_year") or "undated"); source_sha = str(source_plan["source_sha256"])
    zotero_key = str(metadata.get("zotero_key") or structure.get("source", {}).get("zotero_key") or bundle.name)
    paper_guid = str(uuid.uuid5(uuid.NAMESPACE_URL, f"zotero-audio:{zotero_key}"))
    if config is None:
        if private_root is None: raise ValueError("private_root or config is required")
        config = PodcastConfig(private_root.expanduser().resolve(), (state_root or bundle.parent.parent / "podcast").expanduser().resolve(),
                               public_root.expanduser().resolve() if public_root else None, base_url.rstrip("/"),
                               publishing_enabled=publishing_enabled, dry_run=dry_run)
    if license_record is None:
        license_record = license_record_from_metadata(document, source_sha)
    license_result = resolve_license(license_record, source_sha256=source_sha); brief = extract_brief(structure)
    content_qa = content_quality_gate(structure, source_plan, metadata)
    edition_names = [EDITION_FULL] + ([EDITION_BRIEF] if brief["available"] else [])
    if edition != "both":
        if edition not in edition_names:
            raise RuntimeError("Requested brief has no confidently bounded abstract")
        edition_names = [edition]
    planned_state = publication_state(selected=selected, private_ready=content_qa["status"] == "pass",
                                      license_record=license_record, source_sha256=source_sha)
    response: dict[str, Any] = {"schema": "zotero-audio-podcast-build/v1", "paper_guid": paper_guid,
        "source_sha256": source_sha, "state": planned_state, "brief": "available" if brief["available"] else brief["reason"],
        "content_qa": content_qa, "dry_run": config.dry_run, "editions": {}, "published": False, "feed_changed": False}
    if not config.dry_run and license_record:
        atomic_write_json(config.state_root / "license-evidence" / f"{source_sha}.json", {
            "schema": "zotero-audio-license-evidence/v1", "source_sha256": source_sha,
            "resolver": "strict-local-rights-v1", "record": license_record, "resolution": license_result,
        })
    if content_qa["status"] != "pass":
        if not config.dry_run:
            qa_path = config.state_root / "documents" / paper_guid / source_sha / "content-qa.json"
            atomic_write_json(qa_path, content_qa)
        return response
    if config.dry_run:
        for edition in edition_names:
            response["editions"][edition] = {"status": "planned", "title": episode_title(str(document.get("title", bundle.name)), authors, document.get("publication_year"))}
        return response
    if backend is None: raise RuntimeError("a local speech backend is required to render edition introductions")
    private_records = {}
    for edition in edition_names:
        stage = config.state_root / "documents" / paper_guid / source_sha / edition
        plan = create_edition_plan(source_plan, structure, edition, metadata=metadata, license_result=license_result,
                                   public=bool(license_result.get("allowed")))
        existing_plan = load_json(stage / "speech-plan.json") if (stage / "speech-plan.json").is_file() else {}
        stage.mkdir(parents=True, exist_ok=True); atomic_write_json(stage / "speech-plan.json", plan); atomic_write_json(stage / "narration-plan.json", plan)
        atomic_write_text(stage / "narration.md", build_markdown_transcript(plan, str(document.get("title", bundle.name))))
        cached_manifest = load_json(stage / "run-manifest.json") if (stage / "run-manifest.json").is_file() else {}
        cached_qa = load_json(stage / "qa-report.json") if (stage / "qa-report.json").is_file() else {}
        cached_audio = stage / "audio" / f"{stage.name}.m4a"
        reusable = (
            existing_plan.get("plan_sha256") == plan["plan_sha256"]
            and cached_manifest.get("status") == "complete"
            and cached_manifest.get("plan_sha256") == plan["plan_sha256"]
            and cached_manifest.get("synthesis_config_sha256") == json_digest(backend.config)
            and cached_qa.get("status") == "pass"
            and cached_audio.is_file()
            and cached_qa.get("output", {}).get("sha256") == sha256_file(cached_audio)
            and loudness_pass(cached_qa.get("checks", {}).get("loudness", {}))
        )
        if reusable:
            rendered, audio, qa = cached_manifest, cached_audio, cached_qa
        else:
            _seed_source_audio(bundle, stage, plan, backend)
            rendered, _ = synthesize_plan(stage, backend)
        cues, chapters, _ = _timing(plan, rendered)
        if not reusable:
            audio, qa = assemble_m4a(stage, chapters=chapters)
        final_loudness = qa.get("checks", {}).get("loudness", {})
        if not loudness_pass(final_loudness): raise RuntimeError(f"{edition} failed the podcast loudness gate")
        if cues[-1]["end"] > qa["checks"]["m4a_duration_seconds"] + 0.25: raise RuntimeError(f"{edition} transcript exceeds encoded audio duration")
        title = episode_title(str(document.get("title", bundle.name)), authors, document.get("publication_year"))
        private_dir = config.private_root / ("Briefs" if edition == EDITION_BRIEF else "Full Readings") / year
        safe = re.sub(r"[^\w .()\[\]—&-]+", "_", title, flags=re.UNICODE).strip(" .")[:190]
        target_audio = private_dir / f"{safe} - {'Brief' if edition == EDITION_BRIEF else 'Full Reading'} [{zotero_key}].m4a"; artifact_dir = target_audio.with_suffix(""); artifact_dir.mkdir(parents=True, exist_ok=True)
        _copy_if_changed(audio, target_audio); cover, transcript = artifact_dir / "cover.png", artifact_dir / "transcript.vtt"
        transcript_html, chapters_path, markdown = artifact_dir / "transcript.html", artifact_dir / "chapters.json", artifact_dir / "transcript.md"
        render_cover(str(document.get("title", bundle.name)), edition=edition, authors=", ".join(authors), year=document.get("publication_year"), destination=cover)
        build_transcript(cues, transcript); build_transcript_html(cues, title, transcript_html, attribution=", ".join(authors)); chapter_json(chapters, chapters_path)
        atomic_write_text(markdown, build_markdown_transcript(plan, title))
        record = {"edition": edition, "guid": _episode_guid(source_sha, zotero_key, edition), "title": title,
                  "paper_title": str(document.get("title", bundle.name)), "authors": authors, "author_label": spoken_authors(authors),
                  "publication_year": document.get("publication_year"), "duration": float(qa["checks"]["m4a_duration_seconds"]),
                  "audio": str(target_audio), "cover": str(cover), "transcript": str(transcript),
                  "transcript_html": str(transcript_html), "chapters": str(chapters_path), "markdown": str(markdown),
                  "show_notes": _show_notes(document, authors, edition, license_result),
                  "source_sha256": source_sha, "source_license": license_result, "read_url": license_result.get("read_url") or document.get("url"),
                  "doi": document.get("doi"),
                  "plan_sha256": plan["plan_sha256"], "audio_sha256": sha256_file(target_audio), "loudness": final_loudness}
        atomic_write_json(artifact_dir / "episode.json", record); private_records[edition] = record
        response["editions"][edition] = {"status": "private_ready", **record}
    state_record = {"schema": "zotero-audio-paper-publication/v1", "paper_guid": paper_guid, "zotero_key": zotero_key,
                    "source_sha256": source_sha, "state": planned_state, "license": license_result,
                    "editions": {key: {"guid": value["guid"], "audio_sha256": value["audio_sha256"]} for key, value in private_records.items()}}
    state_path = config.state_root / "documents" / paper_guid / source_sha / "episode.json"
    if state_path.is_file():
        old_state = load_json(state_path)
        state_record["editions"] = {**old_state.get("editions", {}), **state_record["editions"]}
    atomic_write_json(state_path, state_record)
    if planned_state == "public_ready" and config.publishing_enabled:
        public_records, changed = _publish(config, paper_guid, source_sha, private_records, license_result)
        response.update({"published": True, "feed_changed": changed, "public_editions": public_records, "state": "published"})
        state_record["state"] = "published"; atomic_write_json(state_path, state_record)
    return response
