import shutil
import wave
from pathlib import Path

import pytest

from zotero_audio.audio import (
    TARGET_SAMPLE_RATE,
    assemble_m4a,
    normalize_kokoro_text,
    split_kokoro_input,
    synthesize_plan,
    validate_wav,
)
from zotero_audio.util import atomic_write_json, json_digest, sha256_file


def _write_wav(path: Path, *, channels: int = 1, frames: int = 2400) -> None:
    with wave.open(str(path), "wb") as stream:
        stream.setnchannels(channels)
        stream.setsampwidth(2)
        stream.setframerate(TARGET_SAMPLE_RATE)
        stream.writeframes(b"\x00\x00" * frames * channels)


def test_validate_wav_reports_duration(tmp_path: Path):
    path = tmp_path / "sample.wav"
    _write_wav(path)
    assert validate_wav(path)["duration_seconds"] == 0.1


def test_validate_wav_rejects_non_mono(tmp_path: Path):
    path = tmp_path / "stereo.wav"
    _write_wav(path, channels=2)
    with pytest.raises(ValueError, match="mono"):
        validate_wav(path)


def test_kokoro_input_split_preserves_text_and_limit():
    text = "One short sentence. " + " ".join(["academic"] * 90) + " Final sentence."
    chunks = split_kokoro_input(text, max_chars=120)
    assert " ".join(chunks) == text
    assert all(len(chunk) <= 120 for chunk in chunks)


def test_kokoro_text_normalization_speaks_pdf_table_symbols():
    assert normalize_kokoro_text("╳ ● ✓") == "cross mark bullet check mark"


class FakeBackend:
    config = {"engine": "fake", "engine_version": "1", "voice": "test"}

    def synthesize(self, text: str, destination: Path) -> None:
        _write_wav(destination, frames=max(2400, len(text) * 120))


class CountingBackend(FakeBackend):
    def __init__(self) -> None:
        self.calls = 0

    def synthesize(self, text: str, destination: Path) -> None:
        self.calls += 1
        _write_wav(destination, frames=2400 + self.calls)


def test_duplicate_segments_share_one_fresh_render(tmp_path: Path):
    segment = {
        "kind": "body",
        "text": "Repeated table heading.",
        "text_sha256": "c" * 64,
        "source_block_ids": ["p0001-b0001"],
        "pdf_pages": [1],
        "pause_after_ms": 0,
    }
    plan = {
        "schema": "zotero-audio-speech-plan/v1",
        "document": {"title": "Test"},
        "source_sha256": "a" * 64,
        "structure_sha256": "b" * 64,
        "segmentation": {"algorithm": "test", "max_chars": 900},
        "segments": [{**segment, "ordinal": 1}, {**segment, "ordinal": 2}],
    }
    plan["plan_sha256"] = json_digest(plan)
    atomic_write_json(tmp_path / "speech-plan.json", plan)
    backend = CountingBackend()
    manifest, reused = synthesize_plan(tmp_path, backend)
    assert backend.calls == 1
    assert reused == 1
    assert manifest["segments"][0]["sha256"] == manifest["segments"][1]["sha256"]


@pytest.mark.skipif(shutil.which("/usr/bin/afconvert") is None, reason="requires macOS Core Audio")
def test_resume_and_deterministic_m4a_assembly(tmp_path: Path):
    plan = {
        "schema": "zotero-audio-speech-plan/v1",
        "document": {"title": "Test", "author": "Tester", "publication_year": "2026"},
        "source_sha256": "a" * 64,
        "structure_sha256": "b" * 64,
        "segmentation": {"algorithm": "test", "max_chars": 900},
        "segments": [
            {
                "ordinal": 1,
                "kind": "body",
                "text": "Test segment.",
                "text_sha256": "c" * 64,
                "source_block_ids": ["p0001-b0001"],
                "pdf_pages": [1],
                "pause_after_ms": 0,
            }
        ],
    }
    plan["plan_sha256"] = json_digest(plan)
    atomic_write_json(tmp_path / "speech-plan.json", plan)
    _, first_reused = synthesize_plan(tmp_path, FakeBackend())
    _, second_reused = synthesize_plan(tmp_path, FakeBackend())
    assert first_reused == 0
    assert second_reused == 1

    record = next((tmp_path / "audio" / "segments").glob("*.wav"))
    _write_wav(record, frames=4800)
    _, tampered_reused = synthesize_plan(tmp_path, FakeBackend())
    assert tampered_reused == 0

    output, qa = assemble_m4a(tmp_path)
    first_sha = sha256_file(output)
    output, qa_again = assemble_m4a(tmp_path)
    assert sha256_file(output) == first_sha
    assert qa["status"] == qa_again["status"] == "pass"
    assert qa["checks"]["m4a_channels"] == 1
    assert qa["checks"]["m4a_codec"] == "aac"
