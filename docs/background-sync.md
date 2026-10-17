# Background sync troubleshooting

The installed 1 More Paper service checks the supported Zotero GET API on startup
and every minute in a dedicated thread, even during generation. No browser window
or manual Sync action is required. Zotero must be open with local API access
enabled and PDFs downloaded locally. `za status --json` includes `zotero.online`,
`checked_at`, `last_success_at` and the selected `auto_generate` mode; the local
Library displays freshness and connection failures. A closed/unavailable Zotero
retries every minute while preserving the cached library. Activity shows queued,
running and failed automatic work with full article titles.

## Zotero Web API mode (headless hosts)

Hosts without the Zotero desktop app (for example the Linux service) read the
library through `https://api.zotero.org` instead. This is a local, per-host
choice and is not cloud-synced. Create `<runtime>/control/zotero.json`:

```json
{"mode": "web", "user_id": 1234567}
```

`user_id` is optional (it is read from the key). Put a read-only personal-library
API key in `control/Zotero API key.txt` (or set `ZOTERO_API_KEY`;
`api_key_file` overrides the path). `ZOTERO_AUDIO_ZOTERO_MODE=local|web`
overrides the mode. Without the file, macOS behavior is unchanged (local API).

How it works: only GET requests are made. Items and collections sync
incrementally (`since`, `If-Modified-Since-Version`, `deleted?since`; state in
`control/zotero-web/library.json`), so an idle one-minute poll costs two
requests answered with 304. Pages use `limit=100`/`start`. `Backoff`,
`Retry-After` and HTTP 429/5xx are honored (process-wide); persistent limiting
shows as "offline" and retries next minute. Stored PDFs download once per
attachment key and md5, are verified against Zotero's md5 and cached in
`control/zotero-web/files/`; the key is never sent to the storage host. At most
`max_downloads_per_sync` (default 20) PDFs download per scan, so a large library
fills over several minutes; remaining ones stay `in_zotero`.

### Group libraries (opt-in)

Add `"groups": "all"` (every group the key's user belongs to) or a list of
group IDs, e.g. `{"mode": "web", "groups": [6696698]}`;
`ZOTERO_AUDIO_ZOTERO_GROUPS=all|<id>,<id>` overrides it. The default is the
personal library only. The key needs read access to those groups.

- Each group syncs separately into `control/zotero-web/groups/<id>/` (own
  `library.json` and `files/<attachment>/<md5>.pdf` cache), with the same
  incremental sync, Backoff/Retry-After handling and read-only GETs. The
  per-scan download limit is shared across all libraries.
- Group articles get library-qualified IDs, `g<groupID>:<attachmentKey>`
  (e.g. `g6696698:ABCD2345`). Personal-library IDs, paths and cache keys are
  unchanged, so enabling groups never re-generates existing work.
- Metadata keeps plain collection names (so collection filters such as
  "PhD Application" match) and adds `zotero_library` (`groups/<id>`) and
  `zotero_library_name`. `za research-fit` classifies group parents under
  `library_id = groups/<id>`.
- Duplicates: when a usable PDF with the same DOI exists in several libraries,
  the personal library wins, then the lowest group ID. Skipped copies are not
  downloaded and are listed under `zotero.duplicates` in `za status --json`
  with the ID they defer to. Items without a DOI are never merged.

Limits: linked-file attachments and files never uploaded to Zotero storage
cannot be downloaded. They are listed under `zotero.unavailable` in
`za status --json` with the article title and reason, and no article is created
for them. Zotero's free storage quota applies to what is synced; PDFs that exist
only on one desktop are invisible until synced. Searches/collection scoping is
as in local mode (all non-trashed PDF attachments; tags/collections only feed
`podcast_selected`).

Generation defaults to Markdown; choose other outputs in Settings → Zotero
automation. Turning it off still updates the library. Existing output and prior
cancelled/failed requests are preserved. A finished Markdown file does not block
automatic creation of the selected audio editions. Use Retry for a failed job or an explicit
regeneration when a source PDF changed.

For a dedicated Linux publisher, also enable user-service linger and set
`control/service.json` to `{"prevent_sleep": true}`, then run `za install`.
Keep synthesis local to that host and configure its own public upload credentials.
Private audio may accumulate in the outbox without a laptop connected.
During runtime migration copy the immutable `control/artifacts/` recordings,
not just JSON/catalog metadata. Before cutover run
`scripts/migrate_runtime_paths.py --runtime <runtime> --old-prefix <old-runtime> --apply --check-artifacts`;
the command verifies recorded audio hashes and fails on missing/corrupt files.
Keep the source runtime as a backup until that check passes.

The following notification details apply to the legacy scheduled sync scripts,
which `za install` disables when the persistent service takes over.

The launchd job does not load interactive shell profiles. At startup, the sync
process preserves its existing PATH and adds installed Homebrew and NVM tool
directories. This makes FFmpeg, Node, and npx available to child processes too.
`ZOTERO_AUDIO_NPX` can explicitly select npx; its directory is added first so
the sibling Node executable is visible. No shell profile is executed.

Sync and feed-health failure notifications are recorded in
`<state-dir>/notification-state.json`. An unchanged failure is announced once,
including across process restarts. A changed failure can notify again; success
clears the corresponding failure so a later recurrence can notify. `--no-notify`
suppresses notifications without marking unseen failures as delivered.

Notifications use macOS AppleScript and may appear under Script Editor
(Prosedyreredigering) in Notification Center. Their title identifies Zotero Audio.
Detailed failures remain in the automatic sync stdout log. Artifact and health
failures cause a nonzero result; generated audio is retained for retry.
