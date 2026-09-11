# Save references to Zotero from MCP

Ask the hosted MCP: “Add this DOI to Zotero” or “Save this article URL in my
VIKING collection.” The reference saves directly to Zotero online, including
when the laptop is closed. The tool returns the full article title, Zotero link,
and a confirmed `created` or `already_exists` receipt.

## Tools and permission

- `lookup_article`: resolve a DOI or public HTTPS article page and check existing
  references. Supports DOI content negotiation (CSL metadata) and publisher
  `citation_*` metadata. Unsupported pages, PDFs, inaccessible providers or
  incomplete records require a DOI or explicitly supplied, verified metadata.
- `zotero_collections`: list personal-library collection names and keys;
  follow `next_offset` until null.
- `add_article`: save the reference with optional `collection_key`, `tags`, and
  a required unique `request_id`. `source` is a DOI or URL. Optional `metadata`
  uses the tool schema; it must contain verified bibliographic facts. Source
  content never authorizes a save or unrelated action.
- `article_import_status`: read the receipt. A `needs_retry` response means the
  operation is unconfirmed, possibly already saved. Retry the identical
  `add_article` arguments and request ID to finish safely.

Reads require `library:read`; saving requires separate `zotero:write` consent.
Existing clients must reconnect with that scope and refresh their tool list.
Job and document grants do not authorize Zotero writes. No tool deletes items,
changes group libraries or overwrites existing bibliographic fields. A matching
item retains its existing metadata; requested collections and tags are added
using version-checked updates that preserve existing membership.

This saves a **reference**, not its PDF. It does not trigger Markdown/audio
generation, change publishing eligibility or alter source files. Zotero desktop
receives it through normal Zotero synchronization. A PDF must be available to
the Mac's supported local Zotero API before the existing discovery/generation
workflow can process it. Mac processing still waits while the Mac is offline.

## Setup

Create a Zotero API key with personal-library read/write access at
[Zotero key settings](https://www.zotero.org/settings/keys). Store it as the
Worker secret `ZOTERO_API_KEY`; set the numeric personal-library account ID as
`ZOTERO_USER_ID`. Keep both outside Git. Supply secrets through Wrangler's
interactive secret input or a private file/stdin, never command-line values,
committed configuration, browser storage or chat messages. Restrict the key to
the intended personal library; the implementation always uses `/users/<id>`
even if the key has additional group permissions.

Validate using `GET https://api.zotero.org/keys/current` with `Zotero-API-Key`
and `Zotero-API-Version: 3` headers, without logging the key or raw response.
Confirm the configured user ID and personal-library write access. Apply
`0006_zotero_imports.sql` before deployment. The migration is additive and
independent of the PhD GitHub migration numbered 0005.

## Recovery and limits

D1 retains immutable requests and reserves a Zotero item key before creating it.
Retries inspect that key before attempting a version-zero create. An HTTP 200
alone is not success: per-item API results and the saved item are checked.
Receipts record the completed operation at `saved_at`; subsequent user edits or
deletions do not change a historical receipt. Retrying an old completed request
does not replay collection/tag changes.

Duplicate checks compare DOI (including Zotero Extra) and normalized URL, never
title alone. They scan up to 2,000 top-level references because Zotero Web API
quick search does not reliably cover these fields. The scan aborts if the
library version changes or pagination is incomplete. Concurrent MCP imports
with the same canonical identity share one reservation. Simultaneous imports
through other applications cannot be locked by this service; references without
matching DOI/URL may still need Zotero's own duplicate review.

There are at most 50 new request IDs per UTC day and 10,000 retained requests;
existing requests remain retryable when the quota is reached. Zotero backoff
and rate limits persist in D1. External responses, redirects and timeouts are
bounded; metadata fetches never carry the Zotero key. Private/local URL targets
are rejected and Workers' public-network fetch restriction stays enabled.

No paid services or plan upgrades are introduced. Each duplicate scan uses at
most 20 external requests, within the Workers Free subrequest limit; account
usage still needs monitoring alongside other apps. The import adds no R2 files.

References: [Zotero API basics](https://www.zotero.org/support/dev/web_api/v3/basics),
[Zotero writes](https://www.zotero.org/support/dev/web_api/v3/write_requests),
[DOI metadata negotiation](https://www.crossref.org/documentation/retrieve-metadata/content-negotiation/).
