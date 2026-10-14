# Private audio outbox and `za pull-audio`

When the always-on service runs on a headless Linux box, it cannot write to the
iCloud Drive folder. Finished **private** audio is kept on the server in a
durable outbox; a Mac pulls new files into its iCloud folder whenever it is
awake. Generation and delivery on the server never wait for the Mac.

## Server: outbox delivery

Private audio goes to `<runtime>/outbox/` instead of iCloud when:

- the server is not macOS (the synced `icloud_folder` path is meaningless there), or
- `<runtime>/control/delivery.json` contains `{"private_audio": "outbox"}`.

`{"private_audio": "icloud"}` forces the historical iCloud copy; `"auto"` (default)
keeps macOS behaviour unchanged (iCloud when the folder is set, otherwise
`not_configured`). Public-eligible audio is unaffected and still publishes.

```text
outbox/index.jsonl                       append-only, one JSON object per line
outbox/files/<sha256>/<readable name>    immutable finished audio
```

Each entry: `id`, `sha256`, `filename` (the same readable name as iCloud),
`path`, `size`, `article_id`, `title` (full title), `edition`, `created`. The copy
is atomic (temporary file, fsync, checksum verification, rename), and repeating
it is a no-op. The delivery is a normal persisted worker delivery with its own
retry; the article's `icloud_status` becomes `outbox` (finished for the worker).
A torn index line from a crash is ignored and repaired on the next append.

## Laptop: `za pull-audio`

```sh
za pull-audio --from perandre@imac --runtime-path '~/Sites/zotero-audio-runtime' [--dest <folder>] [--dry-run]
za pull-audio --from perandre@imac --runtime-path '<path>' --install    # launchd agent
za pull-audio --uninstall
```

- Uses `ssh -o BatchMode=yes` only (key-based; never prompts for a password) to
  read `index.jsonl` and stream each file; no rsync dependency.
- Destination is `--dest` or the `icloud_folder` setting.
- Each file is verified against the entry's sha256 before it is placed. A
  mismatch is reported, nothing is copied, and the entry is retried next run.
- Placement uses the worker's collision rules: an identical file is reused; a
  different existing file is never overwritten (the copy gets
  ` — <sha8>` appended to its name).
- Pulled entry ids are stored in the laptop's SQLite state per host and runtime
  path, so reruns copy only new audio, a file you later delete from iCloud is not
  re-created, and a laptop closed for days simply catches up. Overlapping runs
  are prevented by a lock file. Exit status is 1 when any entry failed or the
  server is unreachable.
- `--install` writes `~/Library/LaunchAgents/com.pesh.one-more-paper.pull-audio.plist`
  with `RunAtLoad` and `StartInterval` 900 s (launchd runs a missed interval
  after wake), logging to `<runtime>/control/pull-audio.log`.

## Retention

Outbox files are kept on the server until an operator removes them; the system
never deletes unpulled (or pulled) files. The outbox is small relative to the
disk, but to reclaim space remove `outbox/files/<sha256>/` directories for
entries already present in iCloud; the index may keep the line (pulling a missing
file reports an error rather than losing data) or the line can be removed too.
Verify with `za pull-audio --dry-run` that nothing is still pending first.

## Cutover checklist

1. On the server set `control/delivery.json` (optional on Linux) and restart the service.
2. On the Mac confirm `ssh -o BatchMode=yes perandre@imac true` works without a prompt.
3. `za pull-audio --from perandre@imac --runtime-path <server runtime> --dry-run`, then run it for real.
4. `za pull-audio --from … --runtime-path … --install`; check `pull-audio.log` after 15 minutes.
