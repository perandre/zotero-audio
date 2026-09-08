#!/usr/bin/env python3
"""Synchronize the public mirror using the shared podcast upload implementation."""
from __future__ import annotations

import argparse
from pathlib import Path
from zotero_audio.publish_sync import upload_keys


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--bucket", required=True)
    parser.add_argument("--state-file", type=Path, required=True)
    args = parser.parse_args(argv)
    root = args.root.expanduser().resolve()
    if not root.is_dir():
        raise RuntimeError(f"Public mirror does not exist: {root}")
    keys = [path.relative_to(root).as_posix() for path in root.rglob("*")
            if path.is_file() and not path.name.endswith(".headers.json")]
    upload_keys(root, args.bucket, args.state_file.expanduser().resolve(), keys)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
