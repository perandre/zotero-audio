#!/usr/bin/env python3
"""Package the local Zotero license add-on outside the source checkout."""
import argparse
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path, help="Destination .xpi file outside the repository")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    output = args.output.expanduser().resolve()
    if output.is_relative_to(root):
        parser.error("Write generated packages outside the repository")
    output.parent.mkdir(parents=True, exist_ok=True)
    with ZipFile(output, "w", compression=ZIP_DEFLATED) as archive:
        for name in ("manifest.json", "bootstrap.js", "license-status.js"):
            archive.write(root / "zotero-addon" / name, name)
    print(output)


if __name__ == "__main__":
    main()
