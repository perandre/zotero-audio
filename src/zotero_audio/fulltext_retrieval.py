"""Resume private full-text acquisition without selecting audio or publication."""
from __future__ import annotations

import fcntl
import hashlib
import json
import re
import shutil
import subprocess
import sys
import time
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import quote, urljoin, urlsplit, urlunsplit

from .abstract_retrieval import get, doi_value, title_match, restore_backoffs
from .app_research import discover
from .app_state import Store, digest, now
from .research_library import write_research_markdown
from .util import atomic_write_json, sha256_file
from .zotero_web import make_zotero_api

MAX_PDF = 40 * 1024 * 1024
RESERVE_BYTES = 5 * 1024**3


def fingerprint(record):
    data = record['data']
    return digest({'library': record['library_id'], 'key': record['key'], 'title': data['title'],
                   'doi': doi_value(data.get('DOI')).casefold(), 'policy': 'fulltext-v1'})


def init_tables(store):
    with store.db() as db:
        db.execute('CREATE TABLE IF NOT EXISTS fulltext_retrievals (hash TEXT PRIMARY KEY, data TEXT NOT NULL, checked_at REAL NOT NULL)')


def receipts(store):
    with store.db() as db:
        exists = db.execute("SELECT 1 FROM sqlite_master WHERE name='fulltext_retrievals'").fetchone()
        return {row[0]: json.loads(row[1]) for row in db.execute('SELECT hash,data FROM fulltext_retrievals')} if exists else {}


def public_resource(url):
    parsed = urlsplit(url)
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, '', ''))


class PageLinks(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.meta, self.pdfs = {}, []
    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'meta':
            name = str(attrs.get('name') or attrs.get('property') or '').casefold()
            self.meta[name] = attrs.get('content', '')
            if name in {'citation_pdf_url', 'wkhealth_pdf_url'}:
                self.pdfs.append(attrs.get('content', ''))
        if tag == 'link' and attrs.get('type') == 'application/pdf':
            self.pdfs.append(attrs.get('href', ''))


def source_candidates(data, *, fetch=get):
    """Collect source-bound locations without filtering private use by license."""
    doi, title = doi_value(data.get('DOI')), data['title']
    candidates, attempts = [], []
    def add(source, url, kind='pdf'):
        if url:
            candidates.append({'source': source, 'url': url, 'kind': kind})
    url = str(data.get('url') or '')
    arxiv = re.search(r'(?:arxiv\.org/(?:abs|pdf)/|10\.48550/arxiv\.)(\d{4}\.\d{4,5}(?:v\d+)?)', doi + ' ' + url, re.I)
    if arxiv:
        add('arxiv', 'https://arxiv.org/pdf/' + arxiv[1])
    if url:
        add('zotero-url', url, 'pdf' if urlsplit(url).path.lower().endswith('.pdf') else 'landing')
    providers = [('openalex', 'https://api.openalex.org/works/https://doi.org/' + quote(doi, safe=''))] if doi else []
    if not doi:
        from urllib.parse import urlencode
        providers.append(('openalex', 'https://api.openalex.org/works?' + urlencode({'search': title, 'per-page': 3})))
    if doi:
        providers.append(('crossref', 'https://api.crossref.org/works/' + quote(doi, safe='')))
    for source, endpoint in providers:
        try:
            raw = fetch(endpoint)
            if not raw:
                attempts.append({'source': source, 'status': 'not_found'})
                continue
            payload = json.loads(raw)
            works = payload.get('results', [payload]) if source == 'openalex' else [payload['message']]
            matched = False
            for work in works:
                actual_doi = doi_value(work.get('doi') if source == 'openalex' else work.get('DOI'))
                actual_title = work.get('title', '') if source == 'openalex' else ' '.join(work.get('title', []))
                if (doi and actual_doi.casefold() != doi.casefold()) or not title_match(title, actual_title, strict=not bool(doi)):
                    continue
                matched = True
                if source == 'openalex':
                    for location in [work.get('best_oa_location') or {}, *work.get('locations', [])]:
                        add(source, location.get('pdf_url'))
                        add(source, location.get('landing_page_url'), 'landing')
                else:
                    for link in work.get('link', []):
                        if link.get('content-type') == 'application/pdf':
                            add(source, link.get('URL'))
                break
            attempts.append({'source': source, 'status': 'matched' if matched else 'identity_mismatch'})
        except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
            attempts.append(failure(source, exc))
    if doi:
        add('publisher', 'https://doi.org/' + quote(doi, safe='/()'), 'landing')
    seen, unique = set(), []
    for candidate in candidates:
        if candidate['url'] not in seen:
            seen.add(candidate['url']); unique.append(candidate)
    ordered = sorted(unique, key=lambda c: c['kind'] != 'pdf')
    return [c for c in ordered if c['kind'] == 'pdf'][:8] + [c for c in ordered if c['kind'] == 'landing'][:3], attempts


def failure(source, exc):
    return {'source': source, 'status': 'unavailable', 'error': type(exc).__name__,
            'reason': str(exc) if isinstance(exc, ValueError) and str(exc) in {'encrypted-source', 'source-identity-unverified', 'insufficient-full-text', 'pdf-extraction-failed'} else None,
            'http_status': getattr(exc, 'code', None), 'retry_at': getattr(exc, 'retry_at', None),
            'host': getattr(exc, 'host', None), 'backoff_key': getattr(exc, 'backoff_key', None)}


def metadata_for(record):
    data = record['data']
    return {'title': data['title'], 'doi': doi_value(data.get('DOI')), 'parent_key': record['key'],
            'authors': [c.get('name') or ' '.join(filter(None, [c.get('firstName'), c.get('lastName')])) for c in data.get('creators', [])],
            'publication_year': next(iter(re.findall(r'\b(?:19|20)\d{2}\b', str(data.get('date') or ''))), None),
            'url': data.get('url'), 'rights': data.get('rights'), 'item_type': data.get('itemType'),
            'abstract': data.get('abstractNote'), 'language': data.get('language')}


def pdf_identity(pdf, data):
    """Check source bytes before enriching their title from Zotero metadata."""
    from pypdf import PdfReader
    reader = PdfReader(pdf)
    if reader.is_encrypted and not reader.decrypt(''):
        raise ValueError('encrypted-source')
    first_pages = ' '.join(page.extract_text() or '' for page in reader.pages[:2])
    normalized = re.sub(r'\s+', ' ', first_pages).casefold()
    doi = doi_value(data.get('DOI'))
    pdf_title = str((reader.metadata or {}).get('/Title') or '')
    expected_words = set(re.findall(r'\w{3,}', data['title'].casefold()))
    found_words = set(re.findall(r'\w{3,}', normalized))
    title_in_text = len(expected_words) >= 4 and len(expected_words & found_words) / len(expected_words) >= .85
    if not (title_match(data['title'], pdf_title, strict=True) or title_in_text or (doi and doi.casefold() in normalized)):
        raise ValueError('source-identity-unverified')
    # Reject landing pages/abstract-only PDFs rather than calling them full text.
    word_count = len(first_pages.split())
    for page in reader.pages[2:]:
        if word_count >= 500:
            break
        word_count += len((page.extract_text() or '').split())
    if word_count < 500:
        raise ValueError('insufficient-full-text')
    return len(reader.pages)


def extract_request(request_path):
    """Run in a bounded child process so one difficult PDF cannot stop the batch."""
    from .extract import extract_pdf, render_research_markdown
    request = json.loads(Path(request_path).read_text())
    pdf = Path(request['pdf'])
    pages = pdf_identity(pdf, request['record']['data'])
    structure = extract_pdf(pdf, zotero_key=request['article_id'], include_references=True, metadata=metadata_for(request['record']))
    bundle = Path(request['bundle']); bundle.mkdir(parents=True, exist_ok=True)
    atomic_write_json(bundle / 'structure.json', structure)
    metadata = metadata_for(request['record'])
    descriptor = {**metadata, 'id': request['article_id'], 'metadata': metadata}
    markdown = write_research_markdown(Path(request['papers']), descriptor, render_research_markdown(structure), provenance='fulltext-retrieval')
    from .markdown_quality import pdf_quality
    quality = pdf_quality(pdf, markdown.read_text())
    atomic_write_json(bundle / 'markdown-quality.json', quality)
    atomic_write_json(bundle / 'extracted.json', {'markdown': str(markdown), 'source_sha256': sha256_file(pdf), 'pdf_pages': pages, 'source_kind': 'pdf', 'markdown_quality': quality})


def extract_source(store, record, pdf, *, timeout=120):
    prefix = 'g' + record['library_id'].split('/')[1] + ':' if record['library_id'].startswith('groups/') else ''
    article_id = prefix + 'ref-' + record['key']
    bundle = store.root / 'fulltext' / 'bundles' / fingerprint(record)
    bundle.mkdir(parents=True, exist_ok=True)
    request_path = bundle / 'request.json'
    # The child never receives arbitrary shell code, credentials or provider bodies.
    atomic_write_json(request_path, {'record': record, 'pdf': str(pdf), 'article_id': article_id,
                                    'bundle': str(bundle), 'papers': str(store.runtime / 'papers')})
    outcome = subprocess.run([sys.executable, '-m', 'zotero_audio.fulltext_retrieval', 'extract', str(request_path)],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=timeout)
    if outcome.returncode:
        error_path = bundle / 'extraction-error.json'
        reason = json.loads(error_path.read_text()).get('reason') if error_path.is_file() else 'pdf-extraction-failed'
        raise ValueError(reason)
    result = json.loads((bundle / 'extracted.json').read_text())
    register_article(store, record, result, bundle=bundle, pdf=pdf)
    return result


def article_id(record):
    prefix = 'g' + record['library_id'].split('/')[1] + ':' if record['library_id'].startswith('groups/') else ''
    return prefix + 'ref-' + record['key']


def register_article(store, record, result, *, bundle, pdf=None):
    metadata = metadata_for(record)
    path = Path(result['markdown'])
    try:
        old = store.article(article_id(record))
    except KeyError:
        old = {}
    article = {'id': article_id(record), 'title': metadata['title'], 'authors': metadata['authors'], 'year': metadata['publication_year'],
        'source_path': str(pdf) if pdf else None, 'source_sha256': result['source_sha256'], 'source_url': metadata.get('url'),
        'metadata': {**old.get('metadata', {}), **metadata}, 'bundle': str(bundle), 'markdown': str(path), 'markdown_sha256': sha256_file(path),
        'markdown_status': 'ready', 'in_zotero': False, 'source_origin': 'fulltext-retrieval',
        'artifacts': {**old.get('artifacts', {}), 'markdown': True},
        'source_changed': bool(old.get('source_sha256') and old['source_sha256'] != result['source_sha256'])}
    if result.get('markdown_quality'):
        article['markdown_quality'] = result['markdown_quality']
    if not old:
        article.update(audio_status='not_started', license_status='private', qa_status='unchecked', editions={}, warnings=[])
    store.put_article(article, markdown=path.read_text())


class ArticleBody(HTMLParser):
    """Read explicit article containers; require start, end and references below."""
    VOID = {'meta', 'link', 'img', 'br', 'hr', 'input', 'source', 'wbr', 'area', 'base', 'embed', 'param', 'track', 'col'}
    def __init__(self, url):
        super().__init__(convert_charrefs=True)
        self.url, self.depth, self.hidden = url, 0, 0
        self.parts, self.headings, self.heading, self.links = [], [], None, []
    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag in self.VOID:
            return
        if self.depth:
            self.depth += 1
        elif tag == 'article' or re.search(r'article[-_]?body|full[-_]?text', ' '.join(str(attrs.get(k) or '') for k in ('class', 'id')), re.I):
            self.depth = 1
        if not self.depth:
            return
        if tag in {'script', 'style', 'nav', 'aside', 'footer', 'form'}:
            self.hidden += 1
        if self.hidden:
            return
        if re.fullmatch(r'h[1-6]', tag):
            self.heading = []
            self.parts.append('\n\n' + '#' * max(2, int(tag[1])) + ' ')
        elif tag in {'p', 'div', 'section', 'ul', 'ol', 'blockquote', 'table', 'tr'}:
            self.parts.append('\n\n')
        elif tag in {'td', 'th'}:
            self.parts.append(' | ')
        elif tag == 'li':
            self.parts.append('\n- ')
        elif tag == 'a':
            href = urljoin(self.url, attrs.get('href', ''))
            href = href if urlsplit(href).scheme in {'http', 'https'} else ''
            self.links.append((self.depth, href))
            if href:
                self.parts.append('[')
    def handle_data(self, data):
        if self.depth and not self.hidden:
            text = re.sub(r'\s+', ' ', data)
            self.parts.append(text)
            if self.heading is not None:
                self.heading.append(text)
    def handle_endtag(self, tag):
        if not self.depth or tag in self.VOID:
            return
        if not self.hidden and tag == 'a' and self.links and self.links[-1][0] == self.depth:
            _, href = self.links.pop()
            if href:
                self.parts.append('](' + href.replace('(', '%28').replace(')', '%29') + ')')
        if tag in {'script', 'style', 'nav', 'aside', 'footer', 'form'} and self.hidden:
            self.hidden -= 1
        if not self.hidden and re.fullmatch(r'h[1-6]', tag) and self.heading is not None:
            self.headings.append(''.join(self.heading)); self.heading = None; self.parts.append('\n\n')
        self.depth -= 1
    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)


def html_markdown(raw, data, url):
    page = PageLinks(); page.feed(raw.decode('utf-8', errors='replace'))
    title = page.meta.get('citation_title') or page.meta.get('dc.title') or page.meta.get('og:title', '')
    doi, actual_doi = doi_value(data.get('DOI')), doi_value(page.meta.get('citation_doi'))
    if not title_match(data['title'], title, strict=True) or (doi and actual_doi and doi.casefold() != actual_doi.casefold()):
        return ''
    body = ArticleBody(url); body.feed(raw.decode('utf-8', errors='replace'))
    headings = ' '.join(body.headings).casefold()
    text = re.sub(r'\n{3,}', '\n\n', ''.join(body.parts)).strip()
    if len(text.split()) < 1500 or not all(re.search(pattern, headings) for pattern in [r'introduction|background', r'conclusion|discussion', r'references|bibliography']):
        return ''
    return text


def save_html(store, record, raw, url, body):
    bundle = store.root / 'fulltext/bundles' / fingerprint(record)
    bundle.mkdir(parents=True, exist_ok=True)
    source_hash = hashlib.sha256(raw).hexdigest()
    (bundle / ('source-' + source_hash + '.html')).write_bytes(raw)
    metadata = metadata_for(record)
    descriptor = {**metadata, 'id': article_id(record), 'metadata': metadata, 'source_sha256': source_hash}
    fields = {'type': 'Research Article', 'title': metadata['title'], 'authors': metadata['authors'], 'doi': metadata['doi'],
              'privacy': 'private', 'source_url': public_resource(url), 'source_sha256': source_hash, 'extractor': 'source-bound-html/v1'}
    markdown = '\n'.join(['---', *[f'{key}: {json.dumps(value, ensure_ascii=False)}' for key,value in fields.items()], '---', '', '# ' + metadata['title'], '', body, ''])
    path = write_research_markdown(store.runtime / 'papers', descriptor, markdown, provenance='fulltext-html-retrieval')
    result = {'markdown': str(path), 'source_sha256': source_hash, 'source_kind': 'html'}
    register_article(store, record, result, bundle=bundle)
    return result


def run(store, *, limit=None, max_download_mib=2048, records=None, fetch=get, extractor=extract_source, progress=print):
    directory = store.root / 'fulltext'; directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (directory / '.run.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError('A full-text retrieval run is already active') from None
        result = _run(store, limit=limit, max_download_mib=max_download_mib, records=records, fetch=fetch, extractor=extractor, progress=progress)
        store.set_state('fulltext_run_summary', {**result, 'finished_at': now()})
        return result


def _run(store, *, limit, max_download_mib, records, fetch, extractor, progress):
    if max_download_mib <= 0 or (limit is not None and limit <= 0):
        raise ValueError('Download budget and limit must be positive')
    init_tables(store)
    saved = receipts(store)
    restore_backoffs(list(saved.values()))
    with store.db() as db:
        exists = db.execute("SELECT 1 FROM sqlite_master WHERE name='research_abstract_retrievals'").fetchone()
        if exists:
            restore_backoffs([json.loads(row[0]) for row in db.execute('SELECT data FROM research_abstract_retrievals')])
    records = records if records is not None else discover(make_zotero_api(store.root, timeout=15))
    store.set_state('fulltext_run', {'references': len(records), 'started_at': now(), 'fingerprints': [fingerprint(record) for record in records]})
    # Preserve authoritative local Markdown and reuse exact-DOI text across references.
    ready, by_doi, local_pdfs = {}, {}, {}
    for article in store.all_articles():
        path = Path(article.get('markdown') or '/missing')
        metadata = article.get('metadata') or {}
        library = 'groups/' + article['id'].split(':', 1)[0][1:] if str(article['id']).startswith('g') and ':' in article['id'] else 'users/0'
        source = Path(article.get('source_path') or '/missing')
        if source.is_file():
            local_pdfs[(library, metadata.get('parent_key') or metadata.get('zotero_parent_key'))] = source
        if path.is_file():
            ready[(library, metadata.get('parent_key') or metadata.get('zotero_parent_key'))] = str(path)
            doi = doi_value(metadata.get('doi'))
            if doi:
                by_doi[doi.casefold()] = (article['title'], str(path))
    files = store.root / 'fulltext/files'; files.mkdir(parents=True, exist_ok=True, mode=0o700)
    budget = max_download_mib * 1024**2
    used = sum(p.stat().st_size for p in files.glob('*.pdf')) + sum(p.stat().st_size for p in (store.root / 'fulltext/bundles').glob('*/source*.html'))
    counts = {'already_ready': 0, 'retrieved': 0, 'retry_pending': 0, 'not_found': 0, 'disk_budget': 0}
    attempted = 0
    # Finish unattempted references before retrying old source failures.
    ordered = sorted(records, key=lambda r: (fingerprint(r) in saved, r['library_id'], r['key']))
    for record in ordered:
        key, data = fingerprint(record), record['data']
        old = saved.get(key, {})
        path = ready.get((record['library_id'], record['key'])) or old.get('markdown')
        doi = doi_value(data.get('DOI')).casefold()
        if not path and doi in by_doi and title_match(data['title'], by_doi[doi][0], strict=True):
            path = by_doi[doi][1]
        if path and Path(path).is_file():
            result = {**old, 'status': 'already_ready', 'markdown': path, 'title': data['title'], 'attempts': old.get('attempts', [])}
            counts['already_ready'] += 1
        else:
            if limit is not None and attempted >= limit:
                continue
            attempted += 1
            deadline = time.monotonic() + 180
            result = {'status': 'not_found', 'title': data['title'], 'attempts': []}
            cached_pdf = Path(old.get('pdf') or '/missing')
            local_pdf = local_pdfs.get((record['library_id'], record['key']))
            if local_pdf or cached_pdf.is_file():
                try:
                    pdf = local_pdf or cached_pdf
                    result.update(extractor(store, record, pdf), status='retrieved', source='local-pdf', pdf=str(pdf))
                except (OSError, ValueError, TypeError, KeyError, subprocess.TimeoutExpired) as exc:
                    result['attempts'].append(failure('local-pdf', exc))
            if result['status'] != 'retrieved' and (used >= budget or shutil.disk_usage(store.runtime).free < RESERVE_BYTES + MAX_PDF):
                result['status'] = 'disk_budget'
            elif result['status'] != 'retrieved':
                try:
                    candidates, source_attempts = source_candidates(data, fetch=fetch)
                    result['attempts'].extend(source_attempts)
                except (OSError, ValueError, TypeError, KeyError) as exc:
                    candidates = []
                    result['attempts'].append(failure('source-discovery', exc))
                seen = set()
                for candidate in candidates:
                    if len(seen) >= 20 or time.monotonic() >= deadline:
                        result['status'] = 'retry_pending'
                        result['attempts'].append({'source': 'fulltext', 'status': 'unavailable', 'reason': 'article-attempt-budget'})
                        break
                    url = candidate['url']
                    if url in seen:
                        continue
                    seen.add(url)
                    try:
                        pdf = files / (hashlib.sha256(url.encode()).hexdigest() + '.pdf')
                        if not pdf.is_file():
                            raw = fetch(url, max_bytes=min(MAX_PDF, budget - used), accept='text/html' if candidate['kind'] == 'landing' else 'application/pdf, text/html;q=0.5')
                            if not raw:
                                result['attempts'].append({'source': candidate['source'], 'status': 'not_found'}); continue
                            if b'%PDF-' not in raw[:1024]:
                                page = PageLinks(); page.feed(raw.decode('utf-8', errors='replace'))
                                actual_title = page.meta.get('citation_title') or page.meta.get('dc.title') or page.meta.get('og:title', '')
                                page_doi = doi_value(page.meta.get('citation_doi'))
                                if title_match(data['title'], actual_title, strict=True) and (not page_doi or not doi or page_doi.casefold() == doi):
                                    candidates.extend({'source': 'publisher-pdf', 'url': urljoin(url, link), 'kind': 'pdf'} for link in page.pdfs[:3])
                                body = html_markdown(raw, data, url)
                                if body:
                                    if shutil.disk_usage(store.runtime).free < RESERVE_BYTES + len(raw):
                                        raise OSError('disk-reserve')
                                    result.update(save_html(store, record, raw, url, body), status='retrieved', source=candidate['source'], resource=public_resource(url))
                                    used += len(raw)
                                    if doi:
                                        by_doi[doi] = (data['title'], result['markdown'])
                                    break
                                result['attempts'].append({'source': candidate['source'], 'status': 'no_full_text'}); continue
                            if shutil.disk_usage(store.runtime).free < RESERVE_BYTES + len(raw):
                                raise OSError('disk-reserve')
                            temporary = pdf.with_suffix('.part'); temporary.write_bytes(raw); temporary.replace(pdf); used += len(raw)
                        result['pdf'] = str(pdf)
                        with store.db() as db:
                            db.execute('INSERT OR REPLACE INTO fulltext_retrievals VALUES (?,?,?)', (key, json.dumps(result), time.time()))
                        extracted = extractor(store, record, pdf, timeout=min(120, max(1, deadline - time.monotonic()))) if extractor is extract_source else extractor(store, record, pdf)
                        result.update(extracted, status='retrieved', source=candidate['source'], resource=public_resource(url))
                        if doi:
                            by_doi[doi] = (data['title'], result['markdown'])
                        break
                    except (OSError, ValueError, TypeError, KeyError, subprocess.TimeoutExpired) as exc:
                        result['attempts'].append(failure(candidate['source'], exc))
                    if used >= budget:
                        result['status'] = 'disk_budget'; break
                if result['status'] == 'not_found' and any(a['status'] == 'unavailable' for a in result['attempts']):
                    result['status'] = 'retry_pending'
            counts[result['status']] += 1
            progress(f"{data['title']}\n  {result['status']}")
        with store.db() as db:
            db.execute('INSERT OR REPLACE INTO fulltext_retrievals VALUES (?,?,?)', (key, json.dumps(result), time.time()))
    return {'references': len(records), 'attempted': attempted, **counts, 'cached_source_bytes': used, 'cache_budget_bytes': budget}


def status(store):
    directory = store.root / 'fulltext'
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / '.run.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            active = False
        except BlockingIOError:
            active = True
    current = store.state('fulltext_run', {})
    keys = set(current.get('fingerprints', []))
    saved = {key: value for key, value in receipts(store).items() if not keys or key in keys}
    from collections import Counter
    counts = Counter(value['status'] for value in saved.values())
    ready = sum(bool(value.get('markdown')) and Path(value['markdown']).is_file() for value in saved.values())
    return {'active': active, 'references': current.get('references', 0), 'processed': len(saved),
            'full_text_ready': ready, 'states': dict(counts), 'last_summary': store.state('fulltext_run_summary', {})}


if __name__ == '__main__':
    if len(sys.argv) == 3 and sys.argv[1] == 'extract':
        try:
            extract_request(sys.argv[2])
        except Exception as exc:
            safe = {'encrypted-source', 'source-identity-unverified', 'insufficient-full-text'}
            reason = str(exc) if isinstance(exc, ValueError) and str(exc) in safe else 'pdf-extraction-failed'
            atomic_write_json(Path(sys.argv[2]).parent / 'extraction-error.json', {'error': type(exc).__name__, 'reason': reason})
            raise SystemExit(1)
    else:
        raise SystemExit('Internal extraction entrypoint requires a saved request')
