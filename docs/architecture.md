# Architecture

`zotero-audio` is a local, macOS-oriented pipeline with four explicit stages.

Zotero remains the source of truth for library concerns. The pipeline prefers
Zotero's local API (`localhost:23119`) for attachment discovery, file paths,
parent-item metadata, collections, and tags. Direct SQLite/storage access is a
compatibility fallback rather than the normal integration. This removes the
need to couple routine runs to Zotero's internal directory layout and supports
linked PDF attachments as well as stored files.

1. **Extract** resolves one PDF directly or through Zotero's local API, hashes
   the source, extracts page text, and writes provenance-preserving Markdown
   plus a machine-readable structure document.
2. **Plan** turns the Markdown into stable, sentence-aware speech segments.
   Every segment records its text hash and source page span.
3. **Synthesize** writes one lossless audio file per segment. A segment is
   reused only when its text, engine, voice, speed, and engine version match the
   manifest.
4. **Assemble** concatenates verified segments, encodes AAC in an M4A container,
   attaches bibliographic and pipeline metadata, and writes a QA report.

The generated bundle is deliberately separate from the source tree:

```text
outputs/<document-id>/
  article.md
  structure.json
  speech-plan.json
  run-manifest.json
  qa-report.json
  audio/
    segments/
    <document-id>.m4a
```

`outputs/`, `work/`, model weights, and caches are ignored by Git. The pipeline
never modifies Zotero storage or the source PDF.

## Design constraints

- Python 3.11+ orchestration with a small required dependency set.
- Local-only synthesis; no document content is sent to a service.
- SHA-256 identities for source files, normalized speech text, segment audio,
  and final output.
- Atomic writes for manifests and generated text.
- Stable segmentation at paragraph and sentence boundaries.
- Explicit tool and model versions in every run manifest.
- Resumption is conservative: stale or unverifiable artifacts are regenerated.
- The baseline extractor is `pypdf`; a higher-fidelity Marker adapter can be
  added later without making its models a baseline cost.

## Zotero-native responsibilities

Zotero is responsible for importing PDFs and retrieving bibliographic metadata,
creating parent items, renaming stored files, organizing items with
collections/tags/saved searches, indexing searchable PDF text, and syncing
library data and attachments. The local API is the supported interface for
external local tools and exposes attachment file URLs without requiring direct
SQLite reads.

The pipeline remains responsible for page-aware speech extraction, reference
and layout filtering, segmentation, local TTS, audio QA, content-addressed
resumption, and podcast/RSS publication. Zotero's full-text endpoint is useful
for search and lightweight integrations, but it does not replace our
page-preserving extraction contract.

Recommended intake is therefore: save the article with Zotero Connector or
drag in the PDF and let Zotero retrieve metadata, then use Zotero collections
and tags to express workflow intent. For example, the existing `Podcast Queue`
collection or `podcast` tag controls publication selection; the generated
audio and podcast artifacts remain outside Zotero because they need delivery
paths, immutable QA manifests, and a public RSS/object-store layout.

## TTS backends

The backend interface produces mono PCM WAV per segment. The repository is
Kokoro-only: Kokoro-82M BF16 through MLX is the quality default on Apple
Silicon, while Kokoro ONNX INT8 remains an explicitly selected compatibility
backend. There is no operating-system TTS backend or automatic fallback.
