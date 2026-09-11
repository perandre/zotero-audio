"""Recency rules for articles selected for reading through the MCP."""
from __future__ import annotations

import re
from typing import Any


MINIMUM_ARTICLE_YEAR = 2025
PREFERRED_ARTICLE_YEAR = 2026
_YEAR_RE = re.compile(r"(?<!\d)(?:19|20)\d{2}(?!\d)")


def article_year(value: Any) -> int | None:
    """Return the first four-digit publication year in a metadata value."""

    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if 1 <= value <= 3000 else None
    match = _YEAR_RE.search(str(value or ""))
    return int(match.group(0)) if match else None


def readable_article(article: dict[str, Any]) -> bool:
    """Only known articles from 2025 onward are eligible for MCP reading."""

    return (year := article_year(article.get("year"))) is not None and year >= MINIMUM_ARTICLE_YEAR


def reading_sort_key(article: dict[str, Any]) -> tuple[int, int, str]:
    """Prefer 2026, then newer eligible years, with a stable title tie-breaker."""

    year = article_year(article.get("year")) or 0
    preferred = 0 if year == PREFERRED_ARTICLE_YEAR else 1
    return preferred, -year, str(article.get("title") or "").casefold()


def unreadable_article_message(article: dict[str, Any]) -> str:
    title = str(article.get("title") or "This article")
    year = article_year(article.get("year"))
    detail = str(year) if year is not None else "an unknown year"
    return (
        f"{title}: article reading is limited to publications from "
        f"{MINIMUM_ARTICLE_YEAR} onward (prefer {PREFERRED_ARTICLE_YEAR}); "
        f"this record has {detail}."
    )
