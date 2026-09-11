"""Stable, human-readable names and migration helpers for article documents."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from .util import episode_title, readable_markdown_filename


def _document(value: dict[str, Any]) -> dict[str, Any]:
    document = value.get("document")
    return document if isinstance(document, dict) else value


def _authors(value: dict[str, Any]) -> list[str]:
    document = _document(value)
    authors = document.get("authors") or document.get("author") or []
    if isinstance(authors, str):
        return [part.strip() for part in authors.split(";") if part.strip()]
    return [str(author).strip() for author in authors if str(author).strip()]


def episode_title_for(value: dict[str, Any]) -> str:
    """Resolve the same human title used for an article's audio episode."""
    explicit = value.get("episode_title")
    if explicit:
        return str(explicit).strip()
    editions = value.get("editions") or {}
    for edition in ("full", "brief"):
        title = editions.get(edition, {}).get("title") if isinstance(editions.get(edition), dict) else None
        if title:
            return str(title).strip()
    document = _document(value)
    title = str(document.get("title") or value.get("title") or "Research article").strip()
    year = document.get("publication_year", value.get("year"))
    return episode_title(title, _authors(value), year)


def research_markdown_filename(value: dict[str, Any]) -> str:
    return readable_markdown_filename(episode_title_for(value))


def review_markdown_filename(value: dict[str, Any]) -> str:
    return readable_markdown_filename(episode_title_for(value), review=True)


def _resolve(bundle: Path, value: dict[str, Any], *, current: Path | None,
             review: bool, migrate: bool) -> Path:
    expected = bundle / (review_markdown_filename(value) if review else research_markdown_filename(value))
    if expected.is_file():
        return expected
    candidates = []
    if current and current.is_file():
        candidates.append(current)
    legacy = bundle / ("ai-review.md" if review else "article.md")
    if legacy.is_file() and legacy not in candidates:
        candidates.append(legacy)
    if not candidates:
        return expected
    source = candidates[0]
    if migrate and source != expected:
        source.replace(expected)
        return expected
    return source


def research_markdown_path(bundle: Path, value: dict[str, Any], *, current: Path | None = None,
                           migrate: bool = False) -> Path:
    return _resolve(bundle, value, current=current, review=False, migrate=migrate)


def review_markdown_path(bundle: Path, value: dict[str, Any], *, current: Path | None = None,
                         migrate: bool = False) -> Path:
    return _resolve(bundle, value, current=current, review=True, migrate=migrate)
