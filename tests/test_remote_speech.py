"""Remote speech protocol, identity, reconnect and failure behaviour with a fake server."""
import json
import os
import sys
from pathlib import Path

import pytest

from zotero_audio import audio, remote_speech
from zotero_audio.app_worker import BackendPool
from zotero_audio.audio import validate_wav
from zotero_audio.remote_speech import RemoteSpeechBackend, RemoteSpeechError

HERE = Path(__file__).resolve().parent
SRC = HERE.parent / "src"


@pytest.fixture(autouse=True)
def server_environment(monkeypatch):
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join([str(SRC), str(HERE)]))


def argv():
    return [sys.executable, "-m", "zotero_audio.remote_speech", "serve", "--backend", "fake_remote_backend:make"]


@pytest.fixture
def backend():
    created = []

    def make(**options):
        created.append(RemoteSpeechBackend(argv(), connect_timeout=30, request_timeout=30, **options))
        return created[-1]
    yield make
    for item in created:
        item.close()


def test_handshake_config_is_the_remote_engine_identity(backend):
    remote = backend(expected_engine="kokoro-torch")
    assert remote.config == {"engine": "kokoro-torch", "engine_version": "fake", "model": "fake",
                             "voice": "am_michael", "speed": 1.0, "language": "a", "sample_rate": 24000}


def test_configure_changes_identity_and_synthesis_is_validated(backend, tmp_path):
    remote = backend()
    remote.configure(voice="af_heart", speed=1, language="a")
    assert remote.config["voice"] == "af_heart"
    destination = tmp_path / "out" / "segment.wav"
    meta = remote.synthesize("hello", destination)
    assert meta["chunks"][0]["text"] == "hello" and meta["chunks"][0]["voice"] == "af_heart"
    assert validate_wav(destination)["sample_rate"] == 24000
    assert not list(destination.parent.glob(".segment.wav.*"))


def test_wrong_engine_is_rejected():
    with pytest.raises(RemoteSpeechError, match="expected 'kokoro-mlx'"):
        RemoteSpeechBackend(argv(), connect_timeout=30, expected_engine="kokoro-mlx")


def test_remote_error_does_not_reconnect_or_fall_back(backend, tmp_path):
    remote = backend()
    process = remote.process
    with pytest.raises(RemoteSpeechError, match="synthetic failure"):
        remote.synthesize("boom", tmp_path / "x.wav")
    assert not (tmp_path / "x.wav").exists()
    remote.synthesize("fine", tmp_path / "y.wav")
    assert remote.process is process


@pytest.mark.parametrize("text", ["bad", "badrate"])
def test_invalid_audio_is_rejected_and_not_written(backend, tmp_path, text):
    with pytest.raises(RemoteSpeechError, match="invalid WAV"):
        backend().synthesize(text, tmp_path / "x.wav")
    assert not (tmp_path / "x.wav").exists() and not list(tmp_path.glob(".x.wav.*"))


def test_one_automatic_reconnect_reapplies_configuration(backend, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_CRASH_ONCE", str(tmp_path / "crashed"))
    remote = backend()
    remote.configure(voice="af_heart", speed=1.0, language="a")
    first_process = remote.process
    meta = remote.synthesize("crash", tmp_path / "x.wav")  # the server dies once, then succeeds
    assert remote.process is not first_process
    assert meta["chunks"][0]["voice"] == "af_heart"
    assert remote.config["voice"] == "af_heart"


def test_second_transport_failure_fails_loudly(backend, tmp_path):
    remote = backend()
    remote.argv = [sys.executable, "-c", "import sys; sys.exit(5)"]
    remote.process.kill()
    with pytest.raises(RemoteSpeechError, match="reconnect also failed"):
        remote.synthesize("hello", tmp_path / "x.wav")
    assert not (tmp_path / "x.wav").exists()


def test_timeout_is_reported():
    sleeper = [sys.executable, "-c", "import time; time.sleep(30)"]
    with pytest.raises(RemoteSpeechError, match="no reply"):
        RemoteSpeechBackend(sleeper, connect_timeout=0.3)


def test_reconnect_refuses_changed_engine_identity(backend, tmp_path):
    remote = backend()
    remote.config = {**remote.config, "engine_version": "other"}
    remote.process.kill()
    with pytest.raises(RemoteSpeechError, match="reconnect also failed.*identity changed"):
        remote.synthesize("hello", tmp_path / "x.wav")


def test_settings_resolution_and_validation(tmp_path):
    with pytest.raises(RemoteSpeechError, match="host is not set"):
        RemoteSpeechBackend.from_settings(tmp_path)
    control = tmp_path / "control"
    control.mkdir()
    (control / "speech.json").write_text(json.dumps({"provider": "kokoro-remote", "host": "me@box"}))
    assert remote_speech.configured_provider(tmp_path) == "kokoro-remote"
    assert remote_speech.remote_speech_config(tmp_path, {"ZOTERO_AUDIO_REMOTE_SPEECH_HOST": "x@y"})["host"] == "x@y"
    command = remote_speech.ssh_command("me@box", remote_speech.DEFAULT_REMOTE_COMMAND)
    assert command[0] == "ssh" and "BatchMode=yes" in command and command[-2] == "me@box"
    for bad in ("", "-oProxyCommand=x", "a b"):
        with pytest.raises(RemoteSpeechError):
            remote_speech.ssh_command(bad, "x")


def test_pool_routes_kokoro_alias_only_when_configured(tmp_path, monkeypatch):
    monkeypatch.setenv("ZOTERO_AUDIO_RUNTIME", str(tmp_path))
    monkeypatch.delenv("ZOTERO_AUDIO_SPEECH_PROVIDER", raising=False)
    monkeypatch.setattr(audio.platform, "machine", lambda: "arm64")
    monkeypatch.setattr(audio.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(audio, "MlxKokoroBackend", lambda: "mlx")
    assert BackendPool().get("kokoro") == "mlx"  # default behaviour is unchanged
    seen = []
    monkeypatch.setattr(RemoteSpeechBackend, "from_settings",
                        classmethod(lambda cls, **kw: seen.append(kw) or "remote"))
    monkeypatch.setenv("ZOTERO_AUDIO_SPEECH_PROVIDER", "kokoro-remote")
    assert BackendPool().get("kokoro") == "remote"
    assert seen == [{"expected_engine": "kokoro-torch"}]
    monkeypatch.delenv("ZOTERO_AUDIO_SPEECH_PROVIDER")
    assert BackendPool().get("kokoro-remote") == "remote"
