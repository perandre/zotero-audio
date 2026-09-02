#!/usr/bin/env python3
"""Apply Zotero parent-item metadata to a completed local M4A library."""

from __future__ import annotations

import argparse
import os
import re
import shutil
import sqlite3
import tempfile
from pathlib import Path
from typing import Any

from mutagen.mp4 import MP4

from zotero_audio.audio import inspect_m4a, normalize_mp4_timestamps
from zotero_audio.util import atomic_write_json, filename_part, load_json, sha256_file


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--zotero-db", type=Path, default=Path.home() / "Zotero" / "zotero.sqlite")
    parser.add_argument("--batch-manifest", type=Path, required=True)
    return parser


def zotero_metadata(database: Path, attachment_key: str) -> dict[str, Any] | None:
    connection = sqlite3.connect(f"file:{database.resolve()}?mode=ro", uri=True)
    try:
        row = connection.execute(
            """
            SELECT ia.parentItemID, parent.key
            FROM items attachment
            JOIN itemAttachments ia ON ia.itemID = attachment.itemID
            LEFT JOIN items parent ON parent.itemID = ia.parentItemID
            WHERE attachment.key = ?
            """,
            (attachment_key,),
        ).fetchone()
        if not row or row[0] is None:
            return None
        parent_id, parent_key = row
        fields = dict(
            connection.execute(
                """
                SELECT f.fieldName, v.value
                FROM itemData d
                JOIN fields f ON f.fieldID = d.fieldID
                JOIN itemDataValues v ON v.valueID = d.valueID
                WHERE d.itemID = ? AND f.fieldName IN ('title', 'date')
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
        year_match = re.search(r"\b(?:19|20)\d{2}\b", fields.get("date", ""))
        return {
            "parent_key": parent_key,
            "title": fields.get("title"),
            "publication_year": year_match.group(0) if year_match else None,
            "authors": [name for name in names if name],
        }
    finally:
        connection.close()


def retag_atomic(source: Path, destination: Path, metadata: dict[str, Any], key: str) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix=f".{destination.name}.", dir=destination.parent)
    os.close(descriptor)
    temporary_path = Path(temporary)
    try:
        shutil.copyfile(source, temporary_path)
        audio = MP4(temporary_path)
        title = metadata.get("title") or audio.tags.get("\xa9nam", [source.stem])[0]
        authors = metadata.get("authors") or []
        audio["\xa9nam"] = [title]
        if authors:
            audio["\xa9ART"] = ["; ".join(authors)]
        audio["\xa9alb"] = ["Zotero Audio"]
        existing_comment = audio.tags.get("\xa9cmt", [""])[0] if audio.tags else ""
        additions = f"zotero-key={key}"
        if metadata.get("parent_key"):
            additions += f"; zotero-parent-key={metadata['parent_key']}"
        audio["\xa9cmt"] = [f"{existing_comment}; {additions}".strip("; ")]
        audio.save()
        normalize_mp4_timestamps(temporary_path)
        inspect_m4a(temporary_path)
        os.replace(temporary_path, destination)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    manifest_path = args.batch_manifest.expanduser().resolve()
    manifest = load_json(manifest_path)
    destination_root = Path(manifest["destination"])
    final_paths: set[Path] = set()
    for item in manifest["items"]:
        if item.get("status") != "complete":
            continue
        if item.get("metadata_finalized"):
            continue
        source = destination_root / item["output_file"]
        metadata = zotero_metadata(args.zotero_db, item["zotero_key"]) or {}
        title = metadata.get("title") or item["title"]
        year = metadata.get("publication_year") or re.match(r"^((?:19|20)\d{2})", source.name)
        year = year if isinstance(year, str) else year.group(1) if year else None
        prefix = f"{year} - " if year else ""
        desired = destination_root / (
            filename_part(f"{prefix}{title} [{item['zotero_key']}]", max_length=230) + ".m4a"
        )
        if desired in final_paths:
            raise RuntimeError(f"Duplicate destination filename: {desired.name}")
        final_paths.add(desired)
        retag_atomic(source, desired, metadata, item["zotero_key"])
        if source != desired:
            source.unlink()
        technical = inspect_m4a(desired)
        item.update(
            {
                "title": title,
                "authors": metadata.get("authors") or [],
                "publication_year": year,
                "zotero_parent_key": metadata.get("parent_key"),
                "output_file": desired.name,
                "output_sha256": sha256_file(desired),
                "duration_seconds": technical["duration_seconds"],
                "sample_rate": technical["sample_rate"],
                "channels": technical["channels"],
                "codec": technical["codec"],
                "bitrate": technical["bitrate"],
                "metadata_finalized": True,
            }
        )
        atomic_write_json(manifest_path, manifest)
        print(desired.name, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
