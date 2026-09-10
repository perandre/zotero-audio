#!/usr/bin/env python3
"""Build and publish the Brief edition for every PDF in a Zotero library."""

from __future__ import annotations

import argparse
import fcntl
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

from zotero_audio.audio import DEFAULT_ENGLISH_VOICE, MlxKokoroBackend
from zotero_audio.pipeline import prepare_bundle
from zotero_audio.podcast import build_local_podcast, health_check, load_podcast_config
from zotero_audio.util import atomic_write_json, load_json, sha256_file
from zotero_audio.zotero import (
    DEFAULT_ZOTERO_DB,
    bundle_metadata_snapshot,
)
from zotero_audio.zotero_local import discover_zotero_pdfs, zotero_metadata_many_preferred


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--zotero-storage", type=Path, default=Path.home() / "Zotero" / "storage")
    parser.add_argument("--zotero-db", type=Path, default=DEFAULT_ZOTERO_DB)
    parser.add_argument(
        "--state-dir",
        type=Path,
        default=Path.home() / "Sites" / "zotero-audio-runtime" / "full-library",
    )
    parser.add_argument(
        "--podcast-config",
        type=Path,
        default=Path.home() / "Sites" / "zotero-audio-runtime" / "podcast.toml",
    )
    parser.add_argument("--model", default="mlx-community/Kokoro-82M-bf16")
    parser.add_argument("--voice", default=DEFAULT_ENGLISH_VOICE)
    parser.add_argument("--speed", type=float, default=1.0)
    parser.add_argument("--max-chars", type=int, default=900)
    parser.add_argument(
        "--select-all",
        action="store_true",
        help="Treat this run as explicit publication intent for every otherwise eligible source",
    )
    parser.add_argument(
        "--no-force-extract",
        action="store_true",
        help="Reuse a current bundle when it already matches the source and extraction options",
    )
    parser.add_argument("--no-r2-sync", action="store_true")
    return parser


def _bundle_for_key(bundles_dir: Path, key: str) -> Path | None:
    matches = [path for path in bundles_dir.iterdir() if path.is_dir() and path.name.endswith(f"[{key}]")]
    return matches[0] if len(matches) == 1 else None


def _license_record(podcast_state: Path, source_sha: str) -> dict[str, Any] | None:
    evidence_path = podcast_state / "license-evidence" / f"{source_sha}.json"
    if not evidence_path.is_file():
        return None
    evidence = load_json(evidence_path)
    record = evidence.get("record")
    return record if isinstance(record, dict) else None


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    storage = args.zotero_storage.expanduser().resolve()
    database = args.zotero_db.expanduser().resolve()
    state_dir = args.state_dir.expanduser().resolve()
    bundles_dir = state_dir / "bundles"
    state_dir.mkdir(parents=True, exist_ok=True)
    bundles_dir.mkdir(parents=True, exist_ok=True)
    config = load_podcast_config(args.podcast_config.expanduser().resolve())
    discovered = discover_zotero_pdfs(storage)
    pdfs = [path for _, path in discovered]
    if not pdfs:
        raise SystemExit(f"No PDFs found through Zotero or in {storage}")

    # Do not race the five-minute automatic synchronizer while replacing
    # extraction bundles or writing the shared publication manifest.
    lock_path = state_dir / "automatic-sync.lock"
    with lock_path.open("w", encoding="utf-8") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("Another automatic synchronization is already running; retry after it finishes", file=sys.stderr)
            return 2

        keys = [key for key, _ in discovered]
        metadata_by_key = zotero_metadata_many_preferred(database, keys)
        report_path = state_dir / "brief-library-manifest.json"
        report: dict[str, Any] = {
            "schema": "zotero-audio-brief-library/v1",
            "source_root": str(storage),
            "pdf_count": len(pdfs),
            "items": [],
        }
        backend: MlxKokoroBackend | None = None

        for number, (key, pdf) in enumerate(discovered, start=1):
            source_sha = sha256_file(pdf)
            metadata = metadata_by_key.get(key) or {}
            print(f"[{number}/{len(pdfs)}] briefing {key}: {pdf.name}", flush=True)
            item: dict[str, Any] = {
                "zotero_key": key,
                "source_path": str(pdf),
                "source_sha256": source_sha,
                "selected": bool(args.select_all or metadata.get("podcast_selected")),
            }
            try:
                bundle, structure, _, reused = prepare_bundle(
                    pdf,
                    zotero_key=key,
                    output_root=bundles_dir,
                    include_references=False,
                    max_chars=args.max_chars,
                    force=not args.no_force_extract,
                    metadata=metadata,
                )
                if metadata:
                    atomic_write_json(
                        bundle / "metadata.json",
                        bundle_metadata_snapshot(metadata, attachment_key=key, source_sha256=source_sha),
                    )
                if backend is None and not config.dry_run:
                    backend = MlxKokoroBackend(
                        model_id=args.model,
                        voice=args.voice,
                        speed=args.speed,
                        language="a",
                    )
                license_record = _license_record(config.state_root, source_sha)
                result = build_local_podcast(
                    bundle,
                    backend=backend,
                    config=config,
                    selected=item["selected"],
                    license_record=license_record,
                    metadata=metadata,
                    edition="brief",
                )
                item.update(
                    {
                        "status": "ready" if result.get("editions", {}).get("brief", {}).get("status")
                        in {"planned", "private_ready"}
                        else result.get("state", "needs_review"),
                        "bundle": str(bundle),
                        "prepared_reused": reused,
                        "state": result.get("state"),
                        "brief": result.get("brief"),
                        "content_qa": result.get("content_qa"),
                        "published": result.get("published", False),
                        "feed_changed": result.get("feed_changed", False),
                    }
                )
                if result.get("editions", {}).get("brief", {}).get("status") == "private_ready":
                    item["audio"] = result["editions"]["brief"].get("audio")
                print(f"[{number}/{len(pdfs)}] {key}: {item['status']} ({item.get('brief')})", flush=True)
            except Exception as exc:
                item.update({"status": "needs_review", "error": f"{type(exc).__name__}: {exc}"})
                print(f"[{number}/{len(pdfs)}] {key}: needs_review ({item['error']})", flush=True)
            report["items"].append(item)
            report["items"].sort(key=lambda value: value["source_path"])
            report["ready_count"] = sum(value.get("status") == "ready" for value in report["items"])
            report["review_count"] = sum(value.get("status") == "needs_review" for value in report["items"])
            atomic_write_json(report_path, report)

        if (
            not args.no_r2_sync
            and config.publishing_enabled
            and not config.dry_run
            and config.public_root
            and config.r2_bucket
        ):
            sync_script = Path(__file__).with_name("sync_public_r2.py")
            sync = subprocess.run(
                [
                    sys.executable,
                    str(sync_script),
                    "--root",
                    str(config.public_root),
                    "--bucket",
                    config.r2_bucket,
                    "--state-file",
                    str(config.state_root / "r2-sync-manifest.json"),
                ],
                check=False,
                text=True,
            )
            if sync.returncode:
                report["r2_sync"] = "failed"
                atomic_write_json(report_path, report)
                return 1
            report["r2_sync"] = "pass"

        if config.publishing_enabled and not config.dry_run and config.public_root:
            health = health_check(config, remote=True)
            report["health"] = health
            atomic_write_json(report_path, report)
            if health["status"] != "pass":
                return 1

        atomic_write_json(report_path, report)
        print(json.dumps({key: report.get(key) for key in ("pdf_count", "ready_count", "review_count", "r2_sync")}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
