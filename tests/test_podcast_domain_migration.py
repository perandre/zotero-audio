from scripts.migrate_podcast_domain import migrate_record, paper_slug
from zotero_audio.podcast import public_episode_page_url


def test_migration_keeps_episode_identity_and_media_key():
    old = "https://feed.mere.no"
    new = "https://perandre.no/1mp"
    original = {
        "title": "A useful paper — Author (2026)",
        "guid": "urn:uuid:episode-1", "paper_guid": "fc90460f-d7e7-57c8-a135-81312cb5706b",
        "edition": "brief", "pub_date": "2026-09-01T12:00:00Z", "bytes": 1234,
        "audio_url": f"{old}/episodes/immutable/audio.m4a",
        "page_url": f"{old}/papers/fc90460f-d7e7-57c8-a135-81312cb5706b/brief/index.html",
        "paired_url": f"{old}/papers/fc90460f-d7e7-57c8-a135-81312cb5706b/full/index.html",
        "show_notes": f"Read the source: https://doi.org/10.1/example\nPaired edition: {old}/papers/fc90460f-d7e7-57c8-a135-81312cb5706b/full/index.html",
    }
    migrated = migrate_record(original, old, new)
    assert migrated["guid"] == original["guid"]
    assert migrated["pub_date"] == original["pub_date"]
    assert migrated["bytes"] == original["bytes"]
    assert migrated["audio_url"] == f"{new}/episodes/immutable/audio.m4a"
    assert migrated["page_url"] == f"{new}/papers/a-useful-paper-fc90460f/brief/"
    assert "https://doi.org/10.1/example" in migrated["show_notes"]
    assert migrated["paired_url"] == f"{new}/papers/a-useful-paper-fc90460f/full/"
    assert migrated["paired_url"] in migrated["show_notes"]
    assert original["audio_url"] == f"{old}/episodes/immutable/audio.m4a"


def test_paper_slug_is_stable_for_paired_editions():
    paper_id = "fc90460f-d7e7-57c8-a135-81312cb5706b"
    assert paper_slug("A useful paper — Author (2026)", paper_id) == "a-useful-paper-fc90460f"


def test_future_publications_use_site_notes_without_changing_legacy_pages():
    paper_id = "fc90460f-d7e7-57c8-a135-81312cb5706b"
    assert public_episode_page_url("https://perandre.no/1mp", "A useful paper — Author (2026)", paper_id, "full") == (
        "https://perandre.no/1mp/papers/a-useful-paper-fc90460f/full/"
    )
    assert public_episode_page_url("https://feed.mere.no", "A useful paper — Author (2026)", paper_id, "full") == (
        f"https://feed.mere.no/papers/{paper_id}/full/index.html"
    )
