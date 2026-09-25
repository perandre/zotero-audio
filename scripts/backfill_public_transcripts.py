#!/usr/bin/env python3
"""Publish available Full Reading narration and upload transcript assets before RSS."""
from __future__ import annotations

import argparse
from pathlib import Path

from zotero_audio.feed_only import publish_existing_audio
from zotero_audio.podcast import load_podcast_config
from zotero_audio.publish_sync import _public_key, upload_keys
from zotero_audio.util import load_json


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--sync", action="store_true", help="Upload transcript assets and feed XML to R2")
    args = parser.parse_args(argv)
    config = load_podcast_config(args.config.expanduser().resolve())
    result = publish_existing_audio(config)
    print(f"Refreshed {result['episodes']} published episodes in the local feed mirror.")
    if not args.sync:
        return 0
    if not config.public_root or not config.r2_bucket:
        raise ValueError("Public root and R2 bucket are required for --sync")
    manifest = load_json(config.state_root / "publication-manifest.json")
    keys = {_public_key(record["transcript_text_url"], config.base_url)
            for record in manifest["episodes"] if record.get("transcript_text_url")}
    # The uploader sorts feeds last, so every advertised transcript is live first.
    keys.update({"brief/feed.xml", "full/feed.xml"})
    upload_keys(config.public_root, config.r2_bucket,
                config.state_root / "r2-sync-manifest.json", keys)
    print(f"Synced {len(keys) - 2} transcripts and both feeds to R2.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
