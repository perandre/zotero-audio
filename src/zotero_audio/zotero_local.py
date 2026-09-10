"""Read Zotero through its native local HTTP API when it is available.

The local API is the supported bridge for local tools.  The SQLite/storage
fallbacks remain useful for older Zotero versions, headless machines, and
tests, but normal desktop runs should not need to know Zotero's data-directory
layout.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urlparse
from urllib.request import Request, urlopen

from .literature import evidence_from_extra
from .zotero import normalize_publication_date, zotero_metadata, zotero_metadata_many


DEFAULT_ZOTERO_API = "http://localhost:23119/api"
_LOCAL_API_TIMEOUT = 2.0
_YEAR_RE = re.compile(r"\b(?:19|20)\d{2}\b")


class ZoteroLocalAPIError(RuntimeError):
    """The local API responded, but not with a usable result."""


class ZoteroLocalAPIUnavailable(ZoteroLocalAPIError):
    """Zotero is not running or local API access is disabled."""


def _rights_from_fields(fields: dict[str, Any]) -> str | None:
    rights = fields.get("rights")
    if rights:
        return str(rights)
    extra = str(fields.get("extra") or "")
    match = re.search(
        r"(?im)^\s*(?:license|rights)\s*:\s*"
        r"(https?://\S+|CC\s*[- ]?BY\s+4\.0|CC0(?:\s+1\.0)?)\s*$",
        extra,
    )
    return match.group(1) if match else None


def _normalize_api_date(value: Any) -> str | None:
    normalized = normalize_publication_date(value)
    return str(normalized) if normalized is not None else None


def _creator_name(creator: dict[str, Any]) -> str:
    if creator.get("name"):
        return str(creator["name"]).strip()
    return " ".join(
        str(creator.get(part) or "").strip()
        for part in ("firstName", "lastName")
    ).strip()


class ZoteroLocalAPI:
    """Small read-only client for Zotero's local API v3."""

    def __init__(
        self,
        base_url: str = DEFAULT_ZOTERO_API,
        *,
        timeout: float = _LOCAL_API_TIMEOUT,
        opener: Callable[..., Any] = urlopen,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.opener = opener
        self._collections_cache: dict[str, dict[str, Any]] | None = None

    def _get(self, path: str, *, raw: bool = False) -> Any:
        url = f"{self.base_url}/users/0/{path.lstrip('/')}"
        request = Request(
            url,
            headers={
                "Accept": "text/plain" if raw else "application/json",
                "Zotero-API-Version": "3",
            },
        )
        try:
            response = self.opener(request, timeout=self.timeout)
            with response:
                body = response.read()
                if raw:
                    return body.decode("utf-8")
                return json.loads(body.decode("utf-8"))
        except HTTPError as exc:
            if exc.code in {403, 503}:
                raise ZoteroLocalAPIUnavailable(
                    "Zotero local API is unavailable or disabled; enable "
                    "Settings → Advanced → Allow other applications on this computer to communicate with Zotero"
                ) from exc
            raise ZoteroLocalAPIError(f"Zotero local API request failed ({exc.code}): {url}") from exc
        except (URLError, OSError, TimeoutError, json.JSONDecodeError) as exc:
            raise ZoteroLocalAPIUnavailable(f"Could not reach Zotero local API at {self.base_url}") from exc

    def item(self, key: str) -> dict[str, Any]:
        value = self._get(f"items/{key}")
        if not isinstance(value, dict):
            raise ZoteroLocalAPIError(f"Unexpected Zotero item response for {key}")
        data = value.get("data")
        if not isinstance(data, dict):
            raise ZoteroLocalAPIError(f"Zotero item {key} has no data payload")
        return data

    def attachment_path(self, key: str) -> Path | None:
        value = self._get(f"items/{key}/file/view/url", raw=True).strip()
        if not value:
            return None
        parsed = urlparse(value)
        if parsed.scheme != "file":
            raise ZoteroLocalAPIError(f"Zotero returned a non-file attachment URL for {key}")
        path = unquote(parsed.path)
        if not path:
            return None
        return Path(path).resolve()

    def _collections(self) -> dict[str, dict[str, Any]]:
        if self._collections_cache is None:
            value = self._get("collections?limit=10000")
            if not isinstance(value, list):
                raise ZoteroLocalAPIError("Unexpected Zotero collections response")
            self._collections_cache = {
                str(item["key"]): {"key": item["key"], **item.get("data", {})}
                for item in value
                if isinstance(item, dict) and item.get("key") and isinstance(item.get("data"), dict)
            }
        return self._collections_cache

    def collection_names(self, keys: Iterable[str]) -> list[str]:
        collections = self._collections()
        names: set[str] = set()
        for key in keys:
            current = collections.get(str(key))
            seen: set[str] = set()
            while current and str(current.get("key") or key) not in seen:
                current_key = str(current.get("key") or key)
                seen.add(current_key)
                if current.get("name"):
                    names.add(str(current["name"]))
                parent_key = current.get("parentCollection")
                if not parent_key:
                    break
                current = collections.get(str(parent_key))
        return sorted(names, key=str.casefold)

    def metadata(self, attachment_key: str) -> dict[str, Any] | None:
        attachment = self.item(attachment_key)
        if attachment.get("itemType") != "attachment":
            return None
        parent_key = attachment.get("parentItem")
        if not parent_key:
            return None
        parent = self.item(str(parent_key))
        item_type = str(parent.get("itemType") or "")
        fields = parent
        extra = str(fields.get("extra") or "")
        creators = [
            _creator_name(creator)
            for creator in fields.get("creators", [])
            if isinstance(creator, dict)
        ]
        creators = [name for name in creators if name]
        tags = [
            str(tag.get("tag"))
            for tag in fields.get("tags", [])
            if isinstance(tag, dict) and tag.get("tag")
        ]
        collections = self.collection_names(fields.get("collections", []))
        raw_date = fields.get("date")
        rights = _rights_from_fields(fields)
        year_match = _YEAR_RE.search(str(raw_date or ""))
        return {
            "item_type": item_type,
            "literature_type": "report" if item_type == "report" else "academic",
            "institution": fields.get("institution"),
            "report_type": fields.get("reportType"),
            "report_number": fields.get("reportNumber"),
            "series_title": fields.get("seriesTitle"),
            "evidence": evidence_from_extra(extra),
            "parent_key": parent_key,
            "title": fields.get("title"),
            "publication_year": year_match.group(0) if year_match else None,
            "publication_date": _normalize_api_date(raw_date),
            "authors": creators,
            "journal": fields.get("publicationTitle") or fields.get("proceedingsTitle"),
            "publisher": fields.get("publisher"),
            "university": fields.get("university"),
            "doi": fields.get("DOI"),
            "url": fields.get("url"),
            "rights": rights,
            "rights_source": "zotero-parent-item" if rights else None,
            "abstract": fields.get("abstractNote"),
            "language": fields.get("language"),
            "tags": tags,
            "collections": collections,
            "podcast_selected": any(tag.casefold() == "podcast" for tag in tags)
            or any(name.casefold() == "podcast queue" for name in collections),
        }

    def metadata_many(self, attachment_keys: Iterable[str]) -> dict[str, dict[str, Any]]:
        result: dict[str, dict[str, Any]] = {}
        for key in dict.fromkeys(str(value) for value in attachment_keys if str(value)):
            value = self.metadata(key)
            if value is not None:
                result[key] = value
        return result

    def pdf_attachments(self) -> list[tuple[str, Path]]:
        value = self._get("items?itemType=attachment&limit=10000")
        if not isinstance(value, list):
            raise ZoteroLocalAPIError("Unexpected Zotero attachment response")
        attachments: list[tuple[str, Path]] = []
        for item in value:
            if not isinstance(item, dict) or not item.get("key"):
                continue
            data = item.get("data")
            if not isinstance(data, dict):
                continue
            filename = str(data.get("filename") or "")
            content_type = str(data.get("contentType") or "").casefold()
            if content_type != "application/pdf" and not filename.casefold().endswith(".pdf"):
                continue
            try:
                path = self.attachment_path(str(item["key"]))
            except ZoteroLocalAPIError:
                continue
            if path is not None and path.is_file():
                attachments.append((str(item["key"]), path))
        return attachments


def _legacy_attachment_path(storage: Path, key: str) -> Path:
    root = storage.expanduser().resolve()
    key_dir = (root / str(key)).resolve()
    if key_dir.parent != root:
        raise ValueError("A Zotero key must be a single directory name")
    candidates = sorted(key_dir.rglob("*.pdf")) if key_dir.exists() else []
    if not candidates:
        raise FileNotFoundError(f"No PDF found for Zotero key {key} in {key_dir}")
    if len(candidates) > 1:
        raise RuntimeError(
            f"Multiple PDF attachments found for {key}: "
            + ", ".join(item.name for item in candidates)
            + ". Use --pdf to choose one."
        )
    return candidates[0].resolve()


def zotero_attachment_path(key: str, storage: Path, *, api: ZoteroLocalAPI | None = None) -> Path:
    """Resolve an attachment through Zotero first, then legacy storage."""

    client = api or ZoteroLocalAPI()
    try:
        path = client.attachment_path(key)
        if path is not None and path.is_file():
            return path
    except ZoteroLocalAPIError:
        pass
    return _legacy_attachment_path(storage, key)


def discover_zotero_pdfs(storage: Path, *, api: ZoteroLocalAPI | None = None) -> list[tuple[str, Path]]:
    """Return `(attachment key, path)` pairs, preferring Zotero's item list."""

    client = api or ZoteroLocalAPI()
    try:
        return client.pdf_attachments()
    except ZoteroLocalAPIError:
        root = storage.expanduser().resolve()
        return [
            (path.relative_to(root).parts[0], path.resolve())
            for path in sorted(root.glob("*/*.pdf"))
        ]


def zotero_metadata_preferred(database: Path, attachment_key: str) -> dict[str, Any] | None:
    """Read metadata through the local API, with SQLite compatibility fallback."""

    try:
        return ZoteroLocalAPI().metadata(attachment_key)
    except ZoteroLocalAPIError:
        return zotero_metadata(database, attachment_key)


def zotero_metadata_many_preferred(
    database: Path, attachment_keys: Iterable[str]
) -> dict[str, dict[str, Any]]:
    """Read many parent items through Zotero, with SQLite compatibility fallback."""

    keys = tuple(dict.fromkeys(str(key) for key in attachment_keys if str(key)))
    try:
        return ZoteroLocalAPI().metadata_many(keys)
    except ZoteroLocalAPIError:
        return zotero_metadata_many(database, keys)
