from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .audio import assemble_m4a, create_backend, synthesize_plan
from .extract import DEFAULT_ZOTERO_STORAGE, resolve_pdf
from .models import default_model_dir, install_kokoro_models
from .pipeline import prepare_bundle


def _source_arguments(parser: argparse.ArgumentParser) -> None:
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--pdf", help="Path to a local PDF")
    source.add_argument("--zotero-key", help="Zotero attachment key under storage/<key>/")
    parser.add_argument("--zotero-storage", type=Path, default=DEFAULT_ZOTERO_STORAGE)


def _prepare_arguments(parser: argparse.ArgumentParser) -> None:
    _source_arguments(parser)
    parser.add_argument("--output-root", type=Path, default=Path("outputs"))
    parser.add_argument("--include-references", action="store_true")
    parser.add_argument("--max-chars", type=int, default=900)
    parser.add_argument("--force", action="store_true")


def _synthesis_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--engine",
        choices=("kokoro-mlx", "kokoro-onnx"),
        default="kokoro-mlx",
        help="Kokoro runtime; MLX BF16 is the quality default",
    )
    parser.add_argument("--voice", help="Kokoro voice preset; defaults to af_heart")
    parser.add_argument("--speed", type=float, default=1.0, help="Kokoro speed multiplier")
    parser.add_argument("--language", default="a", help="Kokoro language code (a=US English, b=British English)")
    parser.add_argument("--mlx-model", default="mlx-community/Kokoro-82M-bf16")
    model_dir = default_model_dir()
    parser.add_argument("--model", type=Path, default=model_dir / "kokoro-v1.0.int8.onnx")
    parser.add_argument("--voices", type=Path, default=model_dir / "voices-v1.0.bin")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="zotero-audio",
        description="Convert local Zotero PDFs to provenance-rich Markdown and AAC/M4A audio.",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    prepare = commands.add_parser("prepare", help="Extract PDF text and create a speech plan")
    _prepare_arguments(prepare)

    synthesize = commands.add_parser("synthesize", help="Generate or resume per-segment speech")
    synthesize.add_argument("bundle", type=Path)
    _synthesis_arguments(synthesize)

    assemble = commands.add_parser("assemble", help="Validate segments and encode AAC/M4A")
    assemble.add_argument("bundle", type=Path)
    assemble.add_argument("--bitrate", type=int, default=64_000)

    run = commands.add_parser("run", help="Prepare, synthesize, and assemble one PDF")
    _prepare_arguments(run)
    _synthesis_arguments(run)
    run.add_argument("--bitrate", type=int, default=64_000)

    models = commands.add_parser("models", help="Manage optional local TTS models")
    model_commands = models.add_subparsers(dest="model_command", required=True)
    install = model_commands.add_parser("install", help="Download verified Kokoro INT8 model files")
    install.add_argument("--model-dir", type=Path, default=default_model_dir())
    install.add_argument("--force", action="store_true")
    return parser


def _backend_from_args(args: argparse.Namespace):
    return create_backend(
        args.engine,
        voice=args.voice,
        speed=args.speed,
        language=args.language,
        model=args.model.expanduser().resolve(),
        voices=args.voices.expanduser().resolve(),
        mlx_model=args.mlx_model,
    )


def _prepare(args: argparse.Namespace) -> tuple[Path, bool]:
    pdf = resolve_pdf(args.pdf, args.zotero_key, args.zotero_storage)
    bundle, _, _, reused = prepare_bundle(
        pdf,
        zotero_key=args.zotero_key,
        output_root=args.output_root,
        include_references=args.include_references,
        max_chars=args.max_chars,
        force=args.force,
    )
    return bundle, reused


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "models":
            paths = install_kokoro_models(args.model_dir, force=args.force)
            for path in paths:
                print(path)
            return 0
        if args.command == "prepare":
            bundle, reused = _prepare(args)
            print(f"{'Reused' if reused else 'Prepared'} {bundle}")
            return 0
        if args.command == "synthesize":
            backend = _backend_from_args(args)
            manifest, reused = synthesize_plan(args.bundle.expanduser().resolve(), backend)
            print(f"Synthesized {len(manifest['segments']) - reused}; reused {reused} segments")
            return 0
        if args.command == "assemble":
            output, _ = assemble_m4a(args.bundle.expanduser().resolve(), bitrate=args.bitrate)
            print(output)
            return 0
        if args.command == "run":
            bundle, prepared_reused = _prepare(args)
            backend = _backend_from_args(args)
            manifest, segment_reused = synthesize_plan(bundle, backend)
            output, _ = assemble_m4a(bundle, bitrate=args.bitrate)
            print(f"Bundle: {bundle} ({'reused' if prepared_reused else 'prepared'})")
            print(f"Segments: {len(manifest['segments']) - segment_reused} generated, {segment_reused} reused")
            print(f"Audio: {output}")
            return 0
        raise AssertionError(f"Unhandled command: {args.command}")
    except (FileExistsError, FileNotFoundError, RuntimeError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
