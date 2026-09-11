import math
import json
import struct
import wave
from pathlib import Path

import pytest

from zotero_audio import generation
from zotero_audio.audio import TARGET_SAMPLE_RATE, synthesize_plan
from zotero_audio.extract import render_research_markdown
from zotero_audio.podcast import PodcastConfig
from zotero_audio.util import atomic_write_json, episode_title, json_digest, readable_markdown_filename, sha256_file


@pytest.fixture
def bundle(tmp_path):
    structure = {
        "document": {"title": "How companies use AI", "authors": ["Anna Author"],
                     "publication_year": "2025", "rights": "CC BY 4.0", "url": "https://example.org/paper"},
        "source": {"filename": "paper.pdf", "path": str(tmp_path / "paper.pdf"), "sha256": "a" * 64,
                   "zotero_key": "ABCDEFGH", "pdf_pages": 2},
        "extraction": {"engine": "fixture", "engine_version": "1", "include_references": True, "errors": []},
        "pages": [],
        "blocks": [],
    }
    values = [
        ("heading", "Abstract", 1),
        ("paragraph", "Companies improved their results using AI [12]. Read the evidence at https://example.org/very/long/url.", 1),
        ("heading", "Introduction", 1),
        ("paragraph", "The authors found useful improvements (Smith et al., 2024).", 1),
        ("heading", "Conclusion", 2),
        ("paragraph", "The evidence supports careful adoption.", 2),
        ("heading", "References", 2),
        ("paragraph", "Smith, A. 2024. A referenced study. https://example.org/reference", 2),
    ]
    for index, (kind, text, page) in enumerate(values):
        structure["blocks"].append({"id": f"b{index}", "type": kind, "text": text, "pdf_page": page,
                                     "heading_level": 2, "included_in_reading": True})
    structure["structure_sha256"] = json_digest(structure)
    atomic_write_json(tmp_path / "structure.json", structure)
    (tmp_path / readable_markdown_filename(episode_title("How companies use AI", ["Anna Author"], "2025"))).write_text(render_research_markdown(structure))
    return tmp_path


class Backend:
    def __init__(self):
        self.config = {"engine": "fixture", "voice": "am_michael", "speed": 1.0, "language": "a", "model_revision": "1"}
        self.texts = []

    def configure(self, **kwargs):
        self.config.update(kwargs)

    def synthesize(self, text, destination):
        self.texts.append(text)
        with wave.open(str(destination), "wb") as stream:
            stream.setnchannels(1)
            stream.setsampwidth(2)
            stream.setframerate(TARGET_SAMPLE_RATE)
            stream.writeframes(b"".join(struct.pack("<h", round(3000 * math.sin(i / 8))) for i in range(2400)))


@pytest.fixture
def assemblies(monkeypatch):
    calls = []

    def assemble(stage, **options):
        calls.append({"stage": stage, **options})
        audio = stage / "audio" / f"{stage.name}.m4a"
        audio.parent.mkdir(exist_ok=True)
        audio.write_bytes(f"encoded fixture {len(calls)}".encode())
        qa = {"status": "pass", "optional_qa": options["optional_qa"],
              "checks": {"m4a_duration_seconds": 10.0, "loudness": {"integrated_lufs": -16, "true_peak_dbtp": -2}},
              "output": {"sha256": sha256_file(audio)}}
        atomic_write_json(stage / "qa-report.json", qa)
        return audio, qa

    monkeypatch.setattr(generation, "assemble_m4a", assemble)
    return calls


def test_markdown_is_first_class_and_preserves_manual_edits_and_references(bundle, monkeypatch):
    path = bundle / readable_markdown_filename(episode_title("How companies use AI", ["Anna Author"], "2025"))
    before = path.read_text().replace("useful improvements", "important measured improvements")
    path.write_text(before)
    monkeypatch.setattr(generation, "extract_pdf", lambda *a, **kw: pytest.fail("must preserve existing Markdown"))
    events = []
    result = generation.process_article(bundle, progress=events.append)
    assert path.read_text() == before
    assert "https://example.org/reference" in path.read_text()
    assert not result["editions"]
    assert events[-1]["stage"] == "markdown_ready"
    assert events[-1]["markdown"] == str(path)
    review = bundle / readable_markdown_filename(episode_title("How companies use AI", ["Anna Author"], "2025"), review=True)
    assert before in review.read_text()


def test_legacy_article_name_is_migrated_to_episode_title(bundle):
    readable = bundle / readable_markdown_filename(episode_title("How companies use AI", ["Anna Author"], "2025"))
    legacy = bundle / "article.md"
    readable.replace(legacy)
    result = generation.process_article(bundle)
    assert Path(result["markdown"]) == readable
    assert readable.is_file()
    assert not legacy.exists()


def test_each_edition_is_ready_independently_and_narration_excludes_references(bundle, assemblies):
    events, ready = [], []
    backend = Backend()

    def completed(record):
        ready.append(record)
        if record["edition"] == "brief":
            assert not (bundle / "editions" / "full" / "edition.json").exists()

    result = generation.process_article(bundle, mode="both", backend=backend, qa=True,
                                         progress=events.append, on_edition_ready=completed)
    assert [item["edition"] for item in ready] == ["brief", "full"]
    assert len(assemblies) == 2
    assert all(item["warn_only"] and "chapters" not in item for item in assemblies)
    narration = " ".join(backend.texts)
    assert "https://" not in narration
    assert "[12]" not in narration
    assert "Smith et al., 2024" not in narration
    assert "A referenced study" not in narration
    assert "You’re listening to" in narration
    assert events.index(next(item for item in events if item["stage"] == "markdown_ready")) < events.index(next(item for item in events if item["stage"] == "synthesizing"))
    assert result["editions"]["full"]["voice"] == "am_michael"
    assert result["editions"]["brief"]["voice"] == "af_heart"
    assert not list(bundle.rglob("*.vtt")) and not list(bundle.rglob("chapters.json"))


def test_retry_reuses_final_audio_and_changing_sound_only_reassembles(bundle, assemblies):
    backend = Backend()
    first = generation.process_article(bundle, mode="full", backend=backend)
    synthesized = len(backend.texts)
    second = generation.process_article(bundle, mode="full", backend=backend)
    assert second["editions"]["full"]["cached"]
    assert first["editions"]["full"]["audio_sha256"] == second["editions"]["full"]["audio_sha256"]
    assert len(assemblies) == 1 and len(backend.texts) == synthesized
    generation.process_article(bundle, mode="full", backend=backend, opening_sound=False, closing_sound=True)
    assert len(assemblies) == 2 and len(backend.texts) == synthesized
    assert assemblies[-1]["closing_sound"] is True


def _write_legacy_speed_cache(bundle, speed):
    """Reproduce persisted artifacts made before numeric-speed normalization."""
    stage = bundle / "editions" / "full"
    edition = json.loads((stage / "edition.json").read_text())
    manifest = json.loads((stage / "run-manifest.json").read_text())
    for record in (edition, manifest):
        record["synthesis_config"]["speed"] = speed
    digest = json_digest(manifest["synthesis_config"])
    manifest["synthesis_config_sha256"] = digest
    renamed = {}
    for segment in manifest["segments"]:
        old_path = stage / segment["path"]
        key = json_digest({"text_sha256": segment["text_sha256"], "synthesis_config_sha256": digest})
        new_path = stage / "audio" / "segments" / f"{key}.wav"
        if old_path != new_path and old_path not in renamed:
            old_path.rename(new_path)
            renamed[old_path] = new_path
        segment.update(cache_key=key, path=str(new_path.relative_to(stage)))
    edition["assembly_config"]["synthesis_config_sha256"] = digest
    edition["assembly_sha256"] = json_digest(edition["assembly_config"])
    atomic_write_json(stage / "run-manifest.json", manifest)
    atomic_write_json(stage / "edition.json", edition)


@pytest.mark.parametrize("legacy_speed,incoming_speed", [(1, 1.0), (1.0, 1)])
def test_json_speed_roundtrip_reuses_legacy_final_audio_and_segments(bundle, assemblies, legacy_speed, incoming_speed):
    backend = Backend()
    first = generation.process_article(bundle, mode="full", backend=backend, speed=legacy_speed)
    _write_legacy_speed_cache(bundle, legacy_speed)
    synthesized = len(backend.texts)
    original_bytes = Path(first["editions"]["full"]["audio"]).read_bytes()

    cached = generation.process_article(bundle, mode="full", backend=backend, speed=incoming_speed)
    assert cached["editions"]["full"]["cached"]
    assert len(assemblies) == 1 and len(backend.texts) == synthesized
    assert Path(cached["editions"]["full"]["audio"]).read_bytes() == original_bytes

    # An assembly-only change must reuse every legacy speech segment, even
    # after the final-edition metadata has already adopted canonical numbers.
    rebuilt = generation.process_article(bundle, mode="full", backend=backend, speed=incoming_speed,
                                         opening_sound=False)
    assert not rebuilt["editions"]["full"]["cached"]
    assert rebuilt["editions"]["full"]["reused_segments"] == synthesized
    assert len(backend.texts) == synthesized and len(assemblies) == 2
    again = generation.process_article(bundle, mode="full", backend=backend, speed=legacy_speed,
                                       opening_sound=False, closing_sound=True)
    assert again["editions"]["full"]["reused_segments"] == synthesized
    assert len(backend.texts) == synthesized and len(assemblies) == 3


@pytest.mark.parametrize("change", ["speed", "voice", "model", "tampered_audio", "tampered_provenance"])
def test_speed_compatibility_does_not_hide_real_cache_changes(bundle, assemblies, change):
    backend = Backend()
    initial = generation.process_article(bundle, mode="full", backend=backend)
    _write_legacy_speed_cache(bundle, 1)
    synthesized = len(backend.texts)
    options = {}
    if change == "speed":
        options["speed"] = 1.1
    elif change == "voice":
        options["full_voice"] = "af_heart"
    elif change == "model":
        backend.config["model_revision"] = "2"
    elif change == "tampered_audio":
        Path(initial["editions"]["full"]["audio"]).write_bytes(b"corrupt")
    else:
        path = bundle / "editions" / "full" / "edition.json"
        record = json.loads(path.read_text())
        record["assembly_config"]["policy"] = "unverified mutation"
        atomic_write_json(path, record)
    result = generation.process_article(bundle, mode="full", backend=backend, **options)
    assert not result["editions"]["full"]["cached"]
    assert len(assemblies) == 2
    if change in {"speed", "voice", "model"}:
        assert len(backend.texts) == synthesized * 2
    else:
        assert len(backend.texts) == synthesized


def test_markdown_edits_invalidate_audio_but_reuse_unchanged_segments(bundle, assemblies):
    backend = Backend()
    generation.process_article(bundle, mode="full", backend=backend)
    before = len(backend.texts)
    path = bundle / readable_markdown_filename(episode_title("How companies use AI", ["Anna Author"], "2025"))
    path.write_text(path.read_text().replace("careful adoption", "careful, measured adoption"))
    result = generation.process_article(bundle, mode="full", backend=backend)
    assert len(backend.texts) == before + 1
    assert result["editions"]["full"]["reused_segments"] > 0
    assert len(assemblies) == 2


def test_quality_findings_warn_and_disabled_checks_do_not_run(bundle, assemblies, monkeypatch):
    monkeypatch.setattr(generation, "content_quality_gate", lambda *a, **kw: {
        "errors": ["unresolved-pdf-extraction-errors"], "warnings": []})
    result = generation.process_article(bundle, mode="full", backend=Backend())
    assert result["status"] == "ready_with_warnings"
    assert result["editions"]["full"]["audio"]
    monkeypatch.setattr(generation, "content_quality_gate", lambda *a, **kw: pytest.fail("optional check ran"))
    result = generation.process_article(bundle, qa=False)
    assert result["status"] == "ready"


def test_private_audio_is_generated_but_never_published(bundle, assemblies, monkeypatch):
    result = generation.process_article(bundle, mode="full", backend=Backend(), metadata={"rights": "All rights reserved", "podcast_selected": True})
    record = result["editions"]["full"]
    assert Path(record["audio"]).is_file()
    assert not record["public_eligible"]
    monkeypatch.setattr(generation, "_publish", lambda *a, **kw: pytest.fail("private audio must not publish"))
    config = PodcastConfig(bundle, bundle, publishing_enabled=True, dry_run=False)
    assert generation.publish_finished_episode(record, config)["reason"] == "license-not-open"


def test_publication_revalidates_license_evidence_and_reuses_finished_bytes(bundle, assemblies, monkeypatch):
    result = generation.process_article(bundle, mode="full", backend=Backend(), metadata={"podcast_selected": True})
    record = result["editions"]["full"]
    config = PodcastConfig(bundle, bundle, publishing_enabled=True, dry_run=False)
    calls = []
    monkeypatch.setattr(generation, "_publish", lambda *args: (calls.append(args) or [record], True))
    assert generation.publish_finished_episode(record, config)["published"]
    assert len(assemblies) == 1
    assert calls[0][3]["full"]["audio_sha256"] == record["audio_sha256"]
    corrupted = {**record, "license_record": {**record["license_record"], "source_sha256": "b" * 64}}
    assert generation.publish_finished_episode(corrupted, config)["reason"] == "license-evidence-invalid"
    assert len(calls) == 1


def test_research_renderer_preserves_nonspoken_captions_and_footnotes():
    structure = {"document": {"title": "Research"}, "source": {"filename": "x.pdf", "sha256": "a", "pdf_pages": 1},
        "extraction": {"engine": "fixture", "engine_version": "1"},
        "blocks": [{"type": "paragraph", "pdf_page": 1, "text": "Figure 1. Important evidence.",
                    "included_in_reading": False, "omission_reason": "table-or-figure-caption"}],
        "pages": [{"pdf_page": 1, "omissions": [{"text": "An important footnote [2].", "reason": "footnote"}]}]}
    markdown = render_research_markdown(structure)
    assert "Important evidence" in markdown and "important footnote [2]" in markdown


def test_brief_failure_does_not_hold_back_full_and_cancellation_propagates(bundle, assemblies):
    class BriefFailureBackend(Backend):
        def synthesize(self, text, destination):
            if self.config["voice"] == "af_heart":
                raise RuntimeError("Provider failed for the brief voice")
            super().synthesize(text, destination)

    result = generation.process_article(bundle, mode="both", backend=BriefFailureBackend())
    assert result["editions"]["brief"]["status"] == "failed"
    assert result["editions"]["full"]["audio"]
    assert result["status"] == "ready_with_warnings"

    class Cancelled(Exception):
        pass

    def cancel(event):
        if event["stage"] == "synthesizing":
            raise Cancelled("User cancelled")

    with pytest.raises(Cancelled):
        generation.process_article(bundle, mode="both", backend=Backend(), progress=cancel)


def test_known_hyphen_artifacts_repaired_without_changing_real_compounds(bundle, assemblies):
    path = bundle / readable_markdown_filename(episode_title("How companies use AI", ["Anna Author"], "2025"))
    path.write_text(path.read_text().replace("careful adoption", "so-cial and long-term adop-tion"))
    backend = Backend()
    generation.process_article(bundle, mode="full", backend=backend)
    assert "social and long-term adoption" in " ".join(backend.texts)
    assert "so-cial and long-term adop-tion" in path.read_text()


def test_publication_creates_feed_with_show_art_only_and_no_optional_sidecars(bundle, assemblies):
    import xml.etree.ElementTree as ET
    result = generation.process_article(bundle, mode="full", backend=Backend(), metadata={"podcast_selected": True})
    config = PodcastConfig(bundle / "private", bundle / "state", public_root=bundle / "public",
                           base_url="https://podcast.example.org", publishing_enabled=True, dry_run=False)
    publication = generation.publish_finished_episode(result["editions"]["full"], config)
    assert publication["published"]
    feed = ET.fromstring((bundle / "public" / "full" / "feed.xml").read_text())
    item = feed.find("./channel/item")
    assert item.find("enclosure") is not None
    assert item.find("{http://www.itunes.com/dtds/podcast-1.0.dtd}image") is None
    assert item.find("{https://podcastindex.org/namespace/1.0}transcript") is None
    assert item.find("{https://podcastindex.org/namespace/1.0}chapters") is None
    assert len(assemblies) == 1
