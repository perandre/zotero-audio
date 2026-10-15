"""Resolve per-episode Apple Podcasts and Spotify URLs for published episodes.

Apple's public lookup lists every episode with its RSS GUID. Spotify exposes
episode IDs only on the show page (the latest few episodes) and titles through
oEmbed, so Spotify episodes are matched by exact title and older ones fall back
to the show link. Both directories ingest a feed after publication, so this is
a separate, repeatable step rather than part of publishing.
"""
from __future__ import annotations

import json
import re
import urllib.parse
import urllib.request
from typing import Any, Callable

Fetch = Callable[[str], str]
USER_AGENT = "Mozilla/5.0 (compatible; 1MorePaper-listen-links)"


def _get(url: str) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read().decode("utf-8", "replace")


def _normal(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def apple_episode_urls(show_url: str, fetch: Fetch = _get) -> dict[str, str]:
    """Map RSS episode GUID -> Apple Podcasts episode URL."""
    match = re.search(r"/id(\d+)", show_url)
    if not match:
        return {}
    payload = json.loads(fetch(
        f"https://itunes.apple.com/lookup?id={match.group(1)}&entity=podcastEpisode&limit=300"))
    result: dict[str, str] = {}
    for item in payload.get("results", []):
        guid, url = item.get("episodeGuid"), item.get("trackViewUrl")
        if guid and url and "?i=" in url.replace("&i=", "?i="):
            parts = urllib.parse.urlsplit(url)
            query = urllib.parse.urlencode([(k, v) for k, v in urllib.parse.parse_qsl(parts.query) if k == "i"])
            result[str(guid)] = urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path, query, ""))
    return result


def spotify_episode_urls(show_url: str, known_urls: set[str] = frozenset(),
                         fetch: Fetch = _get) -> dict[str, str]:
    """Map normalised episode title -> Spotify episode URL (latest episodes only)."""
    page = fetch(show_url)
    ids = list(dict.fromkeys(re.findall(r"/episode/([A-Za-z0-9]{22})", page)))
    result: dict[str, str] = {}
    for episode_id in ids:
        url = f"https://open.spotify.com/episode/{episode_id}"
        try:
            oembed = json.loads(fetch("https://open.spotify.com/oembed?url=" + urllib.parse.quote(url, safe="")))
        except (OSError, ValueError):
            continue
        title = _normal(str(oembed.get("title") or ""))
        if title:
            result[title] = url
    return result


def resolve(episodes: list[dict[str, Any]], *, apple_show_urls: dict[str, str], spotify_show_urls: dict[str, str],
            fetch: Fetch = _get) -> dict[str, dict[str, str]]:
    """Return {guid: {"apple_podcasts": url, "spotify": url}} for the episodes found.

    ``*_show_urls`` map edition -> show URL. A directory that cannot be
    reached is skipped so the other still resolves.
    """
    apple: dict[str, dict[str, str]] = {}
    spotify: dict[str, dict[str, str]] = {}
    for edition, url in apple_show_urls.items():
        if url:
            try:
                apple[edition] = apple_episode_urls(url, fetch)
            except (OSError, ValueError):
                pass
    for edition, url in spotify_show_urls.items():
        if url:
            try:
                spotify[edition] = spotify_episode_urls(url, fetch=fetch)
            except (OSError, ValueError):
                pass
    found: dict[str, dict[str, str]] = {}
    for item in episodes:
        edition, guid = item.get("edition"), str(item.get("guid") or "")
        entry: dict[str, str] = {}
        if guid in apple.get(edition, {}):
            entry["apple_podcasts"] = apple[edition][guid]
        title = spotify.get(edition, {}).get(_normal(str(item.get("title") or "")))
        if title:
            entry["spotify"] = title
        if entry:
            found[guid] = entry
    return found
