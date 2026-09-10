#!/usr/bin/env python3
"""Evaluate a live Zotero snapshot for the local license-status add-on."""
from __future__ import annotations

import argparse
from pathlib import Path

from zotero_audio.license_status import evaluate_library
from zotero_audio.util import atomic_write_json, load_json


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--bundles", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    args = parser.parse_args()
    result = evaluate_library(load_json(args.input), bundles=args.bundles, evidence=args.evidence)
    atomic_write_json(args.output, result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
