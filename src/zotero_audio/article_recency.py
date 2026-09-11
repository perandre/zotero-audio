"""Recency rules for VIKING research articles selected through the MCP."""
from __future__ import annotations

import re
from typing import Any


MINIMUM_ARTICLE_YEAR = 2025
PREFERRED_ARTICLE_YEAR = 2026
VIKING_RESEARCH_COLLECTION = "00 inbox"
VIKING_RESEARCH_ITEM_TYPES = frozenset({"journalarticle", "conferencepaper", "preprint"})
_YEAR_RE = re.compile(r"(?<!\d)(?:19|20)\d{2}(?!\d)")


def article_year(value: Any) -> int | None:
    """Return the first four-digit publication year in a metadata value."""

    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if 1 <= value <= 3000 else None
    match = _YEAR_RE.search(str(value or ""))
    return int(match.group(0)) if match else None


def reading_scope(article: dict[str, Any]) -> str:
    """Return the explicit or metadata-derived scope for a saved Zotero item."""

    explicit = str(article.get("reading_scope") or "").casefold()
    if explicit in {"general", "viking_research"}:
        return explicit
    metadata = article.get("metadata")
    if not isinstance(metadata, dict):
        return "general"
    item_type = str(metadata.get("item_type") or "").casefold()
    collections = metadata.get("collections")
    if not isinstance(collections, list):
        return "general"
    names = {str(value).casefold() for value in collections}
    return (
        "viking_research"
        if item_type in VIKING_RESEARCH_ITEM_TYPES and VIKING_RESEARCH_COLLECTION in names
        else "general"
    )


def readable_article(article: dict[str, Any]) -> bool:
    """Apply recency only to VIKING research articles in the 00 Inbox."""

    if reading_scope(article) != "viking_research":
        return True

    return (year := article_year(article.get("year"))) is not None and year >= MINIMUM_ARTICLE_YEAR


def reading_sort_key(article: dict[str, Any]) -> tuple[int, int, int, str]:
    """Prefer 2026 for VIKING research, then newer years, with stable ties."""

    year = article_year(article.get("year")) or 0
    scoped = reading_scope(article) == "viking_research"
    preferred = 0 if scoped and year == PREFERRED_ARTICLE_YEAR else 1
    return int(not scoped), preferred, -year, str(article.get("title") or "").casefold()


def unreadable_article_message(article: dict[str, Any]) -> str:
    title = str(article.get("title") or "This article")
    year = article_year(article.get("year"))
    detail = str(year) if year is not None else "an unknown year"
    return (
        f"{title}: VIKING research-article reading is limited to publications from "
        f"{MINIMUM_ARTICLE_YEAR} onward (prefer {PREFERRED_ARTICLE_YEAR}); "
        f"this record has {detail}."
    )
