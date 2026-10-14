"""Fast per-user service installation (macOS launchd, Linux systemd --user); models and generated files stay in runtime."""
from __future__ import annotations

import os
import plistlib
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

from .app_state import Store


SERVICE_LABEL = "com.pesh.one-more-paper"
SYSTEMD_UNIT = "one-more-paper.service"


def service_manager() -> str | None:
    """launchd on macOS, systemd user units on Linux, otherwise None (run `za serve`)."""
    if sys.platform == "darwin":
        return "launchd"
    if sys.platform.startswith("linux") and shutil.which("systemctl"):
        return "systemd"
    return None


def _systemd_quote(value: str) -> str:
    """Quote one systemd.syntax word: escape backslash, quotes and %/$ expansion characters."""
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%").replace("$", "$$")
    return f'"{escaped}"'


def unit_path() -> Path:
    return Path.home() / ".config/systemd/user" / SYSTEMD_UNIT


def render_systemd_unit(store: Store, *, python: str, project: Path, path_env: str) -> str:
    """The unit file text; pure so it can be tested without systemd."""
    # Environment= is subject to specifier (%) expansion only, not $ expansion.
    def environment(key: str, value: str) -> str:
        text = f"{key}={value}".replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")
        return f'Environment="{text}"'
    return "\n".join([
        "[Unit]",
        "Description=1 More Paper worker (Zotero monitor, generation, delivery, local API)",
        "After=network-online.target",
        "Wants=network-online.target",
        "",
        "[Service]",
        "Type=simple",
        f"ExecStart={_systemd_quote(python)} -m zotero_audio.app_cli serve",
        f"WorkingDirectory={str(project).replace(chr(37), chr(37) * 2)}",
        environment("ZOTERO_AUDIO_RUNTIME", str(store.runtime)),
        environment("PYTHONUNBUFFERED", "1"),
        environment("PATH", path_env),
        # Clean exits (za stop, SIGTERM) are not restarted; crashes are.
        "Restart=on-failure",
        "RestartSec=10",
        # Let an atomic generation stage reach its checkpoint before systemd escalates.
        "TimeoutStopSec=300",
        f"StandardOutput=append:{str(store.root / 'daemon.log').replace(chr(37), chr(37) * 2)}",
        f"StandardError=append:{str(store.root / 'daemon.stderr.log').replace(chr(37), chr(37) * 2)}",
        "",
        "[Install]",
        "WantedBy=default.target",
        "",
    ])


def _systemctl(*arguments: str, check=True):
    result = subprocess.run(["systemctl", "--user", *arguments], check=False, capture_output=True, text=True)
    if check and result.returncode:
        raise RuntimeError(f"systemctl --user {arguments[0]} failed: {result.stderr.strip() or 'the service could not be updated'}")
    return result


def linger_enabled() -> bool | None:
    """Whether the user's services survive without a login session (None if unknown)."""
    loginctl = shutil.which("loginctl")
    if not loginctl:
        return None
    result = subprocess.run([loginctl, "show-user", str(os.getuid()), "--property=Linger", "--value"],
                            check=False, capture_output=True, text=True)
    if result.returncode:
        return None
    return result.stdout.strip().lower() == "yes"


def installed_unit(store: Store) -> Path | None:
    """Only control the unit that belongs to this runtime."""
    if service_manager() != "systemd":
        return None
    target = unit_path()
    try:
        lines = target.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    wanted = f'Environment="ZOTERO_AUDIO_RUNTIME={str(store.runtime)}"'
    return target if wanted in lines else None


def service_status(store: Store) -> dict:
    manager = service_manager()
    info = {"manager": manager, "installed": False, "active": None}
    if manager == "launchd":
        info["installed"] = installed_agent(store) is not None
        if info["installed"]:
            info["active"] = _launchctl("print", service_target(), check=False).returncode == 0
    elif manager == "systemd":
        info["installed"] = installed_unit(store) is not None
        if info["installed"]:
            info["active"] = _systemctl("is-active", SYSTEMD_UNIT, check=False).stdout.strip() == "active"
        info["linger"] = linger_enabled()
    return info


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
    if sys.platform != "darwin":
        # systemd does not restart a clean SIGTERM exit (Restart=on-failure), so nothing to suspend.
        return installed_unit(store) is not None
    if not installed_agent(store):
        return False
    _launchctl("disable", service_target())
    return True


def unload_installed_service(store: Store):
    if sys.platform != "darwin":
        if installed_unit(store) and _systemctl("is-active", SYSTEMD_UNIT, check=False).stdout.strip() in {"active", "activating"}:
            _systemctl("stop", SYSTEMD_UNIT)
        return
    if not installed_agent(store):
        return
    target = service_target()
    if _launchctl("print", target, check=False).returncode == 0:
        result = _launchctl("bootout", target, check=False)
        if result.returncode and _launchctl("print", target, check=False).returncode == 0:
            raise RuntimeError("The background service could not be unloaded. Check za status before restarting.")


def start_installed_service(store: Store) -> bool:
    if sys.platform != "darwin":
        if not installed_unit(store):
            return False
        _systemctl("enable", SYSTEMD_UNIT)
        _systemctl("start", SYSTEMD_UNIT)  # no-op when already running; never kills an active stage
        return True
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


def _install_launcher(candidates) -> Path | None:
    executable = Path(sys.executable).parent / "za"
    for candidate in candidates:
        if candidate == Path.home() / ".local/bin" and str(candidate) not in os.environ.get("PATH", "").split(os.pathsep):
            continue
        if candidate.exists() and os.access(candidate, os.W_OK):
            link = candidate / "za"
            if link.exists() or link.is_symlink():
                if link.resolve() == executable.resolve():
                    return link
                continue
            link.symlink_to(executable)
            return link
    return None


def install_systemd(store: Store):
    from .app_cli import stop_daemon
    stop_daemon(store)
    project = Path(__file__).resolve().parents[2]
    target = unit_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(render_systemd_unit(store, python=sys.executable, project=project,
                                          path_env=os.environ.get("PATH", os.defpath)), encoding="utf-8")
    _systemctl("daemon-reload")
    start_installed_service(store)
    launcher = _install_launcher([Path.home() / ".local/bin"])
    linger = linger_enabled()
    note = "Use za status, za restart or za stop. Models and cached audio were preserved."
    if linger is not True:
        note += (" Linger is not enabled, so the service stops when your last login session ends: run "
                 f"`sudo loginctl enable-linger {os.environ.get('USER') or 'your-user'}` once to keep it running unattended.")
    return {"installed": True, "service": SYSTEMD_UNIT, "service_manager": "systemd", "unit": str(target),
            "dashboard": "http://127.0.0.1:8765", "command": str(launcher or Path(sys.executable).parent / "za"),
            "linger": linger, "note": note}


def install(store: Store):
    if sys.platform != "darwin":
        if service_manager() == "systemd":
            return install_systemd(store)
        raise RuntimeError("Background installation uses macOS launchd or Linux systemd --user; run za serve on other systems")
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
