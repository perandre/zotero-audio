# Podcast publication automation plan

## Recommendation

Add podcast publication as a final, isolated stage after a listener-ready build.
Do not publish the current page-flat speech plans unchanged: first upgrade
extraction and planning into a structured narration pipeline with a content-
quality gate. That gate applies to private listener files as well as public
episodes; a technically valid M4A is not necessarily fit to hear.

Keep private delivery as the default outcome. A listener-ready paper may enter
the public podcast only when both of these additional gates pass:

1. The item is explicitly selected for publication, preferably by membership in
   a dedicated Zotero collection or by a `podcast` tag.
2. The exact source version has a machine-verifiable license on a strict
   allowlist.

Publish through self-hosted RSS feeds and static object storage rather than by
automating the Spotify for Creators web interface. Spotify needs to be given the
two feeds and each show verified once; after that it polls them for changes. This
keeps the recurring workflow independent of Spotify's UI and also makes the shows
usable in other podcast apps.

```text
Zotero PDF + parent-item metadata
    |
    v
structured extraction -> canonical document -> narration transform
    |                                           |-- Brief plan
    |                                           \-- Full Reading plan
    v
content QA -- failed/uncertain ------------------> needs review; no delivery
    |
    v
synthesize -> assemble -> audio QA --------------> private iCloud artifacts
    |
    v
publication intent gate
    |
    v
license resolver -- not proven/allowed ----------> private only + reason
    |
    v
public artifact builder
    |-- paired audio, covers, transcripts, chapters, and metadata
    v
upload immutable assets -> verify URLs -> replace two RSS feeds last -> Spotify
```

This is not legal advice. The implemented gate is deliberately more
conservative than the maximum use a license might permit.

## Implementation status

The repository now implements the credential-free automation boundary: paired
edition narration plans and cache reuse, private iCloud routing, content and
final-AAC quality gates, exact-source license evidence, deterministic artwork,
timed VTT/HTML/Markdown transcripts, MP4 and JSON chapters, complete sibling RSS
feeds, a static landing site, content-addressed local publication, feed-last
transactions with rollback, dry-run/idempotency controls, launchd integration,
and local/remote health checks.

Two deliberate fail-closed boundaries remain configuration or infrastructure,
not hidden automation. Page-flat PDFs that do not provide reliable scholarly
structure are classified `needs_review`; the system does not pretend that a
generic extractor fixed them. The public directory must be mounted or
synchronized to the chosen HTTPS object store, and the two feed URLs must be
submitted to Spotify once after the domain, public verification email, and show
identity are supplied. No repository code contains hosting credentials or
automates Spotify's web interface.

## Why RSS should be the integration boundary

Spotify accepts externally hosted podcasts through an RSS feed. Its current
delivery specification supports MP4 with AAC-LC, which matches this project's
M4A output, and describes RSS metadata and per-episode artwork. Spotify checks a
subscribed feed several times per hour and respects ETag/Last-Modified headers.
It also warns that changing only the bytes behind an existing audio or artwork
URL does not cause a refresh, so corrected assets must get a new URL.

Relevant current platform documentation:

- [Spotify Podcast Delivery Specification v1.10](https://assets.ctfassets.net/jtdj514wr91r/4r4op9KhH3fY1t3BKt2eiH/7b1b682acf4c41baf79f9574d6dedd7f/Podcast_Delivery_Specification_v1.10_-_master_doc.pdf)
- [Submitting or claiming an externally hosted show](https://support.spotify.com/mw/creators/article/adding-a-new-show-to-your-spotify-for-creators-account/)
- [Managing Spotify transcripts](https://support.spotify.com/uk/creators/article/managing-episode-transcripts-on-spotify/)
- [Spotify episode chapters](https://support.spotify.com/me-en/creators/article/episode-chapters/)
- [Podcasting 2.0 transcript tag](https://podcasting2.org/docs/podcast-namespace/tags/transcript)
- [Podcasting 2.0 chapters tag](https://podcasting2.org/docs/podcast-namespace/tags/chapters)
- [Apple Podcasts chapters](https://podcasters.apple.com/support/5482-using-chapters-on-apple-podcasts)

A static feed avoids a database, web application, admin panel, or always-on
process. A small object-storage bucket behind a custom HTTPS domain is enough.
The host must support public HTTPS, HEAD requests, byte-range requests, stable
URLs, correct content types, ETag, and Content-Length. An S3-compatible store or
podcast host with a documented publishing API can satisfy this; a browser-only
host cannot provide maintenance-free publication.

## Public/private policy

### Publication intent

Use a dedicated Zotero collection such as `Podcast Queue` as the safest trigger.
This prevents a freely licensed paper added for unrelated research from being
published unexpectedly. The synchronizer continues to read Zotero's database in
read-only mode.

The state machine should be explicit:

```text
private_ready -> not_selected
private_ready -> license_pending -> public_ready -> published
                                \-> private_only
                                \-> needs_review
```

An item in `needs_review` never uploads anything and never appears in RSS. The
system should notify only for a new review item or a persistent failure; success
should be silent.

### Two editions per paper

Generate two related listening editions from one source record:

| Edition | Contents | Typical role |
| --- | --- | --- |
| **Brief** | Spoken identification, author-provided abstract, optionally the authors' conclusion when it is confidently detected and the total remains concise, then the closing attribution | Discovery and everyday listening |
| **Full Reading** | Spoken identification followed by the verified main-text listening copy, with parenthetical citation clusters and references omitted, narrative attribution retained, and visual material handled explicitly, then the closing attribution | Deep listening and archival access |

Call the short version `Brief`, `Abstract Edition`, or another explicit name;
do not call it a summary unless a summarization model actually produced it.
Version one should not generate new factual claims. Reading the authors'
abstract verbatim, and optionally their clearly delimited conclusion, is more
reliable and needs no editorial fact-checking. Aim for roughly 3–10 minutes and
omit the conclusion when it would make the edition long or cannot be extracted
with high confidence. If no abstract can be identified, mark the Brief as
unavailable rather than inventing one.

Create both editions for the private iCloud library only after content and audio
QA pass. This costs little because the abstract speech segments can be shared
with the Full Reading. Public copies are still created only after publication
intent and license approval.

Use two sibling podcast feeds rather than inserting two nearly identical
episodes into one feed:

- the primary show contains Brief editions and is optimized for discovery;
- a companion `Full Readings` show contains the long editions;
- both are generated by the same job, hosted under the same domain, and use one
  paper landing page that links the paired editions;
- each show is submitted to Spotify once, after which publication is automatic.

This adds a small one-time setup cost but lets listeners follow only the format
they want and avoids doubling every subscriber's episode list. A single-feed
mode can remain available for a simpler pilot, but must add `Brief` or `Full
Reading` to otherwise identical episode titles. Do not rely on
alternate-enclosure extensions for Spotify: its delivery specification models
one primary enclosure per RSS item.

## Listener-ready document model

### Why the current extraction cannot be published unchanged

The 2026-09-02 corpus audit covered 21 generated `article.md` paths: 12
byte-distinct renderings of nine source PDFs. The findings must become regression
requirements rather than one-off cleanup notes:

- the seven current academic bundles contain essentially one text block per PDF
  page, so no ordinary Abstract, Introduction, Methods, Results, Discussion, or
  Conclusion headings survive;
- reference suppression fails in every academic source because `References` is
  embedded in a page-sized block instead of appearing as an exact standalone
  heading;
- in the six delivered academic M4As, the tails beginning with the reference
  lists account for approximately 143 minutes of audio;
- publisher furniture, download notices, correspondence addresses, footnote
  markers, tables, equations, and citation clusters are treated as ordinary
  prose;
- PDF metadata produces wrong or incomplete titles and authors for several
  sources even though their Zotero parent items contain better metadata;
- Norwegian material is accepted even though the configured Kokoro frontend is
  English and cannot provide publication-quality Norwegian pronunciation.

The existing `article.md` is an archival rendering, not the input actually
consumed by synthesis: both it and `speech-plan.json` are independently derived
from `structure.json`. Keep that separation explicit. Never hand-edit generated
Markdown and expect the audio to change.

### Canonical structure and extraction routing

Create a versioned canonical document before making any speech plan. Its blocks
must carry a semantic role, source page/location, verbatim source text, and
confidence. At minimum support:

- title, subtitle, ordered authors, abstract, keywords, and publication data;
- numbered and unnumbered section headings with hierarchy;
- body paragraphs, quotations, lists, captions, and callouts;
- narrative and parenthetical citations as distinct inline spans;
- footnote markers and footnote bodies;
- display mathematics and inline mathematics;
- tables, figures, algorithms, code, and supplementary material;
- acknowledgments, funding, conflicts, data availability, author biographies,
  references, appendices, and publisher/page furniture.

Use metadata and content sources in this order:

1. Zotero parent-item metadata for bibliographic identity and item type.
2. Structured full text such as JATS XML, TEI, or publisher/repository HTML when
   it is tied to the exact licensed source version.
3. A local layout-aware PDF extractor for headings, columns, reading order,
   tables, captions, footnotes, and mathematics.
4. `pypdf` page text only as a diagnostic fallback. Page-flat extraction cannot
   automatically become a listener-ready or public edition.

Compare title, author, year, and abstract signals across sources and retain both
the selected value and its provenance. Material disagreement, implausible
reading order, or low structural confidence becomes `needs_review`.

Classify the Zotero item before choosing a template. Journal articles,
conference papers, working papers, preprints, reports, books, and procurement
documents need different introductions and section expectations. A non-article
must never be described as published in a journal or licensed as an article.

### Narration layer and transformation log

Derive a separate, versioned `narration-plan.json` and human-auditable
`narration.md` from the canonical structure. Every narration node must retain:

- `source_block_ids`, source page/location, and verbatim `source_text`;
- final `spoken_text` and its hash;
- node kind such as author prose, heading, narrator cue, visual description, or
  closing attribution;
- transformation codes and reasons, for example `citation-omitted`,
  `reference-section-omitted`, `table-cued`, or `abbreviation-expanded`;
- extractor, narration-policy, pronunciation-lexicon, and model versions.

This layer is the only source for synthesis and timed transcripts. Preserve the
authors' prose except for deterministic speech normalization. New connective or
descriptive text is a narrator insertion and must never be represented as an
author quotation. Version one should not use unconstrained generative rewriting
of the paper.

### Spoken-content policy

Apply these rules consistently to Brief and Full Reading plans:

1. **Headings.** Speak meaningful headings without outline numbers unless the
   number carries meaning. Give major headings a longer pause and a modest
   prosodic reset. Do not treat short table cells as headings.
2. **Numeric citations.** Suppress bracketed, parenthesized, and superscript
   citation markers and ranges. Citation detection must be structural or
   context-aware so years, sample sizes, numbered lists, and mathematical values
   are not removed.
3. **Author-year citations.** Omit purely parenthetical citation clusters.
   Preserve narrative attribution: `Smith et al. found` becomes `Smith and
   colleagues found`; do not delete the author from the sentence.
4. **Footnotes.** Omit citation-only footnotes. Move a substantive note to the
   end of its sentence or section with a short `Note:` cue. Never speak glued
   markers such as `11See`.
5. **Front and back matter.** Omit running headers, page numbers, download and
   citation notices, correspondence addresses, raw URLs, keyword lists, author
   biographies, publisher boilerplate, and duplicate title/author blocks. Use
   verified metadata for the introduction instead.
6. **References.** Omit the bibliography in every listener edition, private or
   public. A reference boundary detected only heuristically must be reviewed; it
   must never silently truncate uncertain body content.
7. **Appendices.** Omit raw instruments, codebooks, table-only appendices, and
   supplementary forms by default. Include a substantive prose appendix only
   when the edition policy explicitly enables it and it passes the same content
   QA. State the actual appendix decision in the changes notice.
8. **Pronunciation.** Normalize abbreviations, initialisms, units, percentages,
   statistical notation, and common domain terms using a versioned lexicon.
   Preserve names and diacritics in display text while permitting a separate,
   reviewed spoken pronunciation.

Do not use the same function for archival cleanup and spoken normalization.
Removing a URL or changing mathematical notation may be appropriate in
`spoken_text` but would lose information in the canonical source record.

### Tables, figures, and mathematics

Never send a flattened table grid or unexplained symbol stream to TTS. Resolve
each non-prose object with one of these explicit outcomes:

| Object | Default spoken treatment |
| --- | --- |
| Simple inline expression | Deterministically verbalize it, preserving operands, relation, and units. |
| Complex display equation | Speak its author-provided explanation; otherwise cue that the equation is available in the transcript. |
| Figure | Speak a concise cue using the caption and the authors' surrounding interpretation. Prefer supplied alt text when it accurately describes the visual. |
| Small, genuinely listenable table | State the purpose and a few labeled values only when the structure is unambiguous. |
| Regression or wide table | Do not read cells. Cue the table and continue with the authors' interpretation in the prose. |
| Unresolved visual or mathematical object | Mark the edition `needs_review`; never improvise a factual summary. |

Keep the complete caption, table, formula, and source-page link in a companion
article page adjacent to the HTML transcript, clearly marked as source material
that was not spoken. The VTT and transcript view must contain exactly what the
edition says. Any generated visual description must be grounded in extracted
structure, labeled as narrator text, and either deterministically verifiable or
manually approved.

### Content-quality gate

Write `content-qa.json` before synthesis. A journal-article plan fails closed
when any of these conditions holds:

- bibliographic identity is missing or conflicts materially across sources;
- the document is still page-flat, has implausible reading order, or lacks
  meaningful section structure without a genre-specific explanation;
- an intended Brief lacks a confidently bounded author-provided abstract;
- a reference list, raw table grid, publisher boilerplate, email address, URL
  fragment, or citation marker remains in spoken text;
- a long fused alphabetic token, Unicode replacement character, private-use
  glyph, or unexplained mathematical-symbol sequence remains;
- a table, figure, formula, appendix, or footnote has no recorded narration
  outcome;
- the requested voice frontend does not support the detected language;
- the narration policy cannot account for every omission and narrator insertion.

Warnings from the TTS frontend about word-count or phoneme mismatches belong in
segment QA and must not disappear into logs. Reject or regenerate the affected
segment with corrected spoken text. Maintain approved canonical and narration
fixtures for all nine audited sources, including multi-column layouts,
author-year and numeric citations, regression tables, equations, appendices,
false heading candidates, and the Norwegian non-article.

### Initial license allowlist

For the first release, automatically allow only licenses whose canonical URL
and applicable source version are known:

| Source license | Automatic result | Episode license |
| --- | --- | --- |
| CC BY 4.0 | Publish | CC BY 4.0 |
| CC0 1.0 or Public Domain Mark 1.0 | Publish | CC BY 4.0 for any rights in the produced recording, with a clear source-status notice |
| CC BY-SA 4.0 | Publish only after ShareAlike output handling is implemented and tested | CC BY-SA 4.0 |
| Older/ported CC BY licenses | Review initially | Determined during review |
| CC BY-NC or CC BY-NC-SA | Private only | Not applicable |
| Any NoDerivatives license | Private only | Not applicable |
| "Open access", "free to read", or an OA URL without an exact license | Private only | Not applicable |
| TDM-only or publisher-specific terms | Private only unless manually approved | Not applicable |
| Missing, conflicting, or embargoed license | Private only/review | Not applicable |

CC BY 4.0 permits redistribution and adaptation but requires credit, a license
link, and an indication of changes. The current pipeline omits references and
page furniture and normalizes text for speech, so it should identify its output
as modified even if pure format shifting might sometimes be treated differently.
See the [CC BY 4.0 deed](https://creativecommons.org/licenses/by/4.0/deed.en)
and [Creative Commons FAQ](https://creativecommons.org/faq/).

NonCommercial variants are excluded so that later advertising, sponsorship,
platform monetization, or an ambiguous commercial context cannot silently make
old episodes noncompliant. NoDerivatives variants are excluded because this
pipeline intentionally omits and transforms content.

The Podcasting 2.0 `<podcast:license>` tag describes the license of the episode
audio; it is not evidence that the source paper was licensed for this use. Store
and display both licenses separately.

### Proving the license

The configured Zotero library currently has DOI and URL values but no populated
`rights` fields, so local metadata alone cannot yet drive this gate. Extend the
metadata reader and resolve evidence in this order:

1. Canonical license URI in Zotero `rights` or a strictly parsed `Extra` field.
2. Crossref license metadata for the DOI, including `content-version`, start
   date, and license URL.
3. A licensed OA location from Unpaywall as a discovery source.
4. Publisher or repository landing-page metadata for the exact public copy.

Crossref exposes deposited license metadata and distinguishes version of record,
accepted manuscript, and TDM licenses. Unpaywall describes licenses per OA
location and may return `implied-oa`; `implied-oa` must not pass this gate. See
[Crossref's license schema](https://github.com/CrossRef/rest-api-doc/blob/master/api_format.md#license)
and [Unpaywall's data format](https://unpaywall.org/data-format).

For the strongest automated proof, synthesize the public episode from the OA PDF
at the verified licensed location, not merely from whichever copy happens to be
in Zotero. Store the downloaded public-source SHA-256. If the local and public
copies differ, the local copy can still produce private audio, while the public
copy gets its own provenance-preserving bundle. A checksum mismatch or unclear
version is a review result, never permission.

Store the license evidence snapshot with:

- normalized license identifier and canonical URI;
- evidence source and evidence URL;
- DOI and public paper/landing-page URLs;
- licensed content version (`vor`, `am`, or repository version);
- effective/embargo date;
- retrieval timestamp;
- source PDF SHA-256 to which the decision applies;
- resolver version and final allow/deny reason.

## Metadata model

### Show-level configuration

Keep this in a checked-in, non-secret configuration file:

- stable show-family ID plus separate Brief and Full Reading show GUIDs;
- shared brand name plus edition-specific show titles, subtitles, and
  listener-facing descriptions;
- author/publisher name for the show, not the paper authors;
- canonical show website URL plus Brief and Full Reading feed URLs;
- language and country of origin;
- category, initially `Science` or `Education`;
- show type `episodic`;
- explicit-content default;
- copyright and episode-license policy;
- owner/verification email alias;
- shared artwork system plus each show's artwork path and alt text;
- public asset base URL;
- private iCloud destination;
- publication collection/tag and license allowlist.

The verification email becomes public in the RSS feed, so use a dedicated alias
rather than a personal address. Spotify uses this address when an externally
hosted feed is claimed.

### Paper/source metadata

Extend the current Zotero lookup beyond title, date, and creators:

- exact title and optional listener-friendly short title;
- stable paper GUID derived from normalized DOI, falling back to the canonical
  source URL;
- ordered authors and, when present, ORCIDs;
- author-to-affiliation mappings from authoritative metadata, retaining the
  evidence source and confidence;
- abstract;
- publication title, publisher, volume, issue, pages, and publication date;
- DOI, canonical DOI URL, OA landing URL, and OA PDF URL;
- language using a BCP 47 tag;
- Zotero parent and attachment keys;
- source type/version and source SHA-256;
- license fields and evidence listed above;
- retraction/correction/update status when available.

Do not publish local filesystem paths, Zotero storage paths, or private iCloud
links.

### Episode/publication metadata

One paper publication record owns two edition records. Shared fields include the
source and license evidence; edition-specific fields include:

- `edition`, with the closed values `brief` or `full`;
- a stable edition GUID derived from the paper GUID plus edition, so the two RSS
  items remain distinct and corrections retain identity;
- exact `paper_title`, app-facing `episode_title`, normalized `author_label`, and
  original paper `publication_year`;
- podcast publication timestamp, distinct from the paper publication date;
- listener-facing summary derived from the abstract without adding claims;
- ordered authors and complete attribution statement;
- `read_url`, normally the licensed OA landing page, plus DOI URL;
- a precise changes statement, for example: "Converted to synthetic speech;
  references, repeated page furniture, and non-readable visual material omitted";
- narration-policy version, ordered transformation codes, canonical-document
  hash, narration-plan hash, and content-QA result;
- an AI-voice disclosure and a non-endorsement statement;
- explicit-content decision;
- audio URL, MIME type `audio/mp4`, byte length, duration, and SHA-256;
- cover URL, dimensions, template version, prompt hash, and image SHA-256;
- VTT and HTML transcript URLs, language, hashes, and timing version;
- chapter data and timing version;
- source-paper license and episode-audio license;
- publish status, first-published timestamp, and last-published revision.

The shared paper record should also store `brief_guid`, `full_guid`, and the
stable landing-page URL so each edition can link directly to its counterpart.

The complete paper title belongs at the start of the RSS episode title for
discovery. Keep episode numbers out of the title. If a title is too long for
readable cover art, use a generated short display title on the image while
retaining the complete title in RSS, the episode page, transcript, and
attribution.

### Episode-title convention

Use this app-facing title in both sibling shows:

```text
{paper_title} — {author_label} ({publication_year})
```

For example: `Paper title — Smith et al. (2026)`.

Build `author_label` deterministically:

- one person: `Smith`;
- two people: `Smith & Jones`;
- three or more people: `Smith et al.`;
- a group author: retain the group's supplied name;
- preserve diacritics and surname particles from the authoritative creator
  record.

The year is the paper's publication year, never the podcast release year. When
the year is unavailable, omit the parentheses. When authors are unavailable,
omit the entire author separator rather than speaking or displaying a
placeholder. The complete author list remains in the show notes and attribution.
Because Brief and Full Reading are separate shows, their app-facing episode
titles may be identical. A combined-feed pilot must add `Brief` or `Full
Reading` between the paper title and author label so the two items can be
distinguished.

### Embedded audio metadata

Treat the publication manifest and episode page as the authoritative metadata;
players do not preserve or display every MP4 atom consistently. Still embed the
useful subset in each M4A:

- exact paper title plus explicit Brief/Full Reading edition label;
- complete ordered author string, journal or source publication, and paper
  publication date;
- copyright notice, verified source-license name and canonical URL, and a
  distinct episode-audio license;
- DOI, canonical paper URL, edition GUID, source hash, and narration-plan hash;
- synthetic-voice and changes statements;
- chapter titles and start times in MP4 chapter metadata.

Use stable standard atoms where they exist and namespaced free-form atoms for
DOI, licenses, hashes, and transformation details. Test the resulting files in
the target local players as well as podcast clients; lack of display support is
not permission to omit the same information from RSS and the episode page.

## RSS output

Generate RSS 2.0 with the iTunes, Atom, content, Podcasting 2.0, and Podlove
chapter namespaces. Generate sibling `brief/feed.xml` and `full/feed.xml` files
from the same publication manifest. At minimum, include:

### Channel

- `title`, `link`, `description`, `language`, and `lastBuildDate`;
- `atom:link rel="self"`;
- `itunes:author`, `itunes:owner`, and public `itunes:email`;
- `itunes:image`, `itunes:category`, `itunes:type`, and `itunes:explicit`;
- stable `podcast:guid` and the show-level license/copyright policy.

### Item

- stable `guid isPermaLink="false"`;
- `title`, canonical episode-page `link`, and plain-text `description`;
- rich `content:encoded` show notes;
- `enclosure` with immutable URL, `audio/mp4`, and exact byte length;
- podcast `pubDate` and `itunes:duration`;
- `itunes:episodeType`, `itunes:explicit`, and per-episode `itunes:image`;
- `podcast:transcript` for VTT and optionally HTML;
- Podlove chapters for Spotify and `podcast:chapters` JSON for Apple Podcasts
  and other supporting apps;
- `podcast:license` for the episode audio.

Use the same release timestamp for paired editions. In the Brief description,
link prominently to the Full Reading; in the Full Reading, link back to the
Brief. The paper landing page should present both players together.

An episode-description template should always include:

1. The paper's abstract or a short extract from it.
2. Authors, journal/publisher, and original publication date.
3. "Read the paper" and DOI links.
4. Source license name and canonical link.
5. AI voice disclosure, exact transformation/omissions statement, and a note
   that the authors and publisher do not endorse or sponsor the recording.
6. Transcript link and chapter timestamps as a compatibility fallback.

## Transcript and chapters

Do not embed Markdown in the M4A as the podcast transcript. Keep `article.md` as
the internal archival representation and generate these public sidecars only
after the license gate passes:

- `transcript.vtt`: the primary timed transcript for podcast clients;
- edition-specific `transcript.html`: an accessible, searchable rendering of
  exactly the spoken text, with attribution;
- optional sanitized Markdown download for technically inclined readers;
- `chapters.json` and/or Podlove RSS chapters.

Generate a separate transcript for each edition. The Brief transcript must
contain only what the Brief actually speaks; it should not point at the Full
Reading transcript. A shared episode page can additionally expose the complete
paper transcript.

Reference the VTT with `<podcast:transcript>` in RSS. This is the portable,
open-feed representation and is ingested by clients including Apple Podcasts.
Spotify also accepts uploaded VTT/SRT files and can auto-generate transcripts,
but transcript controls are not available to every creator and Spotify notes
that synchronization can depend on the host. Therefore, the public VTT and HTML
page are the reliable outputs; Spotify displaying that exact VTT is not a safe
automation assumption.

The current run manifest records duration only at the speech-plan segment level.
Update synthesis to preserve the duration and text of each actual Kokoro input
chunk. This makes VTT cues and heading timestamps deterministic without running
speech recognition over known text. Global cue offsets are the accumulated
chunk durations plus inserted pauses. Keep cue lines short, include every spoken
word, and validate that the last cue ends no later than the audio duration.

Map canonical major headings—not headings guessed from flattened page text—to
chapter start times. Normalize common variants into listener-friendly labels,
while retaining meaningful source-specific section names. A typical article may
produce Intro, Abstract, Introduction, Methods, Results, Discussion, Conclusion,
and Closing; never invent a missing scholarly section merely to fill that list.

Write the same chapter model to MP4 file metadata, Podcasting 2.0 JSON, Podlove
RSS, and plain-text show-note timestamps. Keep one canonical timestamp source so
the formats cannot drift. Use at least three chapters, prefer chapters of two
minutes or longer, merge sections shorter than 30 seconds, and keep titles short
enough to scan in a player. If a short Brief cannot support three meaningful
chapters, omit manual chapters instead of inventing or padding them. Spotify
supports Podlove chapters for externally hosted shows, while Apple Podcasts
accepts `podcast:chapters`, episode-description timestamps, and chapters in MP4
file metadata.

## Artwork system

The root [`DESIGN.md`](../DESIGN.md) is the source of truth. It defines a quiet,
human editorial system with fixed typography, a three-color-per-cover palette,
flat abstract motifs, and paired Brief/Full Reading variants. The image model,
when used, creates only a text-free raw motif; local deterministic composition
controls all typography, color, spacing, and branding.

Spotify expects square, high-resolution artwork and supports TIFF, PNG, or JPEG;
per-episode art is optional and may not appear in every podcast app. See the
[Spotify artwork guidance](https://support.spotify.com/st-en/creators/article/uploading-cover-art/)
and delivery specification above.

Make artwork content-addressed by source and design-system versions. Generate it
only once. If the image provider is unavailable or the result fails DESIGN.md,
use the deterministic geometric fallback. The prompt, model/provider version,
safety result, design version, and output hash belong in the episode manifest.
Only public bibliographic text may be sent to the image provider.

## Audio presentation

The existing mono AAC-LC M4A is acceptable to Spotify. For every private and
public edition, add:

- a short, fixed show ident owned by the project;
- a concise generated introduction naming the title, authors, journal,
  publication date, and verified source license, while clearly disclosing the
  use of a synthetic voice;
- consistent loudness and true-peak normalization, with measurements in QA;
- a spoken closing directing listeners to the paper, license, and transcript in
  the show notes;
- section chapters generated from the paper headings.

### Loudness and dynamics

Make loudness a delivery gate, not a listening preference:

- target **-16 LUFS integrated**, measured according to ITU-R BS.1770-5;
- pass range: **-17 to -15 LUFS integrated**;
- final AAC true peak: **no higher than -1 dBTP**;
- working limiter ceiling: **-1.5 dBTP** before AAC encoding;
- no clipped samples, obvious pumping, or large level jump between the ident,
  introduction, paper, and closing;
- record integrated loudness, loudness range, true peak, and normalization gain
  in `qa-report.json`.

A five-minute measurement of one existing library episode produced -28.1 LUFS
integrated with a -4.4 dBFS true peak. That sample is roughly 12 dB below the new
target and confirms that peak normalization alone is insufficient.

Assemble the complete lossless WAV first, apply gentle speech compression only
as needed, then run two-pass loudness normalization before AAC encoding. Measure
the encoded M4A again because lossy encoding can alter true peaks. If the final
file misses either threshold, assembly fails and neither private delivery nor
public publication advances. The installed FFmpeg provides the required
BS.1770 measurement and two-pass normalization path.

Apple's podcast-specific guidance recommends approximately -16 dB LKFS with a
±1 dB tolerance and a true peak no higher than -1 dBFS. This also keeps the
episodes close to mainstream streaming levels without crushing spoken-word
dynamics. See [Apple Podcasts audio requirements](https://podcasters.apple.com/support/893-audio-requirements).

### Spoken introduction

Use edition-specific introductions generated from the same verified metadata.
For the Brief:

> You're listening to a brief audio edition of "{title}," by {spoken_authors}.
> {publication_sentence} {source_license_sentence} This Brief contains
> {brief_contents}, not the full paper. This edition uses a synthetic voice.
> The Full Reading, original paper, license, transcript, and complete
> attribution are linked in the show notes.

For the Full Reading:

> You're listening to an audio edition of "{title}," by {spoken_authors}.
> {publication_sentence} {source_license_sentence} This edition uses a
> synthetic voice. Links to the original paper, license, transcript, and a
> complete account of changes are in the show notes.

Build this from complete sentence components rather than interpolating empty
fields. The result should follow these rules:

- one to three authors: speak every name;
- four or more authors: speak the first author followed by "and colleagues";
- retain the complete author list in the show notes, transcript metadata, and
  attribution statement;
- render `brief_contents` as either "the authors' abstract" or "the authors'
  abstract and conclusion," matching the sections actually included;
- render the publication component naturally as "Published in {journal} on
  {full_date}," "Published in {journal} in {year}," or "Published in {year}";
- render the license component as, for example, "The source article is licensed
  under Creative Commons Attribution 4.0" only from the verified license record
  for the exact source version;
- omit the spoken license sentence from a private edition when the source
  license is missing or unresolved; a public edition cannot reach this point
  without a verified allowlisted license;
- never speak placeholders such as "unknown journal" or "date unavailable";
- use "show notes" as two words.

Keep affiliations in the complete attribution and show notes rather than the
default spoken introduction. They add substantial listening time, are often
ambiguous, and are usually absent from ordinary Zotero item fields. If a future
edition speaks them, enrich them from an authoritative bibliographic source,
cache the evidence, and preserve author-to-institution mappings. Do not infer
institutions from email domains or low-confidence PDF layout extraction.

The Brief assembly plan should be deterministic:

1. Show ident followed by the spoken introduction above.
2. A short pause separating the framing from the paper text.
3. "Abstract," followed by the author-provided abstract.
4. Optional "Authors' conclusion," followed by a confidently extracted
   conclusion when the duration limit permits it.
5. Exact omissions, source and episode licenses, link, and non-endorsement
   closing.

The Full Reading retains all narration-policy-approved paper sections. Before
the closing, use a short edition-specific changes sentence generated from the
actual transformation log, for example: "Parenthetical citations and the
reference list were omitted; detailed tables and figures are available in the
companion page." Say that the main prose otherwise preserves the authors'
wording only when content QA verifies that claim.

Keep shared metadata sentences identical between editions and synthesize intro
sentences separately so content-addressed audio segments can still be reused
where they match. For private files without a show-notes page, substitute a
truthful phrase such as "the companion information file" and embed the paper URL
and DOI in the M4A; never promise links that do not exist.

Keep the ident subtle and do not add third-party music unless its reuse rights
are independently tracked. Do not use or imitate an identifiable person's
voice. Spotify removes podcasts that clone another creator's likeness without
permission; use the existing generic Kokoro voice and disclose that it is
synthetic.

## Publishing implementation

### Generated layout

Keep runtime state outside iCloud, as the project already does. Copy only
completed listener artifacts to iCloud.

```text
private iCloud root/
  Briefs/2026/
    Paper title - Brief [ZOTERO_KEY].m4a
  Full Readings/2026/
    Paper title - Full Reading [ZOTERO_KEY].m4a

runtime/podcast/
  publication-manifest.json
  license-evidence/
  documents/<paper-guid>/<source-revision>/
    canonical-document.json
    content-qa.json
    brief/narration.md
    brief/narration-plan.json
    full/narration.md
    full/narration-plan.json
  public-staging/
    brief/feed.xml
    full/feed.xml
    shows/brief-cover.jpg
    shows/full-cover.jpg
    episodes/<guid>/<revision>/
      brief/audio.m4a
      brief/cover.jpg
      brief/transcript.vtt
      brief/transcript.html
      full/audio.m4a
      full/cover.jpg
      full/transcript.vtt
      full/transcript.html
      companion-article.html
      brief-chapters.json
      full-chapters.json
      episode.json
```

The uploader should:

1. Build and validate every local artifact.
2. Upload immutable episode assets using content-addressed revision paths.
3. Verify each public URL with HEAD and a byte-range GET.
4. Generate RSS only from fully verified published records.
5. Validate XML and podcast requirements locally.
6. Upload versioned feed snapshots, then replace both canonical feed files last.
7. Verify both canonical feeds and retain the previous snapshots for rollback.

Never publish a partial episode. Rerunning with identical inputs must produce no
new episode, upload, or feed change. When correcting audio or artwork, preserve
the GUID and change the asset URL so Spotify fetches the new binary.

Credentials should live in macOS Keychain or a protected runtime environment
file, never in the repository or launchd plist. The object store should allow
upload/delete through a private API credential while the resulting podcast
assets are publicly readable.

### Scheduling and health

Extend the existing launchd job rather than introducing another scheduler:

- scan sources and resolve Zotero parent-item metadata;
- build canonical documents, narration plans, and content-QA reports;
- synthesize and assemble only listener-ready plans;
- normalize and validate audio, then embed final metadata and chapters;
- copy only completed listener artifacts to the configured private iCloud root;
- evaluate publication intent and license;
- build/upload newly eligible episodes;
- regenerate the affected feed or feeds only if public state changed.

Add a once-daily lightweight health check for both feeds, show artwork, each
show's most recent audio/transcript/art URLs, MIME types, byte ranges, and TLS.
Rotate logs. Notify for a new failure, review requirement, or failed health
check, and coalesce repeat notifications by error fingerprint.

## Delivery phases

### Phase 1: Listener-copy foundation, policy, and routing

- Add show configuration and publication manifest schemas.
- Extend Zotero parent-item metadata extraction and apply it before narration
  planning.
- Add the canonical document schema and structured extraction router.
- Evaluate local scholarly/layout extractors such as GROBID and Marker against
  the approved corpus, then select adapters by block-role and reading-order
  accuracy rather than convenience or extraction speed alone.
- Add reliable section, reference, citation, footnote, table, figure,
  mathematics, and appendix classification.
- Add `narration.md`, `narration-plan.json`, transformation logging, and
  deterministic spoken-text normalization.
- Add the content-quality gate and approved regression fixtures for the nine
  audited sources.
- Add reliable Abstract and Conclusion detection with confidence and duration
  limits.
- Add `Podcast Queue`/tag detection.
- Configure the private iCloud destination.
- Implement the fail-closed license resolver and evidence cache.
- Unit-test every allowed, denied, conflicting, embargoed, and missing case.

Outcome: every item is classified, malformed listening copies fail before TTS,
and no public upload occurs.

### Phase 2: Podcast artifacts

- Record Kokoro subchunk timings.
- Add edition-aware speech plans and shared segment caching.
- Generate paired Brief/Full audio, VTT, HTML transcript, and chapters.
- Add genre-aware intro/outro, pronunciation lexicons, loudness QA, embedded MP4
  chapters, and complete attribution metadata.
- Implement show and episode artwork generation with deterministic fallback.
- Generate paired episode pages and standards-compliant Brief/Full RSS feeds.

Outcome: a complete podcast can be previewed and validated locally.

### Phase 3: Safe publication

- Provision the public HTTPS domain and object storage.
- Implement content-addressed upload, URL checks, feed-last commit, and rollback.
- Publish both editions of one CC BY 4.0 pilot paper.
- Validate both feeds, claim/submit each show once in Spotify for Creators, and
  verify the public owner email.
- Check the episode's art, show notes, transcript, chapters, and playback in the
  Spotify clients.

Outcome: future eligible episodes arrive through RSS without UI automation.

### Phase 4: Unattended operation

- Integrate publication into the existing launchd run.
- Add retries with backoff, notification deduplication, log rotation, and daily
  feed health checks.
- Add a dry-run report and an emergency `publishing_enabled = false` switch.
- Document correction, takedown, and feed rollback procedures.

Outcome: normal success requires no intervention; ambiguous rights and real
failures stop safely and request attention.

## Acceptance criteria

- Every listener-ready paper reaches the private iCloud destination regardless
  of public eligibility, but only after content and audio QA pass; a malformed
  extraction remains in runtime as `needs_review` rather than becoming a
  listener file.
- Every paper with an identifiable abstract produces separate private Brief and
  Full Reading files when both narration plans pass; missing abstracts fail only
  the Brief.
- Every listener file is derived solely from a versioned narration plan whose
  nodes map to canonical source blocks or are explicitly labeled narrator text.
- No spoken article contains a bibliography, raw citation marker, page header,
  correspondence address, flattened table grid, unresolved formula, fused-word
  extraction artifact, or unaccounted narrator insertion.
- Every table, figure, equation, footnote, and appendix has an explicit recorded
  outcome, and all omissions appear in the edition changes statement.
- Journal-article chapters derive from verified canonical headings. False table-
  cell headings and invented Abstract/Methods/Results labels fail QA.
- The detected document language is supported by the selected TTS frontend;
  unsupported languages remain `needs_review` rather than using a superficially
  similar voice.
- Every delivered M4A measures between -17 and -15 LUFS integrated, stays at or
  below -1 dBTP after AAC encoding, and contains no clipped samples.
- No network upload occurs without explicit publication intent and an allowlisted
  license tied to the exact source version.
- Missing, vague, conflicting, ND, NC, TDM-only, or embargoed rights fail closed.
- Paired editions have distinct stable GUIDs, reciprocal links, shared source
  provenance, and independently correct audio/transcript durations.
- Both editions use consistent, explicitly labeled introductions containing no
  empty placeholders, unverified affiliations, or unverified license claims.
  The Brief states exactly what it contains, says it is not the full paper, and
  links to the Full Reading. Shortened spoken author lists retain complete
  attribution in the show notes.
- Every public episode has immutable asset URLs, source SHA-256, attribution,
  change notice, AI disclosure, paper link, license link, cover, and transcript.
  Every Full Reading has chapters; a Brief has chapters when it contains at
  least three meaningful sections.
- Both feeds validate and every referenced resource supports HTTPS, HEAD, and
  byte-range GET with the correct MIME type and byte length.
- An identical rerun makes no changes; a corrected asset keeps the GUID but gets
  a new URL.
- Failure during art generation or upload leaves private delivery intact and
  both public feeds unchanged.
- Removing an item from the publication collection does not automatically erase
  it; takedown is a separate, explicit operation with a retained audit record.

## Activation choices for the first public pilot

1. Show name, description, domain, and public verification email alias.
2. Whether publication requires a Zotero collection/tag or automatically selects
   every eligible paper. The collection is recommended.
3. Initial license policy. Recommended: CC BY 4.0 plus CC0/Public Domain only;
   add BY-SA after its output-license behavior is tested.
4. Public object-storage provider and monthly cost ceiling.
5. Artwork palette/typeface/style direction and whether remote AI generation is
   acceptable for public title/abstract metadata.
6. Appendix policy for Full Readings: omit all appendices, or allow substantive
   prose appendices after object-level content QA. Raw instruments, codebooks,
   and table-only appendices remain omitted either way.
7. Which TTS backend provides publication-quality Norwegian before Norwegian
   items can leave `needs_review`, and whether languages use separate feeds.
8. Whether the initial pilot uses one combined feed or immediately launches the
   recommended sibling Brief and Full Reading shows.
