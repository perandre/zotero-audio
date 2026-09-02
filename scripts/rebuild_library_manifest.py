#!/usr/bin/env python3
"""Rebuild a verified batch manifest from completed library outputs."""

from __future__ import annotations

import argparse
from pathlib import Path

from zotero_audio.audio import inspect_m4a
from zotero_audio.util import atomic_write_json, load_json, sha256_file


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--zotero-storage", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--state-dir", type=Path, required=True)
    args = parser.parse_args()

    storage = args.zotero_storage.expanduser().resolve()
    destination = args.destination.expanduser().resolve()
    state_dir = args.state_dir.expanduser().resolve()
    pdfs = sorted(storage.glob("*/*.pdf"))
    bundles = list((state_dir / "bundles").iterdir())
    items = []
    for pdf in pdfs:
        key = pdf.relative_to(storage).parts[0]
        bundle = next((path for path in bundles if path.name.endswith(f"[{key}]")), None)
        outputs = [path for path in destination.glob("*.m4a") if path.stem.endswith(f"[{key}]")]
        if bundle is None or len(outputs) != 1:
            raise RuntimeError(f"Expected one bundle and output for {key}")
        plan = load_json(bundle / "speech-plan.json")
        output = outputs[0]
        technical = inspect_m4a(output)
        language = "nb" if key == "WSQ3WHDC" else "en"
        items.append(
            {
                "status": "complete",
                "zotero_key": key,
                "source_path": str(pdf.resolve()),
                "source_sha256": sha256_file(pdf),
                "title": plan["document"]["title"],
                "language": language,
                "kokoro_language_code": "b" if language == "nb" else "a",
                "voice": "bf_emma" if language == "nb" else "af_heart",
                "segments": len(plan["segments"]),
                "output_file": output.name,
                "output_sha256": sha256_file(output),
                "duration_seconds": technical["duration_seconds"],
                "sample_rate": technical["sample_rate"],
                "channels": technical["channels"],
                "codec": technical["codec"],
                "bitrate": technical["bitrate"],
            }
        )
    manifest = {
        "schema": "zotero-audio-library-batch/v1",
        "source_root": str(storage),
        "destination": str(destination),
        "engine": "kokoro-mlx",
        "model": "mlx-community/Kokoro-82M-bf16",
        "precision": "bf16",
        "workers": 1,
        "speed": 1.0,
        "pdf_count": len(pdfs),
        "complete_count": len(items),
        "failure_count": 0,
        "items": items,
    }
    path = state_dir / "batch-manifest.json"
    atomic_write_json(path, manifest)
    print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
