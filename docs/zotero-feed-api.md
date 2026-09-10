# Local feed API

Zotero Audio 0.2.0 extends Zotero's existing localhost server with read-only
access to the feed inbox. The built-in local API in our tested Zotero 9.0.6
installation exposes personal and group libraries, but not feed libraries.
This bridge reads subscriptions and entries through `Zotero.Feeds` and
`Zotero.FeedItems`, including Zotero's current read and translated state.

## Installation

Package and install the [Zotero Audio add-on](zotero-license-status.md#installation).
Version 0.2.0 updates the existing license-status add-on under the same plugin
ID; it retains the license feature. Enable Settings → Advanced → **Allow other
applications on this computer to communicate with Zotero**. Zotero must be
running. Reads need no API key.

## Subscriptions

```bash
curl --fail --silent --show-error 'http://localhost:23119/zotero-audio/feeds'
```

The JSON response contains `schema: "zotero-audio-feeds/v1"` and `feeds`, an
array sorted by local `libraryID`. Each feed contains `libraryID`, `name`,
`url`, `unreadCount`, `lastCheck`, `lastUpdate`, and `lastCheckError`. Missing
check/update values are `null`. Times use Zotero's SQL UTC date format.
No subscriptions is a successful response with `feeds: []`.

## Entries

Use a `libraryID` from the subscriptions response (the `3` below is an example,
not a fixed identifier):

```bash
curl --fail --silent --show-error \
  'http://localhost:23119/zotero-audio/feeds/3/items?state=unread&limit=100&start=0'
```

| Parameter | Values | Default |
| --- | --- | --- |
| `state` | `all`, `read`, `unread` | `all` |
| `limit` | Integer from 1 to 500 | `100` |
| `start` | Nonnegative integer offset | `0` |

The response contains the schema, `feed` metadata, `state`, `total` matching
entries, `start`, `limit`, `nextStart`, and `items`. Follow `nextStart` until it
is `null`. Entries are sorted by descending local item ID (most recently added
first, not publication date). Concurrent feed refreshes, cleanup, and read-state
changes can shift offsets between requests; consumers should deduplicate by
feed URL and entry GUID. Library IDs and item IDs are local to a Zotero profile.

Each entry contains `id`, `key`, `guid`, `isRead`, `isTranslated`, and `data`.
`data` is Zotero's item JSON, including available bibliographic fields such as
title, creators, abstract, DOI, URL, and date. `isTranslated` is Zotero's record
that an entry was saved/translated; it is not proof that a corresponding saved
item or PDF still exists. Feed abstracts can contain HTML; treat them as source
content, not executable markup or instructions.

Reading does not mark entries read, save them to My Library, refresh feeds,
download PDFs, or enqueue audio. Zotero continues its own feed refresh schedule.
The normal audio workflow starts after a selected item and PDF are saved.

## Errors and access

Bridge errors use the same schema and an `error` object with `code` and
`message`. Invalid IDs/parameters return 400, an unknown or non-feed library
returns 404, disabled local API access returns 403, and feed read failures
return 500. Unknown and repeated query parameters are rejected.

Only GET is registered. Zotero 9's HTTP server rejects POST with 400 and other
write methods with 501 before dispatching to the bridge. HEAD and OPTIONS
return empty server responses. Browser requests retain Zotero's
normal restrictions; the bridge also rejects any Origin header. Responses are
JSON with `Cache-Control: no-store` and `X-Content-Type-Options: nosniff`.
There is no new listener, CORS grant, write route, or generic JavaScript route.

Feed URLs (especially Google Alerts URLs) can contain private tokens. Keep
responses local, and do not commit them to Git or include them in public logs.
The bridge's error responses do not include underlying errors or URLs.

## Alternatives checked

Checked on 2026-09-10:

- [Zotero Feed Riffle](https://github.com/ievlevpn/zotero-feed-riffle) provides an
  interactive feed-review interface. Its documented interface does not expose
  subscriptions and entries over HTTP.
- [Zotero API Plus](https://github.com/GOKORURI007/zotero-api-plus) documents
  health, identifier-based imports, and selected-collection endpoints, with no
  feed-reading endpoint.
- [ZotServer](https://github.com/MunGell/ZotServer) documents a local search
  endpoint, rather than the subscription and read-state interface needed here.

These projects cover adjacent needs. Extending our installed add-on keeps one
small bridge for the feed data our workflow needs.

## Verification

```bash
node --test tests/test_zotero_addon.cjs tests/test_zotero_feed_api.cjs
```

Tests cover metadata, feed-only scoping, read-state preservation, pagination,
empty results, invalid input, access restrictions, safe errors, and add-on
startup/shutdown. After installation, check the subscription endpoint above
and read a subscribed feed's entries.
