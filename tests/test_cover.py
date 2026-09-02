from pathlib import Path

from PIL import Image

from zotero_audio.cover import PALETTE, render_cover
from zotero_audio.util import load_json, sha256_file


def test_cover_is_deterministic_square_rgb_and_manifested(tmp_path: Path):
    first, second = tmp_path / "first.png", tmp_path / "second.png"
    render_cover("A Serious Paper About Listening", edition="brief", authors="Smith et al.", year=2026, destination=first)
    render_cover("A Serious Paper About Listening", edition="brief", authors="Smith et al.", year=2026, destination=second)
    assert sha256_file(first) == sha256_file(second)
    with Image.open(first) as image:
        assert image.size == (3000, 3000) and image.mode == "RGB"
        assert image.getpixel((1500, 1500)) in {(242, 239, 230), (22, 33, 43), (47, 91, 211)}
    manifest = load_json(first.with_suffix(".artwork.json"))
    assert manifest["sha256"] == sha256_file(first) and manifest["palette"] == PALETTE


def test_paired_covers_share_system_but_not_accent(tmp_path: Path):
    brief, full = tmp_path / "brief.png", tmp_path / "full.png"
    render_cover("Paper", edition="brief", destination=brief)
    render_cover("Paper", edition="full", destination=full)
    assert sha256_file(brief) != sha256_file(full)
