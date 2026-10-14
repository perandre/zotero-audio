"""Synthetic SpeechBackend served by the remote-speech tests (no model weights)."""
import os
import wave
from pathlib import Path


def _write(destination, rate, frames):
    with wave.open(str(destination), "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(rate)
        stream.writeframes(b"\x01\x00" * frames)


class FakeBackend:
    def __init__(self):
        self.config = {"engine": "kokoro-torch", "engine_version": "fake", "model": "fake", "voice": "am_michael",
                       "speed": 1.0, "language": "a", "sample_rate": 24000}
        print("stray library output must not corrupt the protocol")

    def configure(self, *, voice, speed, language):
        self.config.update(voice=voice, speed=speed, language=language)

    def synthesize(self, text, destination: Path):
        crash = os.environ.get("FAKE_CRASH_ONCE")
        if crash and text == "crash" and not Path(crash).exists():
            Path(crash).write_text("x")
            os._exit(3)
        if text == "boom":
            raise ValueError("synthetic failure")
        if text == "bad":
            _write(destination, 24000, 0)
        elif text == "badrate":
            _write(destination, 16000, 100)
        else:
            _write(destination, 24000, 2400)
        return {"chunks": [{"text": text, "voice": self.config["voice"], "duration_seconds": 0.1}]}


def make():
    return FakeBackend()
