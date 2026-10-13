#!/usr/bin/env python3
"""Backfill source-grounded descriptions on already published podcast episodes.

Preview by default. This command never generates audio or changes publication
eligibility. Apply writes only the publication manifest, existing episode pages,
the site index and the two feed XML files.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import uuid
from copy import copy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from zotero_audio.episode_descriptions import (  # noqa: E402
    edition_description, normalize_abstract, select_description_abstract,
)
from zotero_audio.podcast import (  # noqa: E402
    EDITION_BRIEF, EDITION_FULL, ITUNES_NS, LocalPublisher, _episode_page, _show,
    _write_site, build_rss, load_podcast_config,
)
from zotero_audio.literature import EVIDENCE_FIELDS  # noqa: E402
from zotero_audio.publish_sync import upload_keys  # noqa: E402
from zotero_audio.util import atomic_write_json, atomic_write_text, load_json  # noqa: E402
from zotero_audio.zotero_local import ZoteroLocalAPI, ZoteroLocalAPIError  # noqa: E402


DISCLAIMER = "The authors and publisher do not sponsor or endorse this recording."
KNOWN_NOTE_PREFIXES = (
    "Authors:", "Literature type:", "Issuing organisation:", "Report type:",
    "Report series:", "Report number:", "Peer review:", "Published in:",
    "Institution:", "Publication date:", "Read the paper:", "Read the report:",
    "Source license:", "Abstract source:", "Narration:", DISCLAIMER, "Paired edition:",
    *(f"{label}:" for label in EVIDENCE_FIELDS.values()),
)


def is_published_episode(record: dict[str, Any]) -> bool:
    """Require a complete existing feed identity and enclosure before touching it."""
    return bool(
        record.get("guid") and record.get("pub_date") and record.get("audio_url")
        and record.get("bytes") and record.get("edition") in {EDITION_BRIEF, EDITION_FULL}
    )


def _structure_for(config, record: dict[str, Any]) -> dict[str, Any]:
    """Read source-matched local metadata; never accept a structure for another PDF."""
    key = str(record.get("zotero_key") or "")
    expected_sha = str(record.get("source_sha256") or "")
    roots = [config.state_root.parent / "library"]
    roots.append(config.state_root.parent / "full-library" / "bundles")
    candidates: list[Path] = []
    if key:
        candidates.extend(root / key / "structure.json" for root in roots)
    # Legacy full-library folders may encode the Zotero key in the directory name.
    if key and roots[1].is_dir():
        candidates.extend(path / "structure.json" for path in roots[1].iterdir()
                          if path.is_dir() and path.name.endswith(f"[{key}]"))
    for path in candidates:
        try:
            value = load_json(path)
        except (OSError, ValueError):
            continue
        document = value.get("document", {}) if isinstance(value, dict) else {}
        source = value.get("source", {}) if isinstance(value, dict) else {}
        source_sha = str(value.get("source_sha256") or source.get("sha256") or "")
        if not expected_sha or source_sha != expected_sha:
            continue
        return value
    return {}


def _metadata(config, record: dict[str, Any], structure: dict[str, Any], api: ZoteroLocalAPI) -> dict[str, Any]:
    key = str(record.get("zotero_key") or "")
    current: dict[str, Any] = {}
    if key:
        try:
            value = api.metadata(key)
            if value:
                current = value
        except ZoteroLocalAPIError:
            pass
        if not current:
            try:
                parent = api.item(key)
                current = {"abstract": parent.get("abstractNote"), "doi": parent.get("DOI"),
                           "title": parent.get("title"), "url": parent.get("url"),
                           "journal": parent.get("publicationTitle"), "parent_key": key}
            except ZoteroLocalAPIError:
                pass
    document = structure.get("document", {}) if isinstance(structure, dict) else {}
    value = document.get("metadata", {}) if isinstance(document, dict) else {}
    return ({**value, **current} if isinstance(value, dict) else current)


def _old_note_abstract(notes: str) -> str:
    return notes.strip().split("\n\n", 1)[0].strip()


def _legacy_note_rejection_reason(record: dict[str, Any], known_abstracts: set[str]) -> str | None:
    """Recognize old generated notes, returning a safe aggregate reason if uncertain."""
    notes = str(record.get("show_notes") or "").strip()
    if not notes:
        return "empty"
    if record.get("generated_show_notes") and notes == str(record["generated_show_notes"]):
        return None
    normalized_first = normalize_abstract(_old_note_abstract(notes))
    if normalized_first not in known_abstracts:
        return "first_paragraph_not_source_matched"
    lines = [line.strip() for line in notes.splitlines() if line.strip()]
    authors = record.get("authors") or []
    expected_authors = ", ".join(str(author) for author in authors if author)
    if not any(line == f"Authors: {expected_authors or 'Not supplied'}" for line in lines):
        return "authors_row_unrecognized"
    if lines[-1] not in {DISCLAIMER} and not lines[-1].startswith("Paired edition:"):
        return "ending_unrecognized"
    if not any(line == DISCLAIMER for line in lines) and not any(line.startswith("Paired edition:") for line in lines):
        return "disclaimer_and_pair_missing"
    remainder = notes.partition("\n\n")[2]
    if any(not line.strip().startswith(KNOWN_NOTE_PREFIXES) for line in remainder.splitlines() if line.strip()):
        return "unrecognized_metadata_or_manual_row"
    paired_line = next((line for line in lines if line.startswith("Paired edition:")), None)
    paired_url = str(record.get("paired_url") or "")
    if paired_line and paired_url and paired_line != f"Paired edition: {paired_url}":
        return "paired_url_mismatch"
    return None


def _looks_like_legacy_generated(record: dict[str, Any], known_abstracts: set[str]) -> bool:
    return _legacy_note_rejection_reason(record, known_abstracts) is None


def _known_abstracts(record: dict[str, Any], metadata: dict[str, Any], structure: dict[str, Any]) -> set[str]:
    values: list[Any] = []
    if (record.get("generated_description_abstract") is not None
            and not record.get("description_is_manual")
            and normalize_abstract(record.get("description_abstract"))
            == normalize_abstract(record.get("generated_description_abstract"))):
        values.append(record.get("description_abstract"))
    document = structure.get("document", {}) if isinstance(structure, dict) else {}
    if isinstance(document, dict):
        values.append(document.get("metadata", {}).get("abstract") if isinstance(document.get("metadata"), dict) else None)
        # The old notes template used document.abstract regardless of
        # provenance. This source-hash-matched value is used only to recognize
        # old generated text; the replacement comes from the clean selector.
        values.append(document.get("abstract"))
    document = structure.get("document", {}) if isinstance(structure, dict) else {}
    literature_type = record.get("literature_type") or (document.get("literature_type") if isinstance(document, dict) else "")
    noun = "report" if str(literature_type or "").casefold() == "report" else "paper"
    values.append(f"A {record.get('edition') or EDITION_FULL} audio edition of this {noun}.")
    return {normalized for value in values if (normalized := normalize_abstract(value))}


def _document(record: dict[str, Any], structure: dict[str, Any], metadata: dict[str, Any]) -> dict[str, Any]:
    source_document = structure.get("document", {}) if isinstance(structure, dict) else {}
    result = dict(source_document) if isinstance(source_document, dict) else {}
    for field in (
        "paper_title", "authors", "doi", "journal", "publication_date", "publication_year",
        "read_url", "source_license", "episode_license_url", "brief_contents",
    ):
        if record.get(field) is not None:
            target = {"paper_title": "title", "read_url": "url"}.get(field, field)
            result[target] = record[field]
    result.update({key: value for key, value in metadata.items() if value not in (None, "", [])})
    return result


def _populate_page_metadata(record: dict[str, Any], document: dict[str, Any], metadata: dict[str, Any]) -> None:
    for field in ("journal", "publication_date", "publication_year", "institution"):
        value = record.get(field) or document.get(field) or metadata.get(field)
        if value:
            record[field] = value
    brief_contents = str(record.get("brief_contents") or "").strip() or None
    if record.get("edition") == EDITION_BRIEF and not brief_contents:
        record["edition_description"] = "This is a Brief audio edition."
    else:
        record["edition_description"] = edition_description(str(record.get("edition") or EDITION_FULL), brief_contents)
    read_url = record.get("read_url") or document.get("url") or metadata.get("url")
    doi = record.get("doi") or document.get("doi") or metadata.get("doi")
    if not read_url and doi:
        read_url = f"https://doi.org/{doi}"
    if read_url:
        record["read_url"] = str(read_url)
    # Old notes sometimes retain metadata omitted from the current source cache.
    for line in str(record.get("show_notes") or "").splitlines():
        for prefix, field in (("Published in:", "journal"), ("Publication date:", "publication_date"), ("Institution:", "institution")):
            if line.startswith(prefix) and not record.get(field):
                record[field] = line[len(prefix):].strip()


def _episode_public_key(record: dict[str, Any]) -> str | None:
    edition, paper_guid = str(record.get("edition") or ""), str(record.get("paper_guid") or "")
    if edition not in {EDITION_BRIEF, EDITION_FULL}:
        return None
    try:
        if str(uuid.UUID(paper_guid)) != paper_guid.casefold():
            return None
    except (ValueError, AttributeError):
        return None
    return f"papers/{paper_guid}/{edition}/index.html"


def _upgrade_legacy_notes(record: dict[str, Any], abstract: str, source: str) -> str:
    """Change only the abstract/edition framing and add the AI disclosure.

    The existing author, publication, license, paired-edition and disclaimer
    rows remain byte-for-byte present so older catalog metadata is retained.
    """
    notes = str(record.get("show_notes") or "").strip()
    first, separator, remainder = notes.partition("\n\n")
    # Remove only previous machine-generated framing. Attribution and source
    # rows remain untouched.
    generated_lines = {
        "This is a Brief audio edition.",
        "This Full Reading presents the paper's text as an audio edition.",
    }
    remainder_lines = [line for line in remainder.splitlines()
                       if line.strip() not in generated_lines
                       and not line.strip().startswith("Abstract source:")]
    remainder = "\n".join(remainder_lines).strip("\n")
    # A rejected or unavailable source abstract must not leave the old PDF
    # front matter at the start of the browsing description.
    current_abstract = abstract
    brief_contents = record.get("brief_contents")
    if record.get("edition") == EDITION_BRIEF and not brief_contents:
        edition_line = "This is a Brief audio edition."
    else:
        edition_line = edition_description(str(record.get("edition") or EDITION_FULL), brief_contents)
    lines = ([current_abstract, ""] if current_abstract else []) + [edition_line]
    lines.extend(("", f"Abstract source: {source or 'unavailable'}"))
    if separator and remainder:
        lines.extend(("", remainder))
    upgraded = "\n".join(lines)
    if "Narration: Read verbatim by an AI-generated voice." not in upgraded:
        marker = "\nThe authors and publisher do not sponsor or endorse this recording."
        if marker in upgraded:
            upgraded = upgraded.replace(marker, "\nNarration: Read verbatim by an AI-generated voice." + marker, 1)
        else:
            upgraded += "\n\nNarration: Read verbatim by an AI-generated voice."
    return upgraded


def enrich_records(config, records: list[dict[str, Any]], *, fetch_zotero: bool = True,
                   allow_external: bool = True) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Return changed record copies and source counts; performs read-only source lookups."""
    api = ZoteroLocalAPI() if fetch_zotero else None
    output: list[dict[str, Any]] = []
    counts = {"zotero": 0, "doi-metadata": 0, "pdf-extraction": 0, "unavailable": 0,
              "manual_notes_preserved": 0, "legacy_notes_updated": 0,
              "legacy_note_rejections": {}}
    doi_cache: dict[tuple[str, str], tuple[str, str]] = {}
    for original in records:
        if not is_published_episode(original):
            output.append(dict(original))
            continue
        record = dict(original)
        structure = _structure_for(config, record)
        metadata = _metadata(config, record, structure, api) if api else (
            structure.get("document", {}).get("metadata", {}) if structure else {})
        if not isinstance(metadata, dict):
            metadata = {}
        known = _known_abstracts(record, metadata, structure)
        document = _document(record, structure, metadata)
        _populate_page_metadata(record, document, metadata)
        source_sha = str(record.get("source_sha256") or "")
        doi = str(document.get("doi") or metadata.get("doi") or "").strip().casefold()
        cached = {"description_abstract": record.get("description_abstract"),
                  "description_abstract_source": record.get("description_abstract_source"),
                  "generated_description_abstract": record.get("generated_description_abstract"),
                  "description_is_manual": record.get("description_is_manual"),
                  "source_sha256": source_sha}
        cache_key = (doi, source_sha)
        if doi and cache_key in doi_cache and not cached.get("description_abstract"):
            cached_text, cached_source = doi_cache[cache_key]
            cached.update(description_abstract=cached_text,
                          description_abstract_source=cached_source,
                          generated_description_abstract=cached_text)
        selection_document = dict(document)
        selection_metadata = dict(metadata)
        if not allow_external:
            selection_document.pop("doi", None)
            selection_metadata.pop("doi", None)
        selected = select_description_abstract(
            selection_document, metadata=selection_metadata, structure=structure,
            cached=cached,
            source_sha256=source_sha,
        )
        source = selected["description_abstract_source"]
        if doi:
            # An empty result still records that all permitted DOI sources were
            # checked, avoiding duplicate Crossref/DataCite/arXiv calls for a pair.
            doi_cache[cache_key] = (selected["description_abstract"], source)
        counts[source] = counts.get(source, 0) + 1
        previous_description = original.get("description_abstract")
        previous_generated = original.get("generated_description_abstract")
        is_manual_description = bool(original.get("description_is_manual")) or (
            bool(previous_description) and previous_generated is not None
            and normalize_abstract(previous_description) != normalize_abstract(previous_generated)
        ) or (bool(previous_description) and previous_generated is None)
        record.update(selected)
        record["description_is_manual"] = is_manual_description
        if not is_manual_description:
            record["generated_description_abstract"] = selected["description_abstract"]
        _populate_page_metadata(record, document, metadata)
        old_notes = str(record.get("show_notes") or "")
        legacy_reason = _legacy_note_rejection_reason(original, known)
        marked_generated = bool(record.get("generated_show_notes")) and old_notes == str(record["generated_show_notes"])
        recognized_legacy = legacy_reason is None
        counts["legacy_note_rejections"][legacy_reason or "recognized"] = counts["legacy_note_rejections"].get(legacy_reason or "recognized", 0) + 1
        already_current = (
            marked_generated
            and normalize_abstract(original.get("description_abstract"))
            == normalize_abstract(selected["description_abstract"])
            and original.get("description_abstract_source", "unavailable") == source
        )
        if marked_generated and already_current:
            # The persisted generated snapshot is the idempotence boundary.
            # Re-parsing it as legacy text would promote the edition line to an
            # abstract when the selected source is unavailable.
            generated = old_notes
        elif marked_generated or recognized_legacy:
            generated = _upgrade_legacy_notes(original, selected["description_abstract"], source)
            record["show_notes"] = generated
            record["generated_show_notes"] = generated
            counts["legacy_notes_updated"] += 1
        elif old_notes:
            record["description_is_manual"] = True
            counts["manual_notes_preserved"] += 1
        output.append(record)
    return output, counts


def _cover_from_feed(path: Path) -> str | None:
    try:
        root = ET.parse(path).getroot()
        image = root.find(f"./channel/{{{ITUNES_NS}}}image")
        return image.get("href") if image is not None else root.findtext("./channel/image/url")
    except (OSError, ET.ParseError):
        return None


def _snapshot(config, manifest_path: Path, keys: set[str]) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    backup = config.state_root / "description-backups" / stamp
    backup.mkdir(parents=True, exist_ok=False)
    shutil.copy2(manifest_path, backup / "publication-manifest.json")
    for key in sorted(keys):
        source = config.public_root / key
        if source.is_file():
            target = backup / key
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            sidecar = source.with_suffix(source.suffix + ".headers.json")
            if sidecar.is_file():
                shutil.copy2(sidecar, target.with_suffix(target.suffix + ".headers.json"))
    return backup


def _restore_snapshot(config, manifest_path: Path, backup: Path, keys: set[str]) -> None:
    shutil.copy2(backup / "publication-manifest.json", manifest_path)
    for key in keys:
        target = config.public_root / key
        saved = backup / key
        saved_sidecar = backup / (key + ".headers.json")
        sidecar = target.with_suffix(target.suffix + ".headers.json")
        if saved.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(saved, target)
        else:
            target.unlink(missing_ok=True)
        if saved_sidecar.is_file():
            shutil.copy2(saved_sidecar, sidecar)
        else:
            sidecar.unlink(missing_ok=True)


def _commit_local_outputs(config, manifest_path: Path, manifest: dict[str, Any],
                          rendered_files: dict[str, Path], staged_feeds: dict[str, Path],
                          keys_to_backup: set[str]) -> Path:
    """Back up and atomically roll back local pages, feed files and manifest."""
    backup = _snapshot(config, manifest_path, keys_to_backup)
    publisher = LocalPublisher(config.public_root, config.base_url)
    try:
        for key, stage in rendered_files.items():
            target = config.public_root / key
            atomic_write_text(target, stage.read_text(encoding="utf-8"))
            atomic_write_json(target.with_suffix(target.suffix + ".headers.json"), {
                "content_type": "text/html; charset=utf-8",
                "cache_control": "public, max-age=300", "accept_ranges": "bytes",
            })
        for key, stage in staged_feeds.items():
            publisher.commit_feed(stage, key, config.state_root / "feed-snapshots" / Path(key).parent)
        atomic_write_json(manifest_path, manifest)
    except Exception:
        _restore_snapshot(config, manifest_path, backup, keys_to_backup)
        raise
    return backup


def _assert_publication_identity_unchanged(before: list[dict[str, Any]], after: list[dict[str, Any]]) -> None:
    fields = ("guid", "audio_url", "bytes", "pub_date", "revision", "source_sha256")
    if len(before) != len(after):
        raise AssertionError("description backfill changed the number of publication records")
    for old, new in zip(before, after):
        for field in fields:
            if old.get(field) != new.get(field):
                raise AssertionError(f"description backfill changed publication identity field {field}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--apply", action="store_true", help="write local manifest, pages, index and feeds")
    parser.add_argument("--sync", action="store_true", help="upload pages, index and feeds to R2 after applying")
    parser.add_argument("--no-network", action="store_true", help="use only the saved publication manifest and local metadata")
    args = parser.parse_args(argv)
    if args.sync and not args.apply:
        parser.error("--sync requires --apply")
    config = load_podcast_config(args.config.expanduser().resolve())
    if not config.public_root or (args.sync and not config.r2_bucket):
        parser.error("configured public_root is required; --sync also requires r2_bucket")
    manifest_path = config.state_root / "publication-manifest.json"
    original = load_json(manifest_path)
    if not isinstance(original, dict) or not isinstance(original.get("episodes"), list):
        raise ValueError("publication manifest has an unsupported shape")
    records, counts = enrich_records(config, original["episodes"], fetch_zotero=not args.no_network,
                                     allow_external=not args.no_network)
    _assert_publication_identity_unchanged(original["episodes"], records)
    manifest = {**original, "episodes": records}
    page_keys: set[str] = set()
    for record in records:
        if not is_published_episode(record):
            continue
        key = _episode_public_key(record)
        if key:
            page_keys.add(key)
    all_upload_keys = set(page_keys) | {"index.html", "brief/feed.xml", "full/feed.xml"}
    if args.apply:
        stage_root = config.state_root / "public-staging" / "description-backfill"
        rendered_pages: dict[str, Path] = {}
        for record in records:
            if not is_published_episode(record):
                continue
            key = _episode_public_key(record)
            if not key:
                continue
            stage = stage_root / key
            atomic_write_text(stage, _episode_page(record))
            rendered_pages[key] = stage
        cover_url = _cover_from_feed(config.public_root / "full" / "feed.xml") or _cover_from_feed(config.public_root / "brief" / "feed.xml")
        site_config = copy(config)
        site_config.public_root = stage_root / "site"
        _write_site(site_config, manifest, cover_url=cover_url)
        rendered_pages["index.html"] = site_config.public_root / "index.html"
        publisher = LocalPublisher(config.public_root, config.base_url)
        staged_feeds: dict[str, Path] = {}
        for edition, show_config in ((EDITION_BRIEF, config.brief_show), (EDITION_FULL, config.full_show)):
            selected_records = [record for record in records if is_published_episode(record) and record.get("edition") == edition]
            if not selected_records:
                continue
            show_image = _cover_from_feed(config.public_root / f"{edition}" / "feed.xml") or cover_url
            stage = stage_root / f"{edition}.xml"
            atomic_write_text(stage, build_rss(_show(config, show_config, show_image), selected_records))
            ET.parse(stage)
            staged_feeds[f"{edition}/feed.xml"] = stage
        backup = _commit_local_outputs(config, manifest_path, manifest, rendered_pages,
                                       staged_feeds, all_upload_keys)
        print(f"Backup saved under {backup}")
        if args.sync:
            upload_keys(config.public_root, config.r2_bucket, config.state_root / "r2-sync-manifest.json", all_upload_keys)
    print(json.dumps({"published_records": len(records), "source_counts": counts,
                      "apply": bool(args.apply), "sync": bool(args.sync),
                      "upload_targets": len(all_upload_keys)}, sort_keys=True))
    if not args.apply:
        print("Preview only. Pass --apply to write local metadata and rendered pages/feeds.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
