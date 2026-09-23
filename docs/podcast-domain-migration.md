# Moving the public podcast domain

The podcast's public files stay in the existing `mere-podcast-feed` R2 bucket. The
new site serves that bucket under `https://perandre.no/1mp/`; no audio files are
re-encoded or copied during the domain move. The publication manifest remains
private on the Mac. Sanity is not involved in feeds, show notes, or media.

## Cutover order

1. Deploy the `perandre.no` Worker with its `PODCAST_BUCKET` binding. Confirm that
   `/1mp/`, both feed paths, show artwork, one episode page, `HEAD` audio, and a
   one-byte audio range request all work on the final hostname.
2. In the private `podcast.toml`, set `base_url = "https://perandre.no/1mp"` and
   `previous_feed_base_url = "https://feed.mere.no"`. Keep the existing R2 bucket.
3. Preview the migration:

   ```sh
   python scripts/migrate_podcast_domain.py \
     --config /Users/pesh/Sites/zotero-audio-runtime/podcast.toml \
     --old-base https://feed.mere.no
   ```

4. When the new origin passes the checks, rerun with `--apply --sync`. This backs
   up the local manifest and both feeds, preserves episode GUIDs, publication
   dates, byte lengths, and immutable media keys, then uploads only the two
   updated RSS files. The new feeds include `itunes:new-feed-url`.
5. Configure HTTP 301 redirects from the two old feed paths to their new paths.
   Keep the old media hostname working for old app caches and bookmarks. Verify
   the new feeds in podcast directories and keep the redirect and feed tag for
   at least four weeks.

The migration can be previewed repeatedly. It changes only public URL fields in
the manifest and regenerated feeds. Local private audio and research Markdown are
untouched. Future publications use the new `base_url` automatically.
