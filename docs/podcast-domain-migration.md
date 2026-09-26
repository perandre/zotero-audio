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
