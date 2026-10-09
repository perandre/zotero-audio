"""Canonical flat store for editable research Markdown."""
from __future__ import annotations

import json
import os
import re
import uuid
from pathlib import Path
from typing import Any

from .article_files import episode_title_for
from .util import atomic_write_json, filename_part, readable_markdown_filename, sha256_file


def _index(root: Path) -> dict[str, Any]:
    try:
        value = json.loads((root / "index.json").read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {"articles": {}}
    except (OSError, ValueError):
        return {"schema": "one-more-paper-library/v1", "articles": {}}


def _key(value: dict[str, Any]) -> str:
    return str(value.get("id") or value.get("zotero_key") or value.get("metadata", {}).get("zotero_key") or "unknown")


def canonical_path(root: Path, article: dict[str, Any]) -> Path:
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    key = _key(article)
    index = _index(root)["articles"]
    recorded = index.get(key, {}).get("path")
    if recorded:
        path = root / Path(recorded).name
        if path.is_file():
            return path
    base = readable_markdown_filename(episode_title_for(article))
    path = root / base
    owners = {row.get("path"): owner for owner, row in index.items()}
    current = Path(article["markdown"]).resolve() if article.get("markdown") else None
    owner = owners.get(path.name)
    if path.exists() and owner != key and current != path.resolve():
        stem = Path(base).stem
        path = root / f"{stem} [{filename_part(key)}].md"
    return path


def register(root: Path, article: dict[str, Any], path: Path, *, provenance: str = "local-generation",
             conflicts: list[str] | None = None) -> None:
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    index = _index(root)
    metadata = article.get("metadata") or {}
    key = _key(article)
    previous_conflicts = index.get("articles", {}).get(key, {}).get("conflicts", [])
    index.setdefault("articles", {})[key] = {
        "path": path.name, "title": article.get("title"),
        "authors": article.get("authors", []), "year": article.get("year"),
        "doi": metadata.get("doi"), "zotero_key": key,
        "provenance": provenance, "sha256": sha256_file(path) if path.is_file() else None,
        "conflicts": sorted(set(previous_conflicts) | set(conflicts or [])),
    }
    atomic_write_json(root / "index.json", index)


def adopt(root: Path, article: dict[str, Any], source: Path, *, provenance: str) -> Path:
    """Copy to the canonical name, preserving differing source revisions."""
    target = canonical_path(root, article)
    conflicts: list[str] = []
    compatible = target
    if source.is_file():
        source_hash = sha256_file(source)
        if target.is_file() and sha256_file(target) != source_hash:
            revision = root / f"{target.stem} — revision-{source_hash[:10]}.md"
            if not revision.exists():
                revision.write_bytes(source.read_bytes())
            conflicts.append(revision.name)
            compatible = revision
        elif not target.exists():
            target.write_bytes(source.read_bytes())
    register(root, article, target, provenance=provenance, conflicts=conflicts)
    if source.is_file() and source.resolve() != target.resolve() and source.parent.resolve() != root.resolve():
        temporary = source.parent / f".{uuid.uuid4().hex}.link"
        temporary.symlink_to(os.path.relpath(compatible, source.parent))
        temporary.replace(source)
    return target
