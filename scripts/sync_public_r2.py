#!/usr/bin/env python3
"""Synchronize the verified podcast mirror to a Cloudflare R2 bucket."""

from __future__ import annotations

import argparse
import hashlib
import json
import mimetypes
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any


IMMUTABLE_CACHE = "public, max-age=31536000, immutable"
SHORT_CACHE = "public, max-age=300"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True, help="Local public mirror")
    parser.add_argument("--bucket", required=True, help="R2 bucket name")
    parser.add_argument("--state-file", type=Path, required=True, help="Local upload manifest")
    return parser


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_state(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _write_state(path: Path, files: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps({"schema": "zotero-audio-r2-sync/v1", "files": files}, indent=2) + "\n",
                                  encoding="utf-8")
    temporary.replace(path)


def _wrangler() -> str:
    configured = os.environ.get("ZOTERO_AUDIO_NPX")
    candidates = [configured] if configured else []
    candidates.extend((shutil.which("npx"), "/Users/pesh/.nvm/versions/node/v22.14.0/bin/npx"))
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return candidate
    raise RuntimeError("npx is required to upload the public mirror with Wrangler")


def _content_type(path: Path) -> str:
    return {
        ".m4a": "audio/mp4",
        ".png": "image/png",
        ".vtt": "text/vtt; charset=utf-8",
        ".html": "text/html; charset=utf-8",
        ".xml": "application/rss+xml; charset=utf-8",
        ".json": "application/json; charset=utf-8",
        ".md": "text/markdown; charset=utf-8",
    }.get(path.suffix.casefold(), mimetypes.guess_type(path.name)[0] or "application/octet-stream")


def _cache_control(key: str) -> str:
    if key == "index.html" or key.endswith("/feed.xml") or key.startswith("papers/"):
        return SHORT_CACHE
    return IMMUTABLE_CACHE


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    root = args.root.expanduser().resolve()
    if not root.is_dir():
        raise RuntimeError(f"public mirror does not exist: {root}")
    state_path = args.state_file.expanduser().resolve()
    state = _load_state(state_path).get("files", {})
    if not isinstance(state, dict):
        state = {}

    files: dict[str, str] = {}
    pending: list[tuple[str, Path, str]] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.name.endswith(".headers.json"):
            continue
        key = path.relative_to(root).as_posix()
        digest = _sha256(path)
        files[key] = digest
        if state.get(key) != digest:
            pending.append((key, path, digest))

    if not pending:
        print(f"R2 mirror is up to date ({len(files)} object{'s' if len(files) != 1 else ''})")
        return 0

    npx = _wrangler()
    # Publish audio and companion artifacts before any feed can reference them.
    pending.sort(key=lambda item: (item[0].endswith('/feed.xml'), item[0]))
    for key, path, digest in pending:
        command = [npx, "--yes", "wrangler@latest", "r2", "object", "put", f"{args.bucket}/{key}",
                   "--remote", "--y", "--file", str(path), "--ct", _content_type(path),
                   "--cc", _cache_control(key)]
        print(f"Uploading {key}")
        result = subprocess.run(command, check=False, text=True, capture_output=True)
        if result.returncode:
            detail = (result.stderr or result.stdout).strip()
            raise RuntimeError(f"R2 upload failed for {key}: {detail}")

    _write_state(state_path, files)
    print(f"Uploaded {len(pending)} R2 object{'s' if len(pending) != 1 else ''}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
