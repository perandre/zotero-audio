# PhD project documents in MCP

The existing 1 More Paper MCP also opens the private VIKING PhD workspace.
Ask it to read current priorities, find a methods draft, compare project sources,
or save a meeting debrief. It uses the same conversational document workflow as
RagnhildAI, with the PhD folder on the Mac as its source. Saved local edits are
included even before a Git push. Project documents never become audio jobs or
public podcast files.

## Tools and current work

| Tool | Purpose |
| --- | --- |
| `whats_next` | Read `NOW.md` and `NEXT.md` when they exist; explicitly report missing files. |
| `list_documents(prefix, offset, limit)` | Browse exact document paths; follow `next_offset` until null and inspect warnings. |
| `read_document(path)` | Complete text, full title, source revision and sync timestamp. |
| `search_documents(query)` | Search paths, titles and full project text. |
| `search` / `fetch` | Search both articles and project documents, then retrieve a returned ID. |
| `save_document(path, text, expected_revision, request_id)` | Save a complete Markdown edit or new document with conflict protection. |
| `save_meeting_note(title, date, body_markdown, request_id)` | Queue a new dated note in `admin/meetings/` (remote MCP). |
| `document_change_status(request_id)` | Inspect a remote save's actual result, including commit/push or conflict. |

`NOW.md` is this project's current working state. The tool does not invent
`NEXT.md` or duplicate `NOW.md`; it also reads `NEXT.md` if one is added later.
`AGENTS.md`, `PROJECT.md`, domain READMEs and playbooks are accessible reference
material. Their contents do not override the user's actual request or authorize
an action on their own.

## Configure this Mac

Save `control/projects.json` under the runtime directory, outside Git:

```json
{
  "phd": {
    "enabled": true,
    "title": "VIKING PhD project",
    "root": "/Users/pesh/Documents/phd",
    "write_enabled": true,
    "include": [],
    "exclude": []
  }
}
```

The root must be a Git repository. Tracked documents are eligible; `include`
adds explicit relative paths and `exclude` removes exact paths or entire
subfolders. A newly saved MCP note is explicitly included until Git tracks it.
Hidden paths, symlinks and files outside the configured root are refused.
Keep credentials, restricted empirical data and unrelated untracked files out
of the eligible document set. Configuration is opt-in; no folder is discovered
or uploaded automatically on another installation. Restart with `za restart`.

Supported text: Markdown, UTF-8 text, JSON, CSV/TSV, YAML, RST, TeX, BibTeX and
HTML. PDFs use local extraction with page markers, and DOCX uses extracted main
document paragraphs. The source files are never modified by extraction. Images,
PDF visual layout and embedded figures are not represented as text. Empty PDF
pages carry an explicit inspection notice; unreadable/unsupported files appear
in warnings instead of silently disappearing or being described as fully read.

## Sync, privacy and limits

The ordinary outbound Mac bridge uploads only changed text, at most 15 documents
per poll (normally one minute). A complete manifest removes stale mirror entries
when an eligible file is deleted, renamed or excluded; it never deletes a source
file. The last synced copy stays available while the Mac is asleep. Check both
the workspace's last contact and each document's sync/source timestamps before
describing it as current. Initial synchronization can take several polls.

Project text and its FTS index live in private D1 tables, using the existing
authenticated Worker. No public bucket, inbound Mac port, GitHub credential,
paid service or new AI service is added. Current text is bounded to 500 documents,
512 KiB each and 8 MiB per workspace (plus index overhead). At most 25 document
changes can wait for the Mac. Terminal change bodies are cleared after
acknowledgement; small cloud receipts are retained for 30 days. Shared account
usage must still be checked before expanding these limits.

`library:read` permits project reading alongside article reading. OAuth consent
describes both. `documents:write` is a separate grant for document edits;
`jobs:write` continues to permit only audio/Markdown generation control. Existing
clients can use project results through `search` and `fetch`. Refresh tool
discovery for the named project tools and reconnect/authorize with
`documents:write` to enable saving. The endpoint is unchanged:
`https://one-more-paper.perandre.workers.dev/mcp`.

## Saving and recovery

Read before editing and pass the exact returned source SHA-256 as
`expected_revision`. Use `null` only when creating a new path. Submit the entire
intended Markdown and a unique `request_id`; use the same ID only for an identical
retry. The Mac compares against the actual file again, preserves an original
backup outside Git, and atomically saves the new text. It commits only the target
file and pushes its current branch's upstream. It preserves unrelated changes,
refuses staged target edits and unpublished unrelated commits, and does not reset,
stash, switch branches or force-push.

Remote saves are durable requests, not immediate edits. They wait for the Mac
and return `queued`. Check `document_change_status` for `completed`, `conflict`
or `failed`; only `completed` establishes that save/commit/push succeeded. A
conflict needs a fresh read and a reconciled request. A Git failure can leave a
saved local file or an unpushed commit: the receipt says so explicitly. Inspect
Git status on the Mac and resolve it before continuing. Lost acknowledgement
retries reuse the persisted local receipt without another edit or commit.

Local stdio saves run synchronously with the same safeguards. Local receipts,
document extraction cache and backups live under the runtime `control/` folder.
`project_sync` and `project_sync_error` state records contain counts, timestamps
and sanitized diagnostics; bodies and credentials are not log messages.

## Release and validation

Apply `0004_project_documents.sql` before deploying the Worker, then update and
restart the Mac service. This migration only adds project tables; article state,
R2 objects, processing queues and publication behavior remain intact. The cloud
code can be deployed before enabling the local project configuration.

Run the project/bridge/MCP pytest checks, `npm run check`, `npm test`, and the
local Wrangler OAuth integration suite. Use synthetic repositories for write
tests. Production read checks can compare hashes/counts without logging private
text. Successful server integration does not establish actual ChatGPT phone
client discovery, consent or write support; verify those in the target account.
