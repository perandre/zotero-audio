# Architecture and maintainer map

1 More Paper has one local processing core and several small adapters. A human
can use the command menu or dashboard without AI. Agents use the same library,
settings, jobs and artifacts through MCP. Cloudflare supplies remote access and
storage; the Mac supplies extraction and Apple-silicon speech processing.

## Components

| Component | Files | Responsibility |
| --- | --- | --- |
| Human CLI | `src/zotero_audio/app_cli.py` | `za` menu, title-based selection, JSON output, lifecycle commands. |
| Local dashboard/API | `app_server.py`, `cloud/public/` | Loopback HTTP, safe file access, Markdown reader, audio player, job progress and settings. The same assets are served remotely. |
| Local catalog | `app_state.py`, `app_library.py` | SQLite state/search, saved Zotero discovery, existing-artifact import, safe article serialization. |
| Local worker | `app_worker.py` | One warm TTS backend, durable generation queue, independent publication/iCloud/backup delivery. |
| Generation | `generation.py` | One-article Markdown/Brief/Full operation and progress callbacks; preserves existing Markdown unless forced. |
| Extraction and audio | `extract.py`, `segment.py`, `audio.py` | Page-aware extraction, narration filtering, speech segmentation, verified cache reuse, normalization and AAC assembly. |
| Publishing | `generation.publish_finished_episode`, `podcast.py`, `publish_sync.py`, `feed_only.py` | Source-bound licensing, finished-file publication, public object synchronization and feed-only updates. |
| Mac/cloud bridge | `app_bridge.py` | Outbound authenticated sync and job polling; separate lease renewal; no inbound Mac port exposed online. |
| Remote service | `cloud/src/` | Worker REST/MCP/OAuth, private R2 documents, D1 FTS/job/settings tables, OAuth KV. |
| Local MCP/plugin | `app_mcp.py`, `plugins/one-more-paper/` | Official Python MCP stdio and the Agent Plugins 1.0.0 package. |
| Installation | `app_install.py` | Per-user launchd service and cutover from the legacy scheduler. |

Paths without a directory in the table are under `src/zotero_audio/`. The
original `cli.py` and batch scripts remain available for direct pipeline work;
they do not define a second product control plane.

## Document and generation flow

Zotero remains responsible for collecting articles, downloading PDFs,
bibliographic metadata, collections/tags and attachment synchronization. The
Python integration prefers **GET requests to Zotero’s local API** at
`http://localhost:23119/api/`, including linked PDF attachment paths. Legacy
SQLite/storage access is a compatibility fallback. The optional Zotero add-on
maintains its own status tags/saved search and exposes a read-only feed inbox;
those existing add-on responsibilities are separate from core GET discovery.

`generation.process_article` resolves source metadata, extracts the PDF when
needed, and writes a human-readable Markdown filename matching the episode
title. Research Markdown retains references, links,
page markers and extracted supporting text. It is editable source material for
subsequent work. Normal reruns preserve it; `force=True` explicitly replaces it
from the source PDF. A changed PDF is surfaced for an explicit regeneration
decision instead of silently discarding Markdown edits.

The `markdown_ready` callback fires before speech. The catalog/index and private
cloud mirror can become useful at that point. Narration is derived separately,
removing reference lists, citation markers, long links, contact furniture and
unsuitable layout artifacts. It does not use an LLM to paraphrase research.
Brief uses a bounded authors’ abstract/conclusion or supported report summary;
it does not fabricate a summary when suitable source content is unavailable.

Each requested audio edition has its own plan, voice configuration and finished
M4A. Brief can finish and enter delivery while Full continues. A failure in one
edition/article does not stop other eligible work. Shared extraction and
verified speech cache entries are reused when their identities match.

The model stays warm in one generation worker. The default is Kokoro through
MLX on Apple silicon. `SpeechBackend` and registered providers/entry points are
the extension boundaries; there is no silent network or system-voice fallback.
Provider identity includes model revision, voice, speed and other synthesis
settings. Model changes must invalidate affected cache identities.

Assembly includes the spoken introduction and optional bundled opening/closing
sound, normalizes audio and encodes the final AAC once. A sound-only change can
reuse speech and reassemble it. **Publication consumes the finished bytes; it
must not synthesize or encode them again.** Shared show artwork is sufficient;
chapters, per-episode art and timed transcripts are deferred in this path.

See [generation-api.md](generation-api.md) for callable contracts and artifacts.

## QA, publication and delivery

Optional QA runs on Markdown before synthesis and on final encoded audio
afterward. Its normal outcome for usable work is ready with warnings. Required
technical prerequisites still reject unusable input/output. Quality evidence
records what was checked, skipped or measured; there is no claim that loudness
measurement verifies semantic accuracy or that an ASR check ran when it did not.

A bundle contains `qa-report.json` and a human-readable `<episode title> - AI review.md` with findings, complete
research Markdown, settings, provenance and repair/reproduction instructions.
The original article text inside a report remains untrusted reference content.
An artifact path in a report is not proof an agent has inspected that artifact.

`auto_publish=true` is publication intent for explicitly requested `full`,
`brief` or `both` generation jobs. The source-bound license check and podcast
configuration still govern public eligibility. Library import and `za sync`
only catalog existing output and **never publish it automatically**. Private or
unverified rights do not prevent Markdown generation, private MCP reading or
local listening.

Generation, public delivery and iCloud/backup delivery have separate persisted
statuses. Public delivery verifies the finished artifact and licensing, uploads
assets first, and updates the affected feed last through a serialized writer.
A failed upload can retry without repeating synthesis or erasing local success.

The chosen iCloud output folder receives clean, readable private-audio filenames.
It receives no research Markdown, logs or podcast metadata by default. Optional
Markdown/audio backup is separate and disabled by default. Local copies are
atomic; the worker does not wait for iCloud’s network synchronization. Filename
collisions must preserve existing user files. Delivery retries remain visible.

## State, restart and remote work

The normal runtime is `/Users/pesh/Sites/zotero-audio-runtime`, outside Git:

```text
control/
  library.sqlite3       local catalog, jobs, deliveries, settings and small state
  daemon.log            process output
  daemon.stderr.log     installed-service errors
  events.jsonl          structured event summaries
  errors/               job failure traces
  cloud.json            remote origin and separate bridge credential (private)
  Cloud login.txt       owner browser login instructions/key (private)
full-library/bundles/
  <bundle>/
    <episode title>.md
    structure.json
    metadata.json
    generation.json
    qa-report.json
    <episode title> - AI review.md
    editions/brief/...
    editions/full/...
venv/                   editable Python installation
huggingface/            model downloads/cache
```

Only one process owns generation/publication for a runtime; the new daemon also
respects the old scheduler lock during migration. Jobs are durable SQLite
records. Restart interruption preserves reusable artifacts and resumes work;
explicit user cancellation stops at a safe checkpoint. `Audio ready` means
final encoding/checking finished, not merely that TTS segments exist.

Cloud D1 holds the remote queue. The Mac polls through an outbound bridge with
its own credential. Claims are idempotent per worker and have expiring leases;
only one live claim may own a job. Lease renewal runs separately from document
uploads every 15 seconds, so a slow sync cannot hold up long TTS work. Idle
heartbeat/catalog polling is bounded to approximately once per minute.
Transient network failure leaves local work running. A definitive lost lease
stops that cloud-owned job; stale processes cannot update newer leases. Terminal
acknowledgements are retryable, and a missing local article fails the remote job
with its complete title and a clear recovery action.

Cloud settings carry an update timestamp. The bridge seeds only an unconfigured
cloud service and otherwise synchronizes intentional newer changes. File sync
uses content hashes and local stat signatures. A bad document backs off while
other articles continue; a successful unchanged sync avoids re-uploading it.

## Cloud storage and authentication

The live dashboard is
[one-more-paper.perandre.workers.dev](https://one-more-paper.perandre.workers.dev),
with the remote MCP endpoint at the same origin’s `/mcp` path.

R2 Standard stores full current Markdown and review files in a **private**
bucket, including non-open articles. D1 stores searchable text, metadata,
settings and jobs. The search index removes YAML frontmatter and internal page
comments for useful snippets; complete originals remain in R2. The public
podcast bucket is separate. Private audio stays on the Mac/iCloud in this phase.

The browser uses an owner login and signed HttpOnly cookie. Cookie mutations
require the correct origin; rendering treats article Markdown as data rather
than executable HTML. OAuth uses the Cloudflare provider’s discovery, S256
PKCE, client consent, audience validation and token lifecycle. The remote MCP
adapter uses the official TypeScript SDK’s declared Streamable HTTP protocol.
Read scope covers search/fetch/status/review; write scope adds durable jobs.
Neither scope grants bridge ingestion, arbitrary shell execution or arbitrary
filesystem access. Local stdio MCP inherits the local user’s authority.

Synced search works while the Mac sleeps; generation waits for it to return.
Authorized MCP retrieval sends requested private text to the connected client.
Initial production checks covered S256 PKCE, read/write isolation, search,
exact private-Markdown fetch and a remotely queued Markdown job completed by
the Mac. Actual ChatGPT account/phone connection remains an explicit
interoperability check beyond those SDK/OAuth tests.

Workers Free, D1, R2 Standard and OAuth KV avoid a paid compute/model dependency.
The private library applies conservative capacity and upload limits documented
in [cloud/README.md](../cloud/README.md). These caps are not an account-wide
billing guarantee: public podcast storage and other projects share free
allowances. Never silently upgrade a plan or expose the private bucket to solve
an integration problem.

## Fast maintenance

Use the editable Python installation and `za restart` for core changes. The
HTML/CSS/JavaScript dashboard has no frontend compilation step. A Cloudflare
change uses Wrangler’s small Worker/assets deployment; model files and runtime
artifacts are excluded. A native App Store package is not required for iteration.

Run affected Python tests, Cloudflare type/contract tests for remote changes,
and actual local integration for auth/storage/job contracts. For an extraction
or narration change, inspect representative Markdown and listen to an affected
sample instead of treating mocked tests as a quality verdict. Keep module
responsibilities small, tool responses structured, provenance explicit and
human output centered on full titles. The [root quickstart](../README.md) and
[legacy reference](legacy-cli.md) contain operational commands.
