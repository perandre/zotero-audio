#!/usr/bin/env python3
"""Preview/repair source omissions from cached structures, preserving edited Markdown.

No network, model calls, audio work or publication. --apply preserves revisions.
"""
from __future__ import annotations
import argparse
import json
from pathlib import Path

from zotero_audio.app_state import Store, runtime_root
from zotero_audio.extract import render_research_markdown
from zotero_audio.markdown_quality import pdf_quality
from zotero_audio.research_library import _library_lock, write_research_markdown
from zotero_audio.util import atomic_write_json, sha256_file, sha256_text


def repair(store, *, apply=False, progress=lambda _: None):
    root = store.runtime / 'papers'
    index = json.loads((root / 'index.json').read_text()).get('articles', {})
    rows = []
    seen = set()
    for article in store.all_articles():
        path = Path(article.get('markdown') or '/missing')
        if not path.is_file() or path in seen:
            continue
        seen.add(path)
        bundle = Path(article.get('bundle') or '/missing')
        structure_path = bundle / 'structure.json'
        if not structure_path.is_file():
            continue
        structure = json.loads(structure_path.read_text())
        pdf = Path(article.get('source_path') or structure.get('source', {}).get('path') or '/missing')
        if not pdf.is_file() or pdf.suffix.casefold() != '.pdf':
            continue
        old_hash = sha256_file(path)
        row = {'title': article['title'], 'id': article['id'], 'markdown': str(path)}
        expected = index.get(article['id'], {}).get('sha256') or article.get('markdown_sha256')
        if not expected or old_hash != expected:
            rows.append({**row, 'status': 'preserved-manual-or-untracked-edits'}); continue
        checksum = sha256_file(pdf)
        if checksum != structure.get('source', {}).get('sha256') or checksum != article.get('source_sha256'):
            rows.append({**row, 'status': 'preserved-source-changed'}); continue
        original = path.read_text()
        # A saved hash alone is not proof of unedited generated content: an
        # earlier import may have indexed an author's edits. Reproduce the old
        # renderer byte-for-byte before allowing automatic replacement.
        if original != render_research_markdown(structure, original_spans=False):
            rows.append({**row, 'status': 'preserved-nonreproducible-text'}); continue
        before = pdf_quality(pdf, original)
        proposed = render_research_markdown(structure)
        after = pdf_quality(pdf, proposed)
        # Do not reformat usable sources for cosmetic differences.
        improved = after['source_token_coverage'] - before['source_token_coverage']
        needs_repair = before['source_token_coverage'] < .9 and improved >= .02
        row.update(before=before, after=after, status='repairable' if needs_repair else 'checked')
        if needs_repair and apply:
            with _library_lock(root):
                if sha256_file(path) != old_hash or sha256_file(pdf) != checksum:
                    row['status'] = 'preserved-concurrent-change'
                else:
                    target = write_research_markdown(root, article, proposed, provenance='source-coverage-repair', force=True)
                    quality = pdf_quality(pdf, target.read_text())
                    store.put_article({'id': article['id'], 'title': article['title'], 'markdown': str(target),
                                       'markdown_sha256': sha256_file(target), 'markdown_quality': quality}, markdown=target.read_text())
                    atomic_write_json(bundle / 'markdown-quality.json', quality)
                    extracted_path = bundle / 'extracted.json'
                    if extracted_path.is_file():
                        extracted = json.loads(extracted_path.read_text()); extracted['markdown_quality'] = quality
                        atomic_write_json(extracted_path, extracted)
                    with store.db() as db:
                        if db.execute("SELECT 1 FROM sqlite_master WHERE name='fulltext_retrievals'").fetchone():
                            for key, data in db.execute('SELECT hash,data FROM fulltext_retrievals').fetchall():
                                receipt = json.loads(data)
                                if receipt.get('markdown') == str(path):
                                    receipt['markdown_quality'] = quality
                                    db.execute('UPDATE fulltext_retrievals SET data=? WHERE hash=?', (json.dumps(receipt), key))
                    row.update(status='repaired', original_sha256=old_hash, repaired_sha256=sha256_text(proposed))
        rows.append(row)
        progress({'title': row['title'], 'status': row['status']})
    return {'applied': apply, 'counts': {s: sum(r['status'] == s for r in rows) for s in sorted({r['status'] for r in rows})}, 'articles': rows}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime', type=Path, default=runtime_root())
    parser.add_argument('--report', type=Path, required=True)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    result = repair(Store(args.runtime), apply=args.apply)
    atomic_write_json(args.report, result)
    print(json.dumps({'applied': result['applied'], 'counts': result['counts'], 'report': str(args.report)}, indent=2))

if __name__ == '__main__':
    main()
