"""Extract a fresh, local Markdown brief without rendering or publishing audio."""
from __future__ import annotations

import argparse
from pathlib import Path

from zotero_audio.extract import extract_pdf, write_extraction, render_markdown
from zotero_audio.podcast import create_edition_plan, build_markdown_transcript, episode_title
from zotero_audio.segment import create_speech_plan
from zotero_audio.util import atomic_write_json, atomic_write_text, load_json


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pdf", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--metadata", type=Path)
    parser.add_argument("--zotero-key")
    args = parser.parse_args()
    metadata = load_json(args.metadata) if args.metadata else {}
    structure = extract_pdf(args.pdf.resolve(), zotero_key=args.zotero_key,
                            include_references=False, metadata=metadata)
    source_plan = create_speech_plan(structure)
    plan = create_edition_plan(source_plan, structure, "brief")
    args.output.mkdir(parents=True, exist_ok=True)
    write_extraction(args.output, structure, render_markdown(structure))
    atomic_write_json(args.output / "speech-plan.json", plan)
    document = plan["document"]
    title = episode_title(document["title"], document.get("authors", []), document.get("publication_year"))
    destination = args.output / "brief.md"
    atomic_write_text(destination, build_markdown_transcript(plan, title))
    print(destination.resolve())


if __name__ == "__main__":
    main()
