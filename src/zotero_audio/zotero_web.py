"""Read-only Zotero Web API discovery for hosts without the Zotero desktop app.

The local API (``zotero_local``) stays the macOS default.  Web mode is selected
by ``<runtime>/control/zotero.json`` or environment variables (see
:func:`zotero_settings`) and gives the rest of the pipeline the same objects:
``pdf_attachments()`` returns ``(attachment key, local PDF path)`` pairs and
``metadata()`` returns the identical metadata dictionary.  Only GET requests
are issued; no Zotero data is ever modified.

Library data is synchronised incrementally (``since`` plus
``If-Modified-Since-Version``) into ``<runtime>/control/zotero-web/`` and PDF
attachments are downloaded once per ``(attachment key, md5)`` into a cache.
The API key is read from a file or the environment and is never logged.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

from . import zotero_local
from .zotero_local import ZoteroLocalAPI, ZoteroLocalAPIError, ZoteroLocalAPIUnavailable

DEFAULT_WEB_API = "https://api.zotero.org"
PAGE_SIZE = 100
MAX_RETRIES = 5
MAX_WAIT_SECONDS = 120.0
DEFAULT_MAX_DOWNLOADS = 20
_KEY_FILE_NAMES = ("Zotero API key.txt", "zotero-api-key.txt")
_MD5_RE = re.compile(r"^[0-9a-fA-F]{32}$")
_ITEM_KEY_RE = re.compile(r"^[A-Za-z0-9]{8}$")

# Zotero's Backoff/Retry-After apply to the whole API key, not one client
# object, and the monitor builds a fresh client each scan.
_BACKOFF_LOCK = threading.Lock()
_NOT_BEFORE = 0.0
_MEMO_LOCK = threading.RLock()
_MEMO: dict[str, dict[str, Any]] = {}


class ZoteroWebAuthError(ZoteroLocalAPIError):
    """The API key is missing, invalid or lacks library read access."""


class ZoteroAttachmentUnavailable(ZoteroLocalAPIError):
    """A PDF attachment exists but its file cannot be downloaded."""


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


_REDIRECT_OPENER = build_opener(_NoRedirect)


def _plain_open(request, timeout):
    return _REDIRECT_OPENER.open(request, timeout=timeout)


def zotero_settings(control: Path, environ: dict[str, str] | None = None) -> dict[str, Any]:
    """Resolve the local Zotero source selection (never cloud-synced).

    ``control/zotero.json`` example (no secrets)::

        {"mode": "web", "user_id": 1234567}

    Optional keys: ``api_key_file``, ``base_url``, ``max_downloads_per_sync``.
    Environment overrides: ``ZOTERO_AUDIO_ZOTERO_MODE``,
    ``ZOTERO_AUDIO_ZOTERO_USER_ID``, ``ZOTERO_AUDIO_ZOTERO_KEY_FILE``;
    ``ZOTERO_API_KEY`` supplies the key itself.
    """
    env = os.environ if environ is None else environ
    value: dict[str, Any] = {}
    path = Path(control) / "zotero.json"
    if path.is_file():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ValueError(f"{path} is not valid JSON") from exc
        if not isinstance(loaded, dict):
            raise ValueError(f"{path} must contain a JSON object")
        value.update(loaded)
    if env.get("ZOTERO_AUDIO_ZOTERO_MODE"):
        value["mode"] = env["ZOTERO_AUDIO_ZOTERO_MODE"]
    if env.get("ZOTERO_AUDIO_ZOTERO_USER_ID"):
        value["user_id"] = env["ZOTERO_AUDIO_ZOTERO_USER_ID"]
    if env.get("ZOTERO_AUDIO_ZOTERO_KEY_FILE"):
        value["api_key_file"] = env["ZOTERO_AUDIO_ZOTERO_KEY_FILE"]
    mode = str(value.get("mode") or "local").strip().lower()
    if mode not in {"local", "web"}:
        raise ValueError("Zotero mode must be 'local' or 'web'")
    value["mode"] = mode
    return value


def read_api_key(control: Path, settings: dict[str, Any], environ: dict[str, str] | None = None) -> str:
    env = os.environ if environ is None else environ
    key = str(env.get("ZOTERO_API_KEY") or "").strip()
    if not key:
        configured = settings.get("api_key_file")
        if configured:
            path = Path(str(configured)).expanduser()
            candidates = [path if path.is_absolute() else Path(control).parent / path]
        else:
            candidates = [Path(control) / name for name in _KEY_FILE_NAMES]
        found = next((p for p in candidates if p.is_file()), None)
        if found is None:
            raise ZoteroWebAuthError(
                "Zotero API key not found; save it in control/Zotero API key.txt or set ZOTERO_API_KEY")
        for line in found.read_text(encoding="utf-8").splitlines():
            if line.strip():
                key = re.split(r"[\s:=]+", line.strip())[-1]
                break
    if not re.fullmatch(r"[A-Za-z0-9]{16,64}", key or ""):
        raise ZoteroWebAuthError("Zotero API key must be a single alphanumeric value")
    return key


def _header(headers: Any, name: str) -> str | None:
    value = headers.get(name) if headers is not None else None
    return None if value is None else str(value)


def _number(value: str | None) -> float | None:
    try:
        return max(0.0, float(value)) if value is not None else None
    except ValueError:
        return None


class ZoteroWebAPI(ZoteroLocalAPI):
    """Read-only Zotero Web API v3 client with the local client's interface."""

    def __init__(
        self,
        api_key: str,
        user_id: int | str | None = None,
        *,
        cache_dir: Path,
        base_url: str = DEFAULT_WEB_API,
        timeout: float = 30.0,
        opener: Callable[..., Any] = _plain_open,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        max_downloads: int = DEFAULT_MAX_DOWNLOADS,
        page_size: int = PAGE_SIZE,
    ) -> None:
        super().__init__(base_url, timeout=timeout, opener=opener)
        self._key = api_key
        self.user_id = str(user_id) if user_id not in (None, "") else None
        self.cache_dir = Path(cache_dir)
        self.sleep = sleep
        self.clock = clock
        self.max_downloads = max_downloads
        self.page_size = page_size
        self.unavailable: list[dict[str, str]] = []
        self.pending: list[str] = []
        self._synced = False

    def __repr__(self) -> str:  # never expose the key
        return f"ZoteroWebAPI(user_id={self.user_id!r})"

    # ------------------------------------------------------------------ HTTP
    def _wait(self) -> None:
        with _BACKOFF_LOCK:
            delay = _NOT_BEFORE - self.clock()
        if delay > 0:
            self.sleep(min(delay, MAX_WAIT_SECONDS))

    def _note_backoff(self, seconds: float | None) -> None:
        global _NOT_BEFORE
        if seconds:
            with _BACKOFF_LOCK:
                _NOT_BEFORE = max(_NOT_BEFORE, self.clock() + min(seconds, MAX_WAIT_SECONDS))

    def _request(self, url: str, *, headers: dict[str, str] | None = None, authed: bool = True):
        """GET with Backoff/Retry-After/429 handling.  Returns (status, headers, body)."""
        send = {"Zotero-API-Version": "3", **(headers or {})}
        if authed:
            send["Zotero-API-Key"] = self._key
        for attempt in range(MAX_RETRIES + 1):
            self._wait()
            try:
                response = self.opener(Request(url, headers=send, method="GET"), timeout=self.timeout)
                with response:
                    body = response.read()
                    status = getattr(response, "status", 200)
                    hdrs = response.headers
                self._note_backoff(_number(_header(hdrs, "Backoff")))
                return status, hdrs, body
            except HTTPError as exc:
                self._note_backoff(_number(_header(exc.headers, "Backoff")))
                if exc.code == 304 or exc.code in {301, 302, 303, 307, 308}:
                    return exc.code, exc.headers, b""
                if exc.code in {429, 500, 502, 503, 504}:
                    wait = _number(_header(exc.headers, "Retry-After"))
                    self._note_backoff(wait if wait is not None else min(2.0 ** attempt, 30.0))
                    if attempt < MAX_RETRIES:
                        continue
                    raise ZoteroLocalAPIUnavailable(
                        f"Zotero Web API unavailable or rate limited ({exc.code}); retrying later") from exc
                if exc.code in {401, 403}:
                    raise ZoteroWebAuthError("Zotero rejected the API key or it lacks library read access") from exc
                if exc.code == 404:
                    raise ZoteroLocalAPIError("Zotero Web API resource not found (404)") from exc
                raise ZoteroLocalAPIError(f"Zotero Web API request failed ({exc.code})") from exc
            except (URLError, OSError, TimeoutError) as exc:
                if attempt < 2:
                    self._note_backoff(min(2.0 ** attempt, 10.0))
                    continue
                raise ZoteroLocalAPIUnavailable("Could not reach the Zotero Web API") from exc
        raise ZoteroLocalAPIUnavailable("Zotero Web API retries exhausted")  # pragma: no cover

    def _resolve_user(self) -> str:
        if self.user_id:
            return self.user_id
        _, _, body = self._request(f"{self.base_url}/keys/current")
        try:
            user_id = json.loads(body.decode("utf-8")).get("userID")
        except (ValueError, AttributeError) as exc:
            raise ZoteroLocalAPIError("Unexpected Zotero key response") from exc
        if not user_id:
            raise ZoteroWebAuthError("Zotero API key does not identify a user")
        self.user_id = str(user_id)
        return self.user_id

    def _get(self, path: str, *, raw: bool = False, headers: dict[str, str] | None = None) -> Any:
        url = f"{self.base_url}/users/{self._resolve_user()}/{path.lstrip('/')}"
        _, _, body = self._request(url, headers=headers)
        try:
            return body.decode("utf-8") if raw else json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise ZoteroLocalAPIError("Zotero Web API returned an unreadable response") from exc

    def _pages(self, path: str, params: dict[str, Any], since: int) -> tuple[list[Any], int, bool]:
        """Fetch every page; returns (rows, library version, modified)."""
        rows: list[Any] = []
        start = 0
        version = since
        first = True
        while True:
            query = urlencode({**params, "limit": self.page_size, "start": start,
                               **({"since": since} if since else {})})
            url = f"{self.base_url}/users/{self._resolve_user()}/{path}?{query}"
            send = {"If-Modified-Since-Version": str(since)} if since and first else {}
            status, headers, body = self._request(url, headers=send)
            if status == 304:
                return [], since, False
            try:
                page = json.loads(body.decode("utf-8"))
            except ValueError as exc:
                raise ZoteroLocalAPIError("Zotero Web API returned an unreadable page") from exc
            if not isinstance(page, list):
                raise ZoteroLocalAPIError("Unexpected Zotero Web API page")
            if first:
                # If the library changes while paging we keep the first version, so
                # the next scan's ``since`` re-reads anything modified meanwhile.
                version = int(_number(_header(headers, "Last-Modified-Version")) or since)
                first = False
            rows.extend(page)
            start += len(page)
            total = _number(_header(headers, "Total-Results"))
            if not page or (total is not None and start >= total) or (total is None and len(page) < self.page_size):
                return rows, version, True

    # ----------------------------------------------------------- local cache
    @property
    def _state_path(self) -> Path:
        return self.cache_dir / "library.json"

    def _load(self) -> dict[str, Any]:
        memo_key = str(self.cache_dir)
        with _MEMO_LOCK:
            if memo_key in _MEMO and _MEMO[memo_key].get("user_id") in (None, self.user_id):
                return _MEMO[memo_key]
        state: dict[str, Any] = {"user_id": self.user_id, "items_version": 0,
                                 "collections_version": 0, "items": {}, "collections": {}}
        try:
            loaded = json.loads(self._state_path.read_text(encoding="utf-8"))
            if (isinstance(loaded, dict) and isinstance(loaded.get("items"), dict)
                    and isinstance(loaded.get("collections"), dict)
                    and (not self.user_id or str(loaded.get("user_id") or "") in {"", self.user_id})):
                state.update(loaded)
        except (OSError, ValueError):
            pass
        with _MEMO_LOCK:
            _MEMO[memo_key] = state
        return state

    def _save(self, state: dict[str, Any]) -> None:
        self.cache_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        tmp = self._state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self._state_path)

    @staticmethod
    def _row(row: Any) -> tuple[str, dict[str, Any]] | None:
        data = row.get("data") if isinstance(row, dict) else None
        key = (row.get("key") or (data or {}).get("key")) if isinstance(row, dict) else None
        if not isinstance(data, dict) or not key:
            return None
        # The local API omits empty fields; the Web API returns them as "".  Drop
        # them so downstream metadata is identical (None, not "", for unset fields).
        data = {k: v for k, v in data.items() if v not in ("", None, [], {})}
        return str(key), {**data, "key": str(key), "version": row.get("version", data.get("version"))}

    def sync(self) -> dict[str, Any]:
        """Incrementally refresh items and collections once per client instance."""
        state = self._load()
        if self._synced:
            return state
        state["user_id"] = self._resolve_user()
        changed = False
        since = int(state["items_version"])
        rows, version, modified = self._pages("items", {"includeTrashed": 1}, since)
        if modified:
            for row in rows:
                parsed = self._row(row)
                if parsed:
                    state["items"][parsed[0]] = parsed[1]
            if since:
                deleted = self._get(f"deleted?since={since}")
                if isinstance(deleted, dict):
                    for key in deleted.get("items", []):
                        state["items"].pop(str(key), None)
                    for key in deleted.get("collections", []):
                        state["collections"].pop(str(key), None)
            state["items_version"] = version
            changed = True
        since = int(state["collections_version"])
        rows, version, modified = self._pages("collections", {}, since)
        if modified:
            for row in rows:
                parsed = self._row(row)
                if parsed:
                    state["collections"][parsed[0]] = parsed[1]
            state["collections_version"] = version
            changed = True
        if changed:
            self._save(state)
        self._collections_cache = state["collections"]
        self._synced = True
        return state

    # ------------------------------------------- local-API-compatible surface
    def item(self, key: str) -> dict[str, Any]:
        data = self.sync()["items"].get(str(key))
        if data is None:
            raise ZoteroLocalAPIError(f"Zotero item {key} was not found")
        return data

    def _collections(self) -> dict[str, dict[str, Any]]:
        self.sync()
        return self._collections_cache or {}

    def attachment_path(self, key: str) -> Path | None:
        return self._cached_path(str(key), self.item(key))

    # ---------------------------------------------------------- attachments
    @staticmethod
    def _classify(data: dict[str, Any]) -> str | None:
        """Return None for a downloadable file, else why it is unavailable."""
        mode = str(data.get("linkMode") or "")
        if mode == "linked_file":
            return "linked file (stored outside Zotero storage; the Web API cannot download it)"
        if mode == "linked_url":
            return "linked URL without a stored file"
        if not _MD5_RE.match(str(data.get("md5") or "")):
            return "file is not uploaded to Zotero storage (no md5)"
        return None

    @staticmethod
    def _is_pdf(data: dict[str, Any]) -> bool:
        filename = str(data.get("filename") or "")
        return (str(data.get("contentType") or "").casefold() == "application/pdf"
                or filename.casefold().endswith(".pdf"))

    def _file_path(self, key: str, md5: str) -> Path:
        if not _ITEM_KEY_RE.match(key):
            raise ZoteroLocalAPIError("Unexpected Zotero attachment key")
        return self.cache_dir / "files" / key / f"{md5.lower()}.pdf"

    def _cached_path(self, key: str, data: dict[str, Any]) -> Path | None:
        if self._classify(data):
            return None
        path = self._file_path(key, str(data["md5"]))
        return path.resolve() if path.is_file() else None

    @staticmethod
    def _md5(path: Path) -> str:
        digest = hashlib.md5(usedforsecurity=False)
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1 << 20), b""):
                digest.update(block)
        return digest.hexdigest()

    def download(self, key: str, data: dict[str, Any] | None = None) -> Path:
        """Download one attachment with read-only GETs and verify its md5."""
        data = data or self.item(key)
        reason = self._classify(data)
        if reason:
            raise ZoteroAttachmentUnavailable(reason)
        md5 = str(data["md5"]).lower()
        target = self._file_path(key, md5)
        if target.is_file():
            return target.resolve()
        url = f"{self.base_url}/users/{self._resolve_user()}/items/{key}/file"
        status, headers, body = self._request(url)
        if status in {301, 302, 303, 307, 308}:
            location = _header(headers, "Location") or ""
            if not location.startswith(("https://", "http://")):
                raise ZoteroLocalAPIError("Zotero file redirect was not usable")
            # The storage host must never receive the Zotero API key.
            status, headers, body = self._request(location, authed=False)
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        partial = target.with_name(target.name + ".part")
        partial.write_bytes(body)
        actual = self._md5(partial)
        if actual != md5:
            partial.unlink(missing_ok=True)
            raise ZoteroLocalAPIError(
                f"Downloaded file for {key} failed md5 verification (expected {md5}, got {actual})")
        os.replace(partial, target)
        for old in target.parent.glob("*.pdf"):  # superseded versions of this attachment
            if old != target:
                old.unlink(missing_ok=True)
        return target.resolve()

    def pdf_attachments(self) -> list[tuple[str, Path]]:
        state = self.sync()
        self.unavailable, self.pending = [], []
        found: list[tuple[str, Path]] = []
        budget = self.max_downloads
        for key in sorted(state["items"]):
            data = state["items"][key]
            if data.get("itemType") != "attachment" or data.get("deleted") or not self._is_pdf(data):
                continue
            reason = self._classify(data)
            if reason:
                self.unavailable.append({"key": key, "title": self._title(state, data), "reason": reason})
                continue
            cached = self._cached_path(key, data)
            if cached is not None:
                found.append((key, cached))
            elif budget <= 0:
                self.pending.append(key)
            else:
                budget -= 1
                try:
                    found.append((key, self.download(key, data)))
                except (ZoteroLocalAPIUnavailable, ZoteroWebAuthError):
                    raise
                except ZoteroLocalAPIError as exc:
                    self.unavailable.append({"key": key, "title": self._title(state, data), "reason": str(exc)})
        return found

    def retained_keys(self) -> set[str]:
        """Attachments present in Zotero but not downloaded yet (per-sync budget)."""
        return set(self.pending)

    @staticmethod
    def _title(state: dict[str, Any], data: dict[str, Any]) -> str:
        parent = state["items"].get(str(data.get("parentItem") or ""))
        return str((parent or {}).get("title") or data.get("title") or data.get("filename") or data.get("key"))


def make_zotero_api(control: Path, *, timeout: float = 5.0, environ: dict[str, str] | None = None):
    """Return the configured Zotero client: web mode, or the local API (default)."""
    settings = zotero_settings(control, environ)
    if settings["mode"] != "web":
        return zotero_local.ZoteroLocalAPI(timeout=timeout)
    return ZoteroWebAPI(
        read_api_key(control, settings, environ),
        settings.get("user_id"),
        cache_dir=Path(control) / "zotero-web",
        base_url=str(settings.get("base_url") or DEFAULT_WEB_API),
        timeout=max(timeout, 30.0),
        max_downloads=int(settings.get("max_downloads_per_sync") or DEFAULT_MAX_DOWNLOADS),
    )
