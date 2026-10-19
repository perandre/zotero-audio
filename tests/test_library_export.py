import json
from pathlib import Path

from zotero_audio.app_state import Store, digest
from zotero_audio.library_export import export_library
from zotero_audio.util import sha256_file


def record(key="REF1", library="users/0", **fields):
    return {"key": key, "library_id": library, "data": {"title": "Complete research [title]", "rights": "All rights reserved", **fields}}


def test_private_reference_and_full_text_are_separate_and_preserved(tmp_path):
    store = Store(tmp_path)
    full = tmp_path / "papers" / "Edited research.md"
    full.parent.mkdir()
    full.write_text("# Authoritative research\nManual edits and references.")
    original = sha256_file(full)
    store.put_article({"id": "PDF1", "title": "Complete research [title]", "markdown": str(full),
                       "metadata": {"parent_key": "REF1"}})
    result = export_library(store, records=[record(abstractNote="An author's abstract.")])
    assert result["references"] == result["with_full_text"] == result["with_abstract"] == 1
    reference = next(p for p in (full.parent / "references").glob("*.md") if p.name != "index.md")
    content = reference.read_text()
    assert 'type: "Research Reference"' in content
    assert 'privacy: "private"' in content
    assert 'All rights reserved' in content
    assert "An author's abstract." in content
    assert "../Edited%20research.md" in content
    assert sha256_file(full) == original
    assert store.all_articles()[0].get("publication_status") is None
    assert store.jobs()["jobs"] == []
    before = reference.stat().st_mtime_ns
    export_library(store, records=[record(abstractNote="An author's abstract.")])
    assert reference.stat().st_mtime_ns == before


def test_missing_abstract_and_full_text_are_honest_and_groups_do_not_collide(tmp_path):
    store = Store(tmp_path)
    result = export_library(store, records=[record(), record(library="groups/123")])
    assert result["references"] == 2
    assert result["with_abstract"] == result["with_full_text"] == 0
    manifest = json.loads((Path(result["directory"]) / "index.json").read_text())
    assert set(manifest["references"]) == {"REF1", "g123:REF1"}
    for row in manifest["references"].values():
        content = (Path(result["directory"]) / row["path"]).read_text()
        assert "No sourced abstract is available" in content
        assert "Full-text Markdown is not available locally" in content


def test_rerun_preserves_reference_and_index_edits_and_removed_records(tmp_path):
    store = Store(tmp_path)
    result = export_library(store, records=[record()])
    directory = Path(result["directory"])
    manifest = json.loads((directory / "index.json").read_text())
    reference = directory / manifest["references"]["REF1"]["path"]
    reference.write_text("Hand-edited reference")
    (directory / "index.md").write_text("Hand-edited navigation")
    for _ in range(2):
        result = export_library(store, records=[record(title="Updated title")])
        assert result["preserved_edits"] == ["Updated title"]
        assert result["index_edited"]
        assert reference.read_text() == "Hand-edited reference"
        assert (directory / "index.md").read_text() == "Hand-edited navigation"
    export_library(store, records=[])
    assert reference.exists()
    assert "REF1" in json.loads((directory / "index.json").read_text())["references"]


def test_reuses_matching_crossref_cache_but_rejects_truncated_abstract(tmp_path):
    store = Store(tmp_path)
    with store.db() as db:
        db.execute("CREATE TABLE research_abstract_cache (hash TEXT PRIMARY KEY, abstract TEXT, checked_at REAL)")
        for key, title, abstract in [("A", "Complete abstract", "Complete cached abstract."), ("B", "Truncated abstract", "x" * 8001)]:
            fingerprint = digest({"doi": "10.1/test", "title": title, "policy": 1})
            db.execute("INSERT INTO research_abstract_cache VALUES (?,?,0)", (fingerprint, abstract))
    result = export_library(store, records=[record("A", title="Complete abstract", DOI="10.1/test"), record("B", title="Truncated abstract", DOI="10.1/test")])
    assert result["with_abstract"] == 1
    manifest = json.loads((Path(result["directory"]) / "index.json").read_text())
    assert manifest["references"]["A"]["abstract_source"] == "cached-crossref-metadata"
    assert not manifest["references"]["B"]["abstract_available"]


def test_pdf_abstract_requires_matching_source_and_group_identity(tmp_path):
    store = Store(tmp_path)
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "structure.json").write_text(json.dumps({"source": {"sha256": "current"}, "document": {"abstract": "Source abstract.", "abstract_source": "pdf-front-matter"}}))
    store.put_article({"id": "g123:PDF", "title": "Group research", "bundle": str(bundle), "source_sha256": "current", "metadata": {"parent_key": "REF1"}})
    result = export_library(store, records=[record(), record(library="groups/123")])
    manifest = json.loads((Path(result["directory"]) / "index.json").read_text())
    assert manifest["references"]["g123:REF1"]["abstract_available"]
    assert not manifest["references"]["REF1"]["abstract_available"]
    with store.edit_article("g123:PDF") as article:
        article["source_sha256"] = "replacement"
    result = export_library(store, records=[record(library="groups/123")])
    assert result["with_abstract"] == 0


def test_cli_exports_inventory_without_starting_daemon(tmp_path, monkeypatch, capsys):
    from zotero_audio import app_cli, library_export
    store = Store(tmp_path)
    monkeypatch.setattr(app_cli, "Store", lambda: store)
    monkeypatch.setattr(app_cli, "ensure_daemon", lambda _: (_ for _ in ()).throw(AssertionError("No daemon needed")))
    monkeypatch.setattr(library_export, "discover", lambda _: [record()])
    monkeypatch.setattr(library_export, "make_zotero_api", lambda *a, **k: object())
    assert app_cli.main(["library", "export", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["references"] == 1


def test_invalid_generated_abstract_is_archived_but_edits_are_preserved(tmp_path):
    from zotero_audio.app_research import abstract_fingerprint
    store = Store(tmp_path)
    r = record()
    # Simulate the previous exporter accepting an editorial provider label.
    with store.db() as db:
        db.execute('CREATE TABLE research_abstract_retrievals (hash TEXT PRIMARY KEY, data TEXT, checked_at REAL)')
        db.execute('INSERT INTO research_abstract_retrievals VALUES (?,?,0)', (abstract_fingerprint(r), json.dumps({'abstract': 'Peer Reviewed', 'source': 'openalex'})))
    directory = tmp_path/'papers/abstracts'; directory.mkdir(parents=True)
    path = directory/'old.md'; text = '# Research\n\n## Abstract\n\nPeer Reviewed\n\n## Source\nProvider'
    path.write_text(text)
    (directory/'index.json').write_text(json.dumps({'abstracts': {'REF1': {'path': 'old.md', 'sha256': sha256_file(path)}}}))
    refs = tmp_path/'papers/references'; refs.mkdir()
    (refs/'index.json').write_text(json.dumps({'references': {'REF1': {'path': 'old.md'}}}))
    result = export_library(store, records=[r])
    assert result['with_abstract'] == result['abstract_files'] == 0
    assert result['retired_invalid_abstracts'] == [r['data']['title']]
    assert not path.exists()
    assert next((directory/'revisions').glob('*.md')).read_text() == text
    # A file changed after generation must never be retired by this cleanup.
    path.write_text(text + '\nManual explanation.')
    (directory/'index.json').write_text(json.dumps({'abstracts': {'REF1': {'path': 'old.md', 'sha256': __import__('hashlib').sha256(text.encode()).hexdigest()}}}))
    edited = path.read_bytes()
    assert export_library(store, records=[r])['retired_invalid_abstracts'] == []
    assert path.read_bytes() == edited
