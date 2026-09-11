"""Background discovery tests use synthetic PDFs and the supported API boundary."""
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from zotero_audio import app_zotero, zotero_local
from zotero_audio.app_server import status
from zotero_audio.app_state import Store
from zotero_audio.app_worker import LocalWorker
from zotero_audio.app_zotero import ZoteroMonitor


@pytest.fixture
def catalog(tmp_path, monkeypatch):
    store = Store(tmp_path / 'runtime')
    pdf = tmp_path / 'synthetic.pdf'
    pdf.write_bytes(b'synthetic PDF source')
    source = {'items': [('NEWPAPER', pdf)], 'metadata': {
        'title': 'A complete synthetic research article title', 'authors': ['Ada Researcher'],
        'publication_year': '2026', 'url': 'https://example.org/paper',
        'rights': 'https://creativecommons.org/licenses/by/4.0/'}, 'online': True}

    class API:
        def __init__(self, **kwargs):
            pass

        def pdf_attachments(self):
            if not source['online']:
                raise zotero_local.ZoteroLocalAPIUnavailable('Open Zotero to resume updates')
            return source['items']

        def metadata(self, key):
            return source['metadata']

    monkeypatch.setattr(zotero_local, 'ZoteroLocalAPI', API)
    return store, source, ZoteroMonitor(store, threading.Event())


def test_discovery_queues_selected_outputs_once_across_scans_and_restart(catalog):
    store, source, monitor = catalog
    store.update_settings({'auto_generate': 'both', 'qa_enabled': False})
    monitor.cycle()
    jobs = store.jobs()['jobs']
    assert len(jobs) == 1
    job = jobs[0]
    assert (job['action'], job['scope'], job['qa'], job['force'], job['automatic']) == ('both', 'one', False, False, True)
    assert job['title'] == source['metadata']['title']
    store.claim_job()
    Store(store.runtime).recover()
    ZoteroMonitor(store, threading.Event()).cycle()
    assert [j['id'] for j in store.jobs()['jobs']] == [job['id']]
    assert store.job(job['id'])['status'] == 'queued'


@pytest.mark.parametrize('terminal', ['failed', 'cancelled', 'completed'])
def test_polling_respects_previous_requests_even_after_settings_change(catalog, terminal):
    store, _, monitor = catalog
    monitor.cycle()
    job = store.jobs()['jobs'][0]
    store.update_job(job['id'], status=terminal)
    store.update_settings({'auto_generate': 'full'})
    monitor.cycle()
    assert len(store.jobs()['jobs']) == 1
    assert store.job(job['id'])['status'] == terminal


def test_generation_off_still_discovers_and_catches_up_after_enabling(catalog):
    store, _, monitor = catalog
    store.update_settings({'auto_generate': 'off'})
    monitor.cycle()
    assert store.article('NEWPAPER')['in_zotero']
    assert not store.jobs()['jobs']
    store.update_settings({'auto_generate': 'brief'})
    monitor.cycle()
    assert store.jobs()['jobs'][0]['action'] == 'brief'


def test_offline_retains_catalog_and_last_success_then_recovers(catalog):
    store, source, monitor = catalog
    store.update_settings({'auto_generate': 'off'})
    monitor.cycle()
    before = store.article('NEWPAPER')
    checked = store.state('zotero')['last_success_at']
    source['online'] = False
    monitor.cycle()
    assert store.article('NEWPAPER') == before
    assert store.state('zotero')['online'] is False
    assert store.state('zotero')['last_success_at'] == checked
    source['online'] = True
    source['items'].append(('SECOND', source['items'][0][1]))
    monitor.cycle()
    assert store.state('zotero')['online']
    assert store.article('SECOND')['title'] == source['metadata']['title']


def test_metadata_edits_and_cleared_fields_update_revision_without_changing_research(catalog, tmp_path):
    store, source, monitor = catalog
    store.update_settings({'auto_generate': 'off'})
    monitor.cycle()
    markdown = tmp_path / 'Edited research.md'
    markdown.write_text('# My authoritative research edits\n')
    store.put_article({'id': 'NEWPAPER', 'title': source['metadata']['title'], 'markdown': str(markdown)}, markdown=markdown.read_text())
    before = status(store)
    time.sleep(0.002)
    source['metadata'].update(title='A corrected complete research article title', authors=[], publication_year=None, url=None, rights=None)
    monitor.cycle()
    article = store.article('NEWPAPER')
    assert article['title'] == source['metadata']['title']
    assert article['authors'] == [] and article['year'] is None and article['source_url'] is None
    assert article['license_status'] == 'private'
    assert status(store)['counts'] == before['counts']
    assert status(store)['catalog_revision'] != before['catalog_revision']
    assert markdown.read_text() == '# My authoritative research edits\n'
    revision = status(store)['catalog_revision']
    monitor.cycle()
    assert status(store)['catalog_revision'] == revision
    assert store.search('authoritative')['results']


def test_existing_output_changed_sources_and_removed_pdfs_are_never_auto_generated(catalog, tmp_path):
    store, source, monitor = catalog
    store.update_settings({'auto_generate': 'off'})
    monitor.cycle()
    markdown = tmp_path / 'Existing.md'
    markdown.write_text('# Existing edited article\n')
    store.put_article({'id': 'NEWPAPER', 'title': source['metadata']['title'], 'markdown': str(markdown)})
    store.update_settings({'auto_generate': 'both'})
    monitor.cycle()
    assert not store.jobs()['jobs']
    source['items'][0][1].write_bytes(b'a replaced source PDF')
    monitor.cycle()
    assert store.article('NEWPAPER')['source_changed']
    assert not store.jobs()['jobs']
    source['items'] = []
    monitor.cycle()
    assert store.article('NEWPAPER')['in_zotero'] is False
    assert markdown.read_text() == '# Existing edited article\n'


def test_automatic_queue_is_atomic_across_store_connections(catalog):
    store, _, monitor = catalog
    store.update_settings({'auto_generate': 'off'})
    monitor.cycle()
    request = {'action': 'both', 'scope': 'one', 'article_id': 'NEWPAPER'}
    stores = [store, Store(store.runtime)]
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda s: s.create_job(request, automatic=True), stores))
    assert len(store.jobs()['jobs']) == 1


def test_discovery_continues_while_generation_is_busy(catalog, monkeypatch):
    store, source, _ = catalog
    store.put_article({'id': 'BUSY', 'title': 'Another complete article already generating'})
    store.create_job({'action': 'markdown', 'scope': 'one', 'article_id': 'BUSY'})
    busy = threading.Event()
    release = threading.Event()
    worker = LocalWorker(store)

    def process(job):
        busy.set()
        release.wait(3)
        store.update_job(job['id'], status='completed')

    monkeypatch.setattr(worker, 'process', process)
    worker.start()
    try:
        assert busy.wait(2)
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and len(store.jobs()['jobs']) < 2:
            time.sleep(0.01)
        jobs = store.jobs()['jobs']
        assert len(jobs) == 2
        assert any(j['article_id'] == 'NEWPAPER' and j['status'] == 'queued' for j in jobs)
        assert worker.current_job_id is not None
        assert store.state('zotero')['online']
    finally:
        worker.stop.set()
        release.set()
        for thread in worker.threads:
            thread.join(timeout=3)
        assert all(not thread.is_alive() for thread in worker.threads)


def test_monitor_retries_unexpected_failure_without_logging_private_details(catalog, monkeypatch):
    store, _, monitor = catalog
    calls = []

    def cycle():
        calls.append(1)
        if len(calls) == 1:
            raise OSError('private source details')
        monitor.stop.set()

    monkeypatch.setattr(monitor, 'cycle', cycle)
    monitor.POLL_SECONDS = 0.001
    monitor.run()
    assert len(calls) == 2
    assert store.state('zotero')['online'] is False
    assert 'private source details' not in (store.root / 'events.jsonl').read_text()


def test_removed_automatic_job_does_not_generate(catalog, monkeypatch):
    store, source, monitor = catalog
    monitor.cycle()
    job = store.claim_job()
    source['items'] = []
    monitor.cycle()
    from zotero_audio import generation
    monkeypatch.setattr(generation, 'process_article', lambda *a, **k: pytest.fail('Removed source must not generate'))
    LocalWorker(store).process(job)
    assert store.job(job['id'])['status'] == 'cancelled'
