"""Bounded, source-checked abstract retrieval without inference or paid APIs."""
from __future__ import annotations

import difflib
import ipaddress
import json
import re
import socket
import threading
import time
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .episode_descriptions import normalize_abstract

MAX_BYTES = 2_000_000
POLICY = 2
_GATE = threading.Lock()
_NEXT: dict[str, float] = {}
_BACKOFF_STATUS: dict[str, int] = {}
INTERVALS = {"export.arxiv.org": 3.1, "api.semanticscholar.org": 1.1, "api.openalex.org": 0.2}


def doi_value(value) -> str:
    value = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", str(value or "").strip(), flags=re.I)
    return value if re.fullmatch(r"10\.\d{4,9}/[^\s?#]+", value) else ""


def title_match(expected: str, actual: str, *, strict=False) -> bool:
    def clean(value):
        return re.sub(r"[\W_]+", " ", normalize_abstract(value).casefold()).strip()
    expected, actual = clean(expected), clean(actual)
    return bool(expected and actual and difflib.SequenceMatcher(None, expected, actual).ratio() >= (0.94 if strict else 0.65))


def public_url(url: str) -> str:
    parsed = urlsplit(url)
    if parsed.scheme not in {"https", "http"} or not parsed.hostname or parsed.username or parsed.password or parsed.port not in {None, 80, 443}:
        raise ValueError("Unsupported public source URL")
    addresses = socket.getaddrinfo(parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80), type=socket.SOCK_STREAM)
    if not addresses or any(not ipaddress.ip_address(row[4][0]).is_global for row in addresses):
        raise ValueError("Source URL does not resolve exclusively to public addresses")
    return url


class PublicRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        public_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class ProviderUnavailable(URLError):
    def __init__(self, host: str, delay: float, code: int = 429, *, bucket: str | None = None):
        super().__init__("Provider cooldown; retry after its recorded reset")
        self.code = code
        self.retry_at = time.time() + delay
        self.host = host
        self.backoff_key = bucket or host


def restore_backoffs(receipts: list[dict]) -> None:
    """Restore provider reset times after a CLI/process restart."""
    with _GATE:
        for receipt in receipts:
            for attempt in receipt.get("attempts", []):
                delay = float(attempt.get("retry_at") or 0) - time.time()
                host = attempt.get("backoff_key") or attempt.get("host")
                if host and delay > 0:
                    _NEXT[host] = max(_NEXT.get(host, 0), time.monotonic() + delay)
                    _BACKOFF_STATUS[host] = attempt.get("http_status") or 429


def quota_reset(headers, *, successful=False) -> float:
    if not headers or headers.get("X-RateLimit-Remaining") != "0":
        return 0
    if successful and headers.get("X-RateLimit-Cost-USD") == "0":
        return 0
    try:
        reset = float(headers.get("X-RateLimit-Reset", "0"))
        return max(0, reset - time.time() if reset > 1_000_000 else reset)
    except (ValueError, TypeError):
        return 0


def get(url: str, *, opener=None, sleep=time.sleep, max_bytes=MAX_BYTES, accept=None) -> bytes:
    """Retry transient failures; pace hosts and validate redirect destinations."""
    public_url(url)
    parsed = urlsplit(url)
    host = parsed.hostname
    # OpenAlex entity lookups are free even when title-search credits are exhausted.
    bucket = host + ":search" if host == "api.openalex.org" and parsed.path.rstrip("/") == "/works" else host
    open_request = opener or build_opener(PublicRedirect()).open
    for attempt in range(3):
        with _GATE:
            delay = max(0, _NEXT.get(bucket, 0) - time.monotonic())
            if delay > 10:
                raise ProviderUnavailable(host, delay, _BACKOFF_STATUS.get(bucket, 429), bucket=bucket)
            _NEXT[bucket] = time.monotonic() + delay + INTERVALS.get(host, 0.5)
        if delay:
            sleep(delay)
        try:
            media_accept = accept or ("application/atom+xml" if host == "export.arxiv.org" else "application/json" if host in {"api.crossref.org", "api.openalex.org", "api.semanticscholar.org", "api.datacite.org"} else "text/html")
            request = Request(url, headers={"User-Agent": "1MorePaper/0.2 (https://github.com/perandre/zotero-audio)", "Accept": media_accept})
            with open_request(request, timeout=12) as response:
                raw = response.read(max_bytes + 1)
                reset = quota_reset(getattr(response, "headers", None), successful=True)
                if reset:
                    with _GATE:
                        _NEXT[bucket] = max(_NEXT.get(bucket, 0), time.monotonic() + reset)
                        _BACKOFF_STATUS[bucket] = 429
            if len(raw) > max_bytes:
                raise ValueError("Source response exceeds size limit")
            return raw
        except HTTPError as exc:
            code = exc.code
            retry = exc.headers.get("Retry-After") if exc.headers else None
            exc.close()
            if code in {404, 410}:
                return b""
            if code not in {408, 429, 500, 502, 503, 504}:
                raise
            delay = max(2 ** (attempt + 1), quota_reset(exc.headers))
            if retry:
                try:
                    delay = max(delay, float(retry))
                except ValueError:
                    try:
                        delay = max(delay, parsedate_to_datetime(retry).timestamp() - time.time())
                    except (ValueError, TypeError, OverflowError):
                        pass
            if code == 429:
                delay = max(delay, 60)
            with _GATE:
                _NEXT[bucket] = max(_NEXT.get(bucket, 0), time.monotonic() + delay)
                _BACKOFF_STATUS[bucket] = code
            if delay > 10:
                raise ProviderUnavailable(host, delay, code, bucket=bucket) from None
            if attempt == 2:
                raise
        except (URLError, TimeoutError):
            if attempt == 2:
                raise
            sleep(2 ** (attempt + 1))
    return b""


def inverted_abstract(index) -> str:
    if not isinstance(index, dict):
        return ""
    words = {}
    for word, positions in index.items():
        if not isinstance(word, str) or not isinstance(positions, list):
            return ""
        for position in positions:
            if type(position) is not int or not 0 <= position < 24000 or position in words:
                return ""
            words[position] = word
    if not words or set(words) != set(range(len(words))):
        return ""
    return normalize_abstract(" ".join(words[p] for p in range(len(words))))


class PublisherMetadata(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.meta = {}
        self.jsonld = []
        self._json = None
        self.abstract_parts = []
        self._abstract_depth = 0

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag not in {"meta", "link", "img", "br", "hr", "input", "source", "wbr", "area", "base", "embed", "param", "track", "col"}:
            marker = " ".join(str(attrs.get(k) or "") for k in ("id", "class", "data-title"))
            if self._abstract_depth:
                self._abstract_depth += 1
            elif tag in {"section", "div", "p"} and re.search(r"abstract|^Abs1(?:-content)?$", marker, flags=re.I):
                self._abstract_depth = 1
        if tag == "meta":
            name = str(attrs.get("name") or attrs.get("property") or "").casefold()
            self.meta.setdefault(name, attrs.get("content", ""))
        if tag == "script" and attrs.get("type", "").lower() == "application/ld+json":
            self._json = []

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in {"meta", "link", "img", "br", "hr", "input", "source", "wbr", "area", "base", "embed", "param", "track", "col"}:
            self.handle_endtag(tag)

    def handle_data(self, data):
        if self._abstract_depth and self._json is None:
            self.abstract_parts.append(data)
        if self._json is not None:
            self._json.append(data)

    def handle_endtag(self, tag):
        if self._abstract_depth:
            self._abstract_depth -= 1
        if tag == "script" and self._json is not None:
            try:
                self.jsonld.append(json.loads("".join(self._json)))
            except ValueError:
                pass
            self._json = None


def publisher_abstract(raw: bytes, data: dict) -> str:
    page = PublisherMetadata()
    page.feed(raw.decode("utf-8", errors="replace"))
    title = page.meta.get("citation_title") or page.meta.get("dc.title") or page.meta.get("og:title", "")
    doi = doi_value(data.get("DOI"))
    returned_doi = doi_value(page.meta.get("citation_doi") or page.meta.get("dc.identifier", ""))
    if (doi and returned_doi and doi.casefold() != returned_doi.casefold()) or not title_match(data["title"], title, strict=True):
        return ""
    text = normalize_abstract(page.meta.get("citation_abstract") or page.meta.get("dc.description.abstract"))
    if not text:
        stack = list(page.jsonld)
        while stack:
            node = stack.pop()
            if isinstance(node, list):
                stack.extend(node)
            elif isinstance(node, dict):
                stack.extend(v for v in node.values() if isinstance(v, (dict, list)))
                if title_match(data["title"], node.get("headline") or node.get("name", ""), strict=True):
                    text = normalize_abstract(node.get("abstract"))
                    if text:
                        break
    if not text:
        text = normalize_abstract(" ".join(page.abstract_parts))
        text = re.sub(r"^abstract\s*[:.—–-]?\s*", "", text, flags=re.I).strip()
    return "" if text.endswith(("...", "…")) else text


def retrieve(data: dict, *, fetch=get, skip_crossref=False) -> dict:
    """Try independent public sources; never treat failure as a verified absence."""
    doi, title = doi_value(data.get("DOI")), data["title"]
    attempts = []

    def attempt(source, url, parser):
        try:
            raw = fetch(url)
            text = parser(raw) if raw else ""
            text = normalize_abstract(text)
            attempts.append({"source": source, "status": "found" if text else "not_found"})
            if text:
                return {"abstract": text, "source": source, "resource": url, "attempts": attempts, "status": "found"}
        except (OSError, ValueError, TypeError, KeyError, AttributeError, ET.ParseError) as exc:
            # Keep exception type/status, never response bodies or private URL queries.
            attempts.append({"source": source, "status": "unavailable", "error": type(exc).__name__, "http_status": getattr(exc, "code", None),
                             "host": urlsplit(url).hostname, "backoff_key": getattr(exc, "backoff_key", None), "retry_at": getattr(exc, "retry_at", None)})
        return None

    providers = []
    if doi and not skip_crossref:
        def crossref(raw):
            message = json.loads(raw)["message"]
            return message.get("abstract", "") if doi_value(message.get("DOI")).casefold() == doi.casefold() and title_match(title, " ".join(message.get("title", []))) else ""
        providers.append(("crossref", "https://api.crossref.org/works/" + quote(doi, safe=""), crossref))
    # OpenAlex can resolve by DOI and by title for items without identifiers.
    def openalex(raw):
        payload = json.loads(raw)
        candidates = payload.get("results", []) if "results" in payload else [payload]
        for work in candidates:
            if doi:
                if doi_value(work.get("doi")).casefold() != doi.casefold():
                    continue
            if title_match(title, work.get("title", ""), strict=not bool(doi)):
                return inverted_abstract(work.get("abstract_inverted_index"))
        return ""
    oa_url = "https://api.openalex.org/works/https://doi.org/" + quote(doi, safe="") if doi else "https://api.openalex.org/works?" + urlencode({"search": title, "per-page": 3})
    providers.append(("openalex", oa_url, openalex))
    def semantic(raw):
        payload = json.loads(raw)
        for work in payload.get("data", [payload]):
            if doi and doi_value(work.get("externalIds", {}).get("DOI")).casefold() != doi.casefold():
                continue
            if title_match(title, work.get("title", ""), strict=not bool(doi)):
                return work.get("abstract", "")
        return ""
    s2_url = "https://api.semanticscholar.org/graph/v1/paper/DOI:" + quote(doi, safe="") + "?fields=title,abstract,externalIds" if doi else "https://api.semanticscholar.org/graph/v1/paper/search?" + urlencode({"query": title, "limit": 3, "fields": "title,abstract,externalIds"})
    providers.append(("semantic-scholar", s2_url, semantic))
    if doi:
        def datacite(raw):
            work = json.loads(raw)["data"]
            if doi_value(work.get("id")).casefold() != doi.casefold():
                return ""
            attrs = work["attributes"]
            if not any(title_match(title, x.get("title", "")) for x in attrs.get("titles", [])):
                return ""
            return next((d.get("description", "") for d in attrs.get("descriptions", []) if d.get("descriptionType", "").lower() == "abstract"), "")
        providers.append(("datacite", "https://api.datacite.org/dois/" + quote(doi, safe=""), datacite))
    arxiv = re.search(r"(?:arxiv\.org/(?:abs|pdf)/|10\.48550/arxiv\.)(\d{4}\.\d{4,5}(?:v\d+)?)", doi + " " + str(data.get("url", "")), flags=re.I)
    if arxiv:
        identifier = arxiv[1]
        def atom(raw):
            for entry in ET.fromstring(raw).findall("{http://www.w3.org/2005/Atom}entry"):
                actual_id = entry.findtext("{http://www.w3.org/2005/Atom}id", "")
                actual_title = entry.findtext("{http://www.w3.org/2005/Atom}title", "")
                if re.search(r"/abs/" + re.escape(identifier) + r"(?:v\d+)?$", actual_id) and title_match(title, actual_title):
                    return entry.findtext("{http://www.w3.org/2005/Atom}summary", "")
            return ""
        providers.insert(0, ("arxiv", "https://export.arxiv.org/api/query?" + urlencode({"id_list": identifier}), atom))
    # Resolve the DOI landing page, or use a public reference URL without query credentials.
    landing = "https://doi.org/" + quote(doi, safe="/()") if doi else str(data.get("url") or "")
    if landing:
        try:
            parsed = urlsplit(landing)
            landing = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
            providers.append(("publisher", landing, lambda raw: publisher_abstract(raw, data)))
        except ValueError:
            attempts.append({"source": "publisher", "status": "unavailable", "error": "InvalidURL"})
    for provider in providers:
        result = attempt(*provider)
        if result:
            return result
    return {"abstract": "", "source": "", "resource": "", "attempts": attempts,
            "status": "retry_pending" if any(a["status"] == "unavailable" for a in attempts) else "not_found"}
