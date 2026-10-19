from zotero_audio.util import filename_part, readable_markdown_filename


def test_filenames_fit_linux_byte_limit_for_non_latin_titles():
    title = "STRATEGIES FOR AI IMPLEMENTATION IN LOCAL NEWSROOMS; " + "СТРАТЕГИИ ВНЕДРЕНИЯ ИИ В РЕДАКЦИЯХ МЕСТНЫХ ГАЗЕТ " * 4
    name = readable_markdown_filename(title, review=True)
    assert len(name.encode("utf-8")) <= 255
    assert name.endswith(".md") and name.startswith("STRATEGIES FOR AI")


def test_ascii_filenames_are_unchanged():
    title = "QuackIR - Retrieval in DuckDB and Other Relational Database Management Systems - Ge et al. (2025)"
    assert filename_part(title) == title


def test_atomic_json_sidecar_with_multibyte_name_near_name_max(tmp_path):
    from zotero_audio.util import atomic_write_json, load_json
    path = tmp_path / ('ø' * 115 + '.m4a.headers.json')
    atomic_write_json(path, {'content_type': 'audio/x-m4a'})
    assert load_json(path) == {'content_type': 'audio/x-m4a'}
    assert list(tmp_path.iterdir()) == [path]


def test_review_suffix_survives_long_ascii_and_unicode_titles():
    for title in ("Long research title " * 30, "研究論文の題名" * 40):
        research = readable_markdown_filename(title)
        review = readable_markdown_filename(title, review=True)
        assert review != research
        assert review.endswith(" - AI review.md")
        assert len(review.encode("utf-8")) <= 244
