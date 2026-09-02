#!/usr/bin/env python3
"""Run an unattended, incremental Zotero PDF to M4A synchronization."""

from __future__ import annotations

import argparse
import fcntl
import json
import subprocess
from pathlib import Path
from typing import Any

from batch_library import main as batch_main
from finalize_library_metadata import main as finalize_main


DEFAULT_STORAGE = Path.home() / "Zotero" / "storage"
DEFAULT_RUNTIME = Path.home() / "Sites" / "zotero-audio-runtime"
DEFAULT_DESTINATION = Path.home() / "Music" / "Zotero Audio"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--zotero-storage", type=Path, default=DEFAULT_STORAGE)
    parser.add_argument("--destination", type=Path, default=DEFAULT_DESTINATION)
    parser.add_argument("--state-dir", type=Path, default=DEFAULT_RUNTIME / "full-library")
    parser.add_argument("--log-file", type=Path)
    parser.add_argument("--model", default="mlx-community/Kokoro-82M-bf16")
    parser.add_argument("--voice", default="af_heart")
    parser.add_argument("--norwegian-voice", default="bf_emma")
    parser.add_argument("--speed", type=float, default=1.0)
    parser.add_argument("--max-chars", type=int, default=900)
    parser.add_argument("--zotero-db", type=Path, default=Path.home() / "Zotero" / "zotero.sqlite")
    parser.add_argument("--notify", action=argparse.BooleanOptionalAction, default=True)
    return parser


def _load_manifest(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    with path.open(encoding="utf-8") as stream:
        value = json.load(stream)
    return value if isinstance(value, dict) else {}


def _complete_keys(manifest: dict[str, Any]) -> set[tuple[str, str]]:
    return {
        (str(item.get("zotero_key")), str(item.get("source_sha256")))
        for item in manifest.get("items", [])
        if item.get("status") == "complete" and item.get("metadata_finalized")
    }


def _failed_keys(manifest: dict[str, Any]) -> set[tuple[str, str, str]]:
    return {
        (
            str(item.get("zotero_key")),
            str(item.get("source_sha256")),
            str(item.get("error")),
        )
        for item in manifest.get("items", [])
        if item.get("status") == "failed"
    }


def _notify(message: str, *, title: str = "Zotero Audio") -> None:
    def apple_script(value: str) -> str:
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'

    subprocess.run(
        ["/usr/bin/osascript", "-e", f"display notification {apple_script(message)} with title {apple_script(title)}"],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    storage = args.zotero_storage.expanduser().resolve()
    destination = args.destination.expanduser().resolve()
    state_dir = args.state_dir.expanduser().resolve()
    state_dir.mkdir(parents=True, exist_ok=True)
    lock_path = state_dir / "automatic-sync.lock"

    with lock_path.open("w", encoding="utf-8") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("Another automatic synchronization is already running")
            return 0

        pdfs = sorted(storage.glob("*/*.pdf"))
        if not pdfs:
            print(f"No PDFs found in {storage}; nothing to do")
            return 0

        manifest_path = state_dir / "batch-manifest.json"
        before = _load_manifest(manifest_path)
        log_file = args.log_file.expanduser().resolve() if args.log_file else state_dir / "batch.log"
        batch_args = [
            "--zotero-storage",
            str(storage),
            "--destination",
            str(destination),
            "--state-dir",
            str(state_dir),
            "--log-file",
            str(log_file),
            "--model",
            args.model,
            "--voice",
            args.voice,
            "--norwegian-voice",
            args.norwegian_voice,
            "--speed",
            str(args.speed),
            "--max-chars",
            str(args.max_chars),
        ]
        result = batch_main(batch_args)

        # Metadata enrichment is deliberately after the verified AAC is copied.
        # If Zotero is busy, the next scheduled run retries this lightweight step.
        if manifest_path.is_file():
            try:
                finalize_main(["--zotero-db", str(args.zotero_db.expanduser().resolve()), "--batch-manifest", str(manifest_path)])
            except Exception as exc:
                print(f"Metadata finalization failed: {type(exc).__name__}: {exc}")
                result = 1

        after = _load_manifest(manifest_path)
        new_count = len(_complete_keys(after) - _complete_keys(before))
        new_failure_count = len(_failed_keys(after) - _failed_keys(before))
        if args.notify and new_count:
            _notify(f"{new_count} new audio file{'s' if new_count != 1 else ''} ready")
        if args.notify and new_failure_count:
            _notify(
                f"{new_failure_count} PDF job{'s' if new_failure_count != 1 else ''} need attention",
                title="Zotero Audio — action required",
            )
        return result


if __name__ == "__main__":
    raise SystemExit(main())
