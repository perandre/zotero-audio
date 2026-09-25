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


def _public_key(url: str, base_url: str) -> str:
    """Map a public URL back to a key in the local podcast mirror."""
    parsed, base = urlsplit(url), urlsplit(base_url)
    prefix = base.path.rstrip("/") + "/"
    if (parsed.scheme, parsed.netloc) != (base.scheme, base.netloc) or not parsed.path.startswith(prefix):
        raise ValueError("Podcast asset URL is outside the configured public base URL")
    return unquote(parsed.path[len(prefix):])


def sync_public(config, records: list[dict]) -> None:
    if not config.public_root or not config.r2_bucket:
        raise ValueError("Public root and R2 bucket are required for --sync")
    keys = set()
    for record in records:
        for field in ("audio_url", "image_url", "transcript_url", "transcript_html_url", "transcript_text_url", "chapters_url", "markdown_url"):
            if record.get(field):
                keys.add(_public_key(record[field], config.base_url))
        if record.get("page_url"):
            if record.get("paper_guid") and record.get("edition"):
                keys.add(f"papers/{record['paper_guid']}/{record['edition']}/index.html")
            else:
                page_path = _public_key(record["page_url"], config.base_url)
                keys.add(page_path.rstrip("/") + "/index.html" if page_path.endswith("/") else page_path)
        keys.add(record["edition"] + "/feed.xml")
    # Show artwork is also referenced by each feed.
    import xml.etree.ElementTree as ET
    for key in list(keys):
        if key.endswith("/feed.xml"):
            root = ET.parse(config.public_root / key)
            image = root.find("./channel/{http://www.itunes.com/dtds/podcast-1.0.dtd}image")
            url = image.get("href") if image is not None else root.findtext("./channel/image/url")
            if url:
                keys.add(_public_key(url, config.base_url))
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
        content_type = {".m4a": "audio/mp4", ".vtt": "text/vtt", ".txt": "text/plain", ".md": "text/markdown", ".xml": "application/rss+xml"}.get(path.suffix, mimetypes.guess_type(path.name)[0] or "application/octet-stream")
        cache = "public, max-age=300" if key.endswith(".xml") or key.endswith("index.html") else "public, max-age=31536000, immutable"
        print(f"Uploading {key}", flush=True)
        subprocess.run([npx, "--yes", "wrangler@latest", "r2", "object", "put", f"{bucket}/{key}",
                        "--remote", "--y", "--file", str(path), "--ct", content_type, "--cc", cache], check=True)
        state.setdefault("files", {})[key] = digest
        atomic_write_json(state_path, state)
