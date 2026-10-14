#!/usr/bin/env python3
"""Rewrite absolute paths after copying a runtime to another host.

The catalog, publication manifest and per-article JSON record absolute paths
(for example ``/Users/<name>/Sites/zotero-audio-runtime/...``). After the
runtime is copied to a host where that path cannot exist (a Linux service
host), rewrite the old prefix to the new runtime path.

Run it on the COPY, never on the source runtime; the source remains the
backup until cutover. It is a dry run unless ``--apply`` is given, and it
reports every remaining occurrence afterwards.

    python scripts/migrate_runtime_paths.py --runtime ~/1mp/runtime \
        --old-prefix /Users/pesh/Sites/zotero-audio-runtime [--apply]
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path

TEXT_SUFFIXES = {".json", ".jsonl", ".toml"}
# Historical logs and published/backup snapshots are left exactly as they were.
SKIP_DIRS = {"public", "public-staging", "feed-snapshots", "feed-transaction-backup", "audio", "outbox"}
SKIP_NAMES = {"events.jsonl"}
FTS_SHADOW_SUFFIXES = ("_data", "_idx", "_docsize", "_config", "_content")


def _text_files(runtime: Path):
    for root, dirs, files in os.walk(runtime):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.endswith("backups") and not d.endswith("-backup")]
        for name in files:
            path = Path(root) / name
            if path.suffix in TEXT_SUFFIXES and name not in SKIP_NAMES and not path.is_symlink():
                yield path


def _symlinks(runtime: Path):
    for root, dirs, files in os.walk(runtime):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for name in dirs + files:
            path = Path(root) / name
            if path.is_symlink():
                yield path


def _tables(db: sqlite3.Connection) -> list[tuple[str, list[str]]]:
    rows = db.execute("SELECT name, sql FROM sqlite_master WHERE type='table'").fetchall()
    fts = {name for name, sql in rows if sql and "VIRTUAL TABLE" in sql.upper()}
    result = []
    for name, _ in rows:
        if name.startswith("sqlite_") or any(name == f"{base}{suffix}" for base in fts for suffix in FTS_SHADOW_SUFFIXES):
            continue
        columns = [row[1] for row in db.execute(f'PRAGMA table_info("{name}")')]
        result.append((name, columns))
    return result


def _count_sqlite(db: sqlite3.Connection, old: str) -> dict[str, int]:
    counts = {}
    for table, columns in _tables(db):
        total = 0
        for column in columns:
            total += db.execute(f'SELECT count(*) FROM "{table}" WHERE CAST("{column}" AS TEXT) LIKE ?',
                                (f"%{old}%",)).fetchone()[0]
        if total:
            counts[table] = total
    return counts


def migrate(runtime: Path, old: str, new: str, apply: bool) -> dict:
    report: dict = {"runtime": str(runtime), "old_prefix": old, "new_prefix": new, "applied": apply,
                    "sqlite_rows": {}, "files": 0, "symlinks": 0, "remaining": {}}
    database = runtime / "control" / "library.sqlite3"
    with sqlite3.connect(database) as db:
        report["sqlite_rows"] = _count_sqlite(db, old)
        if apply:
            for table, columns in _tables(db):
                for column in columns:
                    db.execute(f'UPDATE "{table}" SET "{column}" = replace("{column}", ?, ?) '
                               f'WHERE typeof("{column}")=\'text\' AND instr("{column}", ?) > 0', (old, new, old))
            db.commit()
            report["remaining"]["sqlite"] = _count_sqlite(db, old)
    for path in _text_files(runtime):
        text = path.read_text(encoding="utf-8", errors="strict")
        if old not in text:
            continue
        report["files"] += 1
        if apply:
            updated = text.replace(old, new)
            if path.suffix == ".json":
                json.loads(updated)  # never write a file that stopped being valid JSON
            temporary = path.with_name(f".{path.name}.migrating")
            temporary.write_text(updated, encoding="utf-8")
            os.replace(temporary, path)
    for link in _symlinks(runtime):
        target = os.readlink(link)
        if target.startswith(old):
            report["symlinks"] += 1
            if apply:
                link.unlink()
                link.symlink_to(new + target[len(old):])
    if apply:
        remaining = [str(p.relative_to(runtime)) for p in _text_files(runtime) if old in p.read_text(encoding="utf-8")]
        report["remaining"]["files"] = remaining
        report["remaining"]["symlinks"] = [str(l.relative_to(runtime)) for l in _symlinks(runtime) if os.readlink(l).startswith(old)]
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--runtime", type=Path, required=True, help="The COPIED runtime on the new host")
    parser.add_argument("--old-prefix", required=True, help="Runtime path on the original host")
    parser.add_argument("--new-prefix", help="Defaults to the resolved --runtime path")
    parser.add_argument("--apply", action="store_true", help="Write changes (default: dry run)")
    args = parser.parse_args(argv)
    runtime = args.runtime.expanduser().resolve()
    new = args.new_prefix or str(runtime)
    old = args.old_prefix.rstrip("/")
    if old == new.rstrip("/"):
        parser.error("old and new prefixes are identical")
    if not (runtime / "control" / "library.sqlite3").is_file():
        parser.error(f"{runtime} is not a runtime (control/library.sqlite3 missing)")
    report = migrate(runtime, old, new.rstrip("/"), args.apply)
    print(json.dumps(report, indent=2))
    remaining = report["remaining"]
    return 1 if args.apply and (remaining.get("sqlite") or remaining.get("files") or remaining.get("symlinks")) else 0


if __name__ == "__main__":
    sys.exit(main())
