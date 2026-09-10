import plistlib
from types import SimpleNamespace

import pytest

from zotero_audio import app_cli, app_install
from zotero_audio.app_state import Store


@pytest.fixture
def installation(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(app_install.Path, "home", lambda: home)
    monkeypatch.setattr(app_install.sys, "platform", "darwin")
    store = Store(tmp_path / "runtime")
    path = home / "Library/LaunchAgents" / (app_install.SERVICE_LABEL + ".plist")
    path.parent.mkdir(parents=True)
    config = {"Label": app_install.SERVICE_LABEL, "EnvironmentVariables": {"ZOTERO_AUDIO_RUNTIME": str(store.runtime)}}
    path.write_bytes(plistlib.dumps(config))
    return store, path


def test_install_respects_graceful_stop_and_keeps_alive_only_after_crash(installation, monkeypatch):
    store, path = installation
    events = []
    monkeypatch.setattr(app_cli, "stop_daemon", lambda _: events.append("stopped_safely"))
    monkeypatch.setattr(app_install, "start_installed_service", lambda _: events.append("started") or True)
    monkeypatch.setattr(app_install.os, "access", lambda *args: False)
    result = app_install.install(store)
    config = plistlib.loads(path.read_bytes())
    assert config["KeepAlive"] == {"SuccessfulExit": False}
    assert config["RunAtLoad"] is True
    assert config["EnvironmentVariables"]["ZOTERO_AUDIO_RUNTIME"] == str(store.runtime)
    assert events == ["stopped_safely", "started"]
    assert result["installed"] is True


@pytest.mark.parametrize("loaded", [False, True])
def test_start_restores_disabled_service_without_killing_another_stage(installation, monkeypatch, loaded):
    store, path = installation
    commands = []

    def launchctl(*args, **kwargs):
        commands.append(args)
        return SimpleNamespace(returncode=0 if args[0] != "print" or loaded else 113, stderr="")

    monkeypatch.setattr(app_install, "_launchctl", launchctl)
    assert app_install.start_installed_service(store) is True
    assert commands[0][0] == "enable"
    assert commands[-1][0] == ("kickstart" if loaded else "bootstrap")
    assert all("-k" not in command for command in commands)


def test_installed_service_for_another_runtime_is_not_touched(installation, tmp_path, monkeypatch):
    _, _path = installation
    other = Store(tmp_path / "different-runtime")
    monkeypatch.setattr(app_install, "_launchctl", lambda *args, **kwargs: pytest.fail("Other runtime service must remain untouched"))
    assert app_install.installed_agent(other) is None
    assert app_install.disable_installed_service(other) is False
    assert app_install.start_installed_service(other) is False


def test_stop_unloads_only_the_matching_service(installation, monkeypatch):
    store, _ = installation
    commands = []
    monkeypatch.setattr(app_install, "_launchctl", lambda *args, **kwargs: commands.append(args) or SimpleNamespace(returncode=0))
    app_install.unload_installed_service(store)
    assert commands == [("print", app_install.service_target()), ("bootout", app_install.service_target())]
