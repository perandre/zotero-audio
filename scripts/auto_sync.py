#!/usr/bin/env python3
"""Run an unattended, incremental Zotero PDF to M4A synchronization."""

from __future__ import annotations

import argparse
import fcntl
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from batch_library import main as batch_main
from finalize_library_metadata import main as finalize_main
from zotero_audio.audio import DEFAULT_ENGLISH_VOICE
from zotero_audio.feed_only import publish_existing_audio
from zotero_audio.podcast import EDITIONS, health_check, load_podcast_config
from zotero_audio.runtime import configure_tool_path
from zotero_audio.notifications import report_failure
from zotero_audio.zotero_local import discover_zotero_pdfs


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
    parser.add_argument("--voice", default=DEFAULT_ENGLISH_VOICE)
    parser.add_argument("--norwegian-voice", default="bf_emma")
    parser.add_argument("--speed", type=float, default=1.0)
    parser.add_argument("--max-chars", type=int, default=900)
    parser.add_argument("--zotero-db", type=Path, default=Path.home() / "Zotero" / "zotero.sqlite")
    parser.add_argument("--notify", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--podcast-config", type=Path,
                        help="Podcast TOML config (auto-detects <runtime>/podcast.toml when omitted)")
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
    configure_tool_path()
    storage = args.zotero_storage.expanduser().resolve()
    destination = args.destination.expanduser().resolve()
    state_dir = args.state_dir.expanduser().resolve()
    state_dir.mkdir(parents=True, exist_ok=True)
    notification_state = state_dir / "notification-state.json"
    lock_path = state_dir / "automatic-sync.lock"

    with lock_path.open("w", encoding="utf-8") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("Another automatic synchronization is already running")
            return 0

        if not discover_zotero_pdfs(storage):
            print(f"No PDFs found in {storage}; nothing to do")
            return 0

        manifest_path = state_dir / "batch-manifest.json"
        before = _load_manifest(manifest_path)
        log_file = args.log_file.expanduser().resolve() if args.log_file else state_dir / "batch.log"
        batch_args = [
            "--zotero-storage",
            str(storage),
            "--zotero-db",
            str(args.zotero_db.expanduser().resolve()),
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

        # New bundles receive Zotero metadata before narration. This pass also
        # upgrades older manifests and writes a bundle-local metadata snapshot.
        if manifest_path.is_file():
            try:
                finalize_main(["--zotero-db", str(args.zotero_db.expanduser().resolve()), "--batch-manifest", str(manifest_path)])
            except Exception as exc:
                print(f"Metadata finalization failed: {type(exc).__name__}: {exc}")
                result = 1

        # Podcast publication reuses already-rendered audio. It must never
        # invoke TTS or rebuild podcast artifacts during the automatic sync.
        podcast_config_path = (args.podcast_config.expanduser().resolve() if args.podcast_config
                               else (state_dir.parent / "podcast.toml"))
        if podcast_config_path.is_file() and manifest_path.is_file():
            podcast_config = load_podcast_config(podcast_config_path)
            try:
                feed_result = publish_existing_audio(
                    podcast_config, batch_manifest_path=manifest_path
                )
                print(f"Podcast feed sync: {feed_result}")
            except Exception as exc:
                print(f"Podcast feed sync failed (audio retained): {type(exc).__name__}: {exc}")
                result = 1
            health_path = podcast_config.state_root / "health-report.json"
            public_ready = podcast_config.public_root is not None and podcast_config.public_root.is_dir()
            if podcast_config.publishing_enabled and not podcast_config.dry_run and podcast_config.r2_bucket and public_ready:
                sync_script = Path(__file__).with_name("sync_public_r2.py")
                sync_result = subprocess.run(
                    [sys.executable, str(sync_script), "--root", str(podcast_config.public_root),
                     "--bucket", podcast_config.r2_bucket,
                     "--state-file", str(podcast_config.state_root / "r2-sync-manifest.json")],
                    check=False, text=True, capture_output=True,
                )
                if sync_result.stdout:
                    print(sync_result.stdout, end="")
                if sync_result.returncode:
                    detail = (sync_result.stderr or sync_result.stdout).strip()
                    print(f"Podcast R2 sync failed: {detail}")
                    result = 1
                    report_failure(notification_state, "public-sync", detail.splitlines()[-1] if detail else f"exit {sync_result.returncode}",
                                   "Podcast public mirror sync failed. See the Zotero Audio sync log.",
                                   lambda message: _notify(message, title="Zotero Audio — action required"), enabled=args.notify)
                else:
                    report_failure(notification_state, "public-sync", None, "", _notify)
            health_due = not health_path.is_file() or time.time() - health_path.stat().st_mtime >= 86_400
            feed_ready = (podcast_config.public_root is not None and
                          any((podcast_config.public_root / edition / "feed.xml").is_file() for edition in EDITIONS))
            if podcast_config.publishing_enabled and health_due and feed_ready:
                try:
                    health = health_check(podcast_config, remote=True)
                    if health["status"] != "pass":
                        raise RuntimeError("; ".join(health["failures"][:3]))
                    report_failure(notification_state, "feed-health", None, "", _notify)
                except Exception as exc:
                    print(f"Podcast health check failed: {type(exc).__name__}: {exc}")
                    result = 1
                    report_failure(notification_state, "feed-health", str(exc),
                                   "Podcast feed health check needs attention",
                                   lambda message: _notify(message, title="Zotero Audio — action required"), enabled=args.notify)

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
