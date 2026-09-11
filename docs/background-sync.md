# Background sync troubleshooting

The installed 1 More Paper service checks the supported Zotero GET API on startup
and every minute in a dedicated thread, even during generation. No browser window
or manual Sync action is required. Zotero must be open with local API access
enabled and PDFs downloaded locally. `za status --json` includes `zotero.online`,
`checked_at`, `last_success_at` and the selected `auto_generate` mode; the local
Library displays freshness and connection failures. A closed/unavailable Zotero
retries every minute while preserving the cached library. Activity shows queued,
running and failed automatic work with full article titles.

Generation defaults to Markdown; choose other outputs in Settings → Zotero
automation. Turning it off still updates the library. Existing output and prior
cancelled/failed requests are preserved. A finished Markdown file does not block
automatic creation of the selected audio editions. Use Retry for a failed job or an explicit
regeneration when a source PDF changed.

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
