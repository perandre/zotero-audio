# Backfill published episode descriptions

The maintenance script adds source-grounded abstracts and edition context to
episodes already present in the publication manifest. It reads Zotero metadata
through Zotero's local GET API when available, then uses bounded Crossref,
DataCite or arXiv publisher/repository metadata, then a source-matched abstract
extracted from the PDF. It does not create AI summaries. An unavailable
abstract remains clearly unavailable.

The script never selects imported or newly generated audio for publication. It
updates only records that already have a GUID, publication date, audio enclosure,
byte length and supported edition in the publication manifest. Existing GUIDs,
enclosure URLs, byte lengths, publication dates, source hashes and audio files
are checked before any write. It does not render audio, rewrite transcripts or
replace artwork.

RSS notes are rewritten only when a new record's `generated_show_notes` marker
matches the current notes, or a legacy note matches the old generated layout
and its first paragraph matches known source metadata. Other existing notes are
preserved. The generated template retains paired-edition links and discloses
AI narration. Every record stores `description_abstract` and
`description_abstract_source` (`zotero`, `doi-metadata`, `pdf-extraction`, or
`unavailable`) for later idempotent runs.

Stop the local service before applying so it cannot write the publication
manifest concurrently. Preview first; preview performs read-only lookups and
prints source counts without printing abstracts or credentials:

```sh
REPO=/Users/pesh/Sites/zotero-audio
RUNTIME=/Users/pesh/Sites/zotero-audio-runtime
"$RUNTIME/venv/bin/python" "$REPO/scripts/backfill_public_descriptions.py" \
  --config "$RUNTIME/podcast.toml" --no-network
```

`--no-network` previews cached/local sources only. A full read-only preview
performs Zotero GET lookups and DOI metadata requests. It reuses a DOI lookup
for the Brief and Full Reading of the same source. Before applying, review the
aggregate source counts, recognized legacy notes and preserved-note count.
Apply locally to back up the publication
manifest, both feeds, site index and episode pages (including available HTTP
header sidecars), then update the pages and feeds:

```sh
"$RUNTIME/venv/bin/python" "$REPO/scripts/backfill_public_descriptions.py" \
  --config "$RUNTIME/podcast.toml" --apply
```

Review the changed metadata, RSS descriptions and episode pages in the local
public mirror. Upload only the episode pages, site index and both feeds with:

```sh
"$RUNTIME/venv/bin/python" "$REPO/scripts/backfill_public_descriptions.py" \
  --config "$RUNTIME/podcast.toml" --apply --sync
```

`--apply --sync` is safe to rerun after an interrupted upload. Local description
selection is cached in the publication manifest; the uploader compares object
hashes, sends changed pages and index first, and sends feed XML last. The backup
directory is printed by each apply run. Verify the public site, a representative
Brief and Full episode page, and both public feeds, then restart the local
service. A successful upload confirms the objects reached R2; it does not
confirm Apple Podcasts or other client caches have refreshed.

The public site is a separate Next.js app in
`/Users/pesh/Sites/perandre.no/podcast`. Its pages read RSS and may remain cached
until that site rebuilds or revalidates them (currently up to daily). Updating
the local public mirror and R2 feeds does not by itself rebuild that separate
website; verify deployed episode pages after its refresh path runs.
