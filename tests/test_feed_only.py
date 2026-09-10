import xml.etree.ElementTree as ET
from pathlib import Path

import zotero_audio.feed_only as feed_only
from zotero_audio.podcast import PodcastConfig
from zotero_audio.util import atomic_write_json


def test_publish_existing_audio_emits_required_feed_metadata_only(tmp_path: Path, monkeypatch):
    audio = tmp_path / "already-rendered.m4a"
    audio.write_bytes(b"existing audio")
    source_sha = "a" * 64
    private_record = {
        "edition": "brief",
        "guid": "episode-guid",
        "source_sha256": source_sha,
        "title": "Existing Paper",
        "authors": ["Ada Smith"],
        "duration": 12.5,
        "audio": str(audio),
        "read_url": "https://example.org/paper",
        "page_url": "https://example.org/paper",
        "source_license": {
            "allowed": True,
            "episode_license_url": "https://creativecommons.org/licenses/by/4.0/",
        },
        "image_url": "https://example.org/old-cover.png",
        "transcript_url": "https://example.org/old-transcript.vtt",
        "chapters_url": "https://example.org/old-chapters.json",
    }
    artifact = tmp_path / "private" / "Briefs" / "paper" / "episode.json"
    atomic_write_json(artifact, private_record)
    state = tmp_path / "state" / "documents" / "paper" / source_sha / "episode.json"
    atomic_write_json(state, {"state": "published", "source_sha256": source_sha, "zotero_key": "PAPER"})
    batch_manifest = tmp_path / "batch-manifest.json"
    atomic_write_json(batch_manifest, {"destination": str(tmp_path), "items": []})

    def fake_cover(path: Path, *, edition: str):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(f"{edition} cover".encode())

    monkeypatch.setattr(feed_only, "copy_podcast_cover", fake_cover)
    config = PodcastConfig(
        private_root=tmp_path / "private",
        state_root=tmp_path / "state",
        public_root=tmp_path / "public",
        base_url="https://audio.example",
        owner_email="podcast@example.org",
        publishing_enabled=True,
        dry_run=False,
    )

    result = feed_only.publish_existing_audio(config, batch_manifest_path=batch_manifest)

    assert result["added"] == 1
    feed = ET.parse(tmp_path / "public" / "brief" / "feed.xml")
    item = feed.find("./channel/item")
    assert item is not None
    assert item.find("enclosure") is not None
    assert item.find("{http://www.itunes.com/dtds/podcast-1.0.dtd}image") is None
    assert item.find("{https://podcastindex.org/namespace/1.0}transcript") is None
    assert item.find("{https://podcastindex.org/namespace/1.0}chapters") is None
    assert feed.find("./channel/{http://www.itunes.com/dtds/podcast-1.0.dtd}image") is not None


def test_user_authorized_policy_publishes_existing_brief_without_license_claim(tmp_path: Path, monkeypatch):
    audio = tmp_path / "brief.m4a"
    audio.write_bytes(b"existing brief audio")
    source_sha = "b" * 64
    artifact = tmp_path / "private" / "Briefs" / "paper" / "episode.json"
    atomic_write_json(artifact, {
        "edition": "brief",
        "guid": "brief-guid",
        "source_sha256": source_sha,
        "title": "Unresolved Rights Paper",
        "authors": ["Ada Smith"],
        "duration": 8.0,
        "audio": str(audio),
        "read_url": "https://example.org/paper",
        "source_license": {"allowed": False, "status": "denied", "reason": "not-allowlisted-or-unverified"},
    })
    batch_manifest = tmp_path / "batch-manifest.json"
    atomic_write_json(batch_manifest, {"destination": str(tmp_path), "items": []})

    def fake_cover(path: Path, *, edition: str):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(f"{edition} cover".encode())

    monkeypatch.setattr(feed_only, "copy_podcast_cover", fake_cover)
    config = PodcastConfig(
        private_root=tmp_path / "private",
        state_root=tmp_path / "state",
        public_root=tmp_path / "public",
        base_url="https://audio.example",
        owner_email="podcast@example.org",
        publishing_enabled=True,
        dry_run=False,
        briefs_publication_policy="user_authorized",
    )

    result = feed_only.publish_existing_audio(config, batch_manifest_path=batch_manifest)

    assert result["added"] == 1
    feed = ET.parse(tmp_path / "public" / "brief" / "feed.xml")
    item = feed.find("./channel/item")
    assert item is not None
    assert item.find("{https://podcastindex.org/namespace/1.0}license") is None
