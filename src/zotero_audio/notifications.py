"""Persist failure notification state across scheduled process restarts."""
from pathlib import Path
from collections.abc import Callable

from .util import atomic_write_json, load_json, sha256_text


def report_failure(path: Path, category: str, error: str | None, message: str,
                   notify: Callable[[str], None], *, enabled: bool = True) -> None:
    state = load_json(path) if path.is_file() else {}
    if error is None:
        state.pop(category, None)
    elif enabled:
        signature = sha256_text(error)
        if state.get(category) != signature:
            notify(message)
            state[category] = signature
    atomic_write_json(path, state)
