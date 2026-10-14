"""Open files and URLs in the user's desktop session (macOS `open`, Linux `xdg-open`)."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path


class DesktopUnavailable(ValueError):
    """Raised on headless hosts; surfaces as a clear message, not a traceback."""


def open_target(target: str | Path, *, reveal: bool = False) -> None:
    target = str(target)
    if sys.platform == "darwin":
        subprocess.run(["/usr/bin/open", *(["-R"] if reveal else []), target], check=True)
        return
    opener = shutil.which("xdg-open")
    has_display = bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
    if not opener or not has_display:
        raise DesktopUnavailable(
            "No desktop session is available on this host. Open the file or dashboard from another machine; "
            f"the path is {target}")
    # xdg-open cannot select a file in a file manager; open its folder instead.
    subprocess.run([opener, str(Path(target).parent) if reveal and "://" not in target else target], check=True)
