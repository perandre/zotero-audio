"""Independent source coverage checks for research Markdown; never infer missing text."""
from __future__ import annotations

import re
import unicodedata
from collections import Counter
from pathlib import Path

from .util import sha256_file, sha256_text


def _tokens(text):
    text = unicodedata.normalize('NFKC', text).replace('\u00ad', '')
    return Counter(w for w in re.findall(r'[^\W\d_]+', text.casefold()) if len(w) > 2)


def pdf_quality(pdf: Path, markdown: str) -> dict:
    import pymupdf
    with pymupdf.open(pdf) as document:
        pages = [page.get_text() for page in document]
    body = re.sub(r'\A---\n.*?\n---\n', '', markdown, flags=re.S)
    source, rendered = _tokens('\n'.join(pages)), _tokens(body)
    coverage = sum((source & rendered).values()) / max(1, sum(source.values()))
    findings = []
    if not source:
        findings.append('source-needs-ocr')
    if coverage < .9:
        findings.append('source-text-coverage-low')
    sparse = [i + 1 for i, text in enumerate(pages) if sum(_tokens(text).values()) < 20]
    if len(sparse) > max(1, len(pages) * .15):
        findings.append('sparse-source-pages-review-figures-or-ocr')
    return {'policy': 'pdf-source-coverage/v1', 'status': 'warning' if findings else 'pass',
            'findings': findings, 'source_token_coverage': round(coverage, 4),
            'pdf_pages': len(pages), 'sparse_source_pages': sparse,
            'source_sha256': sha256_file(pdf), 'markdown_sha256': sha256_text(markdown),
            'limits': 'Token coverage does not certify reading order, tables, equations or visual fidelity.'}
