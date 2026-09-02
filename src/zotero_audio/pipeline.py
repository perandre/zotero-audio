from __future__ import annotations

from pathlib import Path
from typing import Any

from .extract import bundle_name, extract_pdf, render_markdown, write_extraction
from .segment import create_speech_plan
from .util import atomic_write_json, load_json


def prepare_bundle(
    source_pdf: Path,
    *,
    zotero_key: str | None,
    output_root: Path,
    include_references: bool,
    max_chars: int,
    force: bool,
) -> tuple[Path, dict[str, Any], dict[str, Any], bool]:
    candidate_structure = extract_pdf(
        source_pdf, zotero_key=zotero_key, include_references=include_references
    )
    bundle = output_root.expanduser().resolve() / bundle_name(candidate_structure)
    structure_path = bundle / "structure.json"
    plan_path = bundle / "speech-plan.json"
    article_path = bundle / "article.md"

    if not force and structure_path.exists() and plan_path.exists() and article_path.exists():
        existing_structure = load_json(structure_path)
        existing_plan = load_json(plan_path)
        same = (
            existing_structure.get("source", {}).get("sha256")
            == candidate_structure["source"]["sha256"]
            and existing_structure.get("extraction", {}).get("include_references")
            == include_references
            and existing_plan.get("structure_sha256")
            == existing_structure.get("structure_sha256")
            and existing_plan.get("segmentation", {}).get("max_chars") == max_chars
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
    return bundle, candidate_structure, plan, False
