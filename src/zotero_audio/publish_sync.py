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

# Upgrade deliberately after checking the uploader on the installed Mac runtime.
WRANGLER_PACKAGE = "wrangler@4.145.0"


def verify_publication(config, record: dict) -> None:
    """A successful upload command is insufficient evidence of live RSS."""
    import xml.etree.ElementTree as ET
    from urllib.request import Request, urlopen
    from urllib.parse import urlencode
    edition = record["edition"]
    feed_key = edition + "/feed.xml"
    origin = getattr(config, "feed_base_url", "") or config.base_url
    revision = sha256_file(config.public_root / feed_key)[:16]
    feed_url = origin.rstrip("/") + "/" + feed_key + "?" + urlencode({"publication": revision})
    try:
        with urlopen(Request(feed_url, headers={"Cache-Control": "no-cache"}), timeout=20) as response:
            data = response.read(32 * 1024 * 1024 + 1)
        if len(data) > 32 * 1024 * 1024:
            raise ValueError("public RSS exceeds verification bound")
        root = ET.fromstring(data)
        matches = [item for item in root.findall("./channel/item") if item.findtext("guid") == record["guid"]]
        if len(matches) != 1:
            raise ValueError("expected exactly one episode in public RSS")
        enclosure = matches[0].find("enclosure")
        if enclosure is None or enclosure.get("url") != record["audio_url"]:
            raise ValueError("public RSS has a different audio revision")
        expected_bytes = int(record["bytes"])
        if int(enclosure.get("length", "0")) != expected_bytes:
            raise ValueError("public RSS has an incorrect audio size")
        with urlopen(Request(record["audio_url"], method="HEAD"), timeout=20) as media:
            if media.status != 200 or int(media.headers.get("Content-Length", "0")) != expected_bytes:
                raise ValueError("public audio is unavailable or has an incorrect size")
    except Exception as exc:
        # Invalidate the upload cache so a stale/missing remote object can be
        # repaired on retry, even if its local bytes have not changed.
        state_path = config.state_root / "r2-sync-manifest.json"
        if state_path.is_file():
            state = load_json(state_path)
            for key in (feed_key, _public_key(record["audio_url"], config.base_url)):
                state.get("files", {}).pop(key, None)
            atomic_write_json(state_path, state)
        raise RuntimeError(f"Live publication verification failed for {record['title']}: {type(exc).__name__}") from exc


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
                try:
                    page_path = _public_key(record["page_url"], config.base_url)
                except ValueError:
                    # Episode pages link the canonical site host directly.
                    page_path = _public_key(record["page_url"], getattr(config, "feed_base_url", "") or config.base_url)
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
                try:
                    artwork_key = _public_key(url, config.base_url)
                except ValueError:
                    # Only show artwork can use the configured canonical feed
                    # origin. Episode assets retain their original public URLs.
                    artwork_key = _public_key(url, getattr(config, "feed_base_url", "") or config.base_url)
                keys.add(artwork_key)
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
        content_type = {".m4a": "audio/x-m4a", ".vtt": "text/vtt", ".txt": "text/plain", ".md": "text/markdown", ".xml": "application/rss+xml"}.get(path.suffix, mimetypes.guess_type(path.name)[0] or "application/octet-stream")
        cache = "public, max-age=300" if key.endswith(".xml") or key.endswith("index.html") else "public, max-age=31536000, immutable"
        print(f"Uploading {key}", flush=True)
        subprocess.run([npx, "--yes", WRANGLER_PACKAGE, "r2", "object", "put", f"{bucket}/{key}",
                        "--remote", "--y", "--file", str(path), "--ct", content_type, "--cc", cache], check=True)
        state.setdefault("files", {})[key] = digest
        atomic_write_json(state_path, state)
