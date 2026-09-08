# Background sync troubleshooting

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
