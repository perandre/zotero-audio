# Industry reports

Industry reports use the existing Zotero → PDF extraction → private audio
routine. The literature type is explicit: a Zotero `Report` becomes
`literature_type: report`; other item types retain the academic workflow.
Collection names and publisher brands do not determine the literature type.

## Intake

1. Save the original PDF as an attachment to a Zotero `Report` item.
2. Record title, institution, date, source URL, language, and named authors when
   supplied. Use Report Type, Report Number, and Series Title where applicable.
3. Place it in `VIKING PhD / Industry Reports`. The pilot uses the tags
   `literature:industry-report` and `status:to-read` for browsing.
4. Add evidence notes in Extra after checking the source. Record sample size,
   geography, fieldwork dates, and limitations where the report supplies them.

The collection is an organisational aid; automatic processing still follows
the existing library scan and podcast configuration. The pipeline reads Zotero
metadata and does not change library items during routine runs.

These exact Extra labels populate the structured `evidence` object and episode
show notes. All values are optional and must come from inspected evidence:

```text
Evidence methodology: Survey; see methodology, PDF page 40.
Evidence sample: 3,235 business and IT leaders across 24 countries.
Evidence data dates: August–September 2025.
Evidence funding: [Only an explicitly disclosed funder or sponsor.]
Evidence claim types: [Distinguish survey findings, forecasts, cases, and advice.]
Peer review: [Only an explicitly verified review statement.]
```

The first three lines illustrate Deloitte's 2026 report; the bracketed lines
are instructions, not values to copy. Missing fields remain missing. The
pipeline does not infer peer review, funding, or credibility from a brand.
Show notes label absent peer-review information as not stated in supplied
metadata. Evidence notes provide context alongside the source narration.

## Audio editions

Report introductions state the title, issuing organisation, publication date,
and series/number where available. Episode titles retain the organisation's
full name. Named authors are retained when available.

Brief selection tries these publisher headings in order: Executive summary,
Key findings, Key takeaways, Key insights, In brief, and Overview. It reads the
source wording with its subheadings; it does not generate a new summary.
Repeated summary headings on consecutive pages can continue a section, while
contents pages are excluded. A following section must bound the summary.
Ambiguous, missing, unbounded, or overlong summaries are unavailable; a lower
priority summary can be used when a preferred one fails these checks. The
default length limit is 600 seconds estimated at 150 words per minute.

Full Readings use the complete approved prose plan. Layout handling includes
three-column reports and slide-style summary rows, with original PDF spans and
page provenance retained. Tables, figures, captions, running furniture, and
references are omitted under the existing extraction policy. Numeric
superscript citation markers after clear percentage, currency, or prose
endings are removed for narration and recorded in layout provenance. Unit and
formula exponents are preserved.

Designed reports still need source/transcript review: visual diagrams may
carry essential meaning, and layout classification is imperfect. A missing
Brief never silently turns into a full report labelled as a Brief.

## Commands

Zotero Report items are detected automatically:

```bash
"$RUNTIME/venv/bin/zotero-audio" prepare \
  --zotero-key ATTACHMENT_KEY --output-root "$RUNTIME/report-bundles"

"$RUNTIME/venv/bin/zotero-audio" podcast build \
  "$RUNTIME/report-bundles/<bundle>" \
  --config "$RUNTIME/podcast.toml" --edition brief --no-publish --no-dry-run
```

For a PDF outside Zotero, pass `--literature-type report` and a metadata JSON
file to `prepare` or `run`. For example:

```json
{
  "title": "Example industry report",
  "literature_type": "report",
  "institution": "Issuing organisation",
  "publication_date": "2026-01",
  "publication_year": "2026",
  "language": "en",
  "url": "https://example.org/report"
}
```

```bash
"$RUNTIME/venv/bin/zotero-audio" prepare \
  --pdf /path/to/report.pdf --literature-type report \
  --metadata-json /path/to/report-metadata.json \
  --output-root "$RUNTIME/report-bundles"
```

Use `podcast rebuild` for a bundle extracted before report support, or rerun
`prepare --force` after correcting its metadata. Changing the type alone does
not re-extract an existing PDF automatically.

## Pilot sources

The pilot collection contains the original PDFs and verified bibliographic
metadata. PDF page numbers below are physical pages, including covers.

| Report | Issuer / date | Selected Brief | PDF pages |
| --- | --- | --- | --- |
| [The economic potential of generative AI: The next productivity frontier](https://www.mckinsey.com/capabilities/tech-and-ai/our-insights/the-economic-potential-of-generative-ai-the-next-productivity-frontier) | McKinsey & Company / June 2023 | Key insights | 5 |
| [State of AI in the Enterprise: The untapped edge](https://www.deloitte.com/content/dam/assets-shared/docs/about/2025/state-of-ai-2026-global.pdf) | Deloitte AI Institute / January 2026 | Overview | 4–7 |
| [Now decides next: Generating a new future](https://www.deloitte.com/content/dam/assets-shared/docs/about/2025/quarter-4.pdf) | Deloitte AI Institute / January 2025 | Key findings | 5–7 |
| [Driving Sustainable Cost Advantage with AI](https://www.bcg.com/assets/2025/executive-perspectives-driving-sustainable-cost-advantage-with-ai-20may.pdf) | Boston Consulting Group / May 2025 | Executive summary | 3 |
| [India’s Triple AI Imperative: Succeeding with AI in India](https://web-assets.bcg.com/e4/14/440d165648a29f02376854919b4d/indias-triple-ai-imperative.pdf) | BCG X and FICCI / December 2025 | Executive summary | 3–4 |

Pilot downloads, source text, extraction caches, transcripts, and audio live
outside the repository. Pilot audio is private. Report literature type never
supplies publication intent or rights: the existing explicit selection and
verified source-license gates still apply.
