#!/usr/bin/env python3
"""Render every PDF in a local Zotero storage directory to AAC/M4A."""

from __future__ import annotations

import argparse
import logging
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any

from zotero_audio.audio import DEFAULT_ENGLISH_VOICE, MlxKokoroBackend, assemble_m4a, inspect_m4a, synthesize_plan
from zotero_audio.extract import extract_pdf
from zotero_audio.pipeline import prepare_bundle
from zotero_audio.util import atomic_write_json, load_json, sha256_file
from zotero_audio.zotero import (
    DEFAULT_ZOTERO_DB,
    METADATA_SCHEMA,
    bibliographic_metadata,
    bundle_metadata_snapshot,
    zotero_metadata_many,
)


NORWEGIAN_WORDS = {"av", "den", "det", "en", "er", "for", "ikke", "med", "og", "på", "som", "til"}
ENGLISH_WORDS = {"a", "and", "for", "in", "is", "of", "that", "the", "this", "to", "with"}


def detect_language(structure: dict[str, Any]) -> str:
    sample = " ".join(
        block["text"]
        for block in structure["blocks"]
        if block["included_in_reading"]
    )[:20_000].casefold()
    words = re.findall(r"[a-zæøå]+", sample)
    norwegian = sum(word in NORWEGIAN_WORDS for word in words)
    english = sum(word in ENGLISH_WORDS for word in words)
    return "nb" if norwegian > english * 1.15 else "en"


def copy_atomic(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{destination.name}.", dir=destination.parent)
    os.close(descriptor)
    temporary_path = Path(temporary)
    try:
        shutil.copyfile(source, temporary_path)
        with temporary_path.open("rb") as stream:
            os.fsync(stream.fileno())
        os.replace(temporary_path, destination)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--zotero-storage", type=Path, default=Path.home() / "Zotero" / "storage")
    parser.add_argument("--zotero-db", type=Path, default=DEFAULT_ZOTERO_DB)
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument(
        "--state-dir",
        type=Path,
        default=Path.home() / "Sites" / "zotero-audio-runtime" / "full-library",
    )
    parser.add_argument("--log-file", type=Path)
    parser.add_argument("--manifest-name", default="batch-manifest.json")
    parser.add_argument("--keys", help="Optional comma-separated Zotero attachment keys")
    parser.add_argument("--model", default="mlx-community/Kokoro-82M-bf16")
    parser.add_argument("--voice", default=DEFAULT_ENGLISH_VOICE)
    parser.add_argument("--norwegian-voice", default="bf_emma")
    parser.add_argument("--speed", type=float, default=1.0)
    parser.add_argument("--max-chars", type=int, default=900)
    parser.add_argument("--force", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    storage = args.zotero_storage.expanduser().resolve()
    destination = args.destination.expanduser().resolve()
    state_dir = args.state_dir.expanduser().resolve()
    bundles_dir = state_dir / "bundles"
    manifest_path = state_dir / args.manifest_name
    destination.mkdir(parents=True, exist_ok=True)
    log_path = args.log_file.expanduser().resolve() if args.log_file else state_dir / "batch.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(filename=log_path, level=logging.INFO, format="%(asctime)s %(message)s")
    log = logging.getLogger("batch")

    def progress(message: str, *, announce: bool = False) -> None:
        log.info(message)
        if announce:
            print(message, flush=True)
    pdfs = sorted(path.resolve() for path in storage.glob("*/*.pdf"))
    requested_keys = {key.strip() for key in args.keys.split(",")} if args.keys else None
    if requested_keys is not None:
        pdfs = [path for path in pdfs if path.relative_to(storage).parts[0] in requested_keys]
    if not pdfs:
        raise SystemExit(f"No PDFs found in {storage}")

    metadata_by_key: dict[str, dict[str, Any]] = {}
    try:
        metadata_by_key = zotero_metadata_many(
            args.zotero_db, (path.relative_to(storage).parts[0] for path in pdfs)
        )
    except (FileNotFoundError, OSError) as exc:
        progress(f"Zotero metadata unavailable; PDF metadata fallback will be used: {exc}")

    previous_items: dict[str, dict[str, Any]] = {}
    if manifest_path.exists() and not args.force:
        previous = load_json(manifest_path)
        previous_items = {
            item["source_path"]: item
            for item in previous.get("items", [])
            if item.get("status") == "complete"
        }
    # Delay loading the MLX model until a source actually needs processing.
    # Automatic runs can then scan and exit cheaply when the library is up to date.
    backend: MlxKokoroBackend | None = None
    manifest: dict[str, Any] = {
        "schema": "zotero-audio-library-batch/v1",
        "source_root": str(storage),
        "destination": str(destination),
        "engine": "kokoro-mlx",
        "model": args.model,
        "precision": "bf16",
        "workers": 1,
        "speed": args.speed,
        "pdf_count": len(pdfs),
        "items": [],
    }
    failures = 0

    def metadata_with_pdf_rights(pdf: Path, metadata: dict[str, Any] | None,
                                 structure: dict[str, Any] | None = None) -> dict[str, Any]:
        value = dict(metadata or {})
        if value.get("rights"):
            return value
        document = (structure or {}).get("document", {})
        if document.get("rights"):
            value.update({key: document[key] for key in ("rights", "rights_source") if document.get(key)})
            return value
        inferred = extract_pdf(pdf, zotero_key=pdf.relative_to(storage).parts[0],
                               include_references=False, metadata=metadata)
        value.update({key: inferred["document"][key] for key in ("rights", "rights_source")
                      if inferred["document"].get(key)})
        return value

    for number, pdf in enumerate(pdfs, start=1):
        key = pdf.relative_to(storage).parts[0]
        zotero_item = metadata_by_key.get(key)
        progress(f"[{number}/{len(pdfs)}] preparing {key}: {pdf.name}")
        source_sha: str | None = None
        try:
            source_sha = sha256_file(pdf)
            previous = previous_items.get(str(pdf))
            if previous and previous.get("source_sha256") == source_sha and not (
                zotero_item and zotero_item.get("podcast_selected")
            ):
                existing_output = destination / previous["output_file"]
                if existing_output.is_file() and sha256_file(existing_output) == previous["output_sha256"]:
                    manifest["items"].append(previous)
                    atomic_write_json(manifest_path, manifest)
                    progress(f"[{number}/{len(pdfs)}] verified existing {existing_output.name}")
                    continue

            # A code or metadata-plan update can make prepare_bundle reject a
            # previously generated bundle even though its verified listener
            # copy is still valid. Reuse that immutable result and refresh the
            # Zotero metadata snapshot instead of needlessly re-rendering it.
            existing_bundles = [
                path for path in bundles_dir.iterdir()
                if path.is_dir() and path.name.endswith(f"[{key}]")
            ]
            if len(existing_bundles) == 1 and not args.force:
                existing_bundle = existing_bundles[0]
                existing_structure = load_json(existing_bundle / "structure.json")
                existing_run = load_json(existing_bundle / "run-manifest.json")
                existing_qa = load_json(existing_bundle / "qa-report.json")
                existing_source_sha = str(existing_structure.get("source", {}).get("sha256", ""))
                existing_output_name = Path(str(existing_qa.get("output", {}).get("path", ""))).name
                existing_output = destination / existing_output_name
                if not existing_output.is_file():
                    matching_outputs = [
                        path for path in destination.glob("*.m4a")
                        if path.name.endswith(f"[{key}].m4a")
                    ]
                    if len(matching_outputs) == 1:
                        existing_output = matching_outputs[0]
                        existing_output_name = existing_output.name
                if (
                    existing_source_sha == source_sha
                    and existing_run.get("status") == "complete"
                    and existing_qa.get("status") == "pass"
                    and existing_output_name
                    and existing_output.is_file()
                ):
                    output_info = inspect_m4a(existing_output)
                    existing_plan = load_json(existing_bundle / "speech-plan.json")
                    language = detect_language(existing_structure)
                    item = {
                        "status": "complete",
                        "zotero_key": key,
                        "source_path": str(pdf),
                        "source_sha256": source_sha,
                        "title": existing_structure["document"]["title"],
                        "language": language,
                        "kokoro_language_code": "b" if language == "nb" else "a",
                        "voice": args.norwegian_voice if language == "nb" else args.voice,
                        "segments": len(existing_plan.get("segments", [])),
                        "segments_generated": 0,
                        "segments_reused": len(existing_plan.get("segments", [])),
                        "prepared_reused": True,
                        "output_file": existing_output.name,
                        "output_sha256": sha256_file(existing_output),
                        **output_info,
                    }
                    if zotero_item:
                        metadata_value = metadata_with_pdf_rights(pdf, zotero_item, existing_structure)
                        item.update(bibliographic_metadata(metadata_value))
                        item.update({"metadata_finalized": True, "metadata_schema": METADATA_SCHEMA})
                        atomic_write_json(
                            existing_bundle / "metadata.json",
                            bundle_metadata_snapshot(metadata_value, attachment_key=key, source_sha256=source_sha),
                        )
                    manifest["items"].append(item)
                    atomic_write_json(manifest_path, manifest)
                    progress(f"[{number}/{len(pdfs)}] reused verified existing {existing_output.name}")
                    continue

            bundle, structure, plan, prepared_reused = prepare_bundle(
                pdf,
                zotero_key=key,
                output_root=bundles_dir,
                include_references=False,
                max_chars=args.max_chars,
                force=args.force,
                metadata=zotero_item,
            )
            language = detect_language(structure)
            voice = args.norwegian_voice if language == "nb" else args.voice
            language_code = "b" if language == "nb" else "a"
            if backend is None:
                backend = MlxKokoroBackend(
                    model_id=args.model,
                    voice=args.voice,
                    speed=args.speed,
                    language="a",
                )
            backend.configure(voice=voice, speed=args.speed, language=language_code)
            run_manifest, segment_reused = synthesize_plan(bundle, backend)
            generated = len(run_manifest["segments"]) - segment_reused
            final, qa = assemble_m4a(bundle)
            delivered = destination / final.name
            copy_atomic(final, delivered)
            delivered_info = inspect_m4a(delivered)
            delivered_sha = sha256_file(delivered)
            if delivered_sha != qa["output"]["sha256"]:
                raise RuntimeError("Delivered M4A checksum does not match verified bundle output")
            item = {
                "status": "complete",
                "zotero_key": key,
                "source_path": str(pdf),
                "source_sha256": source_sha,
                "title": structure["document"]["title"],
                "language": language,
                "kokoro_language_code": language_code,
                "voice": voice,
                "segments": len(plan["segments"]),
                "segments_generated": generated,
                "segments_reused": segment_reused,
                "prepared_reused": prepared_reused,
                "output_file": delivered.name,
                "output_sha256": delivered_sha,
                "duration_seconds": delivered_info["duration_seconds"],
                "sample_rate": delivered_info["sample_rate"],
                "channels": delivered_info["channels"],
                "codec": delivered_info["codec"],
                "bitrate": delivered_info["bitrate"],
            }
            if zotero_item:
                metadata_value = metadata_with_pdf_rights(pdf, zotero_item, structure)
                item.update(bibliographic_metadata(metadata_value))
                item.update({"metadata_finalized": True, "metadata_schema": METADATA_SCHEMA})
                atomic_write_json(
                    bundle / "metadata.json",
                    bundle_metadata_snapshot(metadata_value, attachment_key=key, source_sha256=source_sha),
                )
            manifest["items"].append(item)
            atomic_write_json(manifest_path, manifest)
            progress(
                f"[{number}/{len(pdfs)}] wrote {delivered.name} "
                f"({item['duration_seconds'] / 60:.1f} min, {generated} generated/{segment_reused} reused)",
            )
        except Exception as exc:
            failures += 1
            manifest["items"].append(
                {
                    "status": "failed",
                    "zotero_key": key,
                    "source_path": str(pdf),
                    "source_sha256": source_sha,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
            atomic_write_json(manifest_path, manifest)
            progress(f"[{number}/{len(pdfs)}] FAILED {key}: {type(exc).__name__}: {exc}")

    manifest["complete_count"] = sum(item["status"] == "complete" for item in manifest["items"])
    manifest["failure_count"] = failures
    atomic_write_json(manifest_path, manifest)
    progress(
        f"Batch complete: {manifest['complete_count']}/{len(pdfs)} files; {failures} failures; "
        f"manifest {manifest_path}",
        announce=True,
    )
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
