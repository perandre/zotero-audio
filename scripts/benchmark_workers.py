#!/usr/bin/env python3
"""Benchmark one versus two persistent MLX Kokoro workers on a whole paper."""

from __future__ import annotations

import argparse
import logging
import multiprocessing as mp
import resource
import time
import uuid
from queue import Empty
from pathlib import Path
from typing import Any

from zotero_audio.audio import DEFAULT_ENGLISH_VOICE, MlxKokoroBackend, validate_wav
from zotero_audio.pipeline import prepare_bundle
from zotero_audio.util import atomic_write_json


def _worker(
    worker_id: int,
    segments: list[dict[str, Any]],
    output_dir: str,
    model: str,
    voice: str,
    language: str,
    result_queue: Any,
) -> None:
    worker_dir = Path(output_dir) / f"worker-{worker_id}"
    worker_dir.mkdir(parents=True, exist_ok=True)
    log = logging.getLogger(f"worker-{worker_id}")
    log.setLevel(logging.INFO)
    log.propagate = False
    handler = logging.FileHandler(worker_dir / "progress.log", encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
    log.addHandler(handler)
    started = time.perf_counter()
    try:
        backend_started = time.perf_counter()
        backend = MlxKokoroBackend(model_id=model, voice=voice, language=language)
        model_load_seconds = time.perf_counter() - backend_started
        audio_seconds = 0.0
        synthesis_started = time.perf_counter()
        for segment in segments:
            destination = worker_dir / f"{segment['ordinal']:04d}.wav"
            backend.synthesize(segment["text"], destination)
            audio_seconds += validate_wav(destination)["duration_seconds"]
            log.info("completed ordinal=%s", segment["ordinal"])
        result_queue.put(
            {
                "worker_id": worker_id,
                "status": "complete",
                "segments": len(segments),
                "audio_seconds": round(audio_seconds, 6),
                "model_load_seconds": round(model_load_seconds, 6),
                "synthesis_seconds": round(time.perf_counter() - synthesis_started, 6),
                "wall_seconds": round(time.perf_counter() - started, 6),
                "peak_rss_bytes": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
            }
        )
    except BaseException as exc:
        log.exception("failed")
        result_queue.put(
            {
                "worker_id": worker_id,
                "status": "failed",
                "error": f"{type(exc).__name__}: {exc}",
            }
        )
        raise
    finally:
        handler.close()


def run_workers(
    plan: dict[str, Any],
    *,
    workers: int,
    output_dir: Path,
    model: str,
    voice: str,
    language: str,
    timeout_seconds: float,
    log: logging.Logger,
) -> dict[str, Any]:
    segments = plan["segments"]
    shards = [segments[index::workers] for index in range(workers)]
    context = mp.get_context("spawn")
    result_queue = context.Queue()
    processes = [
        context.Process(
            target=_worker,
            args=(worker_id, shard, str(output_dir), model, voice, language, result_queue),
            name=f"kokoro-worker-{worker_id}",
        )
        for worker_id, shard in enumerate(shards)
    ]
    started = time.perf_counter()
    for process in processes:
        process.start()
    deadline = time.monotonic() + timeout_seconds
    while any(process.is_alive() for process in processes) and time.monotonic() < deadline:
        time.sleep(0.5)
    timed_out = any(process.is_alive() for process in processes)
    if timed_out:
        log.error("timeout workers=%s", workers)
        for process in processes:
            if process.is_alive():
                process.terminate()
    for process in processes:
        process.join(timeout=10)
    results: list[dict[str, Any]] = []
    while len(results) < workers:
        try:
            results.append(result_queue.get(timeout=5))
        except Empty:
            break
    results.sort(key=lambda item: item["worker_id"])
    status = "timeout" if timed_out else "complete" if all(item.get("status") == "complete" for item in results) and len(results) == workers else "failed"
    return {
        "status": status,
        "workers": workers,
        "wall_seconds": round(time.perf_counter() - started, 6),
        "worker_results": results,
        "exit_codes": [process.exitcode for process in processes],
        "timeout_seconds": timeout_seconds,
    }


def main(args: argparse.Namespace) -> int:
    runtime = args.runtime_dir.expanduser().resolve()
    benchmark_root = runtime / "benchmarks" / f"{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}"
    benchmark_root.mkdir(parents=True, exist_ok=True)
    log_path = benchmark_root / "benchmark.log"
    logging.basicConfig(filename=log_path, level=logging.INFO, format="%(asctime)s %(message)s")
    log = logging.getLogger("benchmark")
    results: list[dict[str, Any]] = []
    for segment_chars in args.segment_chars:
        bundle_root = benchmark_root / f"segments-{segment_chars}"
        bundle, structure, plan, _ = prepare_bundle(
            args.pdf.expanduser().resolve(),
            zotero_key=None,
            output_root=bundle_root,
            include_references=args.include_references,
            max_chars=segment_chars,
            force=True,
        )
        language = args.language
        voice = args.voice
        log.info("prepared chars=%s segments=%s bundle=%s", segment_chars, len(plan["segments"]), bundle)
        for workers in (1, 2):
            run_dir = benchmark_root / f"chars-{segment_chars}-workers-{workers}"
            result = run_workers(
                plan,
                workers=workers,
                output_dir=run_dir,
                model=args.model,
                voice=voice,
                language=language,
                timeout_seconds=args.timeout_minutes * 60,
                log=log,
            )
            result.update(
                {
                    "segment_chars": segment_chars,
                    "segment_count": len(plan["segments"]),
                    "bundle": str(bundle),
                    "model": args.model,
                    "voice": voice,
                    "language": language,
                }
            )
            results.append(result)
            atomic_write_json(benchmark_root / "results.json", {"results": results})
            log.info("finished chars=%s workers=%s status=%s", segment_chars, workers, result["status"])
    print(benchmark_root / "results.json")
    return 0 if all(result["status"] == "complete" for result in results) else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pdf", type=Path, required=True)
    parser.add_argument(
        "--runtime-dir",
        type=Path,
        default=Path.home() / "Sites" / "zotero-audio-runtime",
    )
    parser.add_argument("--segment-chars", type=int, nargs="+", default=[600, 900, 1200])
    parser.add_argument("--timeout-minutes", type=float, default=45)
    parser.add_argument("--model", default="mlx-community/Kokoro-82M-bf16")
    parser.add_argument("--voice", default=DEFAULT_ENGLISH_VOICE)
    parser.add_argument("--language", default="a")
    parser.add_argument("--include-references", action="store_true")
    return parser


if __name__ == "__main__":
    raise SystemExit(main(build_parser().parse_args()))
