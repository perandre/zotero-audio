import copy
import importlib.util
import json
from pathlib import Path

from zotero_audio.extract import render_research_markdown, render_markdown
from zotero_audio.episode_descriptions import normalize_abstract, select_description_abstract
from zotero_audio.markdown_quality import pdf_quality


def structure():
    paragraph = 'Substantive source paragraph with scientific evidence that must remain available in the research document. ' * 5
    return {'document': {'title': 'Synthetic research'}, 'source': {'filename': 'source.pdf', 'sha256': 'a', 'pdf_pages': 1},
            'extraction': {'engine': 'fixture', 'engine_version': '1'},
            'blocks': [{'text': 'Readable narration.', 'type': 'paragraph', 'pdf_page': 1, 'included_in_reading': True}],
            'pages': [{'pdf_page': 1, 'layout': [
                {'type': 'page-header', 'raw_text': 'Journal running head', 'included': False},
                {'type': 'page-header', 'raw_text': paragraph, 'included': False},
                {'type': 'table', 'raw_text': 'Dimension Core components Evidence\nRetrieval Verified sources Audit', 'included': False},
                {'type': 'picture', 'raw_text': 'Important source figure labels', 'included': False},
                {'type': 'unassigned', 'raw_text': 'Previously unassigned source evidence', 'included': False},
                {'type': 'text', 'raw_text': 'Readable narration.', 'included': True}]}]}


def test_research_preserves_misclassified_prose_tables_and_unassigned_spans_without_changing_narration():
    s = structure(); before = copy.deepcopy(s)
    text = render_research_markdown(s)
    assert s == before
    assert 'Substantive source paragraph' in text
    assert 'Dimension Core components Evidence' in text
    assert 'Important source figure labels' in text
    assert 'Previously unassigned source evidence' in text
    assert 'Journal running head' in text
    assert text.count('Readable narration.') == 1
    assert 'Substantive source paragraph' not in render_markdown(s)


def test_bounded_inferred_abstract_wins_over_blocks_that_swallowed_article_body():
    selected = select_description_abstract({}, structure={'document': {'abstract': 'Correct bounded abstract.', 'abstract_source': 'pdf-explicit-heading'},
        'blocks': [{'type': 'heading', 'text': 'Abstract'}, {'type': 'paragraph', 'text': 'Wrong article body. ' * 800}]}, fetch_doi=lambda _: '')
    assert selected['description_abstract'] == 'Correct bounded abstract.'


def test_flattened_abstract_stops_at_introduction_and_rejects_unbounded_body():
    selected = select_description_abstract({}, structure={'blocks': [
        {'type': 'heading', 'text': 'Abstract'},
        {'type': 'paragraph', 'text': 'The authors report useful findings. 1. Introduction The rest of this article is not its abstract.'}]}, fetch_doi=lambda _: '')
    assert selected['description_abstract'] == 'The authors report useful findings.'
    selected = select_description_abstract({}, structure={'blocks': [{'type': 'heading', 'text': 'Abstract'}, {'text': 'Unbounded scientific prose. ' * 600}]}, fetch_doi=lambda _: '')
    assert not selected['description_abstract']


def test_editorial_labels_are_not_abstracts_and_xml_is_normalized():
    for text in ['Peer Reviewed', 'version:NA', 'Publisher Copyright: © 2026 Copyright held by the owner/author(s).']:
        assert not normalize_abstract(text)
    assert normalize_abstract('<jats:p>Scientific findings.</jats:p>') == 'Scientific findings.'


def test_source_coverage_exposes_dropped_prose_and_does_not_certify_semantics(tmp_path):
    import pymupdf
    pdf = tmp_path/'source.pdf'
    text = 'Specific scientific evidence about retrieval reliability and organizational knowledge. ' * 30
    doc = pymupdf.open(); page = doc.new_page(); page.insert_textbox(pymupdf.Rect(40,40,550,800), text, fontsize=10); doc.save(pdf); doc.close()
    assert pdf_quality(pdf, '# Article\n'+text)['status'] == 'pass'
    quality = pdf_quality(pdf, '# Article\nUnrelated brief text.')
    assert quality['status'] == 'warning'
    assert 'source-text-coverage-low' in quality['findings']
    assert 'reading order' in quality['limits']


def test_repair_preserves_manual_edits_and_writes_revisions_for_verified_generated_sources(tmp_path):
    import pymupdf
    from zotero_audio.app_state import Store
    from zotero_audio.research_library import write_research_markdown
    from zotero_audio.util import sha256_file
    spec = importlib.util.spec_from_file_location('repair_quality', Path(__file__).parents[1]/'scripts/repair_markdown_quality.py')
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    store = Store(tmp_path); pdf = tmp_path/'source.pdf'; s = structure()
    content = '\n'.join(x['raw_text'] for x in s['pages'][0]['layout'])
    doc = pymupdf.open(); page = doc.new_page(); page.insert_textbox(pymupdf.Rect(40,40,550,800), content, fontsize=9); doc.save(pdf); doc.close()
    s['source']['sha256'] = sha256_file(pdf)
    bundle = tmp_path/'bundle'; bundle.mkdir(); (bundle/'structure.json').write_text(json.dumps(s))
    article = {'id': 'REF1', 'title': 'Synthetic research', 'source_path': str(pdf), 'source_sha256': sha256_file(pdf), 'bundle': str(bundle)}
    path = write_research_markdown(tmp_path/'papers', article, render_research_markdown(s, original_spans=False))
    store.put_article({**article, 'markdown': str(path), 'markdown_sha256': sha256_file(path)})
    preview = module.repair(store)
    assert preview['counts']['repairable'] == 1
    original = path.read_bytes()
    assert module.repair(store, apply=True)['counts']['repaired'] == 1
    assert 'Substantive source paragraph' in path.read_text()
    assert any(p.read_bytes() == original for p in (tmp_path/'papers').rglob('*.md') if p != path)
    path.write_text(path.read_text()+'\nMy edits\n')
    edited = path.read_bytes()
    assert module.repair(store, apply=True)['counts']['preserved-manual-or-untracked-edits'] == 1
    assert path.read_bytes() == edited
    assert store.jobs()['jobs'] == []
