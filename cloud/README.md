# One More Paper cloud service

The Cloudflare Worker hosts the private dashboard, ordinary REST API and authenticated MCP at `/mcp`. D1 indexes research Markdown and stores durable jobs. R2 Standard stores the current full research Markdown and quality reports under human-readable episode-title filenames. Extraction, Kokoro synthesis, audio assembly, public podcast publishing and iCloud copying run on the Mac. No AI service is required to operate the dashboard, CLI, search or job queue.

## Development

Requires Node 22 or newer. From this directory:

```sh
npm ci
npm run migrate:local
npm run dev -- --port 8797 --ip 127.0.0.1
```

Generate independent random values of at least 32 characters for `OWNER_ACCESS_KEY`, `SESSION_SECRET`, and `BRIDGE_TOKEN`, and place them in ignored `.dev.vars` with permissions `0600`. Do not reuse your Cloudflare API token as any application credential. The owner access key is a single-owner password intended to be generated with high entropy and saved in a password manager. Login uses a signed, HttpOnly, SameSite cookie; neither dashboard nor browser localStorage stores an API token. Session secret rotation signs out browsers. All three production values are Workers secrets, never configuration variables or committed files.

```sh
npm run check
npm test
npm run test:integration
npx wrangler deploy --dry-run
```

Integration tests require the local server at `http://127.0.0.1:8797`, read `.dev.vars` without printing secrets, and create clearly named test fixtures in local D1/R2/KV. They exercise actual OAuth registration, consent with CSRF, S256 PKCE code exchange, MCP initialization/tool calls, private documents, search, job claims, stale leases and scope isolation. `TEST_BASE_URL` and `TEST_SECRETS_FILE` override local settings. Remote tests require an explicit `ALLOW_REMOTE_INTEGRATION` because they create fixtures.

## Deployment and free-plan boundaries

Provision the `one-more-paper-library` D1 database and private R2 Standard bucket, plus an `OAUTH_KV` namespace. Fill the returned IDs in `wrangler.jsonc`. Keep R2 public development URL and public custom domains disabled. The existing public podcast bucket stays separate. Apply D1 migrations before deploying, then add the three Worker secrets using Wrangler’s secure stdin/file input. No paid plan upgrade is required or configured.

```sh
npm run migrate:remote
npm run deploy
```

The app limits the private library to 128 MiB of current Markdown/review content, 2,000 articles, 512 KiB per document, 500 changed-article uploads a day and 100 active jobs. It replaces old R2 mirror objects after the new human-readable object and D1 index commit; local processing retains source evidence and revisions. A sync-schema bump migrates existing current objects from ID/hash keys to episode-title keys. These limits provide substantial headroom inside the free allowances. They are not an account-wide billing cap: other Workers, R2 podcast files and other services share Cloudflare allowances. The Mac must poll no more frequently than once a minute when idle and sync only changed articles. Search uses FTS5 and metadata queries, not R2 bucket scans. CPU-intensive processing never runs in Workers.

Current free-plan references: [Workers limits](https://developers.cloudflare.com/workers/platform/limits/), [R2 pricing](https://developers.cloudflare.com/r2/pricing/), [D1 pricing](https://developers.cloudflare.com/d1/platform/pricing/). Validate these and the account’s actual usage before changing storage limits. The pinned Sharp override fixes the dev-only transitive libheif advisory in Wrangler/Miniflare; no image transformations run in the deployed app.

## Browser and MCP authentication

Open `/login`, enter the owner access key, and use the dashboard normally. Every asset and article response is authenticated; only sign-in, OAuth discovery and OAuth protocol routes are public. Cookie mutations require a matching Origin. Canonical article URLs open the sign-in screen when needed. Private article content is available to an authorized MCP client; it is never made public.

Use `https://<deployed-worker>/mcp` as the remote MCP URL. The official `@modelcontextprotocol/sdk` implements stateless Streamable HTTP using the stable `2025-11-25` protocol plus its supported prior protocol versions. The official `@cloudflare/workers-oauth-provider` implements OAuth 2.1, protected-resource/authorization-server discovery, S256 PKCE, audience validation, refresh-token rotation, revocation, Client ID Metadata Documents and a Dynamic Client Registration compatibility endpoint. We deliberately declare the protocol actually implemented by the stable SDK, rather than claiming every newer optional MCP extension.

- `library:read`: search, full article/project text, browse library and PhD documents, quality reports and processing status.
- `documents:write`: queue revision-checked PhD Markdown updates and meeting notes; inspect their save/commit/push receipts.
- `jobs:write`: create, cancel and retry Mac jobs. Request this alongside `library:read` for phone control.
- `zotero:write`: save references directly to the personal Zotero library online, with optional collection/tag additions. Works while the Mac is off. Read tools include `lookup_article`, `zotero_collections` and `article_import_status`.
- No OAuth scope grants Mac bridge ingestion rights or arbitrary filesystem/shell access.

Authorize each client through the owner login and consent screen. Consent states that full private Markdown is shared with the client. Read-only grants expose only read tools; token refresh narrowing updates the effective tool permissions. The OAuth provider supports revocation at `/oauth/token`; clients can revoke their own tokens. ChatGPT account/workspace policy may govern adding custom MCP servers and must be checked in the target account. The local integration checks do not substitute for connecting the actual phone client.

Tools: `search`, `fetch`, `library`, `status`, `review`, plus `create_job`, `cancel_job`, `retry_job` with write consent. Search/fetch return standard structured results and matching text JSON. Citation URLs are authenticated Markdown reader URLs. Complete files are returned within the upload bound; there is no silent truncation. Research content and QA excerpts are explicitly described as reference data, not agent instructions.

## REST and Mac bridge contract

For direct reference imports, configure the optional `ZOTERO_API_KEY` and
`ZOTERO_USER_ID` Worker secrets and reconnect MCP clients for `zotero:write`.
See [Zotero cloud imports](../docs/zotero-cloud-import.md) for setup, tools,
duplicate/retry guarantees and PDF limitations. These imports do not use the
Mac job queue. Migration `0006_zotero_imports.sql` stores private receipts.

Owner-cookie REST endpoints:

| Method     | Path                                   | Response                                                                   |
| ---------- | -------------------------------------- | -------------------------------------------------------------------------- |
| GET        | `/api/library?q=&limit=50&offset=0`    | `{items,total}`; full titles, metadata, artifact flags                     |
| GET        | `/api/articles/:id`                    | Article object                                                             |
| GET        | `/api/articles/:id/markdown`           | Full `text/markdown`                                                       |
| GET        | `/api/articles/:id/review`             | Full quality report Markdown                                               |
| GET        | `/api/articles/:id/audio?edition=full` | Redirect to synced public episode URL; private audio remains on Mac/iCloud |
| GET        | `/api/search?q=`                       | `{results:[{id,title,url,snippet}]}`                                       |
| GET        | `/api/status`                          | `{worker,counts,jobs,storage,capabilities,version}`                        |
| GET, PATCH | `/api/settings`                        | Settings, `updated_at`, `configured`                                       |
| GET, POST  | `/api/jobs`                            | `{jobs}` or `{job}`                                                        |
| GET        | `/api/jobs/:id`                        | `{job}`                                                                    |
| POST       | `/api/jobs/:id/cancel`, `/retry`       | `{job}`                                                                    |

Create job body: `{action:'markdown'|'full'|'brief'|'both'|'sync',scope:'new'|'all'|'one',article_id?,qa?,force?}`. A single article requires a known article ID. Every response includes the full title. Use `Idempotency-Key` on POST to retry safely. Progress is **0–1**; terminal statuses are `completed`, `failed`, `cancelled`. Usable audio with warnings is `completed` with warning text, not a failed job. Jobs are durable and wait while the Mac is offline.

Bridge requests use **only** `Authorization: Bearer <BRIDGE_TOKEN>`:

| Method     | Path                       | Body and response                                                                                 |
| ---------- | -------------------------- | ------------------------------------------------------------------------------------------------- |
| POST       | `/api/bridge/heartbeat`    | `{worker_id,title?,current_job_id?,local_url?,version?}` → `{ok,last_seen,poll_after_seconds:60}` |
| PUT        | `/api/bridge/articles/:id` | `{article,markdown?:string,review?:string}` → `{article}`                                         |
| POST       | `/api/bridge/jobs/claim`   | `{worker_id,lease_seconds?:300}` → `{job:null                                                     | Job}` |
| PATCH      | `/api/bridge/jobs/:id`     | `{worker_id,lease_token,status?,stage?,progress?,message?,title?,lease_seconds?:300}` → `{job}`   |
| GET, PATCH | `/api/bridge/settings`     | Same settings as dashboard                                                                        |

An ingested article requires a full `title`. Supported fields: `id`, `authors:string[]`, `year:number|null`, `source_url`, `license_status`, `markdown_status`, `audio_status`, `qa_status`, `warnings:array`, `audio_url`, `editions`, `zotero_url`, `collection`, `publish_status`, `icloud_status`, `backup_status`. `audio_url`/edition audio URLs must refer only to already published, license-eligible public audio. Never provide filesystem paths as cloud playback URLs. Private AAC files are not uploaded to R2. The body may omit Markdown/review for metadata-only synchronization; existing documents are preserved. Send Markdown at its completion, independently of audio. Article sync is serialized using a short per-article lease to keep storage accounting and revisions consistent.

A claimed job additionally contains `worker_id`, `lease_token`, and `lease_expires`. Renew before expiry, including during long synthesis. After a claim, the Mac resumes/reuses local work for that cloud job ID. Stop if a lease update returns `409 lease_lost`. Only one successful claim can own a job. A running cancel request returns `cancel_requested` during lease updates; acknowledge `cancelled` after a safe checkpoint. If a lease expires, another process may reclaim the job. Settings include `auto_generate` (`off`, `markdown`, `brief`, `full`, `both`; default `markdown`) for the Mac’s independent Zotero monitor. Older settings safely use the default. Deploy the cloud schema before a Mac begins syncing this field. Settings return `configured:false,updated_at:null` until first saved: seed from local defaults only then; later compare `updated_at` and apply the newest intentional user change.

Errors have `{error:{code,message,request_id?}}`. No article bodies, access keys, tokens or private URL query strings are logged. The response request ID connects user-visible failures to structured Workers logs.

## Private PhD project documents

See [PhD project MCP](../docs/phd-project-mcp.md) for tools, local opt-in
configuration, supported formats, limits, permissions and recovery. Migration
`0004_project_documents.sql` adds private project text/FTS and a durable edit
inbox independently of article queues. Existing MCP `search`/`fetch` include
project results; dedicated project tools expose exact paths and source revisions.

Bridge-only endpoints (same bridge bearer credential):

| Method | Path | Purpose |
| --- | --- | --- |
| POST | `/api/bridge/project/manifest` | `{worker_id,title,paths,warnings}` registers the one PhD writer and reconciles removed mirror paths. |
| PUT | `/api/bridge/project/document` | `{worker_id,document:{path,title,text,revision,format,source_modified_at}}` uploads changed text. |
| GET | `/api/bridge/project/changes?worker_id=...` | Returns the oldest queued immutable `{id,path,text,expected_revision}` request. |
| PATCH | `/api/bridge/project/changes/:id` | `{worker_id,status,result}` acknowledges completed/conflict/failed with safe message and optional revision, commit, pushed. |

Owner-authenticated `GET /api/project/document?path=...` provides a plain-text
citation URL. Only the configured bridge writer can ingest or acknowledge changes.
OAuth never grants bridge rights; `documents:write` never grants generation rights.
