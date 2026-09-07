from __future__ import annotations

import re
from typing import Any

from .util import json_digest, sha256_text


SENTENCE_BOUNDARY_RE = re.compile(r"(?<=[.!?])\s+(?=[A-ZÀ-ÖØ-Þ0-9])")


def split_sentences(text: str) -> list[str]:
    protected = text
    placeholders: dict[str, str] = {}
    for index, abbreviation in enumerate(("e.g.", "i.e.", "et al.", "Dr.", "Mr.", "Mrs.", "Prof.")):
        token = f"\uFFF0{index}\uFFF1"
        if abbreviation in protected:
            protected = protected.replace(abbreviation, token)
            placeholders[token] = abbreviation
    sentences = [item.strip() for item in SENTENCE_BOUNDARY_RE.split(protected) if item.strip()]
    return [
        _restore_placeholders(sentence, placeholders)
        for sentence in sentences
    ]


def _restore_placeholders(value: str, placeholders: dict[str, str]) -> str:
    for token, original in placeholders.items():
        value = value.replace(token, original)
    return value


def _hard_split(text: str, max_chars: int) -> list[str]:
    chunks: list[str] = []
    remaining = text.strip()
    while len(remaining) > max_chars:
        window = remaining[: max_chars + 1]
        split_at = max(window.rfind(", "), window.rfind("; "), window.rfind(" "))
        if split_at < max_chars // 2:
            split_at = max_chars
        chunks.append(remaining[:split_at].strip())
        remaining = remaining[split_at:].strip()
    if remaining:
        chunks.append(remaining)
    return chunks


def chunk_text(text: str, max_chars: int, *, preserve_sentences: bool = False) -> list[str]:
    if max_chars < 20:
        raise ValueError("max_chars must be at least 20")
    sentences: list[str] = []
    for sentence in split_sentences(text):
        sentences.extend([sentence] if preserve_sentences else _hard_split(sentence, max_chars))
    chunks: list[str] = []
    current = ""
    for sentence in sentences:
        candidate = f"{current} {sentence}".strip()
        if current and len(candidate) > max_chars:
            chunks.append(current)
            current = sentence
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


def create_speech_plan(structure: dict[str, Any], *, max_chars: int = 900) -> dict[str, Any]:
    segments: list[dict[str, Any]] = []
    title = structure["document"]["title"]
    title_text = f"{title}." if not title.endswith((".", "!", "?")) else title
    segments.append(
        {
            "ordinal": 1,
            "kind": "title",
            "text": title_text,
            "text_sha256": sha256_text(title_text),
            "source_block_ids": [],
            "pdf_pages": [1],
            "pause_after_ms": 700,
        }
    )
    current_section = title
    for block in structure["blocks"]:
        if not block["included_in_reading"]:
            continue
        if block["type"] == "heading":
            current_section = block["text"]
        for chunk in chunk_text(block["text"], max_chars, preserve_sentences=True):
            kind = "heading" if block["type"] == "heading" else "body"
            spoken = chunk
            if kind == "heading" and not spoken.endswith((".", "!", "?")):
                spoken += "."
            segments.append(
                {
                    "ordinal": len(segments) + 1,
                    "kind": kind,
                    "section": current_section,
                    "text": spoken,
                    "text_sha256": sha256_text(spoken),
                    "source_block_ids": [block["id"]],
                    "pdf_pages": [block["pdf_page"]],
                    "pause_after_ms": 500 if kind == "heading" else 240,
                }
            )
    plan: dict[str, Any] = {
        "schema": "zotero-audio-speech-plan/v1",
        "document": structure["document"],
        "source_sha256": structure["source"]["sha256"],
        "structure_sha256": structure["structure_sha256"],
        "segmentation": {
            "algorithm": "paragraph-whole-sentence-v2",
            "max_chars": max_chars,
        },
        "segments": segments,
    }
    plan["plan_sha256"] = json_digest(plan)
    return plan
