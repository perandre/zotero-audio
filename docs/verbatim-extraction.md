# Verbatim PDF extraction and Markdown previews

Article wording must never be paraphrased or rewritten. Headings remain spoken.
Text normalization repairs whitespace and discretionary soft hyphens; it preserves
real hyphens, citations, URLs, numbers, and the authors' grammar and punctuation.

When installed, Poppler's `pdftotext` supplies reading-order text and paragraph
boundaries (`brew install poppler` on macOS). The extractor records the engine and
version. Without Poppler it falls back to pypdf, which may have weaker layout
results. Neither engine guarantees correct table separation or reading order for
every PDF; new full readings still need content review.

Confidently bounded PDF abstracts take precedence over bibliographic abstract
metadata, which can contain stale extraction artifacts. Markdown transcripts are
built from speech-plan source paragraphs, not timed caption windows. Caption
timing remains independent of document paragraph formatting.

Generate a fresh Markdown brief without publishing or synthesizing audio:

```sh
python scripts/preview_brief.py /absolute/path/paper.pdf --output /absolute/path/preview
```

Optional `--metadata` and `--zotero-key` arguments supply bibliographic metadata and
provenance. The output includes `brief.md`, extracted `article.md`, `structure.json`,
and the brief speech plan. Compare the brief to the PDF page before audio synthesis.
Existing published audio and transcripts require a separate rebuild to adopt changes.

## Speech continuity

The MLX English backend now synthesizes complete sentences, measuring the actual
phoneme string against the model's context limit (510 payload symbols). Only an
oversized sentence is divided at clause punctuation, with word boundaries as a
last resort. Oversized individual words fail explicitly instead of being truncated.
The checked phoneme string is passed directly to inference, and every utterance's
text, phonemes, count, and duration are recorded in the run manifest. A new synthesis
configuration digest invalidates audio cached with the old 160-character splitter.
This applies to Briefs and Full Readings. The outer speech-plan character target is
also soft: a long sentence stays whole until the backend measures its phonemes.
Full podcast builds reconstruct boundaries from source blocks even when the input
bundle contains an older speech plan. Existing library files can be deliberately
regenerated with `run --force`; unchanged archived files are not silently rewritten.

Text/phoneme coverage checks prove what was submitted to synthesis; they do not
prove flawless pronunciation. Listening and independent transcription remain useful
checks, especially for names, abbreviations, and long sentences.
