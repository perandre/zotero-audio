# Generation API

`zotero_audio.generation.process_article` is the shared one-article operation
for the local worker, CLI and MCP clients. It requires a bundle directory and
supports `markdown`, `brief`, `full`, and `both` modes. Markdown-only needs no
speech model. Audio receives a `SpeechBackend` or a lazy `backend_factory`;
the worker owns the model's lifetime so it remains warm between articles.

Existing `article.md` is authoritative and is never overwritten unless the
caller explicitly sets `force=True`. New extraction preserves references,
links, figure/table captions and extracted footnotes. The source PDF hash and
page markers remain available. Each edition derives its own narration from
the current Markdown, omitting references, raw links, citation markers and
contact boilerplate. A small explicit list repairs unambiguous PDF hyphen
artifacts; ordinary compounds remain unchanged. This process does not use an
LLM or paraphrase the article.

The progress callback receives dictionaries with `title`, `stage`, optional
`edition`, and segment `completed`/`total` counts. `markdown_ready` includes the
Markdown path/hash and optional QA status and occurs before audio starts.
Assembly reports normalization, encoding, then optional final audio checking;
`audio_ready` is emitted only when the final encoded edition is usable.
Callback exceptions propagate, allowing the worker to cancel safely between
segments/stages. Verified segment WAVs survive interruption.

With `both`, Brief is attempted first and its completion callback fires before
Full starts. A generation failure in one edition is recorded and does not
prevent attempting the other. A callback failure still propagates immediately.
An unavailable Brief is explicit; a request with no usable audio result has
status `failed`.

Artifacts are ordinary files:

- `article.md` and `structure.json`: research source and original extraction.
- `generation.json`: current artifact paths, title, source hashes, licensing,
  settings, statuses and completed editions.
- `qa-report.json` and `ai-review.md`: machine-readable findings and a
  self-contained AI review brief with complete Markdown, instructions,
  evidence, configuration and paths to the original PDF/audio.
- `editions/brief/` and `editions/full/`: independent speech plans, narration,
  run manifests, content-addressed PCM segments, final M4A and edition record.

Optional checks run on Markdown before synthesis and on the final M4A after
encoding. Findings produce `ready_with_warnings` when the artifact is usable.
Basic prerequisites such as nonempty valid PCM/AAC and finite non-silent audio
remain mandatory. Loudness checks do not establish semantic accuracy; no ASR
is claimed. Expensive audio checks are reused when the final audio hash is
unchanged. Two-pass normalization measures its input to calculate gain; the
new path avoids separately measuring the intermediate normalized WAV.

Audio cache identities include the exact narration, provider configuration,
model revision, voice and speed. Final assembly also includes sound toggles,
stinger hash, bitrate and the policy revision. Editing one paragraph reuses
unchanged speech segments. Changing opening/closing sounds reassembles cached
speech and performs one final encode. Publishing never encodes audio again.

`publish_finished_episode(record, config, selected=None)` consumes a completed
edition and returns `{published, reason?}` or
`{published, feed_changed, public_editions}`. It revalidates source-bound
license evidence, selection and the audio hash. Shared show artwork is used;
no episode artwork, chapters or timed transcript is required. The caller must
serialize feed changes and upload assets before uploading the changed feed.
The return value describes the local publication mirror, not remote success.

Private/non-open audio is generated normally. The worker handles clean iCloud
export and optional backup independently; neither is part of generation.

Custom speech providers can implement the existing `SpeechBackend` protocol
and be passed directly, or register a factory with `audio.register_backend`.
Providers must return mono 24 kHz 16-bit PCM WAV and describe their model
revision and synthesis settings accurately in `config`. Providers supporting
multiple voices expose `configure(voice, speed, language)`. There is no implicit
remote fallback.
