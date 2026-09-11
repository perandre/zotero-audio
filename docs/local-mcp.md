# Local MCP and the portable plugin

Run `za mcp` to start the local stdio server. It uses the official Python
`mcp.server.fastmcp.FastMCP` implementation; all non-protocol logging stays on
stderr. It starts the ordinary local daemon when needed and delegates to the
same SQLite catalog and durable worker jobs as the CLI/dashboard. No second
generation implementation or AI dependency is introduced.

The [portable plugin](../plugins/one-more-paper/README.md) follows Agent Plugins
1.0.0. Its root `plugin.json` and `mcp.json` were validated against the canonical
schemas at agent-plugins.org. The package launches `za mcp`; install the app so
`za` is available in the client's executable search path. No marketplace or
client-specific configuration is installed automatically by this package.

The tools are:

| Tool | Purpose |
| --- | --- |
| `search(query)` | Full-text search with titles, excerpts and reader URLs. |
| `fetch(id)` | Complete research Markdown and source/QA metadata. |
| `list_library(query, limit, offset)` | Saved articles with full titles and artifact status. |
| `create_job(action, scope, article_id, qa, force, idempotency_key)` | Queue Markdown/audio/catalog work. |
| `get_job(id)` / `list_jobs(limit)` | Actual progress and persisted outcomes. |
| `cancel_job(id)` | Cancel while keeping existing outputs/cache. |
| `retry_job(id)` | Resume failed/cancelled work without force; retries are idempotent. |
| `review_article(id)` | Complete QA findings and the AI review brief. |

The `article://{article_id}/markdown` resource also exposes complete Markdown.
Tool results have both `structuredContent` and a JSON text content block for
clients that need either representation. Full text is never silently truncated
by this adapter. Search URLs lead to the local reader at
`http://127.0.0.1:8765/articles/{id}`; these URLs are useful on the Mac. The
Cloudflare MCP adapter uses remotely accessible authenticated reader URLs.

Stdio runs with the local user's existing access, including private documents.
There is no arbitrary shell/file-read tool. Remote access is a separate
Cloudflare HTTPS Streamable HTTP endpoint protected by OAuth. The local stdio
transport cannot be added as a remote phone URL, and private retrieval never
overrides the podcast publication licensing gate.

Research Markdown is stored locally inside each article bundle under a filename
matching the human-facing episode title, for example
`How companies use AI - Author (2025).md`. The cloud bridge mirrors the current
full document to R2 using the same readable filename. Legacy `article.md` paths
are migrated when the local daemon opens the article; article IDs and catalog
references remain stable.

Verification uses the official MCP Python client over real subprocess stdio,
including initialization, tool/resource discovery, structured and full-text
retrieval, unknown IDs, invalid arguments, job idempotency, cancellation and
state persistence across client reconnections. SDK 1.30.0 was exercised; it
negotiates MCP 2025-11-25 and supports earlier declared versions. This does not
claim support for optional extensions or newer protocol versions that the SDK
does not advertise. ChatGPT's remote client is tested separately against the
deployed Cloudflare endpoint.

Run the targeted checks using the configured environment:

```sh
python -m pytest tests/test_app_mcp.py tests/test_generation.py
```

The core application's Python dependency declaration installs the official SDK;
running generation, searching through the CLI, or using the dashboard still
requires no AI service or subscription.

## PhD workspace

With `control/projects.json` configured, `whats_next`, `list_documents`,
`read_document`, `search_documents` and `save_document` operate on the saved
PhD folder. `search` and `fetch` also handle project results. Local saves are
synchronous and use the same revision checks, backups and scoped Git workflow
as remote saves. See [the complete project document contract](phd-project-mcp.md).
