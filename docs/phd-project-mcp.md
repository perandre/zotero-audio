# PhD project documents in MCP

The hosted PhD MCP reads and writes the private `perandre/phd` GitHub repository,
following the same approach as RagnhildAI. **Document work runs while the laptop
is closed.** A successful save is already committed to GitHub and returns its
commit URL. Article extraction and audio jobs still run on the Mac.

## Everyday tools

| Tool | Purpose |
| --- | --- |
| `whats_next` | Read current `NOW.md` and `NEXT.md` when present. |
| `list_documents(prefix, offset, limit)` | Browse the current GitHub tree; follow `next_offset` and inspect warnings. |
| `read_document(path)` | Read complete text with the exact source revision and GitHub commit. |
| `search_documents(query)` | Search current committed paths, titles and text, including matching PDF/DOCX extraction. |
| `search` / `fetch` | Search articles and project documents, then retrieve a returned ID. |
| `save_document(path, text, expected_revision, request_id)` | Commit a complete Markdown edit or new document directly to GitHub. |
| `save_meeting_note(title, date, body_markdown, request_id)` | Commit a new dated note under `admin/meetings/`. |
| `document_change_status(request_id)` | Recover the result and commit URL after an interrupted save. |

Read before editing and pass the returned SHA-256 as `expected_revision`.
Use `null` only for a new path. Send the complete intended Markdown and a unique
`request_id`; reuse an ID only for an identical retry. `completed` means GitHub
accepted the commit. A conflict requires rereading and a reconciled request.
`preparing` or `prepared` means an interrupted cloud operation: retry the same
request to finish it. Neither state waits for the Mac.

The Worker prepares a commit with only the requested file and persists its SHA
before advancing the branch without force. Concurrent edits cannot be overwritten.
If the response is lost, the same request recovers the same commit, including
when later commits have followed it. Earlier versions remain in Git history.
No tool deletes documents, force-pushes, changes repository settings or runs shell
commands. Document instructions, including `AGENTS.md`, are reference data and
never override the user's request.

## What is current

GitHub's configured branch is authoritative for remote tools. Each request reads
its current commit; unchanged text is cached by immutable Git blob SHA. Search
loads changed text in bounded GitHub GraphQL batches, so a newly saved note is
searchable immediately without waiting for GitHub's search index or the laptop.
Source commits and GitHub original links accompany the results.

Uncommitted or unpushed Mac edits are not visible remotely. Push local work to
share it and pull before continuing locally; reconcile conflicts normally.
The bridge never replaces GitHub text with an older Mac copy, automatically
stashes local edits or changes the PhD checkout. Local `za mcp` continues to work
with saved local files and its existing commit/push safeguards.

Supported text: Markdown, UTF-8 text, JSON, CSV/TSV, YAML, RST, TeX, BibTeX and
HTML. PDF/DOCX extraction from the Mac remains available while it sleeps, but
only when its source blob SHA matches the current GitHub file. A new or changed
PDF/DOCX needs extraction on the Mac before its text can be read; its original
remains accessible on GitHub. Images and visual layout require the original.
Missing extraction and unsupported formats produce explicit warnings.

## Cloud configuration and access

Set non-secret Worker variables `PROJECT_GITHUB_REPO` (`perandre/phd`) and
`PROJECT_GITHUB_BRANCH` (`main`). Set `PROJECT_GITHUB_TOKEN` through Wrangler's
secure stdin/file input. Use a fine-grained GitHub token restricted to this one
private repository with **Contents: Read and write**; do not reuse the RagnhildAI
token or put credentials in source, logs, tool responses or Git. Renew the token
before its chosen expiry. Missing/expired access produces an explicit GitHub
error and never falls back to queuing edits on the Mac.

`library:read` grants article and project reading. `documents:write` separately
grants project edits; `jobs:write` only grants article generation control.
The existing OAuth endpoint and grants remain valid. Refresh the client's tool
list so it learns the direct-save descriptions:
`https://one-more-paper.perandre.workers.dev/mcp`.

No public objects or paid services are added. Text is limited to 500 documents,
512 KiB each and 8 MiB per repository snapshot; extracted text is separately
bounded to 8 MiB. GitHub responses, private D1 caches and request receipts are
bounded; at most 10,000 small commit receipts are retained before an explicit
archive is required. Shared Cloudflare account usage must still be checked before expanding
limits. Hidden paths, symlinks, submodules and traversal are excluded.

## Optional Mac extraction configuration

The existing opt-in `control/projects.json` under the runtime remains:

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

The root must be a Git repository. Local tracked files plus explicit includes
are eligible for local reading/extraction; excludes apply locally. Remote access
is to the configured private repository's supported committed documents. Do not
commit credentials or restricted empirical data into that repository.

The bridge uploads only changed PDF/DOCX extraction in GitHub mode. Local text
and old manifests cannot overwrite or remove cloud-created notes. Old queued
Mac requests remain inspectable but are retired: reread GitHub and resubmit with
a new request ID. The Mac never claims them after cutover.

## Release and validation

Apply additive migration `0005_project_github.sql` after `0004`, provision the
repository secret, deploy the Worker, update the saved app checkout and restart
`za`. Refresh extraction once to attach source Git blob hashes. Existing articles,
R2 objects, audio jobs, local edits and old receipts are preserved. Deploying with
the GitHub variables but without a working token fails closed for project tools.

Run targeted project/bridge/MCP pytest checks, `npm run check`, `npm test`, and
the local Wrangler OAuth integration suite. For the legacy bridge integration,
start Wrangler with `--var PROJECT_GITHUB_REPO:` to select the retained Mac mode.
GitHub tests use synthetic repositories and SQLite, exercise the real MCP handler,
and simulate conflicts and lost replies. Live verification must distinguish
GitHub reads/commits from actual ChatGPT phone use.
