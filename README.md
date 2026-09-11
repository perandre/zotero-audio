# 1 More Paper

Read, listen to and search the research already saved in Zotero. **Markdown is a
finished product; audio is optional.** A persistent Python worker uses Kokoro on
Apple silicon, while a small dashboard and short commands keep every article’s
full title, progress, files and quality findings visible. The same operations
are available to AI agents through MCP and a portable agent plugin.

- **On your Mac:** run `za dashboard`, or open [the local dashboard](http://127.0.0.1:8765).
- **From another device:** [open your private library](https://one-more-paper.perandre.workers.dev).
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
asking an LLM to rewrite the research. Optional checks run on Markdown before
speech and on the finished audio afterward. Usable results remain available
with warnings; basic failures such as empty or invalid audio still fail clearly.

Each edition gets one final encode, including its spoken introduction and
selected opening/closing sounds. Publishing reuses that finished file. A ready
Brief, Full edition or article can publish while other work continues. Only
show-level artwork is needed; chapters and timed transcripts are not required
by this new generation path.

**`auto_publish` applies to explicitly requested audio jobs.** With it enabled,
new `full`, `brief` and `both` jobs express publication intent, while the existing
source-bound licensing rules and podcast configuration still decide eligibility.
Importing existing files or refreshing Zotero never publishes them automatically.
Private and unverified material remains available for local listening and
private research access.

## Storage and privacy

| Location | Contents |
| --- | --- |
| Mac runtime | Working Markdown, audio, source metadata, QA evidence, SQLite state, logs and reusable model/segment caches. |
| Private Cloudflare R2 | Current full Markdown and QA reports, including private/non-open articles. |
| Cloudflare D1 | Search index, article metadata, durable jobs and settings. |
| Your chosen iCloud folder | Readably named private M4A audio, without podcast metadata or logs. |
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

## Install and iterate on this Mac

Requirements: Apple silicon macOS, Python 3.11+, FFmpeg, macOS `afconvert`, Zotero
and local Kokoro model storage. Enable Zotero’s **Allow other applications on this
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

`za install` installs a per-user `launchd` worker and disables the previous
five-minute scheduler at cutover. It preserves models and generated files.
For foreground development, use `za serve` instead of installing the service.
One generation worker owns the model and the runtime lock; do not run a legacy
batch against the same runtime concurrently.

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
[Kokoro evaluation](docs/tts-evaluation.md). Broader article discovery and saving
new research remain Zotero responsibilities in this version.
