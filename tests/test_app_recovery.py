import json
from pathlib import Path

import pytest

from zotero_audio.app_recovery import reconcile_publications
from zotero_audio.app_state import Store
from zotero_audio.util import sha256_file
from zotero_audio.generation import NARRATION_POLICY


@pytest.fixture
def recovery(tmp_path):
    store = Store(tmp_path / 'runtime')
    store.update_settings({'auto_publish': True})
    paths = {}
    for field in ['audio', 'markdown', 'source']:
        p = tmp_path / field
        p.write_bytes(b'synthetic ' + field.encode())
        paths[field] = p
    record = {'edition': 'full', 'title': 'A complete synthetic research title',
              'selected': True, 'status': 'ready', 'publication_status': 'private',
              'plan_sha256': 'p' * 64, 'assembly_sha256': 'a' * 64,
              'synthesis_config': {'engine': 'synthetic'},
              'source_sha256': sha256_file(paths['source'])}
    for field in ['audio', 'markdown']:
        record[field] = str(paths[field])
        record[field + '_sha256'] = sha256_file(paths[field])
    bundle = store.runtime / 'library/PAPER'
    stage = bundle / 'editions/full'
    stage.mkdir(parents=True)
    (stage / 'speech-plan.json').write_text(json.dumps({'narration_policy': NARRATION_POLICY,
                                                       'plan_sha256': record['plan_sha256']}))
    (stage / 'edition.json').write_text(json.dumps(record))
    store.put_article({'id': 'PAPER', 'bundle': str(bundle), 'title': record['title'], 'source_path': str(paths['source']),
                       'source_sha256': record['source_sha256'], 'editions': {'full': record},
                       'metadata': {'rights': 'https://creativecommons.org/licenses/by/4.0/',
                                    'url': 'https://example.org/paper'}})
    job = store.create_job({'action': 'full', 'scope': 'one', 'article_id': 'PAPER'})
    store.update_job(job['id'], status='completed')
    return store, paths, job


def test_selected_finished_request_recovers_new_license_once_without_encoding(recovery):
    store, paths, _ = recovery
    assert reconcile_publications(store) == 1
    assert reconcile_publications(Store(store.runtime)) == 0
    with store.db() as db:
        rows = db.execute("SELECT data FROM deliveries WHERE kind='publish'").fetchall()
    assert len(rows) == 1
    record = json.loads(rows[0][0])['record']
    assert record['source_license']['allowed']
    assert record['license_record']['source_sha256'] == sha256_file(paths['source'])
    assert Path(record['audio']).read_bytes() == b'synthetic audio'


@pytest.mark.parametrize('blocker', ['import', 'cancelled', 'failed', 'unselected', 'disabled', 'restrictive',
                                     'source_changed', 'source_bytes', 'audio_bytes', 'missing_audio', 'markdown_bytes'])
def test_recovery_preserves_intent_license_and_snapshot_gates(recovery, blocker):
    store, paths, job = recovery
    a = store.article('PAPER')
    if blocker == 'import':
        with store.db() as db:
            db.execute('DELETE FROM jobs')
    elif blocker in {'cancelled', 'failed'}:
        store.update_job(job['id'], status=blocker)
    elif blocker == 'unselected':
        a['editions']['full']['selected'] = False
    elif blocker == 'disabled':
        store.update_settings({'auto_publish': False})
    elif blocker == 'restrictive':
        a['metadata']['rights'] = 'All rights reserved'
    elif blocker == 'source_changed':
        a['source_changed'] = True
    elif blocker == 'missing_audio':
        paths['audio'].unlink()
    else:
        paths[blocker.removesuffix('_bytes')].write_bytes(b'replaced bytes')
    store.put_article(a)
    assert reconcile_publications(store) == 0


def test_cancelled_newer_request_prevents_revival_of_older_intent(recovery):
    store, _, _ = recovery
    newer = store.create_job({'action': 'both', 'scope': 'one', 'article_id': 'PAPER'})
    store.cancel(newer['id'])
    assert reconcile_publications(store) == 0


def test_pending_intentional_refresh_cannot_publish_old_snapshot(recovery):
    store, _, _ = recovery
    store.create_job({'action': 'full', 'scope': 'one', 'article_id': 'PAPER'})
    assert reconcile_publications(store) == 0


@pytest.mark.parametrize('change', ['old_policy', 'different_plan', 'different_audio', 'different_config', 'different_assembly'])
def test_restored_snapshot_must_match_current_requested_narration_revision(recovery, change):
    store, _, _ = recovery
    stage = Path(store.article('PAPER')['bundle']) / 'editions/full'
    path = stage / ('speech-plan.json' if change in {'old_policy', 'different_plan'} else 'edition.json')
    data = json.loads(path.read_text())
    field = {'old_policy': 'narration_policy', 'different_plan': 'plan_sha256',
             'different_audio': 'audio_sha256', 'different_config': 'synthesis_config',
             'different_assembly': 'assembly_sha256'}[change]
    data[field] = 'different'
    path.write_text(json.dumps(data))
    assert reconcile_publications(store) == 0


def test_failed_article_in_partially_completed_batch_cannot_revive_old_revision(recovery):
    store, _, _ = recovery
    batch = store.create_job({'action': 'full', 'scope': 'all'})
    store.update_job(batch['id'], status='completed', results=[{'id': 'PAPER', 'status': 'failed'}])
    assert reconcile_publications(store) == 0
