"""Laptop side of the private-audio outbox: ``za pull-audio``.

Lists the server's outbox over key-based SSH, copies new finished audio into the
local iCloud folder with the same never-overwrite collision rules as the worker,
verifies every sha256 and remembers what it pulled so reruns are idempotent. The
laptop can be closed for days; the next run simply catches up.
"""
from __future__ import annotations

import fcntl
import json
import os
import plistlib
import shlex
import subprocess
import sys
import uuid
from pathlib import Path, PurePosixPath

from .app_outbox import SCHEMA, place_file, read_entries, sha256_file
from .app_state import Store, now

PULL_LABEL = "com.pesh.one-more-paper.pull-audio"
DEFAULT_REMOTE_RUNTIME = "~/Sites/zotero-audio-runtime"
SSH_OPTIONS = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=15", "-o", "ServerAliveInterval=15"]


class Transport:
    def read_index(self) -> str:
        raise NotImplementedError

    def fetch(self, relative: str, destination: Path) -> None:
        raise NotImplementedError


class LocalTransport(Transport):
    """A directory standing in for the server's runtime (tests, mounted shares)."""

    def __init__(self, runtime: Path):
        self.root = Path(runtime) / "outbox"

    def read_index(self) -> str:
        try:
            return (self.root / "index.jsonl").read_text(encoding="utf-8")
        except FileNotFoundError:
            return ""

    def fetch(self, relative: str, destination: Path) -> None:
        destination.write_bytes((self.root / relative).read_bytes())


def remote_path(path: str) -> str:
    """Shell-quote a remote path while still letting the remote shell expand ~/."""
    if path == "~":
        return '"$HOME"'
    if path.startswith("~/"):
        return '"$HOME"/' + shlex.quote(path[2:])
    return shlex.quote(path)


class SshTransport(Transport):
    def __init__(self, host: str, runtime_path: str = DEFAULT_REMOTE_RUNTIME, run=subprocess.run):
        if host.startswith("-") or any(c.isspace() for c in host):
            raise ValueError("Unsafe SSH host")
        self.host, self.base, self.run = host, runtime_path.rstrip("/") + "/outbox", run

    def _ssh(self, command: str, **kwargs):
        return self.run(["ssh", *SSH_OPTIONS, "--", self.host, command], check=False, stdin=subprocess.DEVNULL, **kwargs)

    def read_index(self) -> str:
        result = self._ssh(f"cat {remote_path(self.base + '/index.jsonl')} 2>/dev/null || true", capture_output=True, text=True, timeout=120)
        if result.returncode:
            raise RuntimeError(f"Could not reach {self.host} over SSH (BatchMode, no password): {(result.stderr or '').strip()[:300]}")
        return result.stdout

    def fetch(self, relative: str, destination: Path) -> None:
        # Streaming `cat` copes with spaces/unicode in readable filenames without
        # depending on the rsync flavour installed on either side.
        with destination.open("wb") as output:
            result = self._ssh(f"cat -- {remote_path(self.base + '/' + relative)}", stdout=output, stderr=subprocess.PIPE, timeout=3600)
        if result.returncode:
            raise RuntimeError(f"Download failed: {(result.stderr or b'').decode('utf-8', 'replace').strip()[:300]}")


def _safe_relative(path: str) -> str:
    pure = PurePosixPath(path)
    if pure.is_absolute() or ".." in pure.parts or not pure.parts or pure.parts[0] != "files":
        raise ValueError("Outbox entry has an unsafe path")
    return str(pure)


def pull(store: Store, transport: Transport, *, source_key: str, destination: Path, dry_run: bool = False) -> dict:
    """Pull every outbox entry not already recorded as pulled. Idempotent."""
    state_key = "pull_audio:" + source_key
    state = store.state(state_key, {"pulled": {}})
    pulled = state.setdefault("pulled", {})
    entries = read_entries(transport.read_index())
    result = {"source": source_key, "destination": str(destination), "available": len(entries), "pulled": [], "skipped": 0, "failed": []}
    pending = []
    for entry in entries:
        if entry["id"] in pulled:
            result["skipped"] += 1
        else:
            pending.append(entry)
    if dry_run:
        result["would_pull"] = [{"title": e.get("title", ""), "filename": e["filename"]} for e in pending]
        return result
    staging = store.root / "pull-staging"
    staging.mkdir(parents=True, exist_ok=True)
    for entry in pending:
        label = entry.get("title") or entry["filename"]
        temporary = staging / (uuid.uuid4().hex + ".part")
        try:
            relative = _safe_relative(entry["path"])
            digest = entry["sha256"]
            filename = Path(entry["filename"]).name
            if filename != entry["filename"] or not filename:
                raise ValueError("Outbox entry has an unsafe filename")
            transport.fetch(relative, temporary)
            if sha256_file(temporary) != digest:
                raise RuntimeError("Checksum mismatch; the file was not copied")
            placed = place_file(temporary, destination / filename, digest)
            pulled[entry["id"]] = {"sha256": digest, "filename": filename, "local": str(placed), "pulled_at": now()}
            store.set_state(state_key, state)  # persist each success so an interruption loses nothing
            result["pulled"].append({"title": entry.get("title", ""), "filename": placed.name})
        except Exception as exc:
            result["failed"].append({"title": label, "filename": entry.get("filename", ""), "error": str(exc)})
        finally:
            temporary.unlink(missing_ok=True)
    return result


def run_pull(store: Store, *, host: str, runtime_path: str = DEFAULT_REMOTE_RUNTIME, dest: str | None = None,
             dry_run: bool = False, transport: Transport | None = None) -> dict:
    folder = dest or store.settings().get("icloud_folder")
    if not folder:
        raise ValueError("Set the iCloud folder first (za settings icloud_folder <path>) or pass --dest")
    folder = Path(folder).expanduser()
    if not folder.is_absolute():
        raise ValueError("Choose an absolute destination folder")
    with (store.root / "pull-audio.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {"source": host, "already_running": True, "pulled": [], "failed": []}
        return pull(store, transport or SshTransport(host, runtime_path), source_key=f"{host}:{runtime_path}",
                    destination=folder, dry_run=dry_run)


def render_launch_agent(store: Store, *, host: str, runtime_path: str, dest: str | None = None, interval: int = 900,
                        python: str | None = None) -> dict:
    """launchd definition: run at load/login and every ``interval`` seconds (catches up after wake)."""
    arguments = [python or sys.executable, "-m", "zotero_audio.app_cli", "pull-audio", "--from", host, "--runtime-path", runtime_path]
    if dest:
        arguments += ["--dest", dest]
    return {
        "Label": PULL_LABEL,
        "ProgramArguments": arguments,
        "RunAtLoad": True,
        "StartInterval": max(60, int(interval)),
        "ProcessType": "Background",
        "LowPriorityIO": True,
        "EnvironmentVariables": {"ZOTERO_AUDIO_RUNTIME": str(store.runtime), "PYTHONUNBUFFERED": "1",
                                 "PATH": os.environ.get("PATH", os.defpath)},
        "StandardOutPath": str(store.root / "pull-audio.log"),
        "StandardErrorPath": str(store.root / "pull-audio.log"),
    }


def _launchctl(*arguments: str):
    return subprocess.run(["launchctl", *arguments], check=False, capture_output=True, text=True)


def agent_path() -> Path:
    return Path.home() / "Library/LaunchAgents" / (PULL_LABEL + ".plist")


def install_agent(store: Store, *, host: str, runtime_path: str, dest: str | None = None, interval: int = 900,
                  launchctl=_launchctl) -> dict:
    if sys.platform != "darwin":
        raise RuntimeError("pull-audio --install uses macOS launchd")
    target = agent_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("wb") as output:
        plistlib.dump(render_launch_agent(store, host=host, runtime_path=runtime_path, dest=dest, interval=interval), output)
    domain = f"gui/{os.getuid()}"
    launchctl("bootout", f"{domain}/{PULL_LABEL}")  # replace an older definition; absent is fine
    result = launchctl("bootstrap", domain, str(target))
    if result.returncode:
        raise RuntimeError(f"launchctl bootstrap failed: {result.stderr.strip()}")
    return {"installed": True, "label": PULL_LABEL, "plist": str(target), "interval_seconds": max(60, int(interval)),
            "log": str(store.root / "pull-audio.log")}


def uninstall_agent(*, launchctl=_launchctl) -> dict:
    target = agent_path()
    launchctl("bootout", f"gui/{os.getuid()}/{PULL_LABEL}")
    existed = target.exists()
    target.unlink(missing_ok=True)
    return {"uninstalled": True, "label": PULL_LABEL, "plist_removed": existed}
