from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import unicodedata
from pathlib import Path
from typing import Any, Iterable


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def canonical_json(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def json_digest(data: Any) -> str:
    return sha256_text(canonical_json(data))


def atomic_write_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def atomic_write_json(path: Path, data: Any) -> None:
    atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n")


def load_json(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as stream:
        data = json.load(stream)
    if not isinstance(data, dict):
        raise ValueError(f"Expected a JSON object in {path}")
    return data


def filename_part(value: str, max_length: int = 180) -> str:
    value = unicodedata.normalize("NFKC", value)
    value = value.replace("–", " - ").replace("—", " - ")
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', " - ", value)
    value = re.sub(r"\s+", " ", value).strip(" .")
    return value[:max_length].rstrip(" .") or "document"


def _surname(name: str) -> str:
    clean = re.sub(r"\s+", " ", name.strip())
    if "," in clean:
        return clean.split(",", 1)[0].strip()
    parts = clean.split()
    if len(parts) > 1 and parts[-2].casefold() in {"da", "de", "del", "der", "di", "la", "le", "van", "von"}:
        return " ".join(parts[-2:])
    return parts[-1] if parts else ""


def episode_title(title: str, authors: Iterable[str] = (), year: str | int | None = None,
                  *, institution: str | None = None) -> str:
    """Return the human-facing title shared by audio and document artifacts."""
    names = [_surname(str(author)) for author in authors if str(author).strip()]
    label = names[0] if len(names) == 1 else f"{names[0]} & {names[1]}" if len(names) == 2 else f"{names[0]} et al." if names else ""
    if institution:
        label = institution.strip()
    suffix = " ".join(part for part in (label, f"({str(year).strip()})" if year else "") if part)
    return f"{title.strip()} — {suffix}" if suffix else title.strip()


def readable_markdown_filename(title: str, *, review: bool = False) -> str:
    """Make a safe, human-readable Markdown filename from an episode title."""
    suffix = " — AI review" if review else ""
    return filename_part(f"{title}{suffix}") + ".md"
