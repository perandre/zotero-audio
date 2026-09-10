import json
import signal
from types import SimpleNamespace

import pytest

from zotero_audio import app_cli, app_install
from zotero_audio.app_state import Store


@pytest.fixture
def store(tmp_path):
    value = Store(tmp_path / "runtime")
    value.put_article({"id": "PAPER1", "title": "How a company successfully adopted AI", "authors": ["Anna Author"]})
    return value


def test_stop_waits_for_original_pid_even_if_keepalive_records_replacement(store, monkeypatch):
    store.set_state("daemon", {"pid": 12345, "port": 8765})
    events = []
    monkeypatch.setattr(app_install, "disable_installed_service", lambda _: events.append("disabled") or True)
    monkeypatch.setattr(app_install, "unload_installed_service", lambda _: events.append("unloaded"))
    monkeypatch.setattr(app_cli, "_is_daemon_process", lambda pid: pid == 12345)
    monkeypatch.setattr(app_cli.os, "kill", lambda pid, sig: events.append((pid, sig)))
    states = iter([True, False])
    monkeypatch.setattr(app_cli, "_process_alive", lambda pid: next(states))

    def finish_original(_):
        events.append("checkpoint_finished")
        store.set_state("daemon", {"pid": 12346, "port": 8765})

    monkeypatch.setattr(app_cli.time, "sleep", finish_original)
    app_cli.stop_daemon(store)
    assert events == ["disabled", (12345, signal.SIGTERM), "checkpoint_finished", "unloaded"]
    assert store.state("daemon")["pid"] == 12346


def test_stop_timeout_never_forces_or_unloads_an_active_stage(store, monkeypatch):
    store.set_state("daemon", {"pid": 12345})
    signals = []
    monkeypatch.setattr(app_install, "disable_installed_service", lambda _: True)
    monkeypatch.setattr(app_install, "unload_installed_service", lambda _: pytest.fail("Must wait for the atomic stage"))
    monkeypatch.setattr(app_cli, "_is_daemon_process", lambda _: True)
    monkeypatch.setattr(app_cli.os, "kill", lambda pid, sig: signals.append(sig))
    monkeypatch.setattr(app_cli, "_process_alive", lambda _: True)
    clock = iter([0, 2])
    monkeypatch.setattr(app_cli.time, "monotonic", lambda: next(clock))
    with pytest.raises(RuntimeError, match="still stopping safely"):
        app_cli.stop_daemon(store, timeout=1)
    assert signals == [signal.SIGTERM]


def test_stale_pid_cannot_signal_an_unrelated_process(store, monkeypatch):
    store.set_state("daemon", {"pid": 12345})
    monkeypatch.setattr(app_install, "disable_installed_service", lambda _: False)
    monkeypatch.setattr(app_cli, "_is_daemon_process", lambda _: False)
    monkeypatch.setattr(app_cli.os, "kill", lambda *args: pytest.fail("Unrelated PID must not receive a signal"))
    app_cli.stop_daemon(store)


@pytest.mark.parametrize("command,expected", [
    ("/venv/bin/python -m zotero_audio.app_cli serve", True),
    ("/venv/bin/python /venv/bin/za serve --port 8765", True),
    ("/usr/bin/python some_other_program.py", False),
    ("/venv/bin/za status", False),
])
def test_daemon_identity_checks_command_before_signalling(monkeypatch, command, expected):
    monkeypatch.setattr(app_cli.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout=command))
    assert app_cli._is_daemon_process(12345) is expected


def test_ensure_installed_daemon_uses_launchd_without_detached_process(store, monkeypatch):
    states = iter([None, {"worker": {"online": True}}])
    monkeypatch.setattr(app_cli, "daemon_status", lambda _: next(states))
    starts = []
    monkeypatch.setattr(app_install, "start_installed_service", lambda _: starts.append(True) or True)
    monkeypatch.setattr(app_cli.subprocess, "Popen", lambda *args, **kwargs: pytest.fail("Installed daemon must use launchd"))
    app_cli.ensure_daemon(store)
    assert starts == [True]


@pytest.mark.parametrize("enabled", [True, False])
def test_audio_queue_reports_automatic_publication_policy(store, monkeypatch, capsys, enabled):
    store.update_settings({"auto_publish": enabled})
    monkeypatch.setattr(app_cli, "Store", lambda: store)
    monkeypatch.setattr(app_cli, "ensure_daemon", lambda _: None)
    assert app_cli.main(["full", "How a company successfully adopted AI", "--json"]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["job"]["title"] == "How a company successfully adopted AI"
    assert output["job"]["action"] == "full"
    assert "iCloud" in output["publication_policy"]
    assert ("publishing is off" in output["publication_policy"]) is (not enabled)


def test_markdown_queue_does_not_claim_it_will_publish_audio(store, monkeypatch, capsys):
    monkeypatch.setattr(app_cli, "Store", lambda: store)
    monkeypatch.setattr(app_cli, "ensure_daemon", lambda _: None)
    assert app_cli.main(["markdown", "new", "--json"]) == 0
    assert "publication_policy" not in json.loads(capsys.readouterr().out)
