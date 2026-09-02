import hashlib
import xml.etree.ElementTree as ET
from dataclasses import replace
from pathlib import Path

import pytest

import zotero_audio.cover as cover_module
import zotero_audio.podcast as podcast_module
from zotero_audio.podcast import (
    ITUNES_NS,
    PODCAST_NS,
    LocalPublisher,
    PodcastConfig,
    ShowConfig,
    build_intro,
    build_local_podcast,
    build_rss,
    build_transcript,
    chapter_json,
    content_quality_gate,
    create_edition_plan,
    episode_title,
    extract_brief,
    health_check,
    load_podcast_config,
    loudness_pass,
    publication_state,
    resolve_license,
)
from zotero_audio.util import atomic_write_json, json_digest, load_json


SOURCE_SHA = "a" * 64


def _structure():
    return {
        "document": {"title": "Listening to Evidence", "author": "Ada Smith; Nils Jones", "publication_year": "2026"},
        "source": {"sha256": SOURCE_SHA, "zotero_key": "ABCD1234"},
        "structure_sha256": "b" * 64,
        "blocks": [
            {"id": "a0", "type": "heading", "text": "Abstract", "pdf_page": 1, "included_in_reading": True},
            {"id": "a1", "type": "body", "text": "The authors found a careful result.", "pdf_page": 1, "included_in_reading": True},
            {"id": "m0", "type": "heading", "text": "Methods", "pdf_page": 2, "included_in_reading": True},
            {"id": "m1", "type": "body", "text": "The full paper explains the method.", "pdf_page": 2, "included_in_reading": True},
            {"id": "c0", "type": "heading", "text": "Conclusion", "pdf_page": 3, "included_in_reading": True},
            {"id": "c1", "type": "body", "text": "The authors conclude cautiously.", "pdf_page": 3, "included_in_reading": True},
        ],
    }


def _source_plan():
    structure = _structure()
    segments = []
    for index, (kind, text, block, section) in enumerate((
        ("title", "Listening to Evidence.", None, "Listening to Evidence"),
        ("heading", "Abstract.", "a0", "Abstract"),
        ("body", "The authors found a careful result.", "a1", "Abstract"),
        ("heading", "Methods.", "m0", "Methods"),
        ("body", "The full paper explains the method.", "m1", "Methods"),
        ("heading", "Conclusion.", "c0", "Conclusion"),
        ("body", "The authors conclude cautiously.", "c1", "Conclusion"),
    ), 1):
        segments.append({"ordinal": index, "kind": kind, "section": section, "text": text,
                         "text_sha256": hashlib.sha256(text.encode()).hexdigest(),
                         "source_block_ids": [block] if block else [], "pdf_pages": [1], "pause_after_ms": 100})
    plan = {"schema": "zotero-audio-speech-plan/v1", "document": structure["document"],
            "source_sha256": SOURCE_SHA, "structure_sha256": structure["structure_sha256"],
            "segmentation": {"algorithm": "test", "max_chars": 900}, "segments": segments}
    plan["plan_sha256"] = json_digest(plan)
    return plan


def _license(**updates):
    value = {"source_sha256": SOURCE_SHA, "license_url": "https://creativecommons.org/licenses/by/4.0/",
             "evidence_url": "https://publisher.example/paper", "read_url": "https://publisher.example/paper",
             "content_version": "vor"}
    value.update(updates)
    return value


def test_episode_title_and_introductions_are_edition_aware():
    assert episode_title("Paper", ["Jane Smith", "Ola Nord", "Li Wei"], 2026) == "Paper — Smith et al. (2026)"
    assert episode_title("Paper", ["Smith, Jane", "Nord, Ola"], 2026) == "Paper — Smith & Nord (2026)"
    brief = build_intro("brief", "Paper", ["Jane Smith"], journal="Nature", year=2026)
    full = build_intro("full", "Paper", ["Jane Smith"], journal="Nature", year=2026)
    assert "brief audio edition" in brief and "not the full paper" in brief
    assert "brief audio edition" not in full and "an audio edition" in full
    assert "Published in Nature in 2026" in brief


@pytest.mark.parametrize("record, expected", [
    (None, "unresolved"),
    (_license(source_sha256="b" * 64), "conflict"),
    (_license(license_url="https://creativecommons.org/licenses/by-nc/4.0/"), "denied"),
    (_license(evidence_url=None), "denied"),
    (_license(embargoed=True), "denied"),
    (_license(), "allowed"),
])
def test_license_gate_fails_closed(record, expected):
    assert resolve_license(record, source_sha256=SOURCE_SHA)["status"] == expected


def test_publication_requires_selection_and_exact_license():
    assert publication_state(selected=False, private_ready=True, license_record=_license(), source_sha256=SOURCE_SHA) == "not_selected"
    assert publication_state(selected=True, private_ready=True, license_record=None, source_sha256=SOURCE_SHA) == "private_only"
    assert publication_state(selected=True, private_ready=True, license_record=_license(), source_sha256=SOURCE_SHA) == "public_ready"


def test_brief_is_authored_extract_and_plans_are_distinct():
    structure, source = _structure(), _source_plan()
    brief = extract_brief(structure)
    assert brief["available"] and brief["brief_contents"] == "the authors' abstract and conclusion"
    brief_plan = create_edition_plan(source, structure, "brief")
    full_plan = create_edition_plan(source, structure, "full")
    brief_text = " ".join(item["text"] for item in brief_plan["segments"])
    full_text = " ".join(item["text"] for item in full_plan["segments"])
    assert "not the full paper" in brief_text
    assert "The full paper explains the method" not in brief_text
    assert "The full paper explains the method" in full_text
    assert brief_plan["plan_sha256"] != full_plan["plan_sha256"]


def test_missing_abstract_disables_only_brief():
    structure = _structure(); structure["blocks"] = structure["blocks"][2:]
    assert not extract_brief(structure)["available"]
    create_edition_plan(_source_plan(), structure, "full")
    with pytest.raises(RuntimeError, match="Brief unavailable"):
        create_edition_plan(_source_plan(), structure, "brief")


def test_content_gate_rejects_page_flat_and_unsafe_spoken_text():
    source = _source_plan()
    source["segments"] = [source["segments"][0], {**source["segments"][2], "text": "Email x@example.org and see [1]."}]
    structure = _structure(); structure["blocks"] = [structure["blocks"][1]]
    qa = content_quality_gate(structure, source)
    assert qa["status"] == "needs_review"
    assert "page-flat-or-missing-section-structure" in qa["errors"]
    assert "email-in-spoken-text" in qa["errors"]
    assert "numeric-citation-marker-in-spoken-text" in qa["errors"]


def test_transcript_and_chapter_validation():
    vtt = build_transcript([{"start": 0, "end": 1.25, "text": "A & B --> result"}])
    assert "00:00:00.000 --> 00:00:01.250" in vtt
    assert "A & B → result" in vtt
    with pytest.raises(ValueError, match="positive duration"):
        build_transcript([{"start": 1, "end": 1, "text": "bad"}])
    assert chapter_json([{"start": 0, "title": "Opening"}, {"start": 4.2, "title": "Abstract"}])["version"] == "1.2.0"


def _show():
    return {"title": "Open Paper Briefs", "description": "Brief papers", "link": "https://audio.example/",
            "image_url": "https://audio.example/show.png", "feed_url": "https://audio.example/brief/feed.xml",
            "owner_email": "podcast@example.org", "owner_name": "Owner", "author": "Open Paper Audio",
            "category": "Science", "copyright": "2026 Open Paper Audio", "guid": "show-guid"}


def _episode():
    return {"title": "Paper — Smith et al. (2026)", "guid": "episode-guid", "page_url": "https://audio.example/paper/",
            "audio_url": "https://audio.example/audio.m4a", "bytes": 1234, "pub_date": "2026-09-02T10:00:00Z",
            "duration": 301.2, "image_url": "https://audio.example/cover.png",
            "transcript_url": "https://audio.example/transcript.vtt",
            "transcript_html_url": "https://audio.example/transcript.html",
            "chapters_url": "https://audio.example/chapters.json",
            "episode_license_url": "https://creativecommons.org/licenses/by/4.0/", "show_notes": "Read the paper."}


def test_rss_has_required_metadata_and_namespaces():
    root = ET.fromstring(build_rss(_show(), [_episode()]))
    assert root.find("./channel/item/enclosure").attrib == {"url": "https://audio.example/audio.m4a", "length": "1234", "type": "audio/mp4"}
    assert root.find(f"./channel/{{{ITUNES_NS}}}owner/{{{ITUNES_NS}}}email").text == "podcast@example.org"
    assert root.find(f"./channel/item/{{{PODCAST_NS}}}transcript").attrib["type"] == "text/vtt"
    assert root.find(f"./channel/item/{{{PODCAST_NS}}}chapters") is not None
    assert root.find("./channel/item/pubDate").text.endswith("GMT")


def test_local_publisher_is_immutable_and_rejects_traversal(tmp_path: Path):
    source = tmp_path / "source.bin"; source.write_bytes(b"one")
    publisher = LocalPublisher(tmp_path / "public", "https://audio.example")
    url = publisher.put(source, "episodes/hash/source.bin", "application/octet-stream")
    assert publisher.head(url)["accept_ranges"]
    with pytest.raises(ValueError, match="unsafe"):
        publisher.put(source, "../escape", "application/octet-stream")
    source.write_bytes(b"two")
    with pytest.raises(RuntimeError, match="collision"):
        publisher.put(source, "episodes/hash/source.bin", "application/octet-stream")


def test_config_preserves_safe_defaults(tmp_path: Path):
    config_file = tmp_path / "podcast.toml"
    config_file.write_text(f'[paths]\nprivate_root="{tmp_path}/private"\nstate_root="{tmp_path}/state"\n[podcast]\n', encoding="utf-8")
    config = load_podcast_config(config_file)
    assert config.dry_run and not config.publishing_enabled and config.public_root is None


def _bundle(tmp_path: Path) -> Path:
    bundle = tmp_path / "bundle"; bundle.mkdir()
    plan = _source_plan(); atomic_write_json(bundle / "speech-plan.json", plan); atomic_write_json(bundle / "structure.json", _structure())
    atomic_write_json(bundle / "run-manifest.json", {"status": "complete", "plan_sha256": plan["plan_sha256"],
                      "synthesis_config_sha256": json_digest({"engine": "fake"}), "segments": []})
    return bundle


class Backend:
    config = {"engine": "fake"}

    def synthesize(self, text, destination):  # pragma: no cover - orchestration stubs synthesis below
        raise AssertionError("unexpected direct synthesis")


def _stub_media(monkeypatch):
    def synthesize(stage, backend):
        plan = load_json(stage / "speech-plan.json")
        records = [{"ordinal": item["ordinal"], "duration_seconds": 1.0, "text_sha256": item["text_sha256"]}
                   for item in plan["segments"]]
        manifest = {"status": "complete", "plan_sha256": plan["plan_sha256"],
                    "synthesis_config_sha256": json_digest(backend.config), "segments": records}
        atomic_write_json(stage / "run-manifest.json", manifest)
        return manifest, 0

    def assemble(stage, *, chapters=None):
        plan = load_json(stage / "speech-plan.json"); audio = stage / "audio" / f"{stage.name}.m4a"
        audio.parent.mkdir(parents=True, exist_ok=True); audio.write_bytes(plan["edition"].encode() * 20)
        duration = len(plan["segments"]) + sum(item["pause_after_ms"] for item in plan["segments"]) / 1000
        qa = {"status": "pass", "checks": {"m4a_duration_seconds": duration,
              "embedded_chapter_count": len(chapters or []),
              "loudness": {"integrated_lufs": -16.0, "true_peak_dbtp": -1.2, "clipped_samples": False}},
              "output": {"sha256": hashlib.sha256(audio.read_bytes()).hexdigest()}}
        atomic_write_json(stage / "qa-report.json", qa)
        return audio, qa

    def cover(title, *, destination, **kwargs):
        destination.parent.mkdir(parents=True, exist_ok=True); destination.write_bytes((kwargs.get("edition", "brief") + title).encode())
        return destination

    monkeypatch.setattr(podcast_module, "synthesize_plan", synthesize)
    monkeypatch.setattr(podcast_module, "assemble_m4a", assemble)
    monkeypatch.setattr(cover_module, "render_cover", cover)


def test_dry_run_writes_nothing(tmp_path: Path):
    result = build_local_podcast(_bundle(tmp_path), tmp_path / "private")
    assert result["dry_run"] and set(result["editions"]) == {"brief", "full"}
    assert not (tmp_path / "private").exists()


def test_end_to_end_private_public_and_idempotent_feed(tmp_path: Path, monkeypatch):
    _stub_media(monkeypatch); bundle = _bundle(tmp_path)
    config = PodcastConfig(private_root=tmp_path / "private", state_root=tmp_path / "state",
                           public_root=tmp_path / "public", base_url="https://audio.example",
                           publishing_enabled=True, dry_run=False, owner_email="podcast@example.org")
    first = build_local_podcast(bundle, backend=Backend(), config=config, selected=True, license_record=_license(),
                                metadata={"zotero_key": "ABCD1234", "authors": ["Ada Smith", "Nils Jones"],
                                          "title": "Listening to Evidence", "publication_year": "2026"})
    assert first["state"] == "published" and first["published"] and first["feed_changed"]
    assert Path(first["editions"]["brief"]["audio"]).read_bytes() != Path(first["editions"]["full"]["audio"]).read_bytes()
    assert "The full paper explains the method" not in Path(first["editions"]["brief"]["markdown"]).read_text()
    feed = tmp_path / "public" / "brief" / "feed.xml"; before = hashlib.sha256(feed.read_bytes()).hexdigest()
    second = build_local_podcast(bundle, backend=Backend(), config=config, selected=True, license_record=_license(),
                                 metadata={"zotero_key": "ABCD1234", "authors": ["Ada Smith", "Nils Jones"],
                                           "title": "Listening to Evidence", "publication_year": "2026"})
    assert not second["feed_changed"] and hashlib.sha256(feed.read_bytes()).hexdigest() == before
    assert len(load_json(tmp_path / "state" / "publication-manifest.json")["episodes"]) == 2
    assert health_check(config)["status"] == "pass"

    brief_before = (tmp_path / "public" / "brief" / "feed.xml").read_bytes()
    full_before = (tmp_path / "public" / "full" / "feed.xml").read_bytes()
    original_commit = LocalPublisher.commit_feed
    def fail_second_feed(self, staged, canonical_name, snapshot_dir):
        if canonical_name == "full/feed.xml":
            raise RuntimeError("simulated second-feed failure")
        return original_commit(self, staged, canonical_name, snapshot_dir)
    monkeypatch.setattr(LocalPublisher, "commit_feed", fail_second_feed)
    changed_show = ShowConfig("Changed Brief Title", config.brief_show.description, "brief", config.brief_show.guid)
    with pytest.raises(RuntimeError, match="second-feed"):
        build_local_podcast(bundle, backend=Backend(), config=replace(config, brief_show=changed_show),
                            selected=True, license_record=_license(),
                            metadata={"zotero_key": "ABCD1234", "authors": ["Ada Smith", "Nils Jones"],
                                      "title": "Listening to Evidence", "publication_year": "2026"})
    assert (tmp_path / "public" / "brief" / "feed.xml").read_bytes() == brief_before
    assert (tmp_path / "public" / "full" / "feed.xml").read_bytes() == full_before


@pytest.mark.parametrize("measurement, expected", [
    ({"integrated_lufs": -16, "true_peak_dbtp": -1.2, "clipped_samples": False}, True),
    ({"integrated_lufs": -18, "true_peak_dbtp": -1.2, "clipped_samples": False}, False),
    ({"integrated_lufs": -16, "true_peak_dbtp": -0.8, "clipped_samples": False}, False),
    ({"integrated_lufs": -16, "true_peak_dbtp": -1.2, "clipped_samples": True}, False),
])
def test_loudness_gate(measurement, expected):
    assert loudness_pass(measurement) is expected
