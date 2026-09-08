from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .audio import assemble_m4a, create_backend, synthesize_plan
from .extract import DEFAULT_ZOTERO_STORAGE, resolve_pdf
from .models import default_model_dir, install_kokoro_models
from .pipeline import prepare_bundle
from .util import load_json
from .podcast import PodcastConfig, build_intro, build_local_podcast, episode_title, health_check, load_podcast_config
from .zotero import DEFAULT_ZOTERO_DB, zotero_metadata


def _source_arguments(parser: argparse.ArgumentParser) -> None:
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--pdf", help="Path to a local PDF")
    source.add_argument("--zotero-key", help="Zotero attachment key under storage/<key>/")
    parser.add_argument("--zotero-storage", type=Path, default=DEFAULT_ZOTERO_STORAGE)
    parser.add_argument("--zotero-db", type=Path, default=DEFAULT_ZOTERO_DB,
                        help="Read parent-item metadata from the local Zotero database")


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

    podcast = commands.add_parser("podcast", help="Build local podcast metadata and policy artifacts")
    podcast_sub = podcast.add_subparsers(dest="podcast_command", required=True)
    info = podcast_sub.add_parser("info", help="Print edition-aware title and introduction")
    info.add_argument("--title", required=True)
    info.add_argument("--author", action="append", default=[])
    info.add_argument("--year")
    info.add_argument("--edition", choices=("brief", "full"), default="brief")
    info.add_argument("--journal")
    info.add_argument("--university")
    build = podcast_sub.add_parser("build", aliases=["rebuild"], help="Build either or both podcast editions; rebuild re-extracts the PDF")
    build.add_argument("bundle", type=Path)
    build.add_argument("--config", type=Path, help="TOML config; see podcast.example.toml")
    build.add_argument("--private-root", type=Path)
    build.add_argument("--state-root", type=Path)
    build.add_argument("--public-root", type=Path)
    build.add_argument("--base-url")
    build.add_argument("--selected", action="store_true", help="Explicit publication-intent gate")
    build.add_argument("--license-json", type=Path, help="Exact-source license evidence record")
    build.add_argument("--metadata-json", type=Path, help="Optional enriched bibliographic metadata")
    build.add_argument("--edition", choices=("brief", "full", "both"), default="both")
    build.add_argument("--sync", action="store_true", help="Upload verified artifacts to configured R2 after publishing")
    build.add_argument("--publish", action=argparse.BooleanOptionalAction, default=None)
    build.add_argument("--dry-run", action=argparse.BooleanOptionalAction, default=None)
    _synthesis_arguments(build)
    health = podcast_sub.add_parser("health", help="Validate generated feeds and referenced resources")
    health.add_argument("--config", type=Path, required=True)
    health.add_argument("--remote", action="store_true", help="Also check the public HTTPS origin")

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
    metadata = None
    if args.zotero_key and args.zotero_db.expanduser().is_file():
        metadata = zotero_metadata(args.zotero_db, args.zotero_key)
    bundle, _, _, reused = prepare_bundle(
        pdf,
        zotero_key=args.zotero_key,
        output_root=args.output_root,
        include_references=args.include_references,
        max_chars=args.max_chars,
        force=args.force,
        metadata=metadata,
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
        if args.command == "podcast" and args.podcast_command == "info":
            print(episode_title(args.title, args.author, args.year))
            print(build_intro(args.edition, args.title, args.author, journal=args.journal,
                              university=args.university, year=args.year))
            return 0
        if args.command == "podcast" and args.podcast_command in {"build", "rebuild"}:
            license_record = load_json(args.license_json) if args.license_json else None
            metadata_value = load_json(args.metadata_json) if args.metadata_json else None
            config = load_podcast_config(args.config) if args.config else None
            if config is None:
                if not args.private_root:
                    raise ValueError("--private-root is required without --config")
                config = PodcastConfig(
                    private_root=args.private_root.expanduser().resolve(),
                    state_root=(args.state_root or args.bundle.parent / "podcast").expanduser().resolve(),
                    public_root=args.public_root.expanduser().resolve() if args.public_root else None,
                    base_url=(args.base_url or "https://podcast.example.invalid").rstrip("/"),
                    publishing_enabled=bool(args.publish),
                    dry_run=True if args.dry_run is None else args.dry_run,
                )
            elif args.dry_run is not None or args.publish is not None:
                from dataclasses import replace
                config = replace(
                    config,
                    dry_run=config.dry_run if args.dry_run is None else args.dry_run,
                    publishing_enabled=config.publishing_enabled if args.publish is None else args.publish,
                )
            if args.sync and not config.dry_run and (not config.public_root or not config.r2_bucket):
                raise ValueError("Public root and R2 bucket are required for --sync")
            bundle = args.bundle.expanduser().resolve()
            if args.podcast_command == "rebuild" and not config.dry_run:
                from .zotero import load_bundle_metadata
                structure = load_json(bundle / "structure.json")
                bundle, _, _, _ = prepare_bundle(
                    Path(structure["source"]["path"]), zotero_key=structure["source"].get("zotero_key"),
                    output_root=bundle.parent, include_references=structure["extraction"].get("include_references", False),
                    max_chars=900, force=True, metadata={**load_bundle_metadata(bundle), **(metadata_value or {})})
            backend = None if config.dry_run else _backend_from_args(args)
            result = build_local_podcast(
                bundle, backend=backend, config=config, selected=args.selected,
                license_record=license_record, metadata=metadata_value, edition=args.edition,
            )
            if args.sync and result.get("published"):
                from .publish_sync import sync_public
                sync_public(config, result["public_editions"])
            print(json.dumps(result, indent=2))
            return 0
        if args.command == "podcast" and args.podcast_command == "health":
            result = health_check(load_podcast_config(args.config), remote=args.remote)
            print(json.dumps(result, indent=2))
            return 0 if result["status"] == "pass" else 1
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
