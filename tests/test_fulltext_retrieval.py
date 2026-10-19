import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from zotero_audio import fulltext_retrieval as full
from zotero_audio.app_state import Store
from zotero_audio.research_library import write_research_markdown
from zotero_audio.library_export import export_library


def record(key='REF1', **data):
    return {'key': key, 'library_id': 'users/0', 'data': {'title': 'Synthetic research on organizational knowledge work', 'DOI': '10.1234/synthetic', 'creators': [{'name': 'Ada Author'}], **data}}


def response(value):
    return json.dumps(value).encode()


def test_sources_match_identity_and_include_private_license_locations():
    r = record()
    def fetch(url):
        if 'openalex' in url:
            return response({'doi': 'https://doi.org/10.1234/synthetic', 'title': r['data']['title'],
                             'locations': [{'is_oa': False, 'pdf_url': 'https://publisher.test/source.pdf'}, {'pdf_url': 'https://repo.test/paper.pdf'}]})
        return response({'message': {'DOI': '10.1234/wrong', 'title': [r['data']['title']], 'link': [{'content-type': 'application/pdf', 'URL': 'https://wrong.test/wrong.pdf'}]}})
    sources, attempts = full.source_candidates(r['data'], fetch=fetch)
    assert sources[0]['url'] == 'https://publisher.test/source.pdf'
    assert any(s['url'] == 'https://repo.test/paper.pdf' for s in sources)
    assert not any('wrong.test' in s['url'] for s in sources)
    assert attempts[-1]['status'] == 'identity_mismatch'


def test_existing_markdown_is_preserved_and_doi_alias_gets_a_full_link(tmp_path):
    store = Store(tmp_path)
    path = write_research_markdown(tmp_path/'papers', {'id': 'PDF1', 'title': record()['data']['title']}, '# Manual research edits\n')
    store.put_article({'id': 'PDF1', 'title': record()['data']['title'], 'markdown': str(path), 'metadata': {'parent_key': 'OTHER', 'doi': '10.1234/synthetic'}})
    result = full.run(store, records=[record()], fetch=lambda *_a, **_k: pytest.fail('Existing DOI text requires no network'))
    assert result['already_ready'] == 1
    assert path.read_text() == '# Manual research edits\n'
    exported = export_library(store, records=[record()])
    assert exported['with_full_text'] == 1


def test_durable_pdf_cache_recovers_without_downloading_again(tmp_path, monkeypatch):
    store = Store(tmp_path)
    r = record()
    monkeypatch.setattr(full, 'source_candidates', lambda *a, **k: ([{'source': 'repository', 'url': 'https://repo.test/paper.pdf', 'kind': 'pdf'}], []))
    downloads = []
    def fetch(url, **kwargs):
        downloads.append(url)
        return b'%PDF- synthetic source'
    def fail(*args):
        raise ValueError('pdf-extraction-failed')
    result = full.run(store, records=[r], fetch=fetch, extractor=fail)
    assert result['retry_pending'] == 1
    assert Path(next(iter(full.receipts(store).values()))['pdf']).is_file()
    def extract(store, record, pdf):
        path = write_research_markdown(store.runtime/'papers', {'id': 'ref-REF1', 'title': r['data']['title']}, '# Full research\nSource references preserved.\n')
        return {'markdown': str(path), 'source_sha256': 'hash'}
    # A cache larger than the new run's budget must remain usable.
    result = full.run(store, records=[r], max_download_mib=.000001, fetch=lambda *_a, **_k: pytest.fail('Should reuse source'), extractor=extract)
    assert result['retrieved'] == 1
    assert downloads == ['https://repo.test/paper.pdf']
    assert full.run(store, records=[r], fetch=fetch, extractor=extract)['already_ready'] == 1


def test_disk_reserve_blocks_download_with_a_durable_reason(tmp_path, monkeypatch):
    store = Store(tmp_path)
    monkeypatch.setattr(full.shutil, 'disk_usage', lambda _: SimpleNamespace(free=0))
    result = full.run(store, records=[record()], fetch=lambda *_a, **_k: pytest.fail('Do not fill disk'))
    assert result['disk_budget'] == 1
    assert next(iter(full.receipts(store).values()))['status'] == 'disk_budget'


def article_html(title, content):
    return f'<meta name="citation_title" content="{title}"><article>{content}</article>'.encode()


def test_html_requires_body_end_and_references_and_preserves_links():
    data = record()['data']
    prose = ('Synthetic research text about organizational knowledge work. ' * 230)
    sections = '<h2>Introduction</h2><p>' + prose + '</p><h2>Discussion</h2><p>Findings.</p><h2>References</h2><p><a href="/cited">Cited research</a></p>'
    result = full.html_markdown(article_html(data['title'], sections), data, 'https://publisher.test/article')
    assert prose.strip() in result
    assert '[Cited research](https://publisher.test/cited)' in result
    assert not full.html_markdown(article_html(data['title'], '<h2>Abstract</h2><p>'+prose+'</p>'), data, 'https://publisher.test/article')
    assert not full.html_markdown(article_html('Different paper', sections), data, 'https://publisher.test/article')


def test_html_full_text_is_catalogued_privately_without_audio_or_publication(tmp_path):
    store = Store(tmp_path)
    r = record()
    result = full.save_html(store, r, b'<article>Source</article>', 'https://publisher.test/article?private=query', '## Introduction\n\nFull article text.\n\n## References\n\nReferences.')
    article = store.article('ref-REF1')
    assert article['markdown'] == result['markdown']
    assert Path(article['markdown']).parent == tmp_path/'papers'
    assert article['license_status'] == 'private'
    assert article['in_zotero'] is False
    assert article['editions'] == {}
    assert store.jobs()['jobs'] == []
    assert store.search('Full article')['results']
    assert '?private' not in Path(result['markdown']).read_text()


def test_pdf_bytes_require_matching_identity_and_substantive_text(tmp_path):
    import fitz
    pdf = tmp_path/'source.pdf'
    doc = fitz.open(); page = doc.new_page()
    page.insert_textbox(fitz.Rect(40,40,550,800), '10.1234/synthetic\n' + ('Research knowledge work evidence. ' * 170), fontsize=9)
    doc.save(pdf); doc.close()
    assert full.pdf_identity(pdf, record()['data']) == 1
    with pytest.raises(ValueError, match='identity'):
        full.pdf_identity(pdf, record(title='Unrelated particles and fluid mechanics', DOI='10.1234/wrong')['data'])
    doc = fitz.open(); page = doc.new_page(); page.insert_text((40,40), '10.1234/synthetic Abstract only.'); doc.save(tmp_path/'abstract.pdf'); doc.close()
    with pytest.raises(ValueError, match='insufficient-full-text'):
        full.pdf_identity(tmp_path/'abstract.pdf', record()['data'])


def test_real_pdf_extraction_runs_in_child_and_indexes_canonical_markdown(tmp_path):
    import fitz
    store = Store(tmp_path/'runtime')
    pdf = tmp_path/'source.pdf'
    doc = fitz.open(); page = doc.new_page()
    page.insert_textbox(fitz.Rect(40,40,550,800), record()['data']['title'] + '\n10.1234/synthetic\nIntroduction\n' + ('Research knowledge work evidence. ' * 170), fontsize=9)
    doc.save(pdf); doc.close()
    result = full.extract_source(store, record(), pdf, timeout=60)
    path = Path(result['markdown'])
    assert path.parent == store.runtime/'papers'
    assert record()['data']['title'] in path.read_text()
    assert result['source_kind'] == 'pdf'
    assert store.article('ref-REF1')['source_path'] == str(pdf)
    assert store.search('knowledge work')['results']


def test_cli_fetch_full_refreshes_inventory_without_audio_job(tmp_path, monkeypatch, capsys):
    from zotero_audio import app_cli, library_export
    store = Store(tmp_path)
    monkeypatch.setattr(app_cli, 'Store', lambda: store)
    monkeypatch.setattr(full, 'run', lambda *a, **k: {'retrieved': 0, 'retry_pending': 1})
    monkeypatch.setattr(library_export, 'discover', lambda _: [record()])
    monkeypatch.setattr(library_export, 'make_zotero_api', lambda *a, **k: object())
    assert app_cli.main(['library','fetch-full','--limit','1','--json']) == 0
    result = json.loads(capsys.readouterr().out)
    assert result['acquisition']['retry_pending'] == 1
    assert result['references'] == 1
    assert store.jobs()['jobs'] == []


def test_full_status_checks_actual_files_and_does_not_start_network(tmp_path):
    store = Store(tmp_path)
    r = record()
    full.init_tables(store)
    store.set_state('fulltext_run', {'references': 1, 'fingerprints': [full.fingerprint(r)]})
    with store.db() as db:
        db.execute('INSERT INTO fulltext_retrievals VALUES (?,?,0)', (full.fingerprint(r), json.dumps({'status': 'retrieved', 'markdown': str(tmp_path/'missing.md')})))
    result = full.status(store)
    assert not result['active']
    assert result['full_text_ready'] == 0
    assert result['processed'] == 1
