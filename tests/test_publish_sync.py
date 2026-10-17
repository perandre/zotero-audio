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
        assert command[:6] == ["/bin/npx", "--yes", "wrangler@4.145.0", "r2", "object", "put"]
        assert kwargs["check"] is True
        if command[6].endswith(".m4a"):
            assert command[command.index("--ct") + 1] == "audio/x-m4a"
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


@pytest.mark.parametrize('artwork_origin', ['https://assets.example/media', 'https://www.assets.example/media'])
def test_sync_show_artwork_accepts_original_or_configured_canonical_origin(tmp_path, monkeypatch, artwork_origin):
    root = tmp_path / 'public'
    (root / 'full').mkdir(parents=True)
    (root / 'full/feed.xml').write_text(
        '<rss xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd"><channel>'
        f'<itunes:image href="{artwork_origin}/shows/full-cover.jpg" />'
        '</channel></rss>'
    )
    calls = []
    monkeypatch.setattr('zotero_audio.publish_sync.upload_keys', lambda *args: calls.append(args[3]))
    config = SimpleNamespace(public_root=root, state_root=tmp_path, r2_bucket='bucket',
                             base_url='https://assets.example/media', feed_base_url='https://www.assets.example/media')
    sync_public(config, [{'edition': 'full', 'audio_url': 'https://assets.example/media/episodes/paper/audio.m4a'}])
    assert calls == [{'full/feed.xml', 'shows/full-cover.jpg', 'episodes/paper/audio.m4a', 'index.html'}]
    (root / 'full/feed.xml').write_text(
        '<rss xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd"><channel>'
        '<itunes:image href="https://unconfigured.example/shows/full-cover.jpg" />'
        '</channel></rss>'
    )
    with pytest.raises(ValueError, match='outside the configured'):
        sync_public(config, [{'edition': 'full'}])
    with pytest.raises(ValueError, match='outside the configured'):
        sync_public(config, [{'edition': 'full', 'audio_url': 'https://www.assets.example/media/episodes/paper/audio.m4a'}])
    assert len(calls) == 1


@pytest.fixture
def live_publication(tmp_path):
    import json
    root = tmp_path / 'public'
    (root / 'full').mkdir(parents=True)
    (root / 'full/feed.xml').write_text('local feed revision')
    config = SimpleNamespace(public_root=root, state_root=tmp_path, base_url='https://assets.example',
                             feed_base_url='https://www.assets.example')
    record = {'edition': 'full', 'guid': 'urn:uuid:synthetic', 'audio_url': 'https://assets.example/episodes/audio.m4a',
              'bytes': 1234, 'title': 'A complete synthetic article title'}
    (tmp_path / 'r2-sync-manifest.json').write_text(json.dumps({'files': {
        'full/feed.xml': 'feedhash', 'episodes/audio.m4a': 'audiohash', 'other.m4a': 'untouched'}}))
    return config, record


def test_verify_publication_requires_current_unique_xml_enclosure_and_available_audio(live_publication, monkeypatch):
    from io import BytesIO
    from zotero_audio.publish_sync import verify_publication
    config, record = live_publication
    calls = []
    def fetch(request, **kwargs):
        calls.append((request.full_url, request.get_method()))
        if request.get_method() == 'HEAD':
            result = BytesIO()
            result.status = 200
            result.headers = {'Content-Length': '1234'}
            return result
        return BytesIO(b'<rss><channel><item><guid>urn:uuid:synthetic</guid>'
                       b'<enclosure url="https://assets.example/episodes/audio.m4a" length="1234"/>'
                       b'</item></channel></rss>')
    monkeypatch.setattr('urllib.request.urlopen', fetch)
    verify_publication(config, record)
    assert calls[0][0].startswith('https://www.assets.example/full/feed.xml?publication=')
    assert calls[1] == (record['audio_url'], 'HEAD')
    assert len(load_json(config.state_root / 'r2-sync-manifest.json')['files']) == 3


@pytest.mark.parametrize('failure', ['absent', 'duplicate', 'revision', 'enclosure_size', 'media_size', 'offline'])
def test_verification_failure_retries_real_upload_without_invalidating_other_assets(live_publication, monkeypatch, failure):
    from io import BytesIO
    from urllib.error import URLError
    from zotero_audio.publish_sync import verify_publication
    config, record = live_publication
    item = '<item><guid>urn:uuid:synthetic</guid><enclosure url="https://assets.example/episodes/audio.m4a" length="1234"/></item>'
    if failure == 'absent': item = ''
    if failure == 'duplicate': item *= 2
    if failure == 'revision': item = item.replace('audio.m4a', 'old.m4a')
    if failure == 'enclosure_size': item = item.replace('1234', '123')
    def fetch(request, **kwargs):
        if failure == 'offline': raise URLError('synthetic offline failure')
        if request.get_method() == 'HEAD':
            result = BytesIO()
            result.status = 200
            result.headers = {'Content-Length': '1' if failure == 'media_size' else '1234'}
            return result
        return BytesIO(('<rss><channel>' + item + '</channel></rss>').encode())
    monkeypatch.setattr('urllib.request.urlopen', fetch)
    with pytest.raises(RuntimeError, match='Live publication verification failed for A complete synthetic article title'):
        verify_publication(config, record)
    assert load_json(config.state_root / 'r2-sync-manifest.json')['files'] == {'other.m4a': 'untouched'}
