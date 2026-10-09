from pathlib import Path

from zotero_audio.app_library import migrate_catalog_markdown
from zotero_audio.app_state import Store
from zotero_audio.research_library import canonical_path


def test_catalog_migration_preserves_markdown_and_records_metadata(tmp_path):
    store = Store(tmp_path / "runtime")
    bundle = store.runtime / "library" / "PAPER1"
    bundle.mkdir(parents=True)
    source = bundle / "article.md"
    source.write_text("# Research title\n\nManual edits stay here.\n", encoding="utf-8")
    store.put_article({"id": "PAPER1", "title": "Research title", "authors": ["Ada Author"], "year": 2024,
                       "metadata": {"doi": "10.1234/example"}, "bundle": str(bundle), "markdown": str(source)},
                      markdown=source.read_text())

    assert migrate_catalog_markdown(store) == 1
    article = store.article("PAPER1")
    saved = Path(article["markdown"])
    assert saved.parent == store.runtime / "papers"
    assert saved.read_text() == "# Research title\n\nManual edits stay here.\n"
    assert store.search("Manual edits")['results'][0]['id'] == "PAPER1"
    assert canonical_path(store.runtime / "papers", article) == saved
    index = (store.runtime / "papers" / "index.json").read_text()
    assert '"doi": "10.1234/example"' in index
    assert '"provenance": "catalog-migration"' in index


def test_filename_collision_adds_stable_key_suffix(tmp_path):
    root = tmp_path / "papers"
    first = {"id": "A1", "title": "Same title", "authors": ["Ada Author"], "year": 2024}
    second = {"id": "B2", "title": "Same title", "authors": ["Ada Author"], "year": 2024}
    path = canonical_path(root, first)
    path.write_text("first")
    from zotero_audio.research_library import register
    register(root, first, path)

    collided = canonical_path(root, second)
    assert collided != path
    assert collided.name.endswith("[B2].md")
