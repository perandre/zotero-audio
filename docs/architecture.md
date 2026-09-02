# Architecture

`zotero-audio` is a local, macOS-oriented pipeline with four explicit stages.

1. **Extract** resolves one PDF directly or from Zotero storage, hashes the
   source, extracts page text, and writes provenance-preserving Markdown plus a
   machine-readable structure document.
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

## TTS backends

The backend interface produces mono PCM WAV per segment. The repository is
Kokoro-only: Kokoro-82M BF16 through MLX is the quality default on Apple
Silicon, while Kokoro ONNX INT8 remains an explicitly selected compatibility
backend. There is no operating-system TTS backend or automatic fallback.
