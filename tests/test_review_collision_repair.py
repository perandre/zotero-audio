import runpy
from pathlib import Path

from zotero_audio.app_state import Store
from zotero_audio.article_files import REVIEW_SOURCE_MARKER
from zotero_audio.util import atomic_write_json, sha256_text


def test_repair_audits_report_body_and_intro_then_restores_without_forcing_audio(tmp_path, monkeypatch):
    tool = runpy.run_path(str(Path(__file__).parents[1] / 'scripts/repair_review_collisions.py'))
    store = Store(tmp_path)
    bundle = tmp_path / 'library' / 'SYNTHETIC'
    path = tmp_path / 'papers' / 'Synthetic research.md'
    path.parent.mkdir()
    source = '# Synthetic research\n\nAuthor edits remain exact.\n'
    damaged = ('# Review: Synthetic research\n\n'
               'You are reviewing a Zotero research document and its optional audio editions.\n\n'
               + REVIEW_SOURCE_MARKER + source)
    path.write_text(damaged)
    store.put_article({'id': 'SYNTHETIC', 'title': 'Synthetic research', 'markdown': str(path),
                       'bundle': str(bundle)}, markdown=damaged)
    for edition, text in (('brief', 'An abstract.'), ('full', 'You are reviewing a Zotero research document and its optional audio editions.')):
        atomic_write_json(bundle / 'editions' / edition / 'speech-plan.json', {
            'document': {'title': 'Review: Synthetic research'}, 'segments': [{'text': text}]})
    findings = tool['audit'](store)
    assert findings[0]['contaminated_editions'] == ['brief', 'full']
    assert findings[0]['review_depth'] == 1
    report = tmp_path / 'audit.json'
    monkeypatch.setattr('sys.argv', ['repair', '--runtime', str(tmp_path), '--report', str(report), '--apply', '--queue'])
    assert tool['main']() == 0
    article = store.article('SYNTHETIC')
    assert Path(article['markdown']).read_text() == source
    assert article['markdown_sha256'] == sha256_text(source)
    assert any(p.read_text() == damaged for p in path.parent.glob('*revision*.md'))
    jobs = store.jobs()['jobs']
    assert len(jobs) == 1
    assert jobs[0]['action'] == 'both'
    assert jobs[0]['force'] is False
    assert (tmp_path / 'audit-backups/SYNTHETIC/full/speech-plan.json').is_file()
