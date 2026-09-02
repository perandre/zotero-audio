#!/usr/bin/env python3
"""Benchmark the quality-default Kokoro BF16 MLX path locally."""

from __future__ import annotations

import argparse
import resource
import time
from pathlib import Path

from zotero_audio.audio import MlxKokoroBackend, encode_wav_to_m4a, validate_wav
from zotero_audio.util import atomic_write_json, sha256_file


SAMPLE_TEXT = (
    "Local text to speech can make a research article easier to review while walking. "
    "A trustworthy pipeline keeps every spoken segment connected to its source page, records the exact "
    "engine configuration, and resumes without repeating completed work. This Kokoro benchmark includes "
    "abbreviations such as e.g. and i.e., the year 2026, and a measured quantity of 24.5 percent. The final "
    "sentence tests a slightly longer academic cadence, because clear phrasing matters more than dramatic "
    "delivery when listening to a dense paper."
)


def main(args: argparse.Namespace) -> int:
    args.output_dir.mkdir(parents=True, exist_ok=True)
    backend = MlxKokoroBackend(model=args.model)
    backend.configure(voice=args.voice, speed=args.speed, language=args.language)
    wav_path = args.output_dir / "kokoro-mlx-bf16.wav"
    started = time.perf_counter()
    backend.synthesize(SAMPLE_TEXT, wav_path)
    elapsed = time.perf_counter() - started
    info = validate_wav(wav_path)
    m4a_path = args.output_dir / "kokoro-mlx-bf16.m4a"
    encode_wav_to_m4a(
        wav_path,
        m4a_path,
        title="Zotero Audio Kokoro BF16 benchmark",
        artist="Zotero Audio",
        album="Local Kokoro benchmark",
        comment=f"wav-sha256={sha256_file(wav_path)}",
    )
    result = {
        "schema": "zotero-audio-kokoro-benchmark/v1",
        "sample_text": SAMPLE_TEXT,
        "engine": "kokoro-mlx",
        "config": backend.config,
        "input_chars": len(SAMPLE_TEXT),
        "input_words": len(SAMPLE_TEXT.split()),
        "generation_seconds": round(elapsed, 6),
        "audio_seconds": info["duration_seconds"],
        "realtime_factor": round(elapsed / info["duration_seconds"], 6),
        "peak_rss_bytes": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
        "wav_sha256": sha256_file(wav_path),
        "m4a_sha256": sha256_file(m4a_path),
    }
    atomic_write_json(args.output_dir / "benchmark.json", result)
    print(args.output_dir / "benchmark.json")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=Path("outputs/kokoro-benchmark"))
    parser.add_argument("--model", default="mlx-community/Kokoro-82M-bf16")
    parser.add_argument("--voice", default="af_heart")
    parser.add_argument("--speed", type=float, default=1.0)
    parser.add_argument("--language", default="a")
    return parser


if __name__ == "__main__":
    raise SystemExit(main(build_parser().parse_args()))
