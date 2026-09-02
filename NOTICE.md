# Provenance notice

This standalone project was created on 2026-09-01 after a read-only review of
the user's existing parser at:

`/Users/pesh/Documents/phd/analysis/scripts/zotero_pdf_to_tts.py`

and its tests at:

`/Users/pesh/Documents/phd/analysis/tests/test_zotero_pdf_to_tts.py`

The reviewed work supplied prior-art ideas for deterministic text cleanup,
Zotero attachment resolution, page markers, source SHA-256 recording, and
explicit omission metadata. This repository reimplements those product concepts
inside a staged, resumable standalone pipeline. No source PDF, generated article
Markdown, model weight, or generated audio is copied into this repository.
