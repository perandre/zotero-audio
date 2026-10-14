"""Speech runtime selection, identity and Intel-compatible dependency markers."""
import sys
import tomllib
import types
from pathlib import Path

import pytest
from packaging.requirements import Requirement

from zotero_audio import audio
from zotero_audio.app_worker import BackendPool
from zotero_audio.audio import canonical_synthesis_config, create_backend, default_kokoro_engine
from zotero_audio.util import json_digest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("machine,system,expected", [
    ("arm64", "Darwin", "kokoro-mlx"),
    ("x86_64", "Darwin", "kokoro-torch"),
    ("x86_64", "Linux", "kokoro-torch"),
    ("AMD64", "Windows", "kokoro-torch"),
])
def test_default_engine_by_platform(machine, system, expected):
    assert default_kokoro_engine(machine, system) == expected


def _torch_backend(monkeypatch, voice="am_michael"):
    torch = types.ModuleType("torch")
    kokoro = types.ModuleType("kokoro")
    hub = types.ModuleType("huggingface_hub")

    class KModel:
        context_length = 512

        def __init__(self, **kwargs):
            pass

        def to(self, device):
            return self

        def eval(self):
            return self

    kokoro.KModel, kokoro.KPipeline = KModel, object
    hub.snapshot_download = lambda **kwargs: "/cache/hf/snapshots/rev123"
    monkeypatch.setitem(sys.modules, "torch", torch)
    monkeypatch.setitem(sys.modules, "kokoro", kokoro)
    monkeypatch.setitem(sys.modules, "huggingface_hub", hub)
    monkeypatch.setattr(Path, "resolve", lambda self, *a, **k: self)
    return create_backend("kokoro-torch", voice=voice, speed=1.0, language="a",
                          model=Path("unused"), voices=Path("unused"), mlx_model="unused")


def test_torch_backend_identity_and_shared_voice_settings(monkeypatch):
    backend = _torch_backend(monkeypatch)
    config = canonical_synthesis_config(backend.config)
    assert config["engine"] == "kokoro-torch"
    assert (config["device"], config["precision"], config["model"]) == ("CPU", "fp32", "hexgrad/Kokoro-82M")
    assert config["model_revision"] == "rev123"
    assert config["sample_rate"] == audio.TARGET_SAMPLE_RATE == 24_000
    assert config["voice"] == "am_michael"
    backend.configure(voice="af_heart", speed=1.0, language="a")
    assert backend.config["voice"] == "af_heart"


def test_torch_identity_never_matches_mlx_identity(monkeypatch):
    torch_config = canonical_synthesis_config(_torch_backend(monkeypatch).config)
    mlx = object.__new__(audio.MlxKokoroBackend)
    mlx.model = types.SimpleNamespace(context_length=512)
    mlx.model_id, mlx.model_revision = "mlx-community/Kokoro-82M-bf16", "rev123"
    mlx.engine_version = mlx.mlx_version = "1"
    mlx.configure(voice="am_michael", speed=1.0, language="a")
    mlx_config = canonical_synthesis_config(mlx.config)
    assert mlx_config["engine"] == "kokoro-mlx" != torch_config["engine"]
    assert json_digest(mlx_config) != json_digest(torch_config)


def test_missing_torch_runtime_is_an_explicit_error(monkeypatch):
    monkeypatch.setitem(sys.modules, "torch", None)
    with pytest.raises(RuntimeError, match="kokoro-torch"):
        create_backend("kokoro-torch", voice=None, speed=1.0, language="a",
                       model=Path("x"), voices=Path("y"), mlx_model="m")


@pytest.mark.parametrize("machine,expected", [("x86_64", "TorchKokoroBackend"), ("arm64", "MlxKokoroBackend")])
def test_pool_auto_selection_loads_only_the_selected_provider(monkeypatch, machine, expected):
    monkeypatch.setattr(audio.platform, "machine", lambda: machine)
    monkeypatch.setattr(audio.platform, "system", lambda: "Darwin")
    created = []
    for name in ("TorchKokoroBackend", "MlxKokoroBackend"):
        monkeypatch.setattr(audio, name, lambda name=name: created.append(name) or name)
    assert BackendPool().get("kokoro") == expected
    assert created == [expected]


def test_pool_explicit_names_and_unknown_provider(monkeypatch):
    monkeypatch.setattr(audio, "TorchKokoroBackend", lambda: "torch")
    assert BackendPool().get("kokoro-torch") == "torch"
    with pytest.raises(ValueError, match="not installed"):
        BackendPool().get("no-such-provider")


def test_custom_backends_cannot_shadow_kokoro_names():
    for name in ("kokoro", "auto", "kokoro-mlx", "kokoro-torch", "kokoro-onnx"):
        with pytest.raises(ValueError):
            audio.register_backend(name, lambda **kwargs: None)


def _selected(extra, environment):
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    requirements = [Requirement(item) for item in project["optional-dependencies"][extra]]
    return {r.name: str(r.specifier) for r in requirements
            if r.marker is None or r.marker.evaluate({**environment, "extra": extra})}


INTEL_MAC = {"platform_machine": "x86_64", "sys_platform": "darwin"}
APPLE = {"platform_machine": "arm64", "sys_platform": "darwin"}
LINUX = {"platform_machine": "x86_64", "sys_platform": "linux"}


def test_mlx_packages_install_only_on_apple_silicon():
    assert set(_selected("mlx", APPLE)) == {"mlx-audio", "misaki"}
    assert _selected("mlx", INTEL_MAC) == {} == _selected("mlx", LINUX)


def test_kokoro_torch_dependencies_by_platform():
    assert _selected("kokoro-torch", INTEL_MAC) == {
        "kokoro": "==0.9.4", "misaki": "==0.9.4", "torch": "==2.2.2", "numpy": "<2", "transformers": "<5"}
    assert set(_selected("kokoro-torch", LINUX)) == {"kokoro", "misaki"}
    assert _selected("kokoro-torch", APPLE) == {}
