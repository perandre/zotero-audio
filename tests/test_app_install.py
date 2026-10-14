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


# --- Linux systemd --user ---------------------------------------------------

@pytest.fixture
def linux(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(app_install.Path, "home", lambda: home)
    monkeypatch.setattr(app_install.sys, "platform", "linux")
    monkeypatch.setattr(app_install.shutil, "which", lambda name: "/usr/bin/" + name)
    calls = []

    def systemctl(*args, check=True):
        calls.append(args)
        return SimpleNamespace(returncode=0, stdout="active\n", stderr="")

    monkeypatch.setattr(app_install, "_systemctl", systemctl)
    monkeypatch.setattr(app_install, "linger_enabled", lambda: False)
    return Store(tmp_path / "runtime"), home, calls


def test_systemd_unit_rendering(tmp_path):
    store = Store(tmp_path / "run time")
    text = app_install.render_systemd_unit(store, python="/opt/venv/bin/python", project=tmp_path / "proj",
                                           path_env="/usr/bin:/home/u/.local/bin")
    assert 'ExecStart="/opt/venv/bin/python" -m zotero_audio.app_cli serve' in text
    assert f'Environment="ZOTERO_AUDIO_RUNTIME={store.runtime}"' in text
    assert 'Environment="PATH=/usr/bin:/home/u/.local/bin"' in text
    assert "Restart=on-failure" in text and "WantedBy=default.target" in text
    assert f"StandardOutput=append:{store.root}/daemon.log" in text


def test_systemd_unit_escapes_specifiers(tmp_path):
    store = Store(tmp_path / "100%")
    text = app_install.render_systemd_unit(store, python="/p/my$py", project=tmp_path, path_env="/bin")
    assert "100%%" in text and '"/p/my$$py"' in text


def test_install_writes_unit_and_enables_without_real_systemctl(linux, monkeypatch):
    store, home, calls = linux
    events = []
    monkeypatch.setattr(app_cli, "stop_daemon", lambda _: events.append("stopped"))
    monkeypatch.setattr(app_install.os, "access", lambda *args: False)
    result = app_install.install(store)
    unit = home / ".config/systemd/user" / app_install.SYSTEMD_UNIT
    assert unit.is_file() and result["unit"] == str(unit)
    assert events == ["stopped"]
    assert [c[0] for c in calls] == ["daemon-reload", "enable", "start"]
    assert result["service_manager"] == "systemd"
    assert "enable-linger" in result["note"]
    assert app_install.installed_unit(store) == unit


def test_systemd_unit_for_another_runtime_is_untouched(linux, tmp_path):
    store, home, calls = linux
    unit = home / ".config/systemd/user" / app_install.SYSTEMD_UNIT
    unit.parent.mkdir(parents=True)
    unit.write_text(app_install.render_systemd_unit(store, python="/p", project=tmp_path, path_env="/bin"))
    other = Store(tmp_path / "other")
    assert app_install.start_installed_service(other) is False
    assert app_install.disable_installed_service(other) is False
    app_install.unload_installed_service(other)
    assert calls == []
    assert app_install.start_installed_service(store) is True
    assert [c[0] for c in calls] == ["enable", "start"]
    app_install.unload_installed_service(store)
    assert calls[-1] == ("stop", app_install.SYSTEMD_UNIT)
