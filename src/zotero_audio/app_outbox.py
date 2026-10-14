"""Durable outbox for finished private audio when iCloud is not reachable.

The always-on server (normally Linux) cannot write to iCloud Drive. Instead the
worker copies each finished private edition into ``<runtime>/outbox`` and records
it in an append-only index. A laptop later pulls new entries (``za pull-audio``).
Generation never waits for the laptop, and nothing is deleted until an operator
chooses to prune it.

Layout::

    outbox/index.jsonl                      one JSON object per line, append-only
    outbox/files/<sha256>/<readable name>   the immutable finished audio
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import shutil
import sys
import uuid
from pathlib import Path

SCHEMA = "one-more-paper-outbox/v1"
MODES = {"auto", "icloud", "outbox"}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        for chunk in iter(lambda: source.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def private_audio_mode(runtime: Path, settings: dict) -> str:
    """Return "icloud", "outbox" or "none" for private audio.

    ``control/delivery.json`` {"private_audio": "outbox"|"icloud"|"auto"} is an
    explicit local choice. Automatic: macOS with a configured folder keeps the
    historical iCloud copy; macOS without one stays "none" (unchanged); any other
    system cannot reach iCloud, so the outbox is used (a synced Mac folder path
    is meaningless there).
    """
    choice = "auto"
    try:
        value = json.loads((Path(runtime) / "control" / "delivery.json").read_text(encoding="utf-8")).get("private_audio", "auto")
        if value in MODES:
            choice = value
    except (OSError, ValueError, AttributeError):
        pass
    if choice == "outbox":
        return "outbox"
    if choice == "icloud":
        return "icloud" if settings.get("icloud_folder") else "none"
    if sys.platform != "darwin":
        return "outbox"
    return "icloud" if settings.get("icloud_folder") else "none"


def outbox_root(runtime: Path) -> Path:
    return Path(runtime) / "outbox"


def entry_id(sha256: str, filename: str) -> str:
    return hashlib.sha256(f"{sha256}\0{filename}".encode()).hexdigest()[:24]


def read_entries(text: str) -> list[dict]:
    """Parse index text, ignoring a torn line and unknown schemas."""
    entries = {}
    for line in text.splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict) and entry.get("schema") == SCHEMA and entry.get("id"):
            entries.setdefault(entry["id"], entry)
    return list(entries.values())


def list_entries(runtime: Path) -> list[dict]:
    try:
        return read_entries((outbox_root(runtime) / "index.jsonl").read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []


def deliver_to_outbox(runtime: Path, payload: dict, *, article_id: str = "", title: str = "", edition: str = "", now: str = "") -> dict:
    """Atomically copy a finished file into the outbox and record it. Idempotent."""
    source, filename = Path(payload["source"]), payload["filename"]
    if not source.is_file():
        raise FileNotFoundError("The generated source file is not available")
    if "/" in filename or filename in {"", ".", ".."}:
        raise ValueError("Unsafe outbox filename")
    digest = payload.get("sha256") or sha256_file(source)
    root = outbox_root(runtime)
    folder = root / "files" / digest
    target = folder / filename
    folder.mkdir(parents=True, exist_ok=True)
    if not (target.is_file() and sha256_file(target) == digest):
        temporary = folder / f".{uuid.uuid4().hex}.copying"
        try:
            with source.open("rb") as src, temporary.open("wb") as dst:
                while chunk := src.read(1 << 20):
                    dst.write(chunk)
                dst.flush()
                os.fsync(dst.fileno())
            if sha256_file(temporary) != digest:
                raise RuntimeError("Outbox checksum did not match")
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
    entry = {"schema": SCHEMA, "id": entry_id(digest, filename), "sha256": digest, "filename": filename,
             "path": f"files/{digest}/{filename}", "size": target.stat().st_size, "article_id": article_id,
             "title": title, "edition": edition, "created": now}
    with (root / "index.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        index = root / "index.jsonl"
        known = {e["id"] for e in read_entries(index.read_text(encoding="utf-8"))} if index.exists() else set()
        if entry["id"] not in known:
            with index.open("a", encoding="utf-8") as output:
                if index.stat().st_size and not _ends_with_newline(index):
                    output.write("\n")  # repair a torn previous write
                output.write(json.dumps(entry, ensure_ascii=False) + "\n")
                output.flush()
                os.fsync(output.fileno())
    return entry


def _ends_with_newline(path: Path) -> bool:
    with path.open("rb") as source:
        source.seek(-1, os.SEEK_END)
        return source.read(1) == b"\n"


def place_file(source: Path, target: Path, content_hash: str) -> Path:
    """Copy into a user folder atomically, never overwriting a different file.

    Shared by the macOS iCloud delivery and ``za pull-audio``. An identical file
    already at the target (or at a collision-suffixed name) is reused.
    """
    source, target = Path(source), Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    original = target
    collision = 0
    while target.exists():
        if target.stat().st_size == source.stat().st_size and sha256_file(target) == content_hash:
            return target
        collision += 1
        ending = "" if collision == 1 else f"-{collision}"
        target = original.with_name(original.stem + " — " + content_hash[:8] + ending + original.suffix)
    # Temporary name is independent of the (possibly near-limit) target name.
    temporary = target.parent / f".{uuid.uuid4().hex}.copying"
    try:
        shutil.copyfile(source, temporary)
        if sha256_file(temporary) != content_hash:
            raise RuntimeError("Export checksum did not match")
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return target
