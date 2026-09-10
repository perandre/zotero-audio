# Standing user preferences

- After making code changes, verify them, commit your scoped changes, and push
  them to the remote. Deploy when deployment is needed for the changes to take
  effect. Do not leave these steps for the user to request again.
- Preserve unrelated local changes; never include secrets or generated runtime
  artifacts in commits. If verification, push, or deployment is blocked, report
  the blocker explicitly rather than claiming completion.
- Treat this as a mature app. Use feature branches and pull requests into
  `main` for all changes; never push changes directly to `main`.
- Before merging, review the complete diff, run the relevant checks, address
  blocking feedback, and record validation and material deployment risks in
  the PR. Preserve durable data and backwards compatibility during upgrades.
- The saved checkout runs the installed Mac service. Develop future changes in
  isolated worktrees so edits do not alter the running app before review. After
  a PR merges, update the saved checkout to `main`, deploy the merged revision
  when needed, and verify the running service. Keep this release process fast.

# Maintainer map

The product is **1 More Paper**; the Python package/repository remains
`zotero_audio` / `zotero-audio`. Start with `README.md` and
`docs/architecture.md`. `docs/generation-api.md` defines the shared generation
contract; `cloud/README.md` defines cloud setup, OAuth and bridge contracts.

- `src/zotero_audio/app_cli.py`: `za` commands, title-based selection, JSON output.
- `app_state.py`, `app_library.py`: durable local state/search, Zotero discovery,
  existing-output import and safe serialization.
- `app_worker.py`: one persistent generation worker and independent delivery.
- `generation.py`: Markdown-first, one-article generation and completed-episode
  publication. `extract.py`, `segment.py`, `audio.py` provide
  extraction, narration and speech/audio primitives.
- `app_server.py`, `cloud/public/`: local HTTP API and shared dashboard assets.
- `app_bridge.py`: outbound cloud sync, durable claim handoff and independent
  lease renewal. Keep its schemas aligned with `cloud/src/`.
- `app_mcp.py`, `plugins/one-more-paper/`: official local MCP adapter and the
  Agent Plugins standard package (`plugin.json`, `mcp.json`, `skills/`).
- `cloud/src/`, `cloud/migrations/`: authenticated Worker/MCP, R2/D1 and OAuth KV.
- `app_install.py`: per-user launchd installation and fast local lifecycle.

Unqualified Python filenames above live under `src/zotero_audio/`.

# Product invariants

- Human output always includes the full article title. IDs remain stable machine
  arguments, not the only label a person sees.
- Existing research Markdown is authoritative. Preserve edits unless the caller
  explicitly requests regeneration. Keep references/URLs in research Markdown;
  remove narration distractions from the separate speech text.
- Zotero integration normally uses the supported local GET API. Do not mutate
  source PDFs or substitute internal database reads as the normal discovery path.
- A finished Markdown file is useful independently of audio. Publish/sync each
  ready article or edition without waiting for the rest of a batch.
- Optional QA produces actionable warnings for usable work. Do not quietly turn
  cosmetic/content warnings into a blanket listening block, or claim checks ran
  when they were skipped.
- Each edition is finally encoded once. Publication reuses the verified finished
  file. Changes must preserve content/configuration hashes used for cache reuse.
- `auto_publish` expresses selection for explicit audio jobs; importing/syncing
  existing output never publishes it. Public eligibility still requires the
  source-bound licensing and podcast gates.
- Private Markdown is allowed in authenticated R2/MCP. It must never become a
  public bucket/object. Private audio goes to the chosen iCloud folder; backup
  and network delivery do not gate generation completion.
- Restart/transport failure preserves durable work and cached artifacts. Keep
  lease renewal independent of file uploads, and reject stale cloud leases.
- Cloudflare must stay on the free-plan design. Application quotas do not replace
  checking shared account usage. Do not add paid services or automatic upgrades.

# Validation and runtime hygiene

The local environment is normally
`/Users/pesh/Sites/zotero-audio-runtime/venv`; invoke its Python explicitly when
needed. Most edits need targeted pytest checks and `za restart`, not a rebuilt
app bundle or re-downloaded models. Cloud changes use `npm run check`, `npm test`
and the relevant local Wrangler integration checks from `cloud/`.

Generated documents, audio, model weights, SQLite state and credentials live
outside Git. Never print the owner key file, bridge token, OAuth tokens or
secret configuration. Report full titles, stages and request IDs in diagnostics;
article text and private URL query strings are not ordinary log messages.
Keep sample fixtures synthetic and credentials out of committed test data.

When reporting completion, distinguish implemented/locally tested behavior from
live deployment and actual ChatGPT-phone verification. Preserve useful legacy
commands under `docs/legacy-cli.md`; new behavior belongs in the product docs.
