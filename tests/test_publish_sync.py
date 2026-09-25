from pathlib import Path
import subprocess
from types import SimpleNamespace
import pytest
from zotero_audio.publish_sync import sync_public, upload_keys
from zotero_audio.util import load_json


def test_sync_public_maps_subpath_urls_to_local_keys_and_uploads_feed_last(tmp_path, monkeypatch):
    root = tmp_path / "public"
    audio_key = "episodes/paper/full/audio.m4a"
    page_key = "papers/paper/full/index.html"
    cover_key = "shows/full-cover.png"
    for key in (audio_key, page_key, cover_key, "full/feed.xml", "index.html"):
        path = root / key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"asset")
    (root / "full/feed.xml").write_text(
        '<rss xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd"><channel>'
        '<itunes:image href="https://perandre.no/1mp/shows/full-cover.png" />'
        '</channel></rss>'
    )
    uploaded = []
    monkeypatch.setattr("shutil.which", lambda name: "/bin/npx")
    monkeypatch.setattr(subprocess, "run", lambda command, **kwargs: uploaded.append(command[6]))
    config = SimpleNamespace(public_root=root, state_root=tmp_path, r2_bucket="bucket", base_url="https://perandre.no/1mp")
    sync_public(config, [{"edition": "full", "paper_guid": "paper",
                          "audio_url": "https://perandre.no/1mp/episodes/paper/full/audio.m4a",
                          "page_url": "https://perandre.no/1mp/papers/readable-paper/full"}])
    assert {value.removeprefix("bucket/") for value in uploaded} == {
        audio_key, page_key, cover_key, "full/feed.xml", "index.html"}
    assert uploaded[-1] == "bucket/full/feed.xml"


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
