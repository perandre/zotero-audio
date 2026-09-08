import os
import shutil
from pathlib import Path

from zotero_audio.runtime import configure_tool_path
from zotero_audio.notifications import report_failure


def test_launchd_path_discovers_nvm_and_is_idempotent(tmp_path, monkeypatch):
    node_bin = tmp_path / "versions/node/v22.14.0/bin"
    node_bin.mkdir(parents=True)
    for name in ("node", "npx"):
        tool = node_bin / name
        tool.write_text("#!/bin/sh\nexit 0\n")
        tool.chmod(0o755)
    monkeypatch.setenv("NVM_DIR", str(tmp_path))
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.delenv("ZOTERO_AUDIO_NPX", raising=False)
    configure_tool_path()
    assert shutil.which("node")
    assert shutil.which("npx")
    assert str(node_bin) in os.environ["PATH"].split(os.pathsep)
    before = os.environ["PATH"]
    configure_tool_path()
    assert os.environ["PATH"] == before


def test_configured_npx_exposes_sibling_node(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.setenv("ZOTERO_AUDIO_NPX", str(tmp_path / "npx"))
    configure_tool_path()
    assert os.environ["PATH"].split(os.pathsep)[0] == str(tmp_path)


def test_notifications_deduplicate_persistently_and_reset_on_recovery(tmp_path):
    path = tmp_path / "notifications.json"
    calls = []
    for _ in range(3):
        report_failure(path, "sync", "missing npx", "failed", calls.append)
    assert calls == ["failed"]
    report_failure(path, "sync", "other error", "changed", calls.append)
    report_failure(path, "sync", None, "", calls.append)
    report_failure(path, "sync", "other error", "failed again", calls.append)
    assert calls == ["failed", "changed", "failed again"]


def test_muting_does_not_mark_failure_as_notified(tmp_path):
    path = tmp_path / "notifications.json"
    calls = []
    report_failure(path, "sync", "error", "failed", calls.append, enabled=False)
    assert not calls
    report_failure(path, "sync", "error", "failed", calls.append)
    assert calls == ["failed"]
