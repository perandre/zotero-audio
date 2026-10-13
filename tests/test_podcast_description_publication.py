import xml.etree.ElementTree as ET

from zotero_audio import cover, podcast
from zotero_audio.util import atomic_write_json, load_json


def test_paired_publication_keeps_abstract_marker_and_preserves_public_edits(tmp_path, monkeypatch):
    def fake_cover(path, *, edition):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"synthetic cover")

    monkeypatch.setattr(cover, "copy_podcast_cover", fake_cover)
    audio = tmp_path / "synthetic.m4a"
    audio.write_bytes(b"synthetic finished audio")
    config = podcast.PodcastConfig(
        private_root=tmp_path / "private", state_root=tmp_path / "state",
        public_root=tmp_path / "public", base_url="https://podcast.example",
        owner_email="synthetic@example.org",
    )
    source_sha = "a" * 64
    records = {}
    for edition in ("brief", "full"):
        notes = f"An authors' synthetic abstract.\n\nThis is the {edition} recording."
        records[edition] = {
            "guid": f"synthetic-{edition}", "edition": edition,
            "title": "A Synthetic Paper — Example (2026)", "authors": ["A. Example"],
            "audio": str(audio), "duration": 42, "source_sha256": source_sha,
            "description_abstract": "An authors' synthetic abstract.",
            "description_abstract_source": "zotero",
            "show_notes": notes, "generated_show_notes": notes,
        }
    license_result = {"episode_license_url": "https://creativecommons.org/licenses/by/4.0/"}
    published, _ = podcast._publish(config, "synthetic-paper", source_sha, records, license_result)
    assert all(item["show_notes"] == item["generated_show_notes"] for item in published)
    for edition in records:
        root = ET.parse(config.public_root / edition / "feed.xml")
        assert 'class="episode-abstract"' in root.findtext("./channel/item/description")
        assert root.findtext("./channel/item/description").count("episode-abstract") == 1

    manifest_path = config.state_root / "publication-manifest.json"
    manifest = load_json(manifest_path)
    original = {item["guid"]: (item["pub_date"], item["audio_url"], item["bytes"]) for item in manifest["episodes"]}
    full = next(item for item in manifest["episodes"] if item["edition"] == "full")
    full["show_notes"] = "My manually edited episode notes.\nPaired edition: " + full["paired_url"]
    atomic_write_json(manifest_path, manifest)

    republished, _ = podcast._publish(config, "synthetic-paper", source_sha, records, license_result)
    full = next(item for item in republished if item["edition"] == "full")
    assert full["show_notes"].startswith("My manually edited episode notes.")
    assert full["show_notes"].count("Paired edition:") == 1
    assert full["description_is_manual"]
    page = (config.public_root / "papers" / "synthetic-paper" / "full" / "index.html").read_text()
    assert "My manually edited episode notes." in page
    assert "Authors’ abstract" not in page
    root = ET.parse(config.public_root / "full" / "feed.xml")
    assert "episode-abstract" not in root.findtext("./channel/item/description")
    assert all((item["pub_date"], item["audio_url"], item["bytes"]) == original[item["guid"]] for item in republished)
