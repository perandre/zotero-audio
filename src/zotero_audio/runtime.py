"""Tool discovery for launchd jobs, which do not load shell profiles."""
from __future__ import annotations

import os
from pathlib import Path


def configure_tool_path() -> None:
    """Preserve the caller's PATH and append installed Homebrew/NVM tools."""
    paths = [part for part in os.environ.get("PATH", os.defpath).split(os.pathsep) if part]
    configured = os.environ.get("ZOTERO_AUDIO_NPX")
    if configured:
        # npx uses /usr/bin/env node, so its sibling node must also be visible.
        paths.insert(0, str(Path(configured).expanduser().parent))
    paths.extend(("/opt/homebrew/bin", "/usr/local/bin"))
    nvm = Path(os.environ.get("NVM_DIR", str(Path.home() / ".nvm")))
    def version(path: Path) -> tuple[int, ...]:
        return tuple(int(part) for part in path.parent.name.lstrip("v").split(".") if part.isdigit())
    paths.extend(str(path) for path in sorted((nvm / "versions/node").glob("v*/bin"), key=version, reverse=True))
    os.environ["PATH"] = os.pathsep.join(dict.fromkeys(path for path in paths if Path(path).is_dir()))
