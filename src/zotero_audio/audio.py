from __future__ import annotations

import os
import json
import math
import re
import shutil
import subprocess
import tempfile
import unicodedata
import wave
from importlib import metadata
from pathlib import Path
from typing import Any, Protocol

from mutagen.mp4 import MP4

from . import __version__
from .util import atomic_write_json, json_digest, load_json, sha256_file


TARGET_SAMPLE_RATE = 24_000
TARGET_CHANNELS = 1
TARGET_SAMPLE_WIDTH = 2
KOKORO_INPUT_CHUNK_CHARS = 160
KOKORO_SPOKEN_SYMBOLS = {
    "╳": " cross mark ",
    "×": " times ",
    "●": " bullet ",
    "•": " bullet ",
    "✓": " check mark ",
    "✔": " check mark ",
}
# Podcast delivery gate: integrated loudness is measured after AAC encoding.
TARGET_LOUDNESS_LUFS = -16.0
LOUDNESS_MIN_LUFS = -17.0
LOUDNESS_MAX_LUFS = -15.0
TRUE_PEAK_MAX_DBTP = -1.0


def loudness_is_competitive(integrated_lufs: float, true_peak_dbtp: float, clipped_samples: bool = False) -> bool:
    """Return whether a spoken episode meets the documented delivery gate."""
    return LOUDNESS_MIN_LUFS <= integrated_lufs <= LOUDNESS_MAX_LUFS and true_peak_dbtp <= TRUE_PEAK_MAX_DBTP and not clipped_samples


def measure_loudness(path: Path) -> dict[str, Any]:
    """Measure integrated loudness and true peak with FFmpeg's BS.1770 filter."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg: raise RuntimeError("FFmpeg is required for podcast loudness measurement")
    result = subprocess.run(
        [ffmpeg, "-hide_banner", "-i", str(path), "-af", "loudnorm=I=-16:TP=-1:LRA=11:print_format=json", "-f", "null", "-"],
        capture_output=True,
        text=True,
        check=False,
    )
    match = re.search(r"\{\s*\"input_i\".*?\}", result.stderr, re.S)
    if result.returncode or not match:
        detail = (result.stderr or result.stdout).strip()
        raise RuntimeError(f"Could not measure loudness for {path}: {detail[-1200:]}")
    raw = json.loads(match.group(0))
    def number(key: str) -> float:
        return float(str(raw[key]))
    integrated, peak = number("input_i"), number("input_tp")
    return {
        "integrated_lufs": integrated,
        "true_peak_dbtp": peak,
        "loudness_range": number("input_lra"),
        "clipped_samples": False,
        "silent": not math.isfinite(integrated) or not math.isfinite(peak),
        "raw": raw,
    }


def normalize_wav_loudness(source: Path, destination: Path) -> dict[str, Any]:
    """Two-pass loudnorm to a WAV; returns measured post-normalization values."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg: raise RuntimeError("FFmpeg is required for two-pass loudness normalization")
    first = measure_loudness(source)["raw"]
    if any(str(first.get(key, "")).lower() in {"-inf", "inf", "nan"} for key in ("input_i", "input_tp")):
        raise RuntimeError("Cannot loudness-normalize silent or non-finite audio")
    # Use a lower working ceiling so AAC inter-sample overs remain below -1 dBTP.
    filter_value = (f"loudnorm=I=-16:TP=-1.5:LRA=11:measured_I={first['input_i']}:measured_TP={first['input_tp']}:"
                    f"measured_LRA={first['input_lra']}:measured_thresh={first['input_thresh']}:offset={first['target_offset']}:linear=true:print_format=summary")
    destination.parent.mkdir(parents=True, exist_ok=True)
    _run([ffmpeg, "-y", "-i", str(source), "-af", filter_value, "-ar", str(TARGET_SAMPLE_RATE), "-ac", "1", "-c:a", "pcm_s16le", str(destination)])
    measured = measure_loudness(destination)
    measured["normalization_gain_db"] = float(first["target_offset"])
    measured["input"] = first
    return measured


class SpeechBackend(Protocol):
    config: dict[str, Any]

    def synthesize(self, text: str, destination: Path) -> dict[str, Any] | None: ...


def split_kokoro_input(text: str, max_chars: int = KOKORO_INPUT_CHUNK_CHARS) -> list[str]:
    """Split before phonemization so no MLX Kokoro chunk can be silently truncated."""

    if max_chars < 80:
        raise ValueError("Kokoro input chunks must allow at least 80 characters")
    sentences = re.split(r"(?<=[.!?])\s+", text.strip())
    chunks: list[str] = []
    current = ""
    for sentence in sentences:
        words = sentence.split()
        for word in words:
            candidate = f"{current} {word}".strip()
            if current and len(candidate) > max_chars:
                chunks.append(current)
                current = word
            elif len(word) > max_chars:
                if current:
                    chunks.append(current)
                    current = ""
                chunks.extend(word[index : index + max_chars] for index in range(0, len(word), max_chars))
            else:
                current = candidate
    if current:
        chunks.append(current)
    return chunks


def normalize_kokoro_text(text: str) -> str:
    """Turn common PDF/table glyphs into words Kokoro can pronounce."""

    normalized = unicodedata.normalize("NFKC", text)
    for symbol, spoken in KOKORO_SPOKEN_SYMBOLS.items():
        normalized = normalized.replace(symbol, spoken)
    return re.sub(r"\s+", " ", normalized).strip()


def _require_executable(path_or_name: str) -> str:
    found = shutil.which(path_or_name)
    if not found:
        raise RuntimeError(f"Required executable not found: {path_or_name}")
    return found


def _run(command: list[str]) -> None:
    result = subprocess.run(command, check=False, capture_output=True, text=True)
    if result.returncode:
        detail = (result.stderr or result.stdout).strip()
        raise RuntimeError(f"Command failed ({command[0]}): {detail[-2000:]}")


def inspect_m4a(path: Path) -> dict[str, Any]:
    afinfo = _require_executable("/usr/bin/afinfo")
    result = subprocess.run([afinfo, str(path)], check=False, capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError(f"afinfo could not inspect {path}: {(result.stderr or result.stdout).strip()}")
    output = result.stdout
    format_match = re.search(r"Data format:\s+(\d+) ch,\s+(\d+) Hz,\s+([^\s]+)", output)
    duration_match = re.search(r"estimated duration:\s+([0-9.]+) sec", output)
    bitrate_match = re.search(r"bit rate:\s+(\d+) bits per second", output)
    if not format_match or not duration_match:
        raise RuntimeError(f"Could not parse afinfo output for {path}")
    return {
        "channels": int(format_match.group(1)),
        "sample_rate": int(format_match.group(2)),
        "codec": format_match.group(3),
        "duration_seconds": float(duration_match.group(1)),
        "bitrate": int(bitrate_match.group(1)) if bitrate_match else None,
    }


def normalize_mp4_timestamps(path: Path) -> None:
    """Zero variable ISO BMFF creation/modification fields without touching audio."""

    data = bytearray(path.read_bytes())
    containers = {b"moov", b"trak", b"mdia", b"minf", b"stbl", b"dinf", b"edts", b"udta"}
    timestamp_boxes = {b"mvhd", b"tkhd", b"mdhd"}

    def walk(start: int, end: int) -> None:
        position = start
        while position + 8 <= end:
            size = int.from_bytes(data[position : position + 4], "big")
            box_type = bytes(data[position + 4 : position + 8])
            header_size = 8
            if size == 1:
                if position + 16 > end:
                    raise RuntimeError(f"Invalid extended MP4 box in {path}")
                size = int.from_bytes(data[position + 8 : position + 16], "big")
                header_size = 16
            elif size == 0:
                size = end - position
            if size < header_size or position + size > end:
                raise RuntimeError(f"Invalid MP4 box size in {path}")
            payload = position + header_size
            if box_type in timestamp_boxes:
                version = data[payload]
                field_bytes = 8 if version == 0 else 16 if version == 1 else 0
                if not field_bytes or payload + 4 + field_bytes > position + size:
                    raise RuntimeError(f"Unsupported {box_type.decode()} box in {path}")
                data[payload + 4 : payload + 4 + field_bytes] = b"\x00" * field_bytes
            elif box_type in containers:
                walk(payload, position + size)
            position += size

    walk(0, len(data))
    with path.open("r+b") as stream:
        stream.write(data)
        stream.truncate()
        stream.flush()
        os.fsync(stream.fileno())


def validate_wav(path: Path) -> dict[str, Any]:
    with wave.open(str(path), "rb") as stream:
        info = {
            "channels": stream.getnchannels(),
            "sample_width": stream.getsampwidth(),
            "sample_rate": stream.getframerate(),
            "frames": stream.getnframes(),
        }
    if info["channels"] != TARGET_CHANNELS:
        raise ValueError(f"Expected mono WAV, found {info['channels']} channels: {path}")
    if info["sample_width"] != TARGET_SAMPLE_WIDTH:
        raise ValueError(f"Expected 16-bit WAV: {path}")
    if info["sample_rate"] != TARGET_SAMPLE_RATE:
        raise ValueError(f"Expected {TARGET_SAMPLE_RATE} Hz WAV: {path}")
    if info["frames"] <= 0:
        raise ValueError(f"WAV contains no frames: {path}")
    info["duration_seconds"] = round(info["frames"] / info["sample_rate"], 6)
    return info


class KokoroBackend:
    def __init__(self, *, model: Path, voices: Path, voice: str, speed: float, language: str) -> None:
        if not model.is_file() or not voices.is_file():
            raise FileNotFoundError(
                "Kokoro model files are missing. Run `zotero-audio models install` or pass --model and --voices."
            )
        try:
            from kokoro_onnx import Kokoro
        except ImportError as exc:
            raise RuntimeError("Install neural TTS support with `pip install -e '.[kokoro]'`") from exc
        self.model = Kokoro(str(model), str(voices))
        try:
            engine_version = metadata.version("kokoro-onnx")
        except metadata.PackageNotFoundError:
            engine_version = "unknown"
        self.config = {
            "engine": "kokoro-onnx",
            "engine_version": engine_version,
            "model": model.name,
            "model_sha256": sha256_file(model),
            "voices": voices.name,
            "voices_sha256": sha256_file(voices),
            "voice": voice,
            "speed": speed,
            "language": language,
            "sample_rate": TARGET_SAMPLE_RATE,
            "execution_provider": "CPUExecutionProvider",
            "determinism": "byte-stable in the project benchmark on this machine",
        }

    def synthesize(self, text: str, destination: Path) -> dict[str, Any]:
        import numpy as np

        samples, sample_rate = self.model.create(
            text, voice=self.config["voice"], speed=self.config["speed"], lang=self.config["language"]
        )
        if sample_rate != TARGET_SAMPLE_RATE:
            raise RuntimeError(f"Kokoro returned unexpected sample rate: {sample_rate}")
        pcm = np.rint(np.clip(samples, -1.0, 1.0) * 32767.0).astype("<i2")
        destination.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix=f".{destination.name}.", dir=destination.parent)
        os.close(descriptor)
        temporary_path = Path(temporary)
        try:
            with wave.open(str(temporary_path), "wb") as stream:
                stream.setnchannels(TARGET_CHANNELS)
                stream.setsampwidth(TARGET_SAMPLE_WIDTH)
                stream.setframerate(TARGET_SAMPLE_RATE)
                stream.writeframes(pcm.tobytes())
            validate_wav(temporary_path)
            os.replace(temporary_path, destination)
        except BaseException:
            temporary_path.unlink(missing_ok=True)
            raise
        return {"chunks": [{"text": text, "duration_seconds": round(len(pcm) / sample_rate, 6)}]}


class MlxKokoroBackend:
    """Quality-first Kokoro BF16 backend accelerated by MLX on Apple Silicon."""

    def __init__(
        self,
        *,
        model_id: str = "mlx-community/Kokoro-82M-bf16",
        voice: str = "af_heart",
        speed: float = 1.0,
        language: str = "a",
    ) -> None:
        try:
            from huggingface_hub import snapshot_download
            from mlx_audio.tts.utils import load_model
        except ImportError as exc:
            raise RuntimeError("Install the quality backend with `pip install -e '.[mlx]'`") from exc
        snapshot = Path(
            snapshot_download(
                repo_id=model_id,
                allow_patterns=[
                    "config.json",
                    "kokoro-v1_0.safetensors",
                    # Batch processing may switch between English and British
                    # English voices after the model is loaded.
                    "voices/*.safetensors",
                ],
            )
        ).resolve()
        self.model = load_model(snapshot)
        self.model_id = model_id
        self.model_revision = snapshot.name
        try:
            engine_version = metadata.version("mlx-audio")
        except metadata.PackageNotFoundError:
            engine_version = "unknown"
        try:
            mlx_version = metadata.version("mlx")
        except metadata.PackageNotFoundError:
            mlx_version = "unknown"
        self.engine_version = engine_version
        self.mlx_version = mlx_version
        self.configure(voice=voice, speed=speed, language=language)

    def configure(self, *, voice: str, speed: float, language: str) -> None:
        self.config = {
            "engine": "kokoro-mlx",
            "engine_version": self.engine_version,
            "mlx_version": self.mlx_version,
            "model": self.model_id,
            "model_revision": self.model_revision,
            "precision": "bf16",
            "voice": voice,
            "speed": speed,
            "language": language,
            "sample_rate": TARGET_SAMPLE_RATE,
            "device": "Apple Silicon GPU",
            "input_chunking": {
                "algorithm": "sentence-word-v1",
                "max_chars": KOKORO_INPUT_CHUNK_CHARS,
                "purpose": "prevent MLX Kokoro phoneme-limit truncation",
            },
            "text_normalization": "nfkc-spoken-symbols-v1",
            "determinism": "cached segments are exact; fresh MLX synthesis may differ at the sample level",
        }

    def synthesize(self, text: str, destination: Path) -> dict[str, Any]:
        import mlx.core as mx
        import numpy as np

        results = []
        chunk_records: list[dict[str, Any]] = []
        normalized_text = normalize_kokoro_text(text)
        for chunk in split_kokoro_input(normalized_text):
            generated = list(
                self.model.generate(
                    text=chunk,
                    voice=self.config["voice"],
                    speed=self.config["speed"],
                    lang_code=self.config["language"],
                )
            )
            results.extend(generated)
            frames = sum(len(np.asarray(result.audio).reshape(-1)) for result in generated)
            chunk_records.append({"text": chunk, "duration_seconds": round(frames / TARGET_SAMPLE_RATE, 6)})
        if not results:
            excerpt = normalized_text[:120]
            raise RuntimeError(f"Kokoro MLX returned no audio for text: {excerpt!r}")
        sample_rates = {int(result.sample_rate) for result in results}
        if sample_rates != {TARGET_SAMPLE_RATE}:
            raise RuntimeError(f"Kokoro MLX returned unexpected sample rates: {sorted(sample_rates)}")
        samples = np.concatenate([np.asarray(result.audio).reshape(-1) for result in results])
        pcm = np.rint(np.clip(samples, -1.0, 1.0) * 32767.0).astype("<i2")
        destination.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix=f".{destination.name}.", dir=destination.parent)
        os.close(descriptor)
        temporary_path = Path(temporary)
        try:
            with wave.open(str(temporary_path), "wb") as stream:
                stream.setnchannels(TARGET_CHANNELS)
                stream.setsampwidth(TARGET_SAMPLE_WIDTH)
                stream.setframerate(TARGET_SAMPLE_RATE)
                stream.writeframes(pcm.tobytes())
            validate_wav(temporary_path)
            os.replace(temporary_path, destination)
        except BaseException:
            temporary_path.unlink(missing_ok=True)
            raise
        finally:
            mx.clear_cache()
        return {"chunks": chunk_records}


def create_backend(
    engine: str,
    *,
    voice: str | None,
    speed: float,
    language: str,
    model: Path,
    voices: Path,
    mlx_model: str,
) -> SpeechBackend:
    if engine == "kokoro-mlx":
        return MlxKokoroBackend(
            model_id=mlx_model,
            voice=voice or "af_heart",
            speed=speed,
            language=language,
        )
    if engine == "kokoro-onnx":
        return KokoroBackend(
            model=model,
            voices=voices,
            voice=voice or "af_heart",
            speed=speed,
            language=language,
        )
    raise ValueError(f"Unsupported Kokoro engine: {engine}")


def synthesize_plan(bundle: Path, backend: SpeechBackend) -> tuple[dict[str, Any], int]:
    plan = load_json(bundle / "speech-plan.json")
    segments_dir = bundle / "audio" / "segments"
    segments_dir.mkdir(parents=True, exist_ok=True)
    config_digest = json_digest(backend.config)
    previous_records: dict[str, dict[str, Any]] = {}
    previous_path = bundle / "run-manifest.json"
    if previous_path.exists():
        previous = load_json(previous_path)
        if (
            previous.get("plan_sha256") == plan["plan_sha256"]
            and previous.get("synthesis_config_sha256") == config_digest
        ):
            previous_records = {
                record["cache_key"]: record
                for record in previous.get("segments", [])
                if isinstance(record, dict) and record.get("cache_key")
            }
    records: list[dict[str, Any]] = []
    current_records: dict[str, dict[str, Any]] = {}
    manifest = {
        "schema": "zotero-audio-run-manifest/v1",
        "pipeline_version": __version__,
        "source_sha256": plan["source_sha256"],
        "plan_sha256": plan["plan_sha256"],
        "synthesis_config": backend.config,
        "synthesis_config_sha256": config_digest,
        "status": "in_progress",
        "segments": records,
    }
    atomic_write_json(bundle / "run-manifest.json", manifest)
    reused = 0
    for segment in plan["segments"]:
        render_chunks: list[dict[str, Any]] | None = None
        cache_key = json_digest(
            {"text_sha256": segment["text_sha256"], "synthesis_config_sha256": config_digest}
        )
        path = segments_dir / f"{cache_key}.wav"
        current_record = current_records.get(cache_key)
        if current_record and path.exists():
            verified = current_record.get("sha256") == sha256_file(path)
            if verified:
                try:
                    audio_info = validate_wav(path)
                except (ValueError, wave.Error):
                    verified = False
            if verified:
                reused += 1
                render_chunks = current_record.get("chunks")
            else:
                path.unlink(missing_ok=True)
        elif path.exists():
            previous_record = previous_records.get(cache_key)
            verified = bool(
                previous_record
                and previous_record.get("text_sha256") == segment["text_sha256"]
                and previous_record.get("sha256") == sha256_file(path)
            )
            if verified:
                try:
                    audio_info = validate_wav(path)
                except (ValueError, wave.Error):
                    verified = False
            if not verified:
                path.unlink()
            else:
                reused += 1
                render_chunks = previous_record.get("chunks")
        if not path.exists():
            try:
                synthesis_result = backend.synthesize(segment["text"], path)
                if isinstance(synthesis_result, dict):
                    render_chunks = synthesis_result.get("chunks")
            except Exception as exc:
                manifest["status"] = "failed"
                manifest["failed_ordinal"] = segment["ordinal"]
                manifest["error"] = f"{type(exc).__name__}: {exc}"
                atomic_write_json(bundle / "run-manifest.json", manifest)
                raise RuntimeError(
                    f"Kokoro synthesis failed at segment {segment['ordinal']}: {exc}"
                ) from exc
            audio_info = validate_wav(path)
        if not render_chunks:
            render_chunks = [{"text": segment["text"], "duration_seconds": audio_info["duration_seconds"]}]
        chunk_total = sum(float(chunk["duration_seconds"]) for chunk in render_chunks)
        if abs(chunk_total - float(audio_info["duration_seconds"])) > 0.02:
            raise RuntimeError(f"Chunk timing mismatch for segment {segment['ordinal']}")
        record = {
                "ordinal": segment["ordinal"],
                "cache_key": cache_key,
                "text_sha256": segment["text_sha256"],
                "path": str(path.relative_to(bundle)),
                "sha256": sha256_file(path),
                "chunks": render_chunks,
                **audio_info,
            }
        records.append(record)
        current_records[cache_key] = record
        atomic_write_json(bundle / "run-manifest.json", manifest)
    manifest["status"] = "complete"
    manifest.pop("failed_ordinal", None)
    manifest.pop("error", None)
    atomic_write_json(bundle / "run-manifest.json", manifest)
    return manifest, reused


def _concatenate_pcm(bundle: Path, plan: dict[str, Any], manifest: dict[str, Any], destination: Path) -> None:
    records = {record["ordinal"]: record for record in manifest["segments"]}
    with wave.open(str(destination), "wb") as output:
        output.setnchannels(TARGET_CHANNELS)
        output.setsampwidth(TARGET_SAMPLE_WIDTH)
        output.setframerate(TARGET_SAMPLE_RATE)
        for segment in plan["segments"]:
            record = records.get(segment["ordinal"])
            if not record or record["text_sha256"] != segment["text_sha256"]:
                raise RuntimeError(f"Missing or stale audio for segment {segment['ordinal']}")
            source = bundle / record["path"]
            if sha256_file(source) != record["sha256"]:
                raise RuntimeError(f"Audio checksum mismatch for segment {segment['ordinal']}")
            validate_wav(source)
            with wave.open(str(source), "rb") as stream:
                output.writeframes(stream.readframes(stream.getnframes()))
            if segment is not plan["segments"][-1]:
                pause_frames = round(TARGET_SAMPLE_RATE * segment["pause_after_ms"] / 1000)
                output.writeframes(b"\x00" * pause_frames * TARGET_SAMPLE_WIDTH)


def encode_wav_to_m4a(
    source: Path,
    destination: Path,
    *,
    title: str,
    artist: str | None,
    album: str,
    comment: str,
    bitrate: int = 64_000,
) -> dict[str, Any]:
    if not 32_000 <= bitrate <= 256_000:
        raise ValueError("AAC bitrate must be between 32000 and 256000")
    afconvert = _require_executable("/usr/bin/afconvert")
    validate_wav(source)
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{destination.name}.", dir=destination.parent)
    os.close(descriptor)
    temporary_path = Path(temporary)
    try:
        _run(
            [
                afconvert,
                str(source),
                str(temporary_path),
                "-f",
                "m4af",
                "-d",
                "aac",
                "-b",
                str(bitrate),
                "-c",
                "1",
                "-s",
                "3",
            ]
        )
        tags = MP4(temporary_path)
        tags["\xa9nam"] = [title]
        if artist:
            tags["\xa9ART"] = [artist]
        tags["\xa9alb"] = [album]
        tags["\xa9cmt"] = [comment]
        tags.save()
        normalize_mp4_timestamps(temporary_path)
        os.replace(temporary_path, destination)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise
    return inspect_m4a(destination)


def _embed_m4a_chapters(path: Path, chapters: list[dict[str, Any]], duration_seconds: float) -> None:
    """Remux deterministic Nero-style MP4 chapters without re-encoding AAC."""
    if not chapters:
        return
    ffmpeg = _require_executable("ffmpeg")
    ordered = sorted(chapters, key=lambda item: float(item["start"]))
    with tempfile.TemporaryDirectory(prefix="zotero-audio-chapters-", dir=path.parent) as temporary:
        metadata_path = Path(temporary) / "chapters.ffmeta"
        lines = [";FFMETADATA1"]
        for index, chapter in enumerate(ordered):
            start = max(0, round(float(chapter["start"]) * 1000))
            end_value = float(ordered[index + 1]["start"]) if index + 1 < len(ordered) else duration_seconds
            end = max(start + 1, round(end_value * 1000))
            title = re.sub(r"([\\=;#])", r"\\\1", str(chapter["title"]).replace("\n", " "))
            lines.extend(("[CHAPTER]", "TIMEBASE=1/1000", f"START={start}", f"END={end}", f"title={title}"))
        metadata_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        remuxed = Path(temporary) / "chaptered.m4a"
        _run([ffmpeg, "-y", "-i", str(path), "-f", "ffmetadata", "-i", str(metadata_path),
              "-map", "0:a", "-map_metadata", "0", "-map_chapters", "1", "-c:a", "copy", str(remuxed)])
        normalize_mp4_timestamps(remuxed)
        os.replace(remuxed, path)


def assemble_m4a(bundle: Path, *, bitrate: int = 64_000,
                 chapters: list[dict[str, Any]] | None = None) -> tuple[Path, dict[str, Any]]:
    plan = load_json(bundle / "speech-plan.json")
    manifest = load_json(bundle / "run-manifest.json")
    if manifest["plan_sha256"] != plan["plan_sha256"]:
        raise RuntimeError("run-manifest.json does not match speech-plan.json; synthesize again")
    audio_dir = bundle / "audio"
    audio_dir.mkdir(parents=True, exist_ok=True)
    final_path = audio_dir / f"{bundle.name}.m4a"
    with tempfile.TemporaryDirectory(prefix="zotero-audio-assemble-", dir=audio_dir) as temporary:
        combined = Path(temporary) / "combined.wav"
        _concatenate_pcm(bundle, plan, manifest, combined)
        normalized = Path(temporary) / "normalized.wav"
        normalization = normalize_wav_loudness(combined, normalized)
        combined = normalized
        pcm_info = validate_wav(combined)
        document = plan["document"]
        final_info = encode_wav_to_m4a(
            combined,
            final_path,
            title=document["title"],
            artist=document.get("author"),
            album="Zotero Audio",
            comment=(
                f"source-sha256={plan['source_sha256']}; plan-sha256={plan['plan_sha256']}; "
                f"pipeline=zotero-audio/{__version__}"
            ),
            bitrate=bitrate,
        )
        if chapters:
            _embed_m4a_chapters(final_path, chapters, pcm_info["duration_seconds"])
            final_info = inspect_m4a(final_path)

    # Lossy encoding can create new inter-sample peaks. Delivery is gated on
    # this final encoded measurement, never on the intermediate WAV alone.
    loudness = measure_loudness(final_path)

    final = MP4(final_path)
    expected_audio_seconds = sum(record["duration_seconds"] for record in manifest["segments"])
    expected_pause_seconds = sum(segment["pause_after_ms"] for segment in plan["segments"][:-1]) / 1000
    expected_total = expected_audio_seconds + expected_pause_seconds
    duration = final_info["duration_seconds"]
    duration_delta = abs(duration - expected_total)
    tag_names = sorted(final.tags.keys()) if final.tags else []
    required_tags = {"©nam", "©alb", "©cmt"}
    embedded_chapter_count = len(final.chapters or [])
    technical_pass = (
        duration_delta <= 0.25
        and final_info["channels"] == TARGET_CHANNELS
        and final_info["sample_rate"] == TARGET_SAMPLE_RATE
        and final_info["codec"] == "aac"
        and bool(final_info["bitrate"])
        and required_tags.issubset(tag_names)
        and (not chapters or embedded_chapter_count == len(chapters))
    )
    qa = {
        "schema": "zotero-audio-qa/v1",
        "status": "pass" if technical_pass else "fail",
        "checks": {
            "segment_count": len(manifest["segments"]),
            "segment_checksums_verified": True,
            "pcm_duration_seconds": pcm_info["duration_seconds"],
            "expected_duration_seconds": round(expected_total, 6),
            "m4a_duration_seconds": round(duration, 6),
            "duration_delta_seconds": round(duration_delta, 6),
            "m4a_channels": final_info["channels"],
            "m4a_sample_rate": final_info["sample_rate"],
            "m4a_codec": final_info["codec"],
            "m4a_bitrate": final_info["bitrate"],
            "m4a_tags": tag_names,
            "required_tags_present": required_tags.issubset(tag_names),
            "embedded_chapter_count": embedded_chapter_count,
            "normalization": normalization,
            "loudness": loudness,
        },
        "output": {
            "path": str(final_path.relative_to(bundle)),
            "sha256": sha256_file(final_path),
            "bytes": final_path.stat().st_size,
        },
    }
    qa["checks"]["loudness"]["pass"] = loudness_is_competitive(
        loudness["integrated_lufs"], loudness["true_peak_dbtp"], loudness.get("clipped_samples", False)
    ) and not loudness.get("silent", False)
    technical_pass = technical_pass and qa["checks"]["loudness"]["pass"]
    qa["status"] = "pass" if technical_pass else "fail"
    atomic_write_json(bundle / "qa-report.json", qa)
    if qa["status"] != "pass":
        raise RuntimeError(f"M4A QA failed; see {bundle / 'qa-report.json'}")
    return final_path, qa
