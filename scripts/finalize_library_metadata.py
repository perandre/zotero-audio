#!/usr/bin/env python3
"""Apply Zotero parent-item metadata to a completed local M4A library."""

from __future__ import annotations

import argparse
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any

from mutagen.mp4 import MP4

from zotero_audio.audio import inspect_m4a, normalize_mp4_timestamps
from zotero_audio.util import atomic_write_json, filename_part, load_json, sha256_file
from zotero_audio.zotero import METADATA_SCHEMA, bibliographic_metadata, bundle_metadata_snapshot
from zotero_audio.zotero_local import zotero_metadata_preferred


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--zotero-db", type=Path, default=Path.home() / "Zotero" / "zotero.sqlite")
    parser.add_argument("--batch-manifest", type=Path, required=True)
    return parser


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
        if item.get("metadata_finalized") and item.get("metadata_schema", 0) >= METADATA_SCHEMA:
            continue
        source = destination_root / item["output_file"]
        metadata = zotero_metadata_preferred(args.zotero_db, item["zotero_key"])
        if not metadata:
            item.update({"metadata_finalized": False, "metadata_status": "missing-parent-item"})
            atomic_write_json(manifest_path, manifest)
            continue
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
                **bibliographic_metadata(metadata),
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
                "metadata_schema": METADATA_SCHEMA,
                "metadata_status": "ready",
                "publication_date": metadata.get("publication_date"),
                "journal": metadata.get("journal"),
                "publisher": metadata.get("publisher"),
                "university": metadata.get("university"),
                "doi": metadata.get("doi"),
                "url": metadata.get("url"),
                "rights": metadata.get("rights"),
                "abstract": metadata.get("abstract"),
                "bibliographic_language": metadata.get("language"),
                "tags": metadata.get("tags") or [],
                "collections": metadata.get("collections") or [],
                "podcast_selected": bool(metadata.get("podcast_selected")),
            }
        )
        bundles_root = manifest_path.parent / "bundles"
        matches = [
            path for path in bundles_root.iterdir()
            if path.is_dir() and path.name.endswith(f"[{item['zotero_key']}]")
        ] if bundles_root.is_dir() else []
        if len(matches) == 1:
            atomic_write_json(
                matches[0] / "metadata.json",
                bundle_metadata_snapshot(
                    metadata,
                    attachment_key=item["zotero_key"],
                    source_sha256=item["source_sha256"],
                ),
            )
        atomic_write_json(manifest_path, manifest)
        print(desired.name, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
