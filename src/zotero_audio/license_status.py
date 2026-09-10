"""Read-only license evaluation for Zotero's automatic saved search.

Zotero supplies live item fields and attachment paths. Only Zotero itself writes
tags/Extra; this module never modifies its database or publication state.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from .podcast import resolve_license
from .util import load_json, sha256_file
from .zotero import license_record_from_metadata, rights_from_fields


SCHEMA = "zotero-audio-license-status/v1"


def _pdf_metadata(bundles: Path, key: str, checksum: str) -> dict[str, Any]:
    for path in bundles.glob("*/structure.json"):
        if not path.parent.name.endswith(f"[{key}]"):
            continue
        structure = load_json(path)
        source = structure.get("source", {})
        if source.get("zotero_key") == key and source.get("sha256") == checksum:
            document = structure.get("document", {})
            if document.get("rights_source") == "pdf-text":
                return document
    return {}


def evaluate_attachment(attachment: dict[str, Any], fields: dict[str, Any], *,
                        parent_key: str, bundles: Path, evidence: Path) -> dict[str, Any]:
    result = {"key": attachment["key"], "allowed": False}
    path = Path(attachment["path"]) if attachment.get("path") else None
    if path is None or not path.is_file():
        return result | {"status": "unresolved", "reason": "source-file-unavailable"}
    checksum = sha256_file(path)
    metadata = {"parent_key": parent_key, "rights": rights_from_fields(fields),
                "doi": fields.get("DOI"), "url": fields.get("url")}
    record = license_record_from_metadata(metadata, checksum)
    cached_path = evidence / f"{checksum}.json"
    cached = load_json(cached_path).get("record", {}) if cached_path.is_file() else {}
    if cached and cached.get("source_sha256") != checksum:
        return result | resolve_license(cached, source_sha256=checksum)
    # A prior checksum-bound PDF assessment survives metadata edits. A cached
    # parent-item assertion does not: removing Rights must remove that evidence.
    if record is None and cached.get("content_version") == "local-pdf-license-v1":
        record = cached
    if record is None:
        pdf = _pdf_metadata(bundles, attachment["key"], checksum)
        if pdf:
            record = license_record_from_metadata({**pdf, **{
                k: v for k, v in metadata.items() if v
            }}, checksum)
    if record and cached:
        record = {**record, **{k: cached[k] for k in ("conflict", "embargoed") if cached.get(k)}}
    resolution = resolve_license(record, source_sha256=checksum)
    return result | resolution | {"source_sha256": checksum,
                                 "asserted_license": (record or {}).get("license_url")}


def evaluate_library(request: dict[str, Any], *, bundles: Path, evidence: Path) -> dict[str, Any]:
    items = []
    for item in request["items"]:
        attachments = []
        for attachment in item["attachments"]:
            try:
                outcome = evaluate_attachment(attachment, item["fields"], parent_key=item["key"],
                                              bundles=bundles, evidence=evidence)
            except (OSError, ValueError, TypeError) as exc:
                outcome = {"key": attachment["key"], "allowed": False, "status": "unresolved",
                           "reason": "license-evaluation-error", "error": str(exc)}
            attachments.append(outcome)
        # A record with mixed PDF licenses remains visible for review.
        status = ("pass" if all(a["allowed"] for a in attachments) else "blocked") if attachments else None
        items.append({"key": item["key"], "status": status, "attachments": attachments})
    return {"schema": SCHEMA, "request_id": request["request_id"], "items": items}
