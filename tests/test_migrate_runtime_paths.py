import importlib.util
import json
import sqlite3
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "migrate_runtime_paths.py"
spec = importlib.util.spec_from_file_location("migrate_runtime_paths", SCRIPT)
migrate_runtime_paths = importlib.util.module_from_spec(spec)
spec.loader.exec_module(migrate_runtime_paths)

OLD = "/Users/someone/Sites/zotero-audio-runtime"


def _runtime(tmp_path: Path) -> Path:
    runtime = tmp_path / "runtime"
    (runtime / "control").mkdir(parents=True)
    (runtime / "podcast" / "public").mkdir(parents=True)
    (runtime / "library" / "K1").mkdir(parents=True)
    with sqlite3.connect(runtime / "control" / "library.sqlite3") as db:
        db.execute("CREATE TABLE articles (id TEXT PRIMARY KEY, data TEXT NOT NULL)")
        db.execute("CREATE VIRTUAL TABLE article_search USING fts5(id UNINDEXED, title, body)")
        db.execute("INSERT INTO articles VALUES ('K1', ?)", (json.dumps({"markdown": f"{OLD}/papers/A.md"}),))
        db.execute("INSERT INTO article_search VALUES ('K1', 'Governance', ?)", (f"see {OLD}/papers/A.md",))
    (runtime / "library" / "K1" / "generation.json").write_text(json.dumps({"bundle": f"{OLD}/library/K1"}))
    (runtime / "podcast" / "public" / "feed.json").write_text(json.dumps({"keep": OLD}))
    (runtime / "papers").mkdir()
    (runtime / "papers" / "legacy").symlink_to(f"{OLD}/library/K1")
    return runtime


def test_dry_run_reports_without_writing(tmp_path):
    runtime = _runtime(tmp_path)
    report = migrate_runtime_paths.migrate(runtime, OLD, "/home/me/rt", apply=False)
    assert report["sqlite_rows"] == {"articles": 1, "article_search": 1}
    assert report["files"] == 1 and report["symlinks"] == 1
    assert OLD in (runtime / "library" / "K1" / "generation.json").read_text()


def test_apply_rewrites_catalog_json_and_symlinks_but_not_published_files(tmp_path):
    runtime = _runtime(tmp_path)
    report = migrate_runtime_paths.migrate(runtime, OLD, "/home/me/rt", apply=True)
    assert report["remaining"] == {"sqlite": {}, "files": [], "symlinks": []}
    with sqlite3.connect(runtime / "control" / "library.sqlite3") as db:
        assert "/home/me/rt/papers/A.md" in db.execute("SELECT data FROM articles").fetchone()[0]
        assert db.execute("SELECT count(*) FROM article_search WHERE article_search MATCH 'governance'").fetchone()[0] == 1
    assert json.loads((runtime / "library" / "K1" / "generation.json").read_text())["bundle"] == "/home/me/rt/library/K1"
    assert (runtime / "papers" / "legacy").readlink() == Path("/home/me/rt/library/K1")
    assert OLD in (runtime / "podcast" / "public" / "feed.json").read_text()
    again = migrate_runtime_paths.migrate(runtime, OLD, "/home/me/rt", apply=True)
    assert again["sqlite_rows"] == {} and again["files"] == 0


def test_artifact_audit_detects_missing_and_corrupt_audio_without_rewriting(tmp_path):
    import hashlib
    runtime = _runtime(tmp_path)
    audio = runtime / 'finished.m4a'
    audio.write_bytes(b'valid finished audio')
    record = {'audio': str(audio), 'audio_sha256': hashlib.sha256(audio.read_bytes()).hexdigest()}
    article = {'id': 'K1', 'title': 'A complete synthetic title', 'editions': {'full': record}}
    with sqlite3.connect(runtime / 'control/library.sqlite3') as db:
        db.execute('UPDATE articles SET data=?', (json.dumps(article),))
    assert migrate_runtime_paths.audit_artifacts(runtime, verify_hashes=True)['invalid'] == []
    audio.write_bytes(b'corrupt')
    assert len(migrate_runtime_paths.audit_artifacts(runtime, verify_hashes=True)['invalid']) == 1
    audio.unlink()
    assert len(migrate_runtime_paths.audit_artifacts(runtime)['missing']) == 1
