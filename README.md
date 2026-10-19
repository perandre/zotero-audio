# 1 More Paper

The public podcast is by Per André Rønsen for [Frontkom](https://www.frontkom.com/).
Show creator, owner and production copyright credits use Frontkom; source-paper
author attribution and licensing remain attached to each episode.

The public podcast domain move is documented in
[Podcast domain migration](docs/podcast-domain-migration.md).

Read, listen to and search the research already saved in Zotero. **Markdown is a
finished product; audio is optional.** A persistent Python worker uses Kokoro on
Apple silicon, while a small dashboard and short commands keep every article’s
full title, progress, files and quality findings visible. The same operations
are available to AI agents through MCP and a portable agent plugin.

- **On your Mac:** run `za dashboard`, or open [the local dashboard](http://127.0.0.1:8765).
- **From another device:** [open your private library](https://one-more-paper.perandre.workers.dev).
- **Save references online:** ask MCP to add a DOI or article URL to Zotero, even with the laptop closed. PDF processing and audio are separate.
- **Remote MCP:** `https://one-more-paper.perandre.workers.dev/mcp`.

The owner login key is stored outside Git in
`/Users/pesh/Sites/zotero-audio-runtime/control/Cloud login.txt`. Keep its contents
private. The Mac’s separate bridge connection is configured in
`/Users/pesh/Sites/zotero-audio-runtime/control/cloud.json`.

## Everyday commands

```sh
za                         # A human-readable action menu
za sync --wait             # Read saved Zotero PDFs and import existing output
za markdown new            # Create Markdown for articles without it
za markdown all            # Consider the whole library; preserve existing Markdown
za full                    # Choose one article by its full title
za brief "Article title"   # Create an authors’ summary edition
za both "Article title"    # Create Brief and Full independently
za search "AI adoption"    # Search your research text
za status                  # Current stage, full titles and delivery progress
za dashboard               # Preview text, play audio, reveal files and change settings
za review "Article title"  # Read findings and the complete AI review brief
za open "Article title" --audio --reveal
za retry                   # Retry the latest eligible job using cached work
```

`new` means the requested output is missing. Existing Markdown and verified
speech are reused. `--force` explicitly replaces Markdown from the PDF, including
manual edits. Add `--wait` to watch generation, `--no-qa` to skip optional checks,
or `--json` for machine-readable results. Noninteractive callers must supply an
unambiguous title or an ID returned by `za list --json`; human output always
includes the full title.

The service checks Zotero on startup and every minute while Zotero is open,
independently of generation and delivery. Newly available saved PDFs appear in
the library automatically; an open dashboard refreshes itself. The local library
shows when Zotero was last checked, separately from the Mac connection status.
When Zotero is closed or unavailable, the cached library remains usable and
checks retry automatically.

**Settings → Zotero automation** selects Markdown (the default), Markdown plus
Brief/Full/both audio editions, or no automatic generation. Discovery always
continues. Missing selected outputs get a durable generation job, even when Markdown is already ready;
restarts resume it and repeated scans do not duplicate it. Existing research,
imported audio and explicit failed/cancelled jobs are preserved. Existing audio editions are never selected for publication just because another edition is missing. Inspect Activity
for failed work and use Retry after addressing its cause. Replaced source PDFs
require explicit regeneration to preserve research edits. This catalog covers
locally available saved PDFs; feed entries and records without a downloaded PDF
are not generation sources. Removed PDFs retain their historical output/search
records and are excluded from future whole-Zotero jobs.

The dashboard’s command palette offers the same common actions. It distinguishes
**Markdown ready**, **Audio ready**, **Publishing**, and **iCloud/backup pending**.
Opening local files in Finder requires the local dashboard. Private audio can
also be played from its iCloud export; it is not uploaded to the cloud library.

## What happens to each article

```text
Zotero GET API → extraction → research Markdown → optional text QA
                                 │
                                 ├─ private cloud search and full-text MCP access
                                 └─ optional narration → Kokoro → final AAC → optional audio QA
                                                                          │
                                            eligible public audio → upload assets → update RSS
                                            private audio → chosen iCloud folder
                                            optional backup → separate background delivery
```

Research Markdown preserves links, citations and provenance. Narration omits
long URLs, citation markers, reference lists and contact boilerplate without
asking an LLM to rewrite the research. Narration also recognizes inline IEEE
abstracts and plain reference headings, omits bounded repository cover pages,
decodes HTML entities and repairs known PDF word splits. PDF extraction also checks
the article page behind a recognized repository cover. Brief recognizes spaced
abstract labels, plain keyword/ACM metadata and numbered
introduction boundaries. Its ten-minute text budget applies to every abstract
source; conclusions stop before publisher declarations and licensing notices.
Publisher license notices stay in research Markdown and show notes rather than
speech. Oversized extracts are rejected for source review, never silently
truncated into a summary. Existing research Markdown stays unchanged. Rerunning
an audio edition applies the current policy;
changed speech invalidates its cache while unchanged segments remain reusable.
Optional checks run on Markdown before
speech and on the finished audio afterward. Usable results remain available
with warnings; basic failures such as empty or invalid audio still fail clearly.

Each edition gets one final encode, including its spoken introduction and
selected opening/closing sounds. Publishing reuses that finished file. A ready
Brief, Full edition or article can publish while other work continues. Only
show-level artwork is needed; chapters and timed transcripts are not required
by this new generation path.

Existing published episodes can receive source-grounded abstracts and refreshed
episode pages without regenerating their audio. The preview, backup, apply and
upload procedure is documented in [published description backfill](docs/podcast-description-backfill.md).

**`auto_publish` applies to requested audio jobs, including the audio editions
selected in Zotero automation.** With it enabled,
new `full`, `brief` and `both` jobs express publication intent, while the existing
source-bound licensing rules and podcast configuration still decide eligibility.
Importing existing files or refreshing metadata never selects old output for publication.
Private and unverified material remains available for local listening and
private research access.

Manually reviewed publisher or repository license evidence can be retained as
`reviewed-source-license-v1` records in the bundle's `generation.json` and the
runtime's license-evidence cache. Records identify the exact source PDF hash,
verified license, evidence and reading URLs. Discovery and generation reuse them
when Zotero Rights is empty; replacement PDFs, explicit restrictive rights,
conflicts and embargoes still block publication. Reclassification alone does
not select additional audio editions.

PDF license checks recognize line-wrapped Creative Commons URLs and explicit
source-license notices after the first two pages. A license cited in references
alone does not establish publication permission. OCR and publisher/repository
lookups can supply reviewed, exact-source evidence when PDF text is insufficient;
they do not change the publication allowlist or authorize a different PDF version.

## Storage and privacy

| Location | Contents |
| --- | --- |
| Mac runtime | Authoritative editable research Markdown in `papers/`, with searchable bibliographic metadata in its generated `index.json`; separate article bundles hold extraction, QA and audio-generation state. SQLite search indexes the same Markdown files. |
| Private Cloudflare R2 | Current full Markdown and QA reports, including private/non-open articles. |
| Cloudflare D1 | Search index, article metadata, durable jobs and settings. |
| Your chosen iCloud folder | Readably named private M4A audio, without podcast metadata or logs. |
| Server outbox (headless server only) | Private audio waits in `outbox/` until `za pull-audio` copies it to iCloud; see [docs/private-audio-outbox.md](docs/private-audio-outbox.md). |
| Optional backup folder | Separate versions of Markdown/audio; disabled by default. |

Cloudflare Workers Free hosts the authenticated dashboard and MCP. R2 Standard,
D1 and OAuth KV use their free allowances. Application caps are conservative,
but **account-wide usage is shared, and these are not an absolute $0 billing
cap**. See [cloud setup and limits](cloud/README.md) before increasing capacity.

Searching synced Markdown works while the Mac is asleep. New extraction/audio
jobs wait for the Mac. Access through an authorized MCP client shares requested
private text with that client. Public podcast files remain in a separate bucket.
Backup, iCloud cloud synchronization and publication retries never roll back
completed local generation.

The editable research library is one flat directory at
`/Users/pesh/Sites/zotero-audio-runtime/papers`. Files use the full article title,
author label and year; a Zotero key is added only when filenames collide. The
generated `index.json` records title, authors, year, DOI, Zotero key, provenance,
content hash and any preserved revisions. Existing edits remain authoritative.
Startup imports available full-library output and consolidates catalogued,
full-library and report-pilot Markdown. When two different files claim the same
Zotero key, the catalogued version stays canonical and the other is kept as a
clearly named revision. Old bundle paths remain as links to the canonical file
or preserved revision, so existing generation records continue to resolve.
Generation state, audio editions and immutable delivery snapshots remain
separate from this editable library.

The podcast uploader pins Wrangler to `4.145.0` instead of automatically installing
the latest release. Upgrades require an explicit code change and verification on
the Mac. If npm's uploader installation is damaged, repair that exact version
with its optional platform dependencies, then restart with `za restart`; pending
deliveries reuse finished audio and retry without generation.

## Install and iterate on this Mac

Requirements: macOS (Apple silicon, or Intel via the CPU backend described
below), Python 3.11+, FFmpeg, macOS `afconvert`, Zotero and local Kokoro model
storage. Enable Zotero’s **Allow other applications on this
computer to communicate with Zotero** setting. The normal integration reads
`http://localhost:23119/api/`; Zotero itself supplies stored/linked PDF paths and
parent metadata. Direct database/storage arguments remain recovery fallbacks.

```sh
REPO=/Users/pesh/Sites/zotero-audio
RUNTIME=/Users/pesh/Sites/zotero-audio-runtime
mkdir -p "$RUNTIME"
python3.12 -m venv "$RUNTIME/venv"
"$RUNTIME/venv/bin/python" -m pip install -e "${REPO}[mlx,dev]"
export PATH="$RUNTIME/venv/bin:$PATH"
export ZOTERO_AUDIO_RUNTIME="$RUNTIME"
export HF_HOME="$RUNTIME/huggingface"
za doctor
za install
za sync --wait
za dashboard
```

### Intel Macs (x86_64, CPU only)

Apple silicon uses Kokoro through MLX. On x86_64 the provider named `kokoro`
automatically resolves to **`kokoro-torch`**: the same Kokoro-82M model, voices
(`am_michael`, `af_heart`), misaki phonemizer, 24 kHz sample rate and
loudness/assembly path, run in fp32 by the `kokoro` PyTorch package on the CPU.
Its synthesis config records `engine: kokoro-torch`, so cached segments are
never shared with MLX output. There is no fallback to system or network voices;
if the runtime is missing the job fails with an install hint. Intel macOS has no
PyTorch wheels newer than 2.2.2, which caps Python at **3.12** (3.11 also works;
3.13 does not). Keep the runtime path short (under about 150 characters): the
bundled espeak-ng library cannot open its data files from very long paths.

```sh
brew install ffmpeg python@3.12          # afconvert ships with macOS
REPO=~/Sites/zotero-audio; RUNTIME=~/Sites/zotero-audio-runtime
mkdir -p "$RUNTIME" && python3.12 -m venv "$RUNTIME/venv"
"$RUNTIME/venv/bin/python" -m pip install -e "${REPO}[kokoro-torch,dev]"
"$RUNTIME/venv/bin/python" -m spacy download en_core_web_sm   # misaki's English model
export PATH="$RUNTIME/venv/bin:$PATH" ZOTERO_AUDIO_RUNTIME="$RUNTIME" HF_HOME="$RUNTIME/huggingface"
python - <<'PY'   # one-time ~330 MB download of model + voices (otherwise first job does it)
from huggingface_hub import snapshot_download
snapshot_download("hexgrad/Kokoro-82M", allow_patterns=["config.json", "kokoro-v1_0.pth", "voices/*.pt"])
PY
za doctor   # expect speech_engine: kokoro-torch, kokoro_installed: true
za install
```

The `mlx` extra is restricted to Apple silicon by environment markers, so
`pip install -e '.[mlx]'` is harmless elsewhere. Expect CPU synthesis on an old
Intel Mac to be several times slower than MLX; a Full Reading can take longer
than the audio it produces. On x86_64 Linux install the same extra (use PyTorch's
CPU wheel index to avoid the CUDA download). Without `/usr/bin/afconvert` the final
M4A is encoded by FFmpeg's native AAC-LC (same M4A container, mono 24 kHz, same
bitrate setting) and inspected with `ffprobe`. The encoder (`ffmpeg-aac`) is part of
the assembly identity, so finished files from different encoders are never reused for
each other; macOS identities are unchanged. `ZOTERO_AUDIO_ENCODER=ffmpeg|afconvert`
forces a choice. `za doctor` reports platform, encoder and service manager.
The legacy `zotero-audio` CLI accepts `--engine auto|kokoro-torch|kokoro-mlx`.

### Remote speech (synthesis on a Linux x86_64 box)

The Mac worker keeps planning, the segment cache, assembly with `afconvert`, QA,
publication and the single feed writer. Only per-segment synthesis runs on the
other machine, over one persistent SSH process (`python -m
zotero_audio.remote_speech serve`, newline-delimited JSON on stdin/stdout).
The backend adopts the remote engine's real config (`engine: kokoro-torch`, model
revision, torch version, voice, speed), so cache keys match what that engine
produces locally and never collide with MLX. A different remote torch build
(for example `+cpu`) is a different identity and re-synthesizes once. Requests
time out, reconnect once, then fail the job; there is no fallback to MLX or any
other voice.

On the Linux box (no sudo; short paths because of the espeak-ng limit):

```sh
git archive HEAD | ssh user@box 'mkdir -p ~/1mp/repo ~/1mp/hf && tar -x -C ~/1mp/repo'
ssh user@box
uv venv --python 3.12 ~/1mp/venv
uv pip install --python ~/1mp/venv/bin/python --index-url https://download.pytorch.org/whl/cpu torch
cd ~/1mp/repo && uv pip install --python ~/1mp/venv/bin/python -e ".[kokoro-torch]"
uv pip install --python ~/1mp/venv/bin/python \
  https://github.com/explosion/spacy-models/releases/download/en_core_web_sm-3.8.0/en_core_web_sm-3.8.0-py3-none-any.whl
```

The first request downloads the model into `~/1mp/hf`. On the Mac, create
`$ZOTERO_AUDIO_RUNTIME/control/speech.json` (kept out of the synchronized app
settings, whose cloud schema only allows `tts_model: "kokoro"`), then run
`za doctor` and `za restart`:

```json
{"provider": "kokoro-remote", "host": "user@box"}
```

`command` optionally overrides the remote command (default
`HF_HOME=$HOME/1mp/hf $HOME/1mp/venv/bin/python -m zotero_audio.remote_speech serve`);
`ZOTERO_AUDIO_SPEECH_PROVIDER`, `ZOTERO_AUDIO_REMOTE_SPEECH_HOST` and
`ZOTERO_AUDIO_REMOTE_SPEECH_COMMAND` override the file. Key-based SSH
(BatchMode) is required. Delete the file to return to MLX. Re-extract the repo
archive on the box when the protocol changes.

`za install` installs a per-user `launchd` worker and disables the previous
five-minute scheduler at cutover. The persistent service owns the new one-minute
Zotero monitor. It preserves models and generated files.
For foreground development, use `za serve` instead of installing the service.

On Linux, `za install` writes `~/.config/systemd/user/one-more-paper.service`
(`Restart=on-failure`, `ZOTERO_AUDIO_RUNTIME` in its environment, logs in
`control/daemon*.log`) and enables/starts it with `systemctl --user`; `za restart`,
`za stop` and `za status` use the same lifecycle rules as on macOS. For the service to
run without a login session, run once (may need sudo): `sudo loginctl enable-linger
<user>`. `za install` reports whether linger is enabled.
One generation worker owns the model and the runtime lock; do not run a legacy
batch against the same runtime concurrently.

For a dedicated always-on publishing host, use Zotero Web API discovery and
local Kokoro, with its own podcast/upload credentials and `auto_publish=true`.
Create `control/service.json` containing `{"prevent_sleep": true}` before
`za install` on Linux. The service holds a sleep/idle inhibitor for its lifetime,
restarts after crashes, and supervises generation, discovery and delivery loops.
Keep linger enabled. This host can discover uploaded PDFs, generate and publish
without another computer connected; PDFs not uploaded to Zotero remain unavailable.
Use `private_audio: outbox` in `control/delivery.json` for private recordings;
collecting them on a laptop is independent of generation and public publication.

Completed jobs are historical receipts, not proof that their files still exist.
Missing previously ready editions get a new automatic recovery attempt; failed,
cancelled and unavailable editions still require intentional Retry. The delivery
worker also repairs stranded publication requests and refreshes source-bound
license evidence for editions selected by a completed audio job. Imports alone
never establish publication intent. New restrictive Rights or changed source
bytes still block publication. Existing finished audio is reused without encoding.
Public delivery is only marked published after the live RSS contains exactly
one matching episode/enclosure and its audio URL reports the expected size.
A failed live check retries delivery and invalidates stale upload-cache entries.

Most Python changes need only `za restart`; editable installation avoids an app
bundle rebuild. Frontend files are ordinary HTML/CSS/JavaScript. Reinstall Python
dependencies only when they change. Keep model files and runtime state outside
Git and outside the iCloud delivery folder.

```sh
"$RUNTIME/venv/bin/python" -m pytest tests/test_app_state.py tests/test_app_worker.py tests/test_app_bridge.py tests/test_generation.py tests/test_app_mcp.py
cd "$REPO/cloud"
npm ci
npm run check
npm test
```

Cloud integration tests additionally require local Wrangler and local secrets;
[cloud/README.md](cloud/README.md) documents the setup, migrations, OAuth tests and
deployment commands. Test changes at the affected layer, then use a real article
for any extraction/speech change that needs listening or visual review.

Inspect local failures with `za status --json`, `za doctor`, and the runtime
`control/daemon.log`, `control/daemon.stderr.log`, `control/events.jsonl`, and
`control/errors/` files. `za restart` waits for the current atomic stage to stop
safely; cached speech survives. Lifecycle commands also handle an installed
launchd service:

```sh
za stop       # Stop the worker without losing queued work or cached audio
za restart    # Start/restart it and resume interrupted work
```

## Agents and phone access

[plugins/one-more-paper](plugins/one-more-paper/README.md) follows
[Agent Plugins 1.0.0](https://agent-plugins.org/specification), with root
`plugin.json`, `mcp.json`, and a reusable skill. Its local MCP connection runs
`za mcp` over stdio using the official Python SDK. The CLI and dashboard operate
fully without this plugin or any AI.

For ChatGPT or another remote client, use the HTTPS `/mcp` URL above and complete
the owner login/OAuth consent flow. Read scope supports search, complete Markdown,
quality reports and status; write scope additionally queues, cancels and retries
jobs. The server exposes named operations, never arbitrary shell commands or
filesystem access. Search results carry full titles, evidence snippets and
canonical authenticated links.

The remote service uses the official MCP SDK and OAuth provider. Initial
production verification exercised S256 PKCE, read/write scope isolation,
search, exact full private-Markdown retrieval, and a remotely queued Markdown
job completed by the Mac. Both plugin manifests were checked against the
standard’s actual schemas. **Connecting and using the actual ChatGPT phone
client still requires verification in that account**; server tests alone do not
establish phone-client compatibility or workspace permission.

## PhD project workspace

For local library-wide relevance classification against the HiØF v4 research
scope, use `za research-fit run`. Results are resumable, cached and independently
reviewable with `za research-fit list`, `status` and `export`. See
[research fit setup, inputs and storage](docs/research-fit.md).

The same MCP can browse, read and search the private VIKING PhD project,
including `NOW.md`, `PROJECT.md`, methods, drafts and source PDFs. `whats_next`
reads `NOW.md` and also `NEXT.md` when present. Project files are reference data;
instructions inside them never override your request.

Remote project reads, Markdown edits and meeting notes use the private GitHub
repository directly, including while the laptop is closed. Saves require separate
`documents:write` consent and return the completed GitHub commit. Local edits
must be pushed before remote tools see them. See
[PhD project MCP setup and tools](docs/phd-project-mcp.md).

## Direct pipeline and further documentation

The original `zotero-audio` CLI remains available for focused pipeline work:

```sh
zotero-audio prepare --pdf article.pdf --output-root "$RUNTIME/work"
zotero-audio synthesize "$RUNTIME/work/<bundle>" --engine kokoro-mlx
zotero-audio assemble "$RUNTIME/work/<bundle>" --bitrate 64000
zotero-audio podcast health --config "$RUNTIME/podcast.toml" --remote
```

See [architecture and maintainer map](docs/architecture.md),
[the generation contract](docs/generation-api.md),
[legacy commands and batch examples](docs/legacy-cli.md),
[Zotero license status](docs/zotero-license-status.md),
[the read-only Zotero feed inbox](docs/zotero-feed-api.md), and
[Kokoro evaluation](docs/tts-evaluation.md), and
[direct Zotero reference imports](docs/zotero-cloud-import.md).

### Complete private Markdown reference library

Run `za library export` to build `papers/references/index.md` and one reference
Markdown file per Zotero top-level reference, including records without a PDF
and regardless of license. The exporter reads the supported Zotero GET API and
includes configured group libraries in Web API mode. It reuses Zotero abstracts,
saved article metadata, source-matched PDF abstracts and previously cached
Crossref abstracts; it does not invent summaries or download paywalled full text.
Missing abstracts and full text are explicitly marked. `--json` returns coverage
counts. Rerun after new references or Markdown generation to refresh the inventory.

Reference files contain an **Abstract** section and links to available **Full
text**. Full research Markdown remains authoritative in the flat `papers/`
directory; the exporter neither moves nor rewrites it. The reference inventory
is a separate derived view, not an addition to the generation catalog, private
cloud sync or publication queue. Manually edited reference/navigation files are
preserved on repeated exports; removed Zotero references retain their saved files
and manifest entries. `index.json` records the currently active reference IDs.
All generated files stay in the private runtime, outside the source repository.

These new reference documents use the minimal
[Open Knowledge Format v0.2](https://github.com/GoogleCloudPlatform/open-knowledge-format/blob/main/SPEC.md):
YAML `type`, title, resource, provenance and ordinary Markdown links, with an
`index.md` for navigation. Existing full-text files keep their original format
and content hashes; the entire papers directory is not claimed to be an OKF
bundle. See [the format assessment](docs/open-knowledge-format.md).

Run `za research-fit enrich` to retrieve missing abstracts and refresh both
`papers/references/` and **`papers/abstracts/`**. The latter contains only actual
sourced abstracts, one Markdown file per reference with available text; it never
fills missing abstracts with bibliographic placeholders. Existing reference and
abstract edits are preserved.

The enrichment script tries Crossref, OpenAlex, Semantic Scholar, DataCite,
arXiv identifiers and public publisher abstract metadata/sections. It includes
references without DOIs and articles with existing Markdown excerpts. Results
are matched to the source DOI/title, requests are bounded and paced, temporary
failures retry, and each result is saved immediately for restart recovery.
Provider outages/rate limits are recorded as `retry_pending`, separately from
`not_found`; rerunning retries unresolved records without repeating successes.
The JSON result includes attempt counts and final library coverage. Abstracts
remain complete in storage; only relevance-classifier input is bounded. This
command makes public metadata requests, without invoking a model or buying API
access. External sources can still be unavailable or omit an abstract; inspect
the durable per-reference retrieval receipts rather than assuming empty text
means no abstract exists.

Provider daily allowance reset headers are honored alongside `Retry-After`.
Reset deadlines are stored in retrieval receipts and restored on subsequent
runs, so restarting enrichment does not hammer an exhausted shared allowance.
It resumes source requests after the deadline; it never purchases more access.
OpenAlex title-search allowance is tracked separately from free DOI entity
lookups, so a title-search quota hit does not pause DOI retrieval.
