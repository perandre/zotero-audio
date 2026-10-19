# Open Knowledge Format assessment

Assessed 19 October 2026 against Google's maintained
[Open Knowledge Format v0.2 specification](https://github.com/GoogleCloudPlatform/open-knowledge-format/blob/main/SPEC.md)
and its [Knowledge Catalog integration explanation](https://cloud.google.com/blog/products/data-analytics/scale-okf-bundles-across-an-organization-with-knowledge-catalog).
The older `knowledge-catalog/okf` snapshot is not the source to target.

OKF is a directory of Markdown concepts with YAML frontmatter, a required
nonempty `type`, optional provenance/trust/freshness fields, conventional
`index.md` navigation and standard Markdown links. It requires no Google Cloud
service, SDK, paid model or replacement search database.

## Decision

Use the minimal format for the new private `papers/references/` inventory.
A `Research Reference` concept holds bibliographic fields, an authors' abstract
when sourced, explicit abstract/full-text availability, and links to canonical
full research Markdown. This makes the reference inventory understandable to
people and agents that can read ordinary files. It improves portability and
navigation; it does not improve PDF extraction or supply missing full text.

Do not rewrite authoritative research Markdown to adopt it. Such a migration
would alter user files and content hashes used by generation, search and delivery.
The new reference files carry `type`, `title`, `resource`, `sources` and private
producer fields; they do not assert human verification or semantic trust just
because extraction/QA succeeded. Existing full texts are linked resources outside
the reference directory, so copying only that directory will omit the full texts.
A self-contained shareable bundle would need a separate explicit export with
source permissions considered. This private inventory is not uploaded or published.

## Coverage and lifecycle

`za library export` scans top-level Zotero records, rather than only locally
saved PDFs. Abstract and full-text counts remain separate. Bibliographic-only
records are explicitly identified, not presented as completed article extracts.
The command uses existing matching abstract caches without new provider requests.
An older bounded Crossref cache entry over 8,000 characters is excluded because
it may be truncated. Missing abstract text is reported honestly.

Exports preserve edited files and historical records, and refresh generated
files only when content changes. Navigation and coverage are a snapshot; rerun
after generation or Zotero changes. A preserved manual reference can differ from
the current metadata-derived availability in the manifest. A manually edited
navigation file is preserved and reported by `index_edited`.

The reference inventory is independent of the PDF generation catalog and remote
MCP search. Extending remote discovery to bibliographic-only references would
require a separate shared schema/API change. OKF's optional verification,
freshness and attested-computation machinery is unnecessary for this inventory.

## Abstract retrieval

`za research-fit enrich` now uses independent public metadata sources and
publisher abstract sections, then refreshes the reference inventory and writes
actual abstracts into `papers/abstracts/`. It stores complete source text and
provider provenance separately from bounded relevance-classifier input in the
additive `research_abstract_retrievals` SQLite table. Per-record receipts include
provider attempts, `found`, `not_found` or `retry_pending`; they preserve durable
successes across restarts. Legacy Crossref misses do not suppress other sources.

Publisher requests validate public destinations, including redirects, and omit
URL query credentials. DOI-based responses require the matching DOI and title;
identifier-free searches require a stricter title match. Generic search snippets
are not abstracts. Retrieval does not edit Zotero, full research Markdown,
publication permissions or audio caches. No new paid API or inference service
is required.
