"""Fast per-user macOS installation; models and generated files stay in runtime."""
from __future__ import annotations

import os
import plistlib
import subprocess
import sys
from pathlib import Path

from .app_state import Store


SERVICE_LABEL = "com.pesh.one-more-paper"


def installed_agent(store: Store) -> Path | None:
    """Only control the launch agent belonging to this runtime, including in tests."""
    if sys.platform != "darwin":
        return None
    target = Path.home() / "Library/LaunchAgents" / (SERVICE_LABEL + ".plist")
    try:
        with target.open("rb") as source:
            config = plistlib.load(source)
        runtime = config.get("EnvironmentVariables", {}).get("ZOTERO_AUDIO_RUNTIME")
        if config.get("Label") == SERVICE_LABEL and runtime and Path(runtime).expanduser().resolve() == store.runtime:
            return target
    except (OSError, ValueError, plistlib.InvalidFileException):
        pass
    return None


def service_target() -> str:
    return f"gui/{os.getuid()}/{SERVICE_LABEL}"


def _launchctl(*arguments: str, check=True):
    result = subprocess.run(["launchctl", *arguments], check=False, capture_output=True, text=True)
    if check and result.returncode:
        raise RuntimeError(f"launchctl {arguments[0]} failed: {result.stderr.strip() or 'the service could not be updated'}")
    return result


def disable_installed_service(store: Store) -> bool:
    if not installed_agent(store):
        return False
    _launchctl("disable", service_target())
    return True


def unload_installed_service(store: Store):
    if not installed_agent(store):
        return
    target = service_target()
    if _launchctl("print", target, check=False).returncode == 0:
        result = _launchctl("bootout", target, check=False)
        if result.returncode and _launchctl("print", target, check=False).returncode == 0:
            raise RuntimeError("The background service could not be unloaded. Check za status before restarting.")


def start_installed_service(store: Store) -> bool:
    target = installed_agent(store)
    if not target:
        return False
    service = service_target()
    _launchctl("enable", service)
    if _launchctl("print", service, check=False).returncode:
        _launchctl("bootstrap", f"gui/{os.getuid()}", str(target))
    else:
        # Do not use -k: it can kill an active stage instead of waiting for its checkpoint.
        _launchctl("kickstart", service)
    return True


def install(store: Store):
    if sys.platform != "darwin":
        raise RuntimeError("Background installation uses macOS launchd; run za serve on other systems")
    from .app_cli import stop_daemon
    stop_daemon(store)
    project = Path(__file__).resolve().parents[2]
    launch_agents = Path.home() / "Library/LaunchAgents"
    launch_agents.mkdir(exist_ok=True, parents=True)
    label = SERVICE_LABEL
    target = launch_agents / (label + ".plist")
    domain = f"gui/{os.getuid()}"
    # Disable the older scheduler only at cutover. Its config and outputs remain.
    legacy_label = "com.pesh.zotero-audio"
    legacy = launch_agents / (legacy_label + ".plist")
    if legacy.is_file():
        subprocess.run(["launchctl", "disable", f"{domain}/{legacy_label}"], check=False, capture_output=True)
        subprocess.run(["launchctl", "bootout", domain, str(legacy)], check=False, capture_output=True)
    config = {
        "Label": label,
        "ProgramArguments": [sys.executable, "-m", "zotero_audio.app_cli", "serve"],
        "WorkingDirectory": str(project),
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},
        "ThrottleInterval": 10,
        "EnvironmentVariables": {"ZOTERO_AUDIO_RUNTIME": str(store.runtime), "PYTHONUNBUFFERED": "1", "PATH": os.environ.get("PATH", os.defpath)},
        "StandardOutPath": str(store.root / "daemon.log"),
        "StandardErrorPath": str(store.root / "daemon.stderr.log"),
    }
    with target.open("wb") as output:
        plistlib.dump(config, output)
    start_installed_service(store)
    executable = Path(sys.executable).parent / "za"
    launcher = None
    for candidate in (Path.home() / ".local/bin", Path("/opt/homebrew/bin")):
        if candidate == Path.home() / ".local/bin" and str(candidate) not in os.environ.get("PATH", "").split(os.pathsep):
            continue
        if candidate.exists() and os.access(candidate, os.W_OK):
            link = candidate / "za"
            if link.exists() or link.is_symlink():
                if link.resolve() == executable.resolve():
                    launcher = link
                    break
                continue
            link.symlink_to(executable)
            launcher = link
            break
    return {"installed": True, "service": label, "dashboard": "http://127.0.0.1:8765", "command": str(launcher or executable),
            "note": "Use za dashboard, za markdown new, or za status. Models and cached audio were preserved."}
