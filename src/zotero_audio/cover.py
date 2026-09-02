"""Deterministic editorial covers implementing the root DESIGN.md contract."""
from __future__ import annotations

import hashlib
import textwrap
from pathlib import Path

from .util import atomic_write_json, atomic_write_text, sha256_file, sha256_text

DESIGN_VERSION = "open-paper-audio/v1"
PALETTE = {"paper": "#F2EFE6", "ink": "#16212B", "brief": "#2F5BD3", "full": "#D9553F"}


def _font_path(serif: bool) -> Path:
    candidates = (
        [Path("/System/Library/Fonts/Supplemental/Georgia.ttf"), Path("/usr/share/fonts/truetype/dejavu/DejaVuSerif.ttf")]
        if serif else
        [Path("/System/Library/Fonts/Helvetica.ttc"), Path("/System/Library/Fonts/Supplemental/Arial.ttf"),
         Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")]
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise RuntimeError("No supported local cover-art font is installed")


def _wrap(draw, text: str, font, max_width: int) -> list[str]:
    lines: list[str] = []
    current = ""
    for word in text.strip().split():
        candidate = f"{current} {word}".strip()
        if current and draw.textbbox((0, 0), candidate, font=font)[2] > max_width:
            lines.append(current)
            current = word
        else:
            current = candidate
    if current:
        lines.append(current)
    return lines


def _title_layout(draw, title: str, max_width: int, max_height: int, scale: float):
    from PIL import ImageFont
    path = _font_path(True)
    for base_size in range(118, 59, -2):
        font = ImageFont.truetype(str(path), max(1, round(base_size * scale)))
        lines = _wrap(draw, title, font, max_width)
        line_height = round(base_size * 1.18 * scale)
        if lines and len(lines) <= 8 and len(lines) * line_height <= max_height:
            return font, lines, line_height, path
    raise ValueError("Paper title is too long for the cover safe area; review it instead of truncating it")


def render_cover(title: str, *, edition: str = "brief", authors: str = "", year: str | int | None = None,
                 destination: Path | None = None, size: int = 3000) -> Path | str:
    """Render all typography locally and a stable, flat, three-shape motif."""
    if edition not in ("brief", "full"):
        raise ValueError("edition must be brief or full")
    if not title.strip():
        raise ValueError("cover title cannot be empty")
    if size < 1200:
        raise ValueError("cover size must be at least 1200 pixels")
    if destination is None or destination.suffix.casefold() == ".svg":
        svg = _svg(title, edition, authors, year, size)
        if destination is None:
            return svg
        destination.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(destination, svg)
        return destination

    if destination.suffix.casefold() != ".png":
        raise ValueError("cover destination must be .png or .svg")
    from PIL import Image, ImageDraw, ImageFont

    scale = size / 3000
    px = lambda value: round(value * scale)
    image = Image.new("RGB", (size, size), PALETTE["paper"])
    draw = ImageDraw.Draw(image)
    ink, accent = PALETTE["ink"], PALETTE[edition]
    sans_path = _font_path(False)
    title_font, lines, line_height, title_path = _title_layout(draw, title, px(1640), px(1120), scale)
    series_font = ImageFont.truetype(str(sans_path), px(42))
    band_font = ImageFont.truetype(str(sans_path), px(44))
    meta_font = ImageFont.truetype(str(sans_path), px(40))
    draw.rectangle((px(16), px(16), size - px(16), size - px(16)), outline=ink, width=px(16))
    draw.text((px(144), px(130)), "OPEN PAPER AUDIO", fill=ink, font=series_font)
    draw.rectangle((px(144), px(250), px(720 if edition == "full" else 570), px(360)), fill=accent)
    draw.text((px(180), px(278)), "FULL READING" if edition == "full" else "BRIEF", fill=PALETTE["paper"], font=band_font)
    for index, line in enumerate(lines):
        draw.text((px(144), px(500) + index * line_height), line, fill=ink, font=title_font)

    digest = hashlib.sha256(f"{DESIGN_VERSION}:{title}".encode()).digest()
    x, y = px(2140 + digest[0] % 170), px(2040 + digest[1] % 150)
    draw.ellipse((x - px(420), y - px(420), x + px(420), y + px(420)), fill=ink)
    draw.polygon(((x - px(360), y - px(120)), (x + px(310), y - px(350)),
                  (x + px(370), y - px(100)), (x - px(300), y + px(130))), fill=accent)
    draw.ellipse((x + px(160), y + px(115), x + px(350), y + px(305)), fill=PALETTE["paper"])

    meta = " · ".join(part for part in (authors.strip(), str(year).strip() if year else "") if part)
    if meta:
        if draw.textbbox((0, 0), meta, font=meta_font)[2] > size - px(288):
            meta = textwrap.shorten(meta, width=92, placeholder=" …")
        draw.text((px(144), px(2730)), meta, fill=ink, font=meta_font)
    destination.parent.mkdir(parents=True, exist_ok=True)
    image.save(destination, format="PNG", optimize=False)
    with Image.open(destination) as rendered:
        if rendered.size != (size, size) or rendered.mode != "RGB":
            raise RuntimeError("rendered cover failed dimensions or color-mode QA")
    atomic_write_json(destination.with_suffix(".artwork.json"), {
        "schema": "zotero-audio-artwork/v1", "design_version": DESIGN_VERSION,
        "generation": "deterministic-geometric", "edition": edition, "title_sha256": sha256_text(title),
        "palette": PALETTE, "title_font": title_path.name, "sans_font": sans_path.name,
        "width": size, "height": size, "sha256": sha256_file(destination),
    })
    return destination


def _svg(title: str, edition: str, authors: str, year: str | int | None, size: int) -> str:
    import xml.sax.saxutils as sax
    scale = size / 3000
    p = lambda value: round(value * scale)
    accent = PALETTE[edition]
    digest = hashlib.sha256(f"{DESIGN_VERSION}:{title}".encode()).digest()
    x, y = p(2140 + digest[0] % 170), p(2040 + digest[1] % 150)
    lines: list[str] = []
    current = ""
    for word in title.split():
        candidate = f"{current} {word}".strip()
        if current and len(candidate) > 30:
            lines.append(current)
            current = word
        else:
            current = candidate
    if current:
        lines.append(current)
    if len(lines) > 8:
        raise ValueError("Paper title is too long for the cover safe area")
    title_nodes = "".join(f'<text x="{p(144)}" y="{p(560 + i * 132)}">{sax.escape(line)}</text>' for i, line in enumerate(lines))
    meta = sax.escape(" · ".join(part for part in (authors, str(year) if year else "") if part))
    label = "FULL READING" if edition == "full" else "BRIEF"
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{size}" height="{size}" viewBox="0 0 {size} {size}">'
            f'<rect width="100%" height="100%" fill="{PALETTE["paper"]}"/><rect x="{p(16)}" y="{p(16)}" width="{size-p(32)}" height="{size-p(32)}" fill="none" stroke="{PALETTE["ink"]}" stroke-width="{p(16)}"/>'
            f'<text x="{p(144)}" y="{p(180)}" font-family="Helvetica,Arial,sans-serif" font-size="{p(42)}" letter-spacing="{p(7)}" fill="{PALETTE["ink"]}">OPEN PAPER AUDIO</text>'
            f'<rect x="{p(144)}" y="{p(250)}" width="{p(576 if edition=="full" else 426)}" height="{p(110)}" fill="{accent}"/><text x="{p(180)}" y="{p(325)}" font-family="Helvetica,Arial,sans-serif" font-size="{p(44)}" fill="{PALETTE["paper"]}">{label}</text>'
            f'<g font-family="Georgia,serif" font-size="{p(112)}" font-weight="600" fill="{PALETTE["ink"]}">{title_nodes}</g>'
            f'<circle cx="{x}" cy="{y}" r="{p(420)}" fill="{PALETTE["ink"]}"/><path d="M{x-p(360)} {y-p(120)} L{x+p(310)} {y-p(350)} L{x+p(370)} {y-p(100)} L{x-p(300)} {y+p(130)}Z" fill="{accent}"/><circle cx="{x+p(255)}" cy="{y+p(210)}" r="{p(95)}" fill="{PALETTE["paper"]}"/>'
            f'<text x="{p(144)}" y="{p(2770)}" font-family="Helvetica,Arial,sans-serif" font-size="{p(40)}" fill="{PALETTE["ink"]}">{meta}</text></svg>')
