import xml.etree.ElementTree as ET
import uuid
from pathlib import Path

import pytest

import zotero_audio.feed_only as feed_only
from zotero_audio.podcast import PodcastConfig
from zotero_audio.util import atomic_write_json


@pytest.mark.parametrize(("base_url", "feed_base_url", "link_base", "suffix"), [
    ("https://perandre.no/1mp", "", "https://perandre.no/1mp", ""),
    # Production: media stays on base_url; pages link the canonical www host with
    # the site's trailing slash so neither redirect runs.
    ("https://1morepaper.com", "https://www.1morepaper.com", "https://www.1morepaper.com", "/"),
])
def test_publish_existing_audio_emits_required_feed_metadata_only(tmp_path: Path, monkeypatch, base_url, feed_base_url,
                                                                  link_base, suffix):
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
        base_url=base_url,
        feed_base_url=feed_base_url,
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
    paper_id = uuid.uuid5(uuid.NAMESPACE_URL, "zotero-audio:episode-guid")
    assert item.findtext("link") == f"{link_base}/papers/existing-paper-{str(paper_id)[:8]}/brief{suffix}"
    assert item.find("enclosure").get("url").startswith(base_url + "/episodes/")
    assert item.findtext("guid") == "episode-guid"
    assert feed.findtext("./channel/link") == (feed_base_url or base_url) + "/"
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


def test_minimal_record_preserves_unknown_custom_show_notes(tmp_path: Path):
    audio = tmp_path / "custom.m4a"
    audio.write_bytes(b"audio")
    record = {
        "edition": "brief", "guid": "custom-guid", "source_sha256": "c" * 64,
        "title": "Paper", "authors": ["Ada Smith"], "audio": str(audio),
        "duration": 5, "read_url": "https://example.org/paper",
        "source_license": {"allowed": True, "episode_license_url": "https://example.org/license"},
        "show_notes": "A hand-edited introduction and attribution.",
    }
    candidate = feed_only._minimal_record(record, published_at="2026-10-14T00:00:00Z")
    assert candidate is not None
    assert candidate["show_notes"] == record["show_notes"]
    assert candidate["description_is_manual"] is True


def test_existing_full_reading_publishes_source_matched_narration(tmp_path: Path, monkeypatch):
    source_sha = "c" * 64
    paper_guid = str(uuid.uuid5(uuid.NAMESPACE_URL, "zotero-audio:PAPER"))
    state_root = tmp_path / "runtime" / "podcast"
    library = tmp_path / "runtime" / "library" / "PAPER"
    narration = library / "editions" / "full" / "narration.md"
    narration.parent.mkdir(parents=True)
    narration.write_text("Spoken introduction.\n\nThe paper text.\n", encoding="utf-8")
    atomic_write_json(library / "generation.json", {"source_sha256": source_sha})
    atomic_write_json(state_root / "publication-manifest.json", {"episodes": [{
        "edition": "full", "guid": "urn:uuid:full-paper", "paper_guid": paper_guid,
        "zotero_key": "PAPER", "source_sha256": source_sha, "revision": source_sha,
        "title": "Full Paper", "paper_title": "Full Paper", "pub_date": "2026-09-25T00:00:00Z",
        "page_url": "https://perandre.no/1mp/papers/full-paper-" + paper_guid[:8] + "/full",
        "audio_url": "https://perandre.no/1mp/episodes/audio.m4a", "bytes": 100,
        "duration": 60, "source_license": {"allowed": True},
    }]})
    batch_manifest = tmp_path / "batch-manifest.json"
    atomic_write_json(batch_manifest, {"destination": str(tmp_path), "items": []})

    def fake_cover(path: Path, *, edition: str):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"show cover")

    monkeypatch.setattr(feed_only, "copy_podcast_cover", fake_cover)
    config = PodcastConfig(private_root=tmp_path / "private", state_root=state_root,
                           public_root=tmp_path / "public", base_url="https://perandre.no/1mp",
                           owner_email="podcast@example.org", publishing_enabled=True, dry_run=False)
    result = feed_only.publish_existing_audio(config, batch_manifest_path=batch_manifest)
    assert result["episodes"] == 1
    item = ET.parse(config.public_root / "full" / "feed.xml").find("./channel/item")
    transcript = item.find("{https://podcastindex.org/namespace/1.0}transcript")
    assert transcript is not None
    assert transcript.get("type") == "text/plain"
    key = transcript.get("url").removeprefix("https://perandre.no/1mp/")
    assert (config.public_root / key).read_text(encoding="utf-8") == narration.read_text(encoding="utf-8")
