"""Shared R2 delivery for either edition, with feeds uploaded last."""
from __future__ import annotations

import mimetypes
import os
import shutil
import subprocess
from pathlib import Path
from urllib.parse import unquote, urlsplit

from .util import atomic_write_json, load_json, sha256_file
from .runtime import configure_tool_path


def sync_public(config, records: list[dict]) -> None:
    if not config.public_root or not config.r2_bucket:
        raise ValueError("Public root and R2 bucket are required for --sync")
    keys = set()
    for record in records:
        for field in ("audio_url", "image_url", "transcript_url", "transcript_html_url", "chapters_url", "markdown_url"):
            keys.add(unquote(urlsplit(record[field]).path).lstrip("/"))
        keys.add(unquote(urlsplit(record["page_url"]).path).lstrip("/") + "index.html")
        keys.add(record["edition"] + "/feed.xml")
    # Show artwork is also referenced by each feed.
    import xml.etree.ElementTree as ET
    for key in list(keys):
        if key.endswith("/feed.xml"):
            root = ET.parse(config.public_root / key)
            url = root.findtext("./channel/image/url")
            if url:
                keys.add(unquote(urlsplit(url).path).lstrip("/"))
    keys.add("index.html")
    upload_keys(config.public_root, config.r2_bucket, config.state_root / "r2-sync-manifest.json", keys)


def upload_keys(public_root: Path, bucket: str, state_path: Path, keys) -> None:
    configure_tool_path()
    npx = shutil.which(os.environ.get("ZOTERO_AUDIO_NPX", "npx"))
    if not npx:
        raise RuntimeError("npx is required for R2 publishing")
    for key in sorted(keys, key=lambda value: (value.endswith("/feed.xml"), value)):
        path = (public_root / key).resolve()
        if not path.is_relative_to(public_root.resolve()):
            raise ValueError("Artifact escaped public root")
        digest = sha256_file(path)
        state = load_json(state_path) if state_path.is_file() else {"files": {}}
        if state.get("files", {}).get(key) == digest:
            continue
        content_type = {".m4a": "audio/mp4", ".vtt": "text/vtt", ".md": "text/markdown", ".xml": "application/rss+xml"}.get(path.suffix, mimetypes.guess_type(path.name)[0] or "application/octet-stream")
        cache = "public, max-age=300" if key.endswith(".xml") or key.endswith("index.html") else "public, max-age=31536000, immutable"
        print(f"Uploading {key}", flush=True)
        subprocess.run([npx, "--yes", "wrangler@latest", "r2", "object", "put", f"{bucket}/{key}",
                        "--remote", "--y", "--file", str(path), "--ct", content_type, "--cc", cache], check=True)
        state.setdefault("files", {})[key] = digest
        atomic_write_json(state_path, state)
