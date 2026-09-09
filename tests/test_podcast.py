import hashlib
import xml.etree.ElementTree as ET
from dataclasses import replace
from pathlib import Path

import pytest

import zotero_audio.cover as cover_module
import zotero_audio.podcast as podcast_module
from zotero_audio.audio import episode_stinger_duration, episode_stinger_metadata
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
    EDITION_VOICES,
    extract_brief,
    health_check,
    load_podcast_config,
    loudness_pass,
    publication_state,
    resolve_license,
    sanitize_spoken_text,
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
    assert brief == "You’re listening to a brief of “Paper,” by Jane Smith. Published in Nature in 2026."
    assert full == "You’re listening to “Paper,” by Jane Smith. Published in Nature in 2026."
    assert "Published in Nature in 2026" in brief
    dated = build_intro("full", "Paper", ["Jane Smith"], journal="Nature", publication_date="2026-04-05")
    assert "Published in Nature on April 5, 2026" in dated
    partial = build_intro("full", "Paper", ["Jane Smith"], journal="Nature", publication_date="2026-02-00")
    assert "Published in Nature in February 2026" in partial
    assert "2026-02-00" not in partial


def test_show_notes_render_safe_links_and_omit_unrequested_disclosures():
    document = {
        "abstract": "An abstract.",
        "journal": "International Journal of Information Management",
        "publication_date": "2026-02",
        "doi": "10.1016/j.ijinfomgt.2025.102982",
    }
    license_result = {
        "allowed": True,
        "name": "Creative Commons Attribution 4.0 International",
        "read_url": "https://doi.org/10.1016/j.ijinfomgt.2025.102982",
        "license_url": "https://creativecommons.org/licenses/by/4.0/",
        "episode_license_url": "https://creativecommons.org/licenses/by/4.0/",
    }
    notes = podcast_module._show_notes(document, ["Laurie Hughes", "Fern Davies"], "brief", license_result)
    assert "Episode recording license" not in notes
    assert "synthetic voice" not in notes

    pair_url = "https://feed.mere.no/papers/example/full/"
    rendered = podcast_module._show_notes_html(notes + f"\nPaired edition: {pair_url}", edition="brief")
    assert '<a href="https://doi.org/10.1016/j.ijinfomgt.2025.102982">https://doi.org/10.1016/j.ijinfomgt.2025.102982</a>' in rendered
    assert '<a href="https://creativecommons.org/licenses/by/4.0/">https://creativecommons.org/licenses/by/4.0/</a>' in rendered
    assert f'<a href="{pair_url}">Full episode</a>' in rendered
    assert f">{pair_url}</a>" not in rendered

    page = podcast_module._episode_page({
        "title": "Paper",
        "edition": "brief",
        "image_url": "https://audio.example/cover.png",
        "audio_url": "https://audio.example/audio.m4a",
        "transcript_url": "https://audio.example/transcript.vtt",
        "show_notes": notes + f"\nPaired edition: {pair_url}",
    })
    assert '<footer class="episode-footer"' in page
    assert '<a href="https://audio.example/transcript.vtt">Timed transcript</a>' in page


def test_sanitize_spoken_text_omits_keyword_and_publisher_furniture():
    keyword_text, keyword_transformations = sanitize_spoken_text(
        "Trustworthy Artificial Intelligence (T-AI), Generative AI assistants, "
        "Knowledge graphs, Agentic AI, Industrial troubleshooting, Manufacturing",
        section="Abstract",
    )
    assert keyword_text == ""
    assert "omit-keyword-list" in keyword_transformations

    raw = (
        "This paper treats trustworthiness as a design concern. "
        "TRUST-AI: The Second European Workshop on Trustworthy AI. "
        "Organized as part of the International Joint Conference on Artificial Intelligence - "
        "IJCAI/ECAI 2026. August 2026, Bremen, Germany. "
        "/envel⌢pe-⌢ (R. Jadhav) /orcid0000-0001-8669-2420 "
        "©2026 Copyright for this paper by its authors. Use permitted under Creative Commons "
        "License Attribution 4.0 International (CC BY 4.0). "
        "The architecture continues with human validation."
    )
    cleaned, transformations = sanitize_spoken_text(raw)
    assert cleaned == "This paper treats trustworthiness as a design concern. The architecture continues with human validation."
    assert "TRUST-AI" not in cleaned
    assert "orcid" not in cleaned.casefold()
    assert "omit-publisher-front-matter" in transformations

    repaired, repair_transformations = sanitize_spoken_text(
        "Retrievalaugmented generation supports human-inthe-loop review of the domainspecific graph and crosssection links."
    )
    assert repaired == "Retrieval-augmented generation supports human-in-the-loop review of the domain-specific graph and cross-section links."
    assert "repair-fused-word" in repair_transformations


def test_cleanup_preserves_prose_without_complete_publisher_boundaries():
    for text in (
        "An envelope model describes the results. ©2026 Copyright reserved. The findings continue.",
        "©2026 Copyright reserved. The findings continue.",
        "TRUST-AI: The Second European Workshop on Trustworthy AI. We studied participants in Bremen, Germany.",
    ):
        assert sanitize_spoken_text(text)[0] == text


def test_full_edition_rebuilds_legacy_mid_sentence_boundaries():
    from zotero_audio.podcast import create_edition_plan
    structure = _structure()
    sentence = "The authors " + "preserve these exact original words " * 35 + "through the final sentence."
    structure["blocks"][1]["text"] = sentence
    source = _source_plan()
    source["segments"][2]["text"] = "The authors"
    plan = create_edition_plan(source, structure, "full")
    abstract = [s["text"] for s in plan["segments"] if s.get("source_block_ids") == ["a1"]]
    assert abstract == [sentence]
    assert any(s["text"] == "Methods." for s in plan["segments"])


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
    assert "You’re listening to a brief of" in brief_text
    assert "The full paper explains the method" not in brief_text
    assert "The full paper explains the method" in full_text
    assert brief_plan["plan_sha256"] != full_plan["plan_sha256"]


def test_brief_replaces_contaminated_abstract_metadata_with_bounded_blocks():
    structure = _structure()
    structure["document"]["abstract"] = (
        "The authors found a careful result. Keywords AI governance 1 Introduction "
        "contact@example.org https://example.org"
    )
    brief = extract_brief(structure)
    abstract_text = " ".join(block["text"] for block in brief["abstract"])
    assert abstract_text == "The authors found a careful result."
    assert "contact@example.org" not in abstract_text


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


def test_spoken_artifact_cleanup_preserves_source_and_clears_gate_errors():
    cleaned, transformations = sanitize_spoken_text(
        "See [1, 2] at https://example.org/paper or contact x@example.org."
    )
    assert cleaned == "See at the linked source or contact the email address."
    assert transformations == [
        "omit-numeric-citation-marker",
        "replace-email-address",
        "replace-web-link",
    ]
    source = _source_plan()
    source["segments"] = [
        source["segments"][0],
        {**source["segments"][2], "text": "See [1] at https://example.org."},
    ]
    structure = _structure()
    qa = content_quality_gate(structure, source, sanitize_spoken_artifacts=True)
    assert qa["status"] == "pass"
    assert qa["warnings"] == ["spoken-artifact-cleanup-applied"]


def test_brief_gate_scopes_quality_checks_to_brief_content():
    source = _source_plan()
    source["segments"] = [
        source["segments"][0],
        {**source["segments"][1], "text": "The full reading contains x@example.org and [1]."},
    ]
    structure = {
        "document": {
            "title": "Listening to Evidence",
            "author": "Ada Smith",
            "abstract": "The authors found a careful result.",
        },
        "blocks": [],
    }
    qa = content_quality_gate(structure, source, edition="brief")
    assert qa["status"] == "pass"
    assert qa["errors"] == []


def test_transcript_and_chapter_validation():
    vtt = build_transcript([{"start": 0, "end": 1.25, "text": "A & B --> result"}])
    assert "00:00:00.000 --> 00:00:01.250" in vtt
    assert "A & B → result" in vtt
    with pytest.raises(ValueError, match="positive duration"):
        build_transcript([{"start": 1, "end": 1, "text": "bad"}])
    assert chapter_json([{"start": 0, "title": "Opening"}, {"start": 4.2, "title": "Abstract"}])["version"] == "1.2.0"


def test_episode_timing_starts_after_opening_sound():
    plan = {"segments": [{"ordinal": 1, "section": "Introduction", "pause_after_ms": 0}]}
    manifest = {"segments": [{"ordinal": 1, "duration_seconds": 1.0, "chunks": [{"text": "Start", "duration_seconds": 1.0}]}]}
    cues, chapters, total = podcast_module._timing(plan, manifest)
    assert cues[0]["start"] == pytest.approx(episode_stinger_duration())
    assert chapters[0]["start"] == pytest.approx(episode_stinger_duration())
    assert total == pytest.approx(episode_stinger_duration() + 1.0)


def _show():
    return {"title": "1 More Paper", "description": "Brief papers", "link": "https://audio.example/",
            "image_url": "https://audio.example/show.png", "feed_url": "https://audio.example/brief/feed.xml",
            "owner_email": "podcast@example.org", "owner_name": "Owner", "author": "1 More Paper",
            "category": "Science", "copyright": "2026 1 More Paper", "guid": "show-guid"}


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


def test_homepage_has_cover_description_and_edition_links(tmp_path: Path):
    config = PodcastConfig(
        private_root=tmp_path / "private",
        state_root=tmp_path / "state",
        public_root=tmp_path / "public",
        site_description="A short description of the show.",
    )
    podcast_module._write_site(config, {"episodes": []}, cover_url="https://audio.example/show-cover.png")
    page = (tmp_path / "public" / "index.html").read_text(encoding="utf-8")
    assert "A short description of the show." in page
    assert 'alt="1 More Paper cover"' in page
    assert 'href="brief/feed.xml"' in page
    assert 'href="full/feed.xml"' in page
    assert 'class="edition-links"' in page


def test_config_preserves_safe_defaults(tmp_path: Path):
    config_file = tmp_path / "podcast.toml"
    config_file.write_text(f'[paths]\nprivate_root="{tmp_path}/private"\nstate_root="{tmp_path}/state"\n[podcast]\n', encoding="utf-8")
    config = load_podcast_config(config_file)
    assert config.dry_run and not config.publishing_enabled and config.public_root is None
    assert config.site_title == config.author == config.brief_show.title == config.full_show.title == "1 More Paper"


def _bundle(tmp_path: Path) -> Path:
    bundle = tmp_path / "bundle"; bundle.mkdir()
    plan = _source_plan(); atomic_write_json(bundle / "speech-plan.json", plan); atomic_write_json(bundle / "structure.json", _structure())
    atomic_write_json(bundle / "run-manifest.json", {"status": "complete", "plan_sha256": plan["plan_sha256"],
                      "synthesis_config_sha256": json_digest({"engine": "fake"}), "segments": []})
    return bundle


class Backend:
    def __init__(self):
        self.config = {"engine": "fake", "voice": "initial", "speed": 1.0, "language": "a"}
        self.configured_voices = []

    def configure(self, *, voice, speed, language):
        self.configured_voices.append(voice)
        self.config.update({"voice": voice, "speed": speed, "language": language})

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

    def assemble(stage, *, chapters=None, album="Zotero Audio"):
        plan = load_json(stage / "speech-plan.json"); audio = stage / "audio" / f"{stage.name}.m4a"
        audio.parent.mkdir(parents=True, exist_ok=True); audio.write_bytes(plan["edition"].encode() * 20)
        duration = episode_stinger_duration() + len(plan["segments"]) + sum(item["pause_after_ms"] for item in plan["segments"]) / 1000
        qa = {"status": "pass", "checks": {"m4a_duration_seconds": duration,
              "embedded_chapter_count": len(chapters or []),
              "episode_stinger": episode_stinger_metadata(),
              "loudness": {"integrated_lufs": -16.0, "true_peak_dbtp": -1.2, "clipped_samples": False}},
              "output": {"sha256": hashlib.sha256(audio.read_bytes()).hexdigest()}}
        atomic_write_json(stage / "qa-report.json", qa)
        return audio, qa

    def cover(destination, *, edition="full"):
        destination.parent.mkdir(parents=True, exist_ok=True); destination.write_bytes(b"1 More Paper cover")
        return destination

    monkeypatch.setattr(podcast_module, "synthesize_plan", synthesize)
    monkeypatch.setattr(podcast_module, "assemble_m4a", assemble)
    monkeypatch.setattr(cover_module, "copy_podcast_cover", cover)


def test_dry_run_writes_nothing(tmp_path: Path):
    result = build_local_podcast(_bundle(tmp_path), tmp_path / "private")
    assert result["dry_run"] and set(result["editions"]) == {"brief", "full"}
    assert not (tmp_path / "private").exists()


def test_podcast_build_uses_fixed_edition_voices(tmp_path: Path, monkeypatch):
    _stub_media(monkeypatch)
    backend = Backend()
    result = build_local_podcast(_bundle(tmp_path), tmp_path / "private", backend=backend,
                                 dry_run=False, edition="both")
    assert EDITION_VOICES == {"brief": "af_heart", "full": "am_michael"}
    assert set(backend.configured_voices) == {"af_heart", "am_michael"}
    assert result["editions"]["brief"]["voice"] == "af_heart"
    assert result["editions"]["full"]["voice"] == "am_michael"


@pytest.mark.parametrize("edition", ["brief", "full"])
def test_shared_build_accepts_prepared_unsynthesized_source(tmp_path: Path, monkeypatch, edition):
    _stub_media(monkeypatch)
    bundle = _bundle(tmp_path)
    (bundle / "run-manifest.json").unlink()
    result = build_local_podcast(bundle, tmp_path / "private", backend=Backend(),
                                 dry_run=False, edition=edition)
    assert set(result["editions"]) == {edition}
    assert Path(result["editions"][edition]["markdown"]).is_file()


def test_extraction_failure_blocks_publication(tmp_path: Path):
    structure = _structure()
    structure["extraction"] = {"errors": [{"error": "unassigned-layout-text"}]}
    assert podcast_module.content_quality_gate(structure, _source_plan())["status"] == "needs_review"


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
    homepage = (tmp_path / "public" / "index.html").read_text(encoding="utf-8")
    full_image_url = next(item["image_url"] for item in first["public_editions"] if item["edition"] == "full")
    assert full_image_url in homepage
    assert 'href="brief/feed.xml"' in homepage and 'href="full/feed.xml"' in homepage
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
