# Design System: 1 More Paper Covers
**Project ID:** zotero-audio-cover-system

## 1. Visual Theme & Atmosphere

Quiet editorial modernism: scholarly, tactile, precise, and unmistakably human.
The covers should feel like a small independent academic press, using strong
typography, flat ink, and one restrained abstract form.

The canonical `1 More Paper` show and episode artwork is the user-supplied square
asset at `src/zotero_audio/assets/1_more_paper_cover.png`. Podcast builds copy
that PNG byte-for-byte to every show and episode cover path and record its hash;
the deterministic renderer below remains available for legacy non-podcast cover
requests.

Avoid the visual shorthand of generated technology art: no glossy gradients,
glowing brains, circuit patterns, floating symbols, fake scientific diagrams,
cinematic light, glass effects, or dense surreal collage. Never generate text
inside an image model.

## 2. Color Palette & Roles

- **Warm Paper (#F2EFE6):** Default background; softens the screen and evokes a
  printed journal.
- **Archival Ink (#16212B):** Titles, metadata, borders, and dark motif areas.
- **Brief Cobalt (#2F5BD3):** Edition band and motif accent for Brief covers.
- **Full Vermilion (#D9553F):** Edition band and motif accent for Full Reading
  covers.

Each cover uses Warm Paper, Archival Ink, and exactly one edition accent. Do not
introduce additional colors, gradients, transparency effects, or color themes by
subject.

## 3. Typography Rules

- **Paper title:** Georgia Semibold, left aligned, sentence case as supplied by
  the paper. Use no more than eight lines and never add a shadow,
  outline, or artificial texture.
- **Series, edition, author, and year:** Helvetica Medium or Semibold. Edition
  labels are uppercase with open letter spacing; all other metadata uses normal
  title case.
- **Hierarchy:** The paper title is always the largest element. The edition is
  second, and author/year metadata is quiet but readable.
- **Long titles:** Keep the complete supplied title and reduce type to the
  defined minimum rather than crowding or truncating it. Titles that still do
  not fit fail artwork QA for review.

Use only these two type families. The macOS-targeted renderer resolves exact
system font files and records their names in the artwork manifest.

## 4. Component Stylings

- **Canvas:** 3000 × 3000 px, square, sRGB, with a 16 px Archival Ink frame and a
  144 px safe area.
- **Series label:** Small IBM Plex Sans text at the upper left. It stays in the
  same position on every cover.
- **Edition band:** A simple solid rectangle labeled `BRIEF` in Brief Cobalt or
  `FULL READING` in Full Vermilion.
- **Title block:** Occupies the upper-left portion of the grid with generous
  whitespace and no decorative container.
- **Author line:** First-author surname plus `et al.` when applicable, followed
  by the paper's publication year. Place it at the lower left.
- **Paper motif:** One text-free abstract form in the lower-right area, using no
  more than three geometric or cut-paper shapes. The same motif is used for the
  paired Brief and Full Reading covers.

If AI is used, it may create only the raw motif. Crop it, remove its background,
and reduce it to Archival Ink plus the edition accent before composition. Reject
motifs containing text, logos, people, recognizable copyrighted figures,
scientific claims, or visible generation artifacts. A deterministic geometric
motif generated from the paper GUID is the permanent fallback.

The design is flat: no shadows, bevels, simulated depth, glow, reflections, or
rounded app-card styling. A subtle monochrome paper grain is permitted at 2%
opacity and must not reduce text contrast.

## 5. Layout Principles

Use a six-column grid with a 48 px baseline rhythm. Align the series label,
title, edition band, and author line to one left edge. Preserve at least one
third of the canvas as quiet space; the motif supports the title and never
competes with it.

Brief and Full Reading covers must be recognizable as a pair: identical title
layout and motif, with only the edition label and accent color changing. The
show-level covers use the same frame, typography, and palette without
paper-specific metadata.

Every export must remain legible at 160 × 160 px, contain no image-generated
text, and match this palette and grid. If a motif fails those checks, use the
deterministic fallback rather than repairing it manually.
