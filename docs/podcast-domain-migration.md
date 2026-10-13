# Moving the public podcast domain

The podcast lives at `https://1morepaper.com/`. Its public files remain in the
existing `mere-podcast-feed` R2 bucket. The site's Worker serves that bucket;
audio files are neither re-encoded nor copied. The publication manifest remains
private on the Mac. Sanity is not involved in feeds, show notes, or media.

The private `podcast.toml` uses:

```toml
base_url = "https://1morepaper.com"
previous_feed_base_url = "https://perandre.no/1mp"
```

The September 2026 migration updated the manifest and both RSS feeds with:

```sh
python scripts/migrate_podcast_domain.py \
  --config /Users/pesh/Sites/zotero-audio-runtime/podcast.toml \
  --old-base https://perandre.no/1mp --apply --sync
```

The command backs up the local manifest and feeds, preserves episode GUIDs,
publication dates, byte lengths, and immutable media keys, and uploads only the
two revised RSS files. The feeds include `itunes:new-feed-url` pointing to the
new domain. Future publications use the configured `base_url` automatically.

The personal site redirects former `/1mp/` pages and RSS paths to the new site
and keeps old audio URLs available. The `feed.mere.no` Worker redirects its two
RSS paths to the new feeds and continues serving old media URLs for app caches.

## Apple Podcasts compatibility

Public M4A enclosures and upload metadata use Apple's documented
`audio/x-m4a` media type. The website must serve public artwork and media with
`Last-Modified`, `ETag`, working HEAD requests, and byte-range responses.
Cloudflare Browser Integrity Check must not challenge public podcast GET/HEAD
requests; use a rule scoped to the public feed, show-artwork, and episode paths.
Keep private library routes and their authentication separate.

After a hosting or metadata repair, refresh the feed in Apple Podcasts Connect
and inspect its imported details before publishing. A Draft or successful HTTP
check alone does not confirm Apple has accepted the show.

Public show covers are exported from the original PNG designs as 3000 × 3000
RGB JPEGs at 72 dpi. The current exports are below Cast Feed Validator's 500 KB
recommendation; the source PNGs and private cover exports remain unchanged.
Artwork objects use content hashes and fresh `.jpg` names so clients see the
new versions. Apple accepts both JPEG and PNG and does not specify a 500 KB
limit in its current Show Cover specifications.

Set `feed_base_url = "https://www.1morepaper.com"` in `[podcast]` to make the
Atom self link and migration tag match the canonical, directly served feed.
This leaves `base_url` and existing media URLs intact. Without that optional
setting, feed URLs continue to use `base_url`. Keep the migration tag and old
feed redirects for at least four weeks after migration, as Apple requires;
validators may report an informational migration warning during that period.
