from __future__ import annotations

from pathlib import Path
from typing import Any

from .article_files import research_markdown_path
from .extract import bundle_name, extract_pdf, render_markdown, write_extraction
from .segment import create_speech_plan
from .util import atomic_write_json, load_json
from .zotero import bundle_metadata_snapshot


def prepare_bundle(
    source_pdf: Path,
    *,
    zotero_key: str | None,
    output_root: Path,
    include_references: bool,
    max_chars: int,
    force: bool,
    metadata: dict[str, Any] | None = None,
) -> tuple[Path, dict[str, Any], dict[str, Any], bool]:
    candidate_structure = extract_pdf(
        source_pdf, zotero_key=zotero_key, include_references=include_references, metadata=metadata
    )
    root = output_root.expanduser().resolve()
    bundle = root / bundle_name(candidate_structure)
    if zotero_key and root.is_dir() and not bundle.exists():
        existing = [path for path in root.iterdir() if path.is_dir() and path.name.endswith(f"[{zotero_key}]")]
        if len(existing) == 1:
            bundle = existing[0]
        elif len(existing) > 1:
            raise RuntimeError(f"Multiple bundles found for Zotero key {zotero_key}")
    structure_path = bundle / "structure.json"
    plan_path = bundle / "speech-plan.json"
    article_path = research_markdown_path(bundle, candidate_structure.get("document", {}), migrate=True)

    if not force and structure_path.exists() and plan_path.exists() and article_path.exists():
        existing_structure = load_json(structure_path)
        existing_plan = load_json(plan_path)
        same = (
            existing_structure.get("source", {}).get("sha256")
            == candidate_structure["source"]["sha256"]
            and existing_structure.get("extraction", {}).get("include_references")
            == include_references
            and existing_structure.get("extraction", {}).get("algorithm")
            == candidate_structure.get("extraction", {}).get("algorithm")
            and existing_plan.get("structure_sha256")
            == existing_structure.get("structure_sha256")
            and existing_structure.get("structure_sha256")
            == candidate_structure.get("structure_sha256")
            and existing_plan.get("segmentation", {}).get("max_chars") == max_chars
            and existing_plan.get("segmentation", {}).get("algorithm") == "paragraph-whole-sentence-v2"
        )
        if same:
            return bundle, existing_structure, existing_plan, True
        raise FileExistsError(
            f"Bundle exists with different source or options: {bundle}. Use --force to replace generated text."
        )

    markdown = render_markdown(candidate_structure)
    plan = create_speech_plan(candidate_structure, max_chars=max_chars)
    write_extraction(bundle, candidate_structure, markdown)
    atomic_write_json(plan_path, plan)
    if metadata and zotero_key:
        atomic_write_json(
            bundle / "metadata.json",
            bundle_metadata_snapshot(
                metadata,
                attachment_key=zotero_key,
                source_sha256=candidate_structure["source"]["sha256"],
            ),
        )
    return bundle, candidate_structure, plan, False
