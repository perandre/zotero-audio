"""Publish existing audio files to the podcast feeds without rendering."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .audio import inspect_m4a
from .cover import copy_podcast_cover
from .literature import is_report, report_organisation
from .podcast import (
    EDITION_FULL,
    EDITIONS,
    LocalPublisher,
    _artifact_url,
    _episode_guid,
    _show,
    _show_notes,
    build_rss,
    episode_title,
    resolve_license,
)
from .util import atomic_write_json, atomic_write_text, load_json, sha256_file
from .zotero import license_record_from_metadata


BRIEFS_USER_AUTHORIZED = "user_authorized"


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _published_sources(state_root: Path) -> set[tuple[str, str]]:
    result: set[tuple[str, str]] = set()
    for path in state_root.rglob("episode.json"):
        try:
            record = load_json(path)
        except (OSError, ValueError):
            continue
        if record.get("state") == "published" and record.get("zotero_key") and record.get("source_sha256"):
            result.add((str(record["zotero_key"]), str(record["source_sha256"])))
    return result


def _authors(item: dict[str, Any]) -> list[str]:
    value = item.get("authors")
    if isinstance(value, list):
        return [str(author).strip() for author in value if str(author).strip()]
    raw = str(value or item.get("author") or "").strip()
    return [part.strip() for part in raw.split(";") if part.strip()]


def _license_record(config, item: dict[str, Any]) -> dict[str, Any] | None:
    source_sha = str(item.get("source_sha256") or "")
    evidence_path = config.state_root / "license-evidence" / f"{source_sha}.json"
    if evidence_path.is_file():
        evidence = load_json(evidence_path)
        record = evidence.get("record")
        if isinstance(record, dict):
            return record
    return license_record_from_metadata(item, source_sha) if source_sha else None


def _minimal_record(record: dict[str, Any], *, published_at: str,
                    allow_unlicensed_brief: bool = False) -> dict[str, Any] | None:
    audio = Path(str(record.get("audio") or "")).expanduser()
    license_value = record.get("source_license")
    if not audio.is_file():
        return None
    if not isinstance(license_value, dict):
        license_value = {}
    explicit_block = license_value.get("embargoed") or license_value.get("conflict") or license_value.get("reason") in {
        "source-is-embargoed", "conflicting-license-evidence"
    }
    allowed = bool(license_value.get("allowed"))
    if not allowed and (not allow_unlicensed_brief or explicit_block):
        return None
    read_url = str(record.get("read_url") or "").strip()
    if not read_url:
        return None
    duration = record.get("duration")
    if not duration:
        duration = inspect_m4a(audio)["duration_seconds"]
    source_sha = str(record.get("source_sha256") or "")
    if not source_sha:
        return None
    return {
        "edition": str(record.get("edition") or ""),
        "guid": str(record.get("guid") or ""),
        "revision": source_sha,
        "title": str(record.get("title") or audio.stem),
        "paper_title": record.get("paper_title"),
        "authors": record.get("authors") or [],
        "author_label": record.get("author_label"),
        "publication_year": record.get("publication_year"),
        "brief_contents": record.get("brief_contents"),
        "duration": float(duration),
        "audio": str(audio),
        "audio_sha256": sha256_file(audio),
        "bytes": audio.stat().st_size,
        "show_notes": str(record.get("show_notes") or ""),
        "source_sha256": source_sha,
        "source_license": license_value,
        "read_url": read_url,
        "doi": record.get("doi"),
        "page_url": str(record.get("page_url") or read_url),
        "pub_date": str(record.get("pub_date") or published_at),
        "episode_license_url": str(license_value.get("episode_license_url") or ""),
        "show_artwork_only": True,
        "publication_basis": "license" if allowed else "user-authorized-brief",
    }


def _existing_artifacts(config, existing: dict[tuple[str, str], dict[str, Any]]) -> list[dict[str, Any]]:
    published = _published_sources(config.state_root)
    candidates: list[dict[str, Any]] = []
    for path in config.private_root.rglob("episode.json"):
        try:
            record = load_json(path)
        except (OSError, ValueError):
            continue
        if record.get("edition") not in EDITIONS:
            continue
        key = (str(record.get("guid") or ""), str(record.get("source_sha256") or ""))
        if not key[0] or key in existing:
            continue
        brief_override = (
            record.get("edition") == "brief"
            and str(getattr(config, "briefs_publication_policy", "license_required")) == BRIEFS_USER_AUTHORIZED
        )
        if not brief_override and not any(source_sha == key[1] for _, source_sha in published):
            # Published state is the durable publication-intent record. Older
            # artifact records that are already in the publication manifest
            # were handled above and need no further approval.
            continue
        candidate = _minimal_record(record, published_at=_now(), allow_unlicensed_brief=brief_override)
        if candidate:
            candidates.append(candidate)
    return candidates


def _batch_full_candidates(config, batch_manifest: dict[str, Any],
                           existing: dict[tuple[str, str], dict[str, Any]]) -> list[dict[str, Any]]:
    destination = Path(str(batch_manifest.get("destination") or "")).expanduser().resolve()
    approved = _published_sources(config.state_root)
    candidates: list[dict[str, Any]] = []
    for item in batch_manifest.get("items", []):
        if item.get("status") != "complete" or not item.get("metadata_finalized"):
            continue
        source_sha = str(item.get("source_sha256") or "")
        key = str(item.get("zotero_key") or "")
        if not source_sha or not key or not (item.get("podcast_selected") or (key, source_sha) in approved):
            continue
        audio = destination / str(item.get("output_file") or "")
        if not audio.is_file() or item.get("output_sha256") != sha256_file(audio):
            continue
        license_record = _license_record(config, item)
        license_result = resolve_license(license_record, source_sha256=source_sha)
        if not license_result.get("allowed"):
            continue
        authors = _authors(item)
        document = dict(item)
        title = episode_title(
            str(item.get("title") or audio.stem),
            authors,
            item.get("publication_year"),
            institution=report_organisation(document) if is_report(document) else None,
        )
        paper_guid = str(uuid.uuid5(uuid.NAMESPACE_URL, f"zotero-audio:{key}"))
        guid = _episode_guid(source_sha, key, EDITION_FULL)
        manifest_key = (guid, source_sha)
        if manifest_key in existing:
            continue
        read_url = str(license_result.get("read_url") or "").strip()
        if not read_url:
            continue
        candidates.append({
            "edition": EDITION_FULL,
            "guid": guid,
            "revision": source_sha,
            "paper_guid": paper_guid,
            "zotero_key": key,
            "title": title,
            "paper_title": item.get("title"),
            "authors": authors,
            "author_label": ", ".join(authors),
            "publication_year": item.get("publication_year"),
            "duration": float(item.get("duration_seconds") or inspect_m4a(audio)["duration_seconds"]),
            "audio": str(audio),
            "audio_sha256": str(item["output_sha256"]),
            "bytes": audio.stat().st_size,
            "show_notes": _show_notes(document, authors, EDITION_FULL, license_result),
            "source_sha256": source_sha,
            "source_license": license_result,
            "read_url": read_url,
            "doi": item.get("doi"),
            "page_url": read_url,
            "pub_date": _now(),
            "episode_license_url": str(license_result.get("episode_license_url") or ""),
            "show_artwork_only": True,
        })
    return candidates


def publish_existing_audio(config, *, batch_manifest_path: Path) -> dict[str, Any]:
    """Publish already-rendered files and metadata without invoking TTS."""
    if config.dry_run or not config.publishing_enabled:
        return {"status": "disabled", "added": 0, "skipped": 0}
    if not config.public_root:
        raise ValueError("public_root is required for feed-only publishing")
    manifest_path = config.state_root / "publication-manifest.json"
    manifest = load_json(manifest_path) if manifest_path.is_file() else {
        "schema": "zotero-audio-publication/v1", "episodes": []
    }
    existing = {
        (str(item.get("guid") or ""), str(item.get("revision") or "")): item
        for item in manifest.get("episodes", [])
    }
    batch_manifest = load_json(batch_manifest_path.expanduser().resolve())
    candidates: list[dict[str, Any]] = []
    candidate_keys = set(existing)
    for candidate in _existing_artifacts(config, existing) + _batch_full_candidates(config, batch_manifest, existing):
        key = (candidate["guid"], candidate["revision"])
        if key not in candidate_keys:
            candidates.append(candidate)
            candidate_keys.add(key)
    publisher = LocalPublisher(config.public_root, config.base_url)
    added = 0
    published_at = _now()
    for candidate in candidates:
        audio = Path(candidate["audio"])
        if not audio.is_file():
            continue
        paper_guid = str(candidate.get("paper_guid") or uuid.uuid5(
            uuid.NAMESPACE_URL, f"zotero-audio:{candidate.get('zotero_key', candidate['guid'])}"
        ))
        prefix = f"episodes/{paper_guid}/{candidate['source_sha256'][:16]}/{candidate['edition']}"
        record = {
            **candidate,
            "pub_date": candidate.get("pub_date") or published_at,
            "audio_url": _artifact_url(publisher, audio, prefix),
        }
        existing[(record["guid"], record["revision"])] = record
        added += 1

    # Existing episode artwork is no longer referenced by the feeds. The
    # show-level image is the only artwork emitted for feed-only records. The
    # feed also carries only the required episode fields; old sidecar URLs are
    # deliberately left out rather than making transcript/chapter generation
    # part of publication.
    for record in existing.values():
        record["show_artwork_only"] = True
        for optional_key in ("image_url", "transcript_url", "transcript_html_url", "chapters_url"):
            record.pop(optional_key, None)
    manifest["episodes"] = sorted(existing.values(), key=lambda item: (item["guid"], item.get("revision", "")))

    images: dict[str, str] = {}
    for edition in EDITIONS:
        if not any(item.get("edition") == edition for item in manifest["episodes"]):
            continue
        stage = config.state_root / "public-staging" / f"{edition}-show-cover.png"
        copy_podcast_cover(stage, edition=edition)
        images[edition] = _artifact_url(publisher, stage, "shows")

    changed = False
    rollback = config.state_root / "feed-transaction-backup"
    rollback.mkdir(parents=True, exist_ok=True)
    backups: dict[str, Path | None] = {}
    feed_stages: list[tuple[str, Path]] = []
    for edition in EDITIONS:
        episodes = [item for item in manifest["episodes"] if item.get("edition") == edition]
        if not episodes:
            continue
        stage = config.state_root / "public-staging" / edition / "feed.xml"
        atomic_write_text(stage, build_rss(_show(config, getattr(config, f"{edition}_show"), images[edition]), episodes))
        import xml.etree.ElementTree as ET
        ET.fromstring(stage.read_text(encoding="utf-8"))
        feed_stages.append((edition, stage))

    for edition, _ in feed_stages:
        canonical = config.public_root / edition / "feed.xml"
        backup = rollback / f"{edition}.xml"
        if canonical.is_file():
            backup.write_bytes(canonical.read_bytes())
            backups[edition] = backup
        else:
            backups[edition] = None
    try:
        for edition, stage in feed_stages:
            _, feed_changed = publisher.commit_feed(
                stage, f"{edition}/feed.xml", config.state_root / "feed-snapshots" / edition
            )
            changed |= feed_changed
    except Exception:
        for edition, backup in backups.items():
            canonical = config.public_root / edition / "feed.xml"
            if backup and backup.is_file():
                canonical.parent.mkdir(parents=True, exist_ok=True)
                canonical.write_bytes(backup.read_bytes())
            else:
                canonical.unlink(missing_ok=True)
        raise
    atomic_write_json(manifest_path, manifest)
    return {"status": "published", "added": added, "episodes": len(manifest["episodes"]), "feed_changed": changed}
