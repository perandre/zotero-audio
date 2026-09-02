#!/usr/bin/env python3
"""Regenerate audited Kokoro segments with safe pre-phonemization chunking."""

from __future__ import annotations

import argparse
from pathlib import Path

from zotero_audio.audio import MlxKokoroBackend, validate_wav
from zotero_audio.util import atomic_write_json, load_json, sha256_file


def parse_target(value: str) -> tuple[str, set[int]]:
    key, separator, ordinals = value.partition(":")
    if not separator or not key or not ordinals:
        raise argparse.ArgumentTypeError("target must be KEY:ordinal,ordinal")
    try:
        return key, {int(ordinal) for ordinal in ordinals.split(",")}
    except ValueError as exc:
        raise argparse.ArgumentTypeError("ordinals must be integers") from exc


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundles-dir", type=Path, required=True)
    parser.add_argument("--target", action="append", type=parse_target, required=True)
    args = parser.parse_args()

    bundles = list(args.bundles_dir.expanduser().resolve().iterdir())
    targets = dict(args.target)
    backend = MlxKokoroBackend()
    for key, ordinals in targets.items():
        matches = [bundle for bundle in bundles if bundle.name.endswith(f"[{key}]")]
        if len(matches) != 1:
            raise RuntimeError(f"Expected one bundle for {key}, found {len(matches)}")
        bundle = matches[0]
        plan = load_json(bundle / "speech-plan.json")
        manifest = load_json(bundle / "run-manifest.json")
        plan_segments = {segment["ordinal"]: segment for segment in plan["segments"]}
        records = {record["ordinal"]: record for record in manifest["segments"]}
        missing = ordinals - plan_segments.keys() | ordinals - records.keys()
        if missing:
            raise RuntimeError(f"Missing ordinals for {key}: {sorted(missing)}")

        config = manifest["synthesis_config"]
        backend.configure(
            voice=config["voice"], speed=float(config["speed"]), language=config["language"]
        )
        corrected_ordinals: set[int] = set()
        corrected_paths: set[str] = set()
        for ordinal in sorted(ordinals):
            segment = plan_segments[ordinal]
            record = records[ordinal]
            if record["path"] in corrected_paths:
                continue
            path = bundle / record["path"]
            backend.synthesize(segment["text"], path)
            audio_info = validate_wav(path)
            audio_sha = sha256_file(path)
            shared_records = [item for item in manifest["segments"] if item["path"] == record["path"]]
            for shared in shared_records:
                shared.update(audio_info)
                shared["sha256"] = audio_sha
                shared["synthesis_override"] = backend.config
                shared["quality_correction"] = "safe pre-phonemization chunking and checksum-verified re-render"
                corrected_ordinals.add(shared["ordinal"])
            corrected_paths.add(record["path"])
            print(f"corrected {key} shared segment(s) {sorted(item['ordinal'] for item in shared_records)}", flush=True)

        existing_corrections = set(manifest.get("quality_corrections", {}).get("ordinals", []))
        manifest["quality_corrections"] = {
            "reason": "phoneme-limit audit or checksum validation required a safe re-render",
            "ordinals": sorted(existing_corrections | corrected_ordinals),
            "synthesis_override": backend.config,
        }
        atomic_write_json(bundle / "run-manifest.json", manifest)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
