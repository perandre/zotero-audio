#!/usr/bin/env python3
"""Build and publish Full Reading editions for every PDF in a Zotero collection."""

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
from zotero_audio.zotero import DEFAULT_ZOTERO_DB, bundle_metadata_snapshot
from zotero_audio.zotero_local import discover_zotero_pdfs, zotero_metadata_many_preferred


DEFAULT_STORAGE = Path.home() / "Zotero" / "storage"
DEFAULT_STATE = Path.home() / "Sites" / "zotero-audio-runtime" / "full-library"
DEFAULT_CONFIG = Path.home() / "Sites" / "zotero-audio-runtime" / "podcast.toml"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--zotero-storage", type=Path, default=DEFAULT_STORAGE)
    parser.add_argument("--zotero-db", type=Path, default=DEFAULT_ZOTERO_DB)
    parser.add_argument("--state-dir", type=Path, default=DEFAULT_STATE)
    parser.add_argument("--podcast-config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--collection", default="00 Inbox", help="Exact Zotero collection name to include")
    parser.add_argument("--model", default="mlx-community/Kokoro-82M-bf16")
    parser.add_argument("--voice", default=DEFAULT_ENGLISH_VOICE)
    parser.add_argument("--speed", type=float, default=1.0)
    parser.add_argument("--max-chars", type=int, default=900)
    parser.add_argument("--no-force-extract", action="store_true")
    parser.add_argument("--no-r2-sync", action="store_true")
    return parser


def _license_record(state_root: Path, source_sha: str) -> dict[str, Any] | None:
    evidence_path = state_root / "license-evidence" / f"{source_sha}.json"
    if not evidence_path.is_file():
        return None
    evidence = load_json(evidence_path)
    record = evidence.get("record")
    return record if isinstance(record, dict) else None


def _in_collection(metadata: dict[str, Any], collection: str) -> bool:
    wanted = collection.strip().casefold()
    return any(str(value).strip().casefold() == wanted for value in metadata.get("collections", []))


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

    keys = [key for key, _ in discovered]
    metadata_by_key = zotero_metadata_many_preferred(database, keys)
    inbox = [
        (pdf, key, metadata_by_key.get(key) or {})
        for key, pdf in discovered
        if _in_collection(metadata_by_key.get(key) or {}, args.collection)
    ]

    lock_path = state_dir / "automatic-sync.lock"
    with lock_path.open("w", encoding="utf-8") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("Another automatic synchronization is already running; retry after it finishes", file=sys.stderr)
            return 2

        report_path = state_dir / "inbox-full-manifest.json"
        report: dict[str, Any] = {
            "schema": "zotero-audio-inbox-full/v1",
            "source_root": str(storage),
            "collection": args.collection,
            "pdf_count": len(pdfs),
            "collection_pdf_count": len(inbox),
            "items": [],
        }
        backend: MlxKokoroBackend | None = None

        for number, (pdf, key, metadata) in enumerate(inbox, start=1):
            source_sha = sha256_file(pdf)
            print(f"[{number}/{len(inbox)}] full reading {key}: {pdf.name}", flush=True)
            item: dict[str, Any] = {
                "zotero_key": key,
                "source_path": str(pdf),
                "source_sha256": source_sha,
                "collection": args.collection,
                "selected": True,
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
                result = build_local_podcast(
                    bundle,
                    backend=backend,
                    config=config,
                    selected=True,
                    license_record=_license_record(config.state_root, source_sha),
                    metadata=metadata,
                    edition="full",
                )
                full = result.get("editions", {}).get("full", {})
                item.update(
                    {
                        "status": "ready" if full.get("status") == "private_ready" else result.get("state", "needs_review"),
                        "bundle": str(bundle),
                        "prepared_reused": reused,
                        "state": result.get("state"),
                        "content_qa": result.get("content_qa"),
                        "published": result.get("published", False),
                        "feed_changed": result.get("feed_changed", False),
                    }
                )
                if full.get("status") == "private_ready":
                    item["audio"] = full.get("audio")
                print(f"[{number}/{len(inbox)}] {key}: {item['status']}", flush=True)
            except Exception as exc:
                item.update({"status": "needs_review", "error": f"{type(exc).__name__}: {exc}"})
                print(f"[{number}/{len(inbox)}] {key}: needs_review ({item['error']})", flush=True)
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
        print(json.dumps({key: report.get(key) for key in ("pdf_count", "collection_pdf_count", "ready_count", "review_count", "r2_sync")}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
