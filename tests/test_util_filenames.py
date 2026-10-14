from zotero_audio.util import filename_part, readable_markdown_filename


def test_filenames_fit_linux_byte_limit_for_non_latin_titles():
    title = "STRATEGIES FOR AI IMPLEMENTATION IN LOCAL NEWSROOMS; " + "СТРАТЕГИИ ВНЕДРЕНИЯ ИИ В РЕДАКЦИЯХ МЕСТНЫХ ГАЗЕТ " * 4
    name = readable_markdown_filename(title, review=True)
    assert len(name.encode("utf-8")) <= 255
    assert name.endswith(".md") and name.startswith("STRATEGIES FOR AI")


def test_ascii_filenames_are_unchanged():
    title = "QuackIR - Retrieval in DuckDB and Other Relational Database Management Systems - Ge et al. (2025)"
    assert filename_part(title) == title
