from __future__ import annotations

import re
import shutil
import sqlite3
import tempfile
from datetime import date
from pathlib import Path
from typing import Any, Callable, Iterable, TypeVar

from .util import json_digest, load_json
from .literature import evidence_from_extra


DEFAULT_ZOTERO_DB = Path.home() / "Zotero" / "zotero.sqlite"
METADATA_SCHEMA = 5
T = TypeVar("T")

BIBLIOGRAPHIC_FIELDS = (
    "item_type", "literature_type", "institution", "report_type", "report_number", "series_title", "evidence",
    "parent_key",
    "title",
    "publication_year",
    "publication_date",
    "authors",
    "journal",
    "publisher",
    "university",
    "doi",
    "url",
    "rights",
    "rights_source",
    "abstract",
    "language",
    "bibliographic_language",
    "tags",
    "collections",
    "podcast_selected",
)

PUBLICATION_DATE_RE = re.compile(
    r"^(?P<year>(?:19|20)\d{2})(?:-(?P<month>\d{1,2})(?:-(?P<day>\d{1,2}))?)?$"
)


def normalize_publication_date(value: Any) -> Any:
    """Canonicalize Zotero's partial ISO dates without inventing a day."""

    if value is None:
        return None
    raw = str(value).strip()
    match = PUBLICATION_DATE_RE.fullmatch(raw)
    if not match:
        return raw

    year = int(match.group("year"))
    month_text, day_text = match.group("month"), match.group("day")
    if month_text is None or int(month_text) == 0:
        return str(year)
    month = int(month_text)
    if not 1 <= month <= 12:
        return raw
    if day_text is None or int(day_text) == 0:
        return f"{year:04d}-{month:02d}"
    day = int(day_text)
    try:
        date(year, month, day)
    except ValueError:
        return raw
    return f"{year:04d}-{month:02d}-{day:02d}"


def rights_from_fields(fields: dict[str, Any]) -> str | None:
    rights = fields.get("rights")
    if not rights and fields.get("extra"):
        match = re.search(
            r"(?im)^\s*(?:license|rights)\s*:\s*"
            r"(https?://\S+|CC\s*[- ]?BY\s+4\.0|CC0(?:\s+1\.0)?)\s*$",
            fields["extra"],
        )
        rights = match.group(1) if match else None
    return rights


def _query_metadata(connection: sqlite3.Connection, attachment_key: str) -> dict[str, Any] | None:
    row = connection.execute(
        """
        SELECT ia.parentItemID, parent.key, item_type.typeName
        FROM items attachment
        JOIN itemAttachments ia ON ia.itemID = attachment.itemID
        LEFT JOIN items parent ON parent.itemID = ia.parentItemID
        LEFT JOIN itemTypes item_type ON item_type.itemTypeID = parent.itemTypeID
        WHERE attachment.key = ?
        """,
        (attachment_key,),
    ).fetchone()
    if not row or row[0] is None:
        return None
    parent_id, parent_key, item_type = row
    fields = dict(
        connection.execute(
            """
            SELECT f.fieldName, v.value
            FROM itemData d
            JOIN fields f ON f.fieldID = d.fieldID
            JOIN itemDataValues v ON v.valueID = d.valueID
            WHERE d.itemID = ? AND f.fieldName IN (
                'title', 'date', 'publicationTitle', 'proceedingsTitle', 'publisher',
                'university', 'DOI', 'url', 'rights', 'abstractNote', 'language', 'extra',
                'institution', 'reportType', 'reportNumber', 'seriesTitle'
            )
            """,
            (parent_id,),
        ).fetchall()
    )
    creators = connection.execute(
        """
        SELECT c.firstName, c.lastName
        FROM itemCreators ic
        JOIN creators c ON c.creatorID = ic.creatorID
        JOIN creatorTypes ct ON ct.creatorTypeID = ic.creatorTypeID
        WHERE ic.itemID = ? AND ct.creatorType IN ('author', 'bookAuthor')
        ORDER BY ic.orderIndex
        """,
        (parent_id,),
    ).fetchall()
    names = [" ".join(part for part in creator if part).strip() for creator in creators]
    tags = [value[0] for value in connection.execute(
        "SELECT t.name FROM itemTags it JOIN tags t ON t.tagID = it.tagID WHERE it.itemID = ? ORDER BY t.name",
        (parent_id,),
    ).fetchall()]
    collections = [value[0] for value in connection.execute(
        """
        WITH RECURSIVE ancestors(collectionID, parentCollectionID, collectionName) AS (
            SELECT c.collectionID, c.parentCollectionID, c.collectionName
            FROM collectionItems ci JOIN collections c ON c.collectionID = ci.collectionID
            WHERE ci.itemID = ?
            UNION ALL
            SELECT parent.collectionID, parent.parentCollectionID, parent.collectionName
            FROM collections parent JOIN ancestors child ON child.parentCollectionID = parent.collectionID
        )
        SELECT DISTINCT collectionName FROM ancestors ORDER BY collectionName
        """,
        (parent_id,),
    ).fetchall()]
    year_match = re.search(r"\b(?:19|20)\d{2}\b", fields.get("date", ""))
    rights = rights_from_fields(fields)
    raw_date = fields.get("date")
    iso_date = re.search(r"\b(?:19|20)\d{2}-\d{1,2}-\d{1,2}\b", raw_date or "")
    publication_date = normalize_publication_date(iso_date.group(0) if iso_date else raw_date)
    return {
        "item_type": item_type,
        "literature_type": "report" if item_type == "report" else "academic",
        "institution": fields.get("institution"),
        "report_type": fields.get("reportType"),
        "report_number": fields.get("reportNumber"),
        "series_title": fields.get("seriesTitle"),
        "evidence": evidence_from_extra(fields.get("extra", "")),
        "parent_key": parent_key,
        "title": fields.get("title"),
        "publication_year": year_match.group(0) if year_match else None,
        "publication_date": publication_date,
        "authors": [name for name in names if name],
        "journal": fields.get("publicationTitle") or fields.get("proceedingsTitle"),
        "publisher": fields.get("publisher"),
        "university": fields.get("university"),
        "doi": fields.get("DOI"),
        "url": fields.get("url"),
        "rights": rights,
        "abstract": fields.get("abstractNote"),
        "language": fields.get("language"),
        "tags": tags,
        "collections": collections,
        "podcast_selected": any(tag.casefold() == "podcast" for tag in tags)
        or any(name.casefold() == "podcast queue" for name in collections),
    }


def _read_database(database: Path, operation: Callable[[sqlite3.Connection], T]) -> T:
    database = database.expanduser().resolve()
    if not database.is_file():
        raise FileNotFoundError(f"Zotero database does not exist: {database}")
    try:
        connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True, timeout=0.5)
        try:
            connection.execute("PRAGMA query_only = ON")
            return operation(connection)
        finally:
            connection.close()
    except sqlite3.OperationalError as exc:
        if "locked" not in str(exc).casefold() and "busy" not in str(exc).casefold():
            raise

    # Zotero can briefly hold an exclusive lock during maintenance. A private
    # copy, including the WAL when present, provides a read-only point-in-time
    # view without changing or delaying Zotero itself.
    with tempfile.TemporaryDirectory(prefix="zotero-audio-db-") as temporary:
        snapshot = Path(temporary) / database.name
        shutil.copy2(database, snapshot)
        for suffix in ("-wal", "-shm"):
            companion = Path(str(database) + suffix)
            if companion.is_file():
                shutil.copy2(companion, Path(str(snapshot) + suffix))
        connection = sqlite3.connect(snapshot)
        try:
            connection.execute("PRAGMA query_only = ON")
            return operation(connection)
        finally:
            connection.close()


def zotero_metadata(database: Path, attachment_key: str) -> dict[str, Any] | None:
    return _read_database(database, lambda connection: _query_metadata(connection, attachment_key))


def zotero_metadata_many(database: Path, attachment_keys: Iterable[str]) -> dict[str, dict[str, Any]]:
    keys = tuple(dict.fromkeys(str(key) for key in attachment_keys if str(key)))

    def query(connection: sqlite3.Connection) -> dict[str, dict[str, Any]]:
        return {key: value for key in keys if (value := _query_metadata(connection, key)) is not None}

    return _read_database(database, query)


def bibliographic_metadata(value: dict[str, Any] | None) -> dict[str, Any]:
    if not value:
        return {}
    result = {key: value[key] for key in BIBLIOGRAPHIC_FIELDS if key in value and value[key] is not None}
    if "publication_date" in result:
        result["publication_date"] = normalize_publication_date(result["publication_date"])
    return result


def merge_document_metadata(document: dict[str, Any], metadata: dict[str, Any] | None) -> dict[str, Any]:
    result = dict(document)
    clean = bibliographic_metadata(metadata)
    for key in (
        "item_type", "literature_type", "institution", "report_type", "report_number", "series_title", "evidence",
        "title", "publication_year", "publication_date", "authors", "journal", "publisher",
        "university", "doi", "url", "rights", "abstract", "language",
    ):
        value = clean.get(key)
        if value not in (None, "", []):
            result[key] = value
    if clean.get("authors"):
        result["author"] = "; ".join(str(author) for author in clean["authors"])
    if clean:
        result["metadata_source"] = "zotero-parent-item"
        if clean.get("parent_key"):
            result["zotero_parent_key"] = clean["parent_key"]
    if result.get("publication_date") is not None:
        result["publication_date"] = normalize_publication_date(result["publication_date"])
    return result


def bundle_metadata_snapshot(metadata: dict[str, Any], *, attachment_key: str, source_sha256: str) -> dict[str, Any]:
    return {
        "schema": "zotero-audio-zotero-metadata/v1",
        "metadata_schema": METADATA_SCHEMA,
        "zotero_key": attachment_key,
        "source_sha256": source_sha256,
        **bibliographic_metadata(metadata),
    }


def load_bundle_metadata(bundle: Path) -> dict[str, Any]:
    bundle = bundle.expanduser().resolve()
    structure_path = bundle / "structure.json"
    structure = load_json(structure_path) if structure_path.is_file() else {}
    key = str(structure.get("source", {}).get("zotero_key") or "")
    source_sha = str(structure.get("source", {}).get("sha256") or "")

    snapshot_path = bundle / "metadata.json"
    if snapshot_path.is_file():
        snapshot = load_json(snapshot_path)
        if (not key or snapshot.get("zotero_key") == key) and (
            not source_sha or snapshot.get("source_sha256") == source_sha
        ):
            return bibliographic_metadata(snapshot) | {
                "zotero_key": snapshot.get("zotero_key", key),
                "source_sha256": snapshot.get("source_sha256", source_sha),
            }

    manifest_path = bundle.parent.parent / "batch-manifest.json"
    if manifest_path.is_file():
        manifest = load_json(manifest_path)
        matches = [
            item for item in manifest.get("items", [])
            if item.get("status") == "complete"
            and (not key or str(item.get("zotero_key")) == key)
            and (not source_sha or str(item.get("source_sha256")) == source_sha)
        ]
        if len(matches) == 1:
            item = matches[0]
            return bibliographic_metadata(item) | {
                "zotero_key": item.get("zotero_key", key),
                "source_sha256": item.get("source_sha256", source_sha),
            }
    return {}


def license_record_from_metadata(metadata: dict[str, Any], source_sha256: str) -> dict[str, Any] | None:
    rights = metadata.get("rights")
    read_url = metadata.get("url") or (
        f"https://doi.org/{metadata['doi']}" if metadata.get("doi") else None
    )
    if not rights:
        return None
    evidence = {
        "zotero_parent_key": metadata.get("parent_key") or metadata.get("zotero_parent_key"),
        "rights": rights,
        "rights_source": metadata.get("rights_source") or "zotero-parent-item",
    }
    return {
        "source_sha256": source_sha256,
        "license_url": rights,
        "evidence_sha256": json_digest(evidence),
        "read_url": read_url,
        "content_version": "zotero-local-source",
    }
