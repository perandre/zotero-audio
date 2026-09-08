from pathlib import Path
import subprocess
import pytest
from zotero_audio.publish_sync import upload_keys
from zotero_audio.util import load_json


def test_feeds_are_last_and_failed_upload_never_updates_feed(tmp_path, monkeypatch):
    root = tmp_path / "public"
    (root / "full").mkdir(parents=True)
    (root / "full/feed.xml").write_text("feed")
    (root / "audio.m4a").write_bytes(b"audio")
    calls = []
    monkeypatch.setattr("shutil.which", lambda name: "/bin/npx")
    def run(command, **kwargs):
        calls.append(command[6])
    monkeypatch.setattr(subprocess, "run", run)
    state = tmp_path / "state.json"
    upload_keys(root,"bucket",state,["full/feed.xml","audio.m4a"])
    assert calls == ["bucket/audio.m4a","bucket/full/feed.xml"]
    upload_keys(root,"bucket",state,["full/feed.xml","audio.m4a"])
    assert len(calls) == 2
    (root / "audio.m4a").write_bytes(b"new audio")
    (root / "full/feed.xml").write_text("new feed")
    def fail(command, **kwargs):
        raise subprocess.CalledProcessError(1,command)
    monkeypatch.setattr(subprocess,"run",fail)
    previous = load_json(state)
    with pytest.raises(subprocess.CalledProcessError):
        upload_keys(root,"bucket",state,["full/feed.xml","audio.m4a"])
    assert load_json(state) == previous
