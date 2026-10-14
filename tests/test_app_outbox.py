import json
import plistlib
import threading
import time
from pathlib import Path

import pytest

from zotero_audio import app_outbox, app_pull, generation
from zotero_audio.app_state import Store
from zotero_audio.app_worker import LocalWorker, readable_filename
from zotero_audio.util import sha256_file
from test_app_worker import article_store, claim, deliveries  # noqa: F401  (fixtures/helpers)


def finished(directory: Path, name="source.m4a", content=b"finished audio"):
    path = directory / name
    path.write_bytes(content)
    return path


def test_private_audio_mode_selection(tmp_path, monkeypatch):
    runtime = tmp_path
    (runtime / "control").mkdir()
    monkeypatch.setattr(app_outbox.sys, "platform", "darwin")
    assert app_outbox.private_audio_mode(runtime, {"icloud_folder": "/x"}) == "icloud"
    assert app_outbox.private_audio_mode(runtime, {"icloud_folder": ""}) == "none"
    (runtime / "control/delivery.json").write_text(json.dumps({"private_audio": "outbox"}))
    assert app_outbox.private_audio_mode(runtime, {"icloud_folder": "/x"}) == "outbox"
    (runtime / "control/delivery.json").write_text("not json")
    assert app_outbox.private_audio_mode(runtime, {"icloud_folder": "/x"}) == "icloud"
    monkeypatch.setattr(app_outbox.sys, "platform", "linux")
    assert app_outbox.private_audio_mode(runtime, {"icloud_folder": "/Users/someone/iCloud"}) == "outbox"


def test_deliver_to_outbox_is_atomic_checksummed_and_idempotent(tmp_path):
    runtime = tmp_path / "runtime"
    source = finished(tmp_path)
    digest = sha256_file(source)
    payload = {"source": str(source), "filename": "2025 — Title — Author — Full.m4a", "sha256": digest}
    first = app_outbox.deliver_to_outbox(runtime, payload, article_id="A1", title="Title", edition="full", now="2026-01-01T00:00:00Z")
    again = app_outbox.deliver_to_outbox(runtime, payload, article_id="A1", title="Title", edition="full")
    assert first["id"] == again["id"]
    entries = app_outbox.list_entries(runtime)
    assert len(entries) == 1 and entries[0]["sha256"] == digest and entries[0]["title"] == "Title"
    stored = runtime / "outbox" / entries[0]["path"]
    assert stored.read_bytes() == b"finished audio"
    assert not list((runtime / "outbox").rglob("*.copying"))
    with pytest.raises(RuntimeError, match="checksum"):
        app_outbox.deliver_to_outbox(runtime, {**payload, "filename": "other.m4a", "sha256": "0" * 64})
    assert len(app_outbox.list_entries(runtime)) == 1


def test_index_survives_torn_line(tmp_path):
    runtime = tmp_path / "runtime"
    source = finished(tmp_path)
    app_outbox.deliver_to_outbox(runtime, {"source": str(source), "filename": "a.m4a"})
    with (runtime / "outbox/index.jsonl").open("a") as index:
        index.write('{"schema": "one-more-paper-outbox/v1", "id": "torn')  # crash mid-write
    second = finished(tmp_path, "s2.m4a", b"second")
    app_outbox.deliver_to_outbox(runtime, {"source": str(second), "filename": "b.m4a"})
    assert sorted(e["filename"] for e in app_outbox.list_entries(runtime)) == ["a.m4a", "b.m4a"]


def test_worker_delivers_private_audio_to_outbox_when_icloud_is_unavailable(article_store, monkeypatch):  # noqa: F811
    store, article = article_store
    store.update_settings({"icloud_folder": str(store.runtime / "icloud")})
    (store.root / "delivery.json").write_text(json.dumps({"private_audio": "outbox"}))

    def generate(bundle, **kwargs):
        path = bundle / "full.m4a"
        path.write_bytes(b"private finished audio")
        record = {"edition": "full", "title": article["title"], "audio": str(path), "audio_sha256": sha256_file(path),
                  "public_eligible": False, "selected": True, "status": "ready"}
        kwargs["on_edition_ready"](record)
        return {"source_sha256": "a" * 64, "warnings": [], "ai_review": str(bundle / "ai-review.md"),
                "editions": {"full": record}, "status": "ready"}

    monkeypatch.setattr(generation, "process_article", generate)
    job = claim(store, "full", qa=False)
    worker = LocalWorker(store)
    worker.process(job)
    # Generation is complete before any delivery runs; the laptop is never awaited.
    assert store.job(job["id"])["status"] == "completed"
    assert [d["kind"] for d in deliveries(store)] == ["outbox"]
    assert store.article(article["id"])["icloud_status"] == "pending"
    thread = threading.Thread(target=worker.deliver)
    thread.start()
    deadline = time.time() + 10
    while time.time() < deadline and deliveries(store)[0]["status"] != "completed":
        time.sleep(0.05)
    worker.stop.set()
    thread.join(5)
    assert deliveries(store)[0]["status"] == "completed"
    assert store.article(article["id"])["icloud_status"] == "outbox"
    assert not (store.runtime / "icloud").exists()
    [entry] = app_outbox.list_entries(store.runtime)
    assert entry["filename"] == readable_filename(store.article(article["id"]), "full")
    assert entry["article_id"] == article["id"] and entry["edition"] == "full" and entry["title"] == article["title"]
    assert (store.runtime / "outbox" / entry["path"]).read_bytes() == b"private finished audio"


@pytest.fixture
def server(tmp_path):
    runtime = tmp_path / "server"
    return runtime, app_pull.LocalTransport(runtime)


def add(runtime, tmp_path, name, content, title="A Paper"):
    source = finished(tmp_path, "src-" + name, content)
    return app_outbox.deliver_to_outbox(runtime, {"source": str(source), "filename": name}, title=title)


def test_pull_is_idempotent_and_catches_up_after_days(tmp_path, server):
    runtime, transport = server
    laptop = Store(tmp_path / "laptop")
    destination = tmp_path / "iCloud"
    add(runtime, tmp_path, "one.m4a", b"1")
    add(runtime, tmp_path, "two.m4a", b"2")
    result = app_pull.pull(laptop, transport, source_key="imac", destination=destination)
    assert [p["filename"] for p in result["pulled"]] == ["one.m4a", "two.m4a"] and not result["failed"]
    assert (destination / "one.m4a").read_bytes() == b"1"
    assert not list((laptop.root / "pull-staging").iterdir())
    # Re-run: nothing new, and a file the user deleted is not resurrected.
    (destination / "one.m4a").unlink()
    again = app_pull.pull(laptop, transport, source_key="imac", destination=destination)
    assert again["pulled"] == [] and again["skipped"] == 2 and not (destination / "one.m4a").exists()
    # The laptop was closed; more audio piled up; a fresh Store instance still remembers.
    add(runtime, tmp_path, "three.m4a", b"3")
    later = app_pull.pull(Store(tmp_path / "laptop"), transport, source_key="imac", destination=destination)
    assert [p["filename"] for p in later["pulled"]] == ["three.m4a"] and later["skipped"] == 2


def test_pull_never_overwrites_a_different_user_file(tmp_path, server):
    runtime, transport = server
    laptop = Store(tmp_path / "laptop")
    destination = tmp_path / "iCloud"
    destination.mkdir()
    (destination / "paper.m4a").write_bytes(b"the user's own recording")
    entry = add(runtime, tmp_path, "paper.m4a", b"generated")
    result = app_pull.pull(laptop, transport, source_key="imac", destination=destination)
    assert (destination / "paper.m4a").read_bytes() == b"the user's own recording"
    [pulled] = result["pulled"]
    assert pulled["filename"] == f"paper — {entry['sha256'][:8]}.m4a"
    assert (destination / pulled["filename"]).read_bytes() == b"generated"


def test_pull_reuses_identical_existing_file(tmp_path, server):
    runtime, transport = server
    laptop = Store(tmp_path / "laptop")
    destination = tmp_path / "iCloud"
    destination.mkdir()
    (destination / "same.m4a").write_bytes(b"identical")
    add(runtime, tmp_path, "same.m4a", b"identical")
    app_pull.pull(laptop, transport, source_key="imac", destination=destination)
    assert sorted(p.name for p in destination.iterdir()) == ["same.m4a"]


def test_pull_rejects_checksum_mismatch_and_retries_later(tmp_path, server):
    runtime, transport = server
    laptop = Store(tmp_path / "laptop")
    destination = tmp_path / "iCloud"
    entry = add(runtime, tmp_path, "bad.m4a", b"original")
    (runtime / "outbox" / entry["path"]).write_bytes(b"corrupted in transit")
    result = app_pull.pull(laptop, transport, source_key="imac", destination=destination)
    assert result["pulled"] == [] and "Checksum mismatch" in result["failed"][0]["error"]
    assert not destination.exists() or not list(destination.iterdir())
    (runtime / "outbox" / entry["path"]).write_bytes(b"original")  # repaired; next run succeeds
    fixed = app_pull.pull(laptop, transport, source_key="imac", destination=destination)
    assert [p["filename"] for p in fixed["pulled"]] == ["bad.m4a"]


def test_pull_rejects_unsafe_index_paths(tmp_path, server):
    runtime, transport = server
    laptop = Store(tmp_path / "laptop")
    (runtime / "outbox").mkdir(parents=True)
    (tmp_path / "secret").write_bytes(b"secret")
    entry = {"schema": app_outbox.SCHEMA, "id": "x", "sha256": sha256_file(tmp_path / "secret"), "filename": "x.m4a",
             "path": "../../secret"}
    (runtime / "outbox/index.jsonl").write_text(json.dumps(entry) + "\n")
    result = app_pull.pull(laptop, transport, source_key="imac", destination=tmp_path / "iCloud")
    assert result["pulled"] == [] and "unsafe" in result["failed"][0]["error"]


def test_dry_run_changes_nothing(tmp_path, server):
    runtime, transport = server
    laptop = Store(tmp_path / "laptop")
    add(runtime, tmp_path, "one.m4a", b"1", title="Full Title")
    result = app_pull.pull(laptop, transport, source_key="imac", destination=tmp_path / "iCloud", dry_run=True)
    assert result["would_pull"] == [{"title": "Full Title", "filename": "one.m4a"}]
    assert not (tmp_path / "iCloud").exists()


def test_ssh_transport_uses_batch_mode_and_quotes_paths(tmp_path):
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        if "stdout" in kwargs and hasattr(kwargs["stdout"], "write"):
            kwargs["stdout"].write(b"bytes")
        return type("R", (), {"returncode": 0, "stdout": "", "stderr": b""})()

    transport = app_pull.SshTransport("perandre@imac", "~/my runtime", run=fake_run)
    transport.read_index()
    transport.fetch("files/abc/It's a title.m4a", tmp_path / "out")
    for command in calls:
        assert command[0] == "ssh" and "BatchMode=yes" in command and command[command.index("--") + 1] == "perandre@imac"
    assert calls[0][-1].startswith('cat "$HOME"/\'my runtime/outbox/index.jsonl\'')
    assert "It'\"'\"'s a title.m4a" in calls[1][-1]
    assert (tmp_path / "out").read_bytes() == b"bytes"
    with pytest.raises(ValueError):
        app_pull.SshTransport("-oProxyCommand=evil")


def test_launch_agent_plist_runs_at_load_and_on_interval(tmp_path, monkeypatch):
    store = Store(tmp_path / "runtime")
    home = tmp_path / "home"
    monkeypatch.setattr(app_pull.Path, "home", lambda: home)
    monkeypatch.setattr(app_pull.sys, "platform", "darwin")
    calls = []

    def launchctl(*args):
        calls.append(args)
        return type("R", (), {"returncode": 0, "stderr": ""})()

    info = app_pull.install_agent(store, host="perandre@imac", runtime_path="~/rt", dest="/Users/x/iCloud", launchctl=launchctl)
    plist = plistlib.loads(Path(info["plist"]).read_bytes())
    assert plist["Label"] == app_pull.PULL_LABEL and plist["RunAtLoad"] is True and plist["StartInterval"] == 900
    assert plist["ProgramArguments"][1:] == ["-m", "zotero_audio.app_cli", "pull-audio", "--from", "perandre@imac",
                                             "--runtime-path", "~/rt", "--dest", "/Users/x/iCloud"]
    assert plist["EnvironmentVariables"]["ZOTERO_AUDIO_RUNTIME"] == str(store.runtime)
    assert plist["StandardOutPath"].endswith("pull-audio.log")
    assert [c[0] for c in calls] == ["bootout", "bootstrap"]
    removed = app_pull.uninstall_agent(launchctl=launchctl)
    assert removed["plist_removed"] and not Path(info["plist"]).exists() and calls[-1][0] == "bootout"


def test_cli_requires_source_and_destination(tmp_path, monkeypatch, capsys):
    from zotero_audio import app_cli
    monkeypatch.setenv("ZOTERO_AUDIO_RUNTIME", str(tmp_path / "laptop"))
    assert app_cli.main(["pull-audio"]) == 1
    assert app_cli.main(["pull-audio", "--from", "perandre@imac"]) == 1
    assert "iCloud folder" in capsys.readouterr().err
