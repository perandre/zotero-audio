from pathlib import Path
import json

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


def test_pilot_bundle_is_added_to_flat_searchable_library(tmp_path):
    store = Store(tmp_path / "runtime")
    bundle = store.runtime / "industry-reports-pilot" / "bundles" / "A report [PILOT01]"
    bundle.mkdir(parents=True)
    (bundle / "metadata.json").write_text(json.dumps({"zotero_key": "PILOT01", "title": "A report",
        "authors": ["Rae Researcher"], "publication_year": 2025, "doi": "10.1234/pilot"}))
    (bundle / "structure.json").write_text(json.dumps({"document": {"title": "A report", "authors": ["Rae Researcher"],
        "publication_year": 2025}, "source": {"zotero_key": "PILOT01"}}))
    (bundle / "article.md").write_text("# A report\n\nPilot findings.\n")

    assert migrate_catalog_markdown(store) == 1
    article = store.article("PILOT01")
    assert Path(article["markdown"]).parent == store.runtime / "papers"
    assert store.search("Pilot findings")['results'][0]['id'] == "PILOT01"
    index = json.loads((store.runtime / "papers" / "index.json").read_text())
    assert index["articles"]["PILOT01"]["doi"] == "10.1234/pilot"
