"""Canonical flat store for editable research Markdown."""
from __future__ import annotations

import json
import os
import re
import fcntl
import threading
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .article_files import episode_title_for, research_markdown_path
from .util import atomic_write_json, atomic_write_text, filename_part, readable_markdown_filename, sha256_file

_lock_guard = threading.RLock()
_lock_state = threading.local()


@contextmanager
def _library_lock(root: Path):
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    identity = str(root.resolve())
    with _lock_guard:
        held = getattr(_lock_state, "held", set())
        if identity in held:
            yield
            return
        lock_path = root / ".index.lock"
        with lock_path.open("a+") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            _lock_state.held = held | {identity}
            try:
                yield
            finally:
                _lock_state.held = held
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _index(root: Path) -> dict[str, Any]:
    try:
        value = json.loads((root / "index.json").read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {"articles": {}}
    except (OSError, ValueError):
        return {"schema": "one-more-paper-library/v1", "articles": {}}


def _key(value: dict[str, Any]) -> str:
    metadata = value.get("metadata") or {}
    stable = value.get("id") or value.get("zotero_key") or metadata.get("zotero_key")
    if stable and str(stable).lower() != "unknown":
        return str(stable)
    doi = value.get("doi") or metadata.get("doi")
    source_hash = value.get("source_sha256") or metadata.get("source_sha256")
    if doi:
        return "doi:" + str(doi).strip().lower().removeprefix("https://doi.org/")
    if source_hash:
        return "sha256:" + str(source_hash)
    raise ValueError("A Zotero key, DOI or source hash is required to identify research Markdown")


def canonical_path(root: Path, article: dict[str, Any]) -> Path:
    with _library_lock(root):
        return _canonical_path(root, article)


def _canonical_path(root: Path, article: dict[str, Any]) -> Path:
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    key = _key(article)
    index = _index(root)["articles"]
    recorded = index.get(key, {}).get("path")
    if recorded:
        path = root / Path(recorded).name
        old_title = index.get(key, {}).get("title")
        if path.is_file() and (not article.get("title") or old_title == article.get("title")):
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


def research_path(root: Path, bundle: Path, article: dict[str, Any], *, provenance: str) -> Path:
    """Resolve or safely adopt a bundle's editable source into the flat store."""
    with _library_lock(root):
        target = _canonical_path(root, article)
        key = _key(article)
        recorded = _index(root).get("articles", {}).get(key, {}).get("path")
        if recorded:
            previous = root / Path(recorded).name
            if previous.is_file() and previous.resolve() != target.resolve():
                return adopt(root, article, previous, provenance=provenance)
        legacy = research_markdown_path(bundle, article, migrate=True)
        if legacy.is_file() and legacy.resolve() != target.resolve():
            return adopt(root, article, legacy, provenance=provenance)
        if target.is_file():
            return target
        return target


def link_legacy_path(source: Path, target: Path) -> None:
    """Keep a historical bundle path resolving without storing another copy."""
    if source.resolve() == target.resolve():
        return
    source.parent.mkdir(parents=True, exist_ok=True)
    temporary = source.parent / f".{uuid.uuid4().hex}.link"
    temporary.symlink_to(os.path.relpath(target, source.parent))
    temporary.replace(source)


def register(root: Path, article: dict[str, Any], path: Path, *, provenance: str = "local-generation",
             conflicts: list[str] | None = None) -> None:
    with _library_lock(root):
        _register(root, article, path, provenance=provenance, conflicts=conflicts)


def _register(root: Path, article: dict[str, Any], path: Path, *, provenance: str,
              conflicts: list[str] | None) -> None:
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    index = _index(root)
    metadata = article.get("metadata") or {}
    key = _key(article)
    previous_conflicts = index.get("articles", {}).get(key, {}).get("conflicts", [])
    index.setdefault("articles", {})[key] = {
        "path": path.name, "title": article.get("title"),
        "authors": article.get("authors") or article.get("author") or [],
        "year": article.get("year") or article.get("publication_year"),
        "doi": article.get("doi") or metadata.get("doi"), "zotero_key": key,
        "provenance": provenance, "sha256": sha256_file(path) if path.is_file() else None,
        "conflicts": sorted(set(previous_conflicts) | set(conflicts or [])),
    }
    atomic_write_json(root / "index.json", index)


def adopt(root: Path, article: dict[str, Any], source: Path, *, provenance: str) -> Path:
    """Copy to the canonical name, preserving differing source revisions."""
    with _library_lock(root):
        target = _canonical_path(root, article)
        conflicts: list[str] = []
        compatible = target
        if source.is_file():
            source_hash = sha256_file(source)
            if source.resolve() == target.resolve():
                pass
            elif target.is_file() and sha256_file(target) != source_hash:
                revision = root / f"{target.stem} — revision-{source_hash[:10]}.md"
                if not revision.exists():
                    revision.write_bytes(source.read_bytes())
                conflicts.append(revision.name)
                compatible = revision
            elif not target.exists():
                atomic_write_text(target, source.read_text(encoding="utf-8"))
        _register(root, article, target, provenance=provenance, conflicts=conflicts)
        if source.is_file() and source.resolve() != compatible.resolve():
            link_legacy_path(source, compatible)
        return target


def write_research_markdown(root: Path, article: dict[str, Any], markdown: str, *,
                            provenance: str = "local-generation", force: bool = False) -> Path:
    """Create canonical research text, preserving any overwritten version."""
    with _library_lock(root):
        target = _canonical_path(root, article)
        conflicts: list[str] = []
        if target.is_file() and (not force or target.read_text(encoding="utf-8") != markdown):
            if not force:
                _register(root, article, target, provenance=provenance, conflicts=None)
                return target
            old_hash = sha256_file(target)
            revision = root / f"{target.stem} — revision-{old_hash[:10]}.md"
            if not revision.exists():
                atomic_write_text(revision, target.read_text(encoding="utf-8"))
            conflicts.append(revision.name)
        atomic_write_text(target, markdown)
        _register(root, article, target, provenance=provenance, conflicts=conflicts)
        return target
