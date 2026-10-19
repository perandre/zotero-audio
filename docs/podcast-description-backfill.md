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

The public site is a separate static app in
`/Users/pesh/Sites/perandre.no/podcast`. It displays published RSS descriptions
and recording transcripts; it does not read the private canonical `papers/`
Markdown. Repairing research Markdown alone therefore does not update an episode
page, its existing recording, or its transcript.

For description changes, run this backfill on the active publisher and sync the
updated feeds. The website's freshness worker detects feed changes and dispatches
the `Podcast site` build/deploy workflow. For an immediate refresh, run that
existing GitHub workflow after verifying the live RSS. Verify the deployed page
as well as the feed; a successful R2 upload alone does not establish that the
static page refreshed. Transcripts remain matched to the actual recording;
changing narration requires explicit audio regeneration and publication.

The exact legacy “A brief/full audio edition of this paper” boilerplate can be
upgraded when its author/metadata/link rows match the saved record. It is not a
manual edit merely because it predates `generated_show_notes`. Genuine custom
prose and changed metadata rows remain preserved. The backfill also clears an
incorrect note-derived manual flag only when the saved abstract is unedited.
