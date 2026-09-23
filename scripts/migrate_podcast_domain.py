#!/usr/bin/env python3
"""Move published podcast URLs to a new base without changing episode GUIDs.

Preview by default. Run only after the new origin serves the existing R2 objects.
The old feed remains available from the same bucket and advertises the new URL.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
import unicodedata
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from zotero_audio.podcast import (  # noqa: E402
    EDITIONS, LocalPublisher, _show, build_rss, load_podcast_config,
)
from zotero_audio.publish_sync import upload_keys  # noqa: E402
from zotero_audio.util import atomic_write_json, atomic_write_text  # noqa: E402


MEDIA_FIELDS = ("audio_url", "image_url", "transcript_url", "transcript_html_url", "chapters_url", "markdown_url")


def paper_slug(title: str, paper_id: str) -> str:
    title = title.split(" — ")[0]
    normalized = unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode("ascii").lower()
    slug = re.sub(r"[^a-z0-9]+", "-", normalized).strip("-")[:78].rstrip("-") or "paper"
    return f"{slug}-{paper_id[:8]}"


def rebase_url(value: str, old_base: str, new_base: str) -> str:
    return new_base + value[len(old_base):] if value.startswith(old_base + "/") else value


def migrate_record(record: dict, old_base: str, new_base: str) -> dict:
    result = dict(record)
    for field in (*MEDIA_FIELDS, "paired_url"):
        if result.get(field):
            result[field] = rebase_url(str(result[field]), old_base, new_base)
    if result.get("show_notes"):
        result["show_notes"] = str(result["show_notes"]).replace(old_base + "/", new_base + "/")
    old_page = str(result.get("page_url") or "")
    old_match = re.search(r"/papers/([0-9a-f-]{36})/", old_page, re.IGNORECASE)
    identity = str(result.get("doi") or result.get("read_url") or result.get("paper_title") or str(result["title"]).split(" — ")[0]).lower()
    paper_id = str(result.get("paper_guid") or (old_match.group(1) if old_match else uuid.uuid5(uuid.NAMESPACE_URL, f"1-more-paper:{identity}")))
    result["paper_guid"] = paper_id
    result["page_url"] = f"{new_base}/papers/{paper_slug(str(result['title']), paper_id)}/{result['edition']}/"
    return result


def show_image(feed_path: Path) -> str:
    import xml.etree.ElementTree as ET
    root = ET.parse(feed_path).getroot()
    image = root.findtext("./channel/image/url")
    if not image:
        image_node = root.find("./channel/{http://www.itunes.com/dtds/podcast-1.0.dtd}image")
        image = image_node.get("href") if image_node is not None else None
    if not image:
        raise ValueError(f"show artwork missing from {feed_path}")
    return image


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--old-base", required=True)
    parser.add_argument("--apply", action="store_true", help="Write the migrated local manifest and feeds")
    parser.add_argument("--sync", action="store_true", help="Upload the two feeds to R2 after applying")
    args = parser.parse_args()
    if args.sync and not args.apply:
        parser.error("--sync requires --apply")
    config = load_podcast_config(args.config)
    old_base = args.old_base.rstrip("/")
    new_base = config.base_url.rstrip("/")
    if not config.public_root or not config.r2_bucket or old_base == new_base:
        parser.error("configure a public root, R2 bucket, and a different new base URL")
    if config.previous_feed_base_url != old_base:
        parser.error("set previous_feed_base_url to --old-base in the podcast config")
    manifest_path = config.state_root / "publication-manifest.json"
    original = json.loads(manifest_path.read_text(encoding="utf-8"))
    records = [migrate_record(record, old_base, new_base) for record in original.get("episodes", [])]
    if not records:
        parser.error("publication manifest has no episodes")
    root = config.public_root
    for record in records:
        for field in MEDIA_FIELDS:
            value = record.get(field)
            if not value or not str(value).startswith(new_base + "/"):
                continue
            key = unquote(urlsplit(str(value)).path[len(urlsplit(new_base).path.rstrip('/')):]).lstrip("/")
            if not (root / key).is_file():
                raise FileNotFoundError(f"missing published {field}: {key}")
    stages: dict[str, Path] = {}
    for edition in EDITIONS:
        feed = root / edition / "feed.xml"
        image = rebase_url(show_image(feed), old_base, new_base)
        show = _show(config, getattr(config, f"{edition}_show"), image)
        xml = build_rss(show, [item for item in records if item["edition"] == edition])
        stage = config.state_root / "public-staging" / f"migrated-{edition}.xml"
        if args.apply:
            atomic_write_text(stage, xml)
        stages[edition] = stage
    print(f"{len(records)} episodes: {old_base} -> {new_base}")
    print("GUIDs, enclosure byte lengths, publication dates, and media object keys are preserved")
    if not args.apply:
        print("Preview only; pass --apply after the new origin is live")
        return

    backup_dir = config.state_root / "domain-migration-backups" / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_dir.mkdir(parents=True, exist_ok=False)
    shutil.copy2(manifest_path, backup_dir / "publication-manifest.json")
    for edition in EDITIONS:
        shutil.copy2(root / edition / "feed.xml", backup_dir / f"{edition}-feed.xml")
    publisher = LocalPublisher(root, new_base)
    for edition, stage in stages.items():
        publisher.commit_feed(stage, f"{edition}/feed.xml", config.state_root / "feed-snapshots" / edition)
    atomic_write_json(manifest_path, {**original, "episodes": records})
    if args.sync:
        upload_keys(root, config.r2_bucket, config.state_root / "r2-sync-manifest.json",
                    {f"{edition}/feed.xml" for edition in EDITIONS})
    print(f"Applied. Backup: {backup_dir}")


if __name__ == "__main__":
    main()
