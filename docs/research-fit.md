# Research fit classification

`za research-fit` classifies the personal Zotero library for **Creating value
responsibly with agentic AI in organizational knowledge work**, using a compact
profile derived from the full HiØF application v4. The model assesses usefulness,
not study quality or support for the project's hypothesis. Negative results,
foundational theory, methods and non-agentic GenAI can be useful.

Labels are **Poor**, **Moderate**, **Good**, and **Excellent**. Each record also
has a primary contribution (Map, Build, Evaluate, theory, methods, background or
none), independent RQ1/RQ2/RQ3 relevance probabilities, confidence and evidence
coverage. Low confidence, title-only inputs and bounded excerpts are flagged
for review; missing abstracts do not automatically imply poor relevance.

## Configure and run

Store only the Typesafe key in the runtime's `control/typesafe-api-key.txt`
with mode `600`, or supply `TYPESAFE_API_KEY` in the process environment. Never
commit it, enter it in command arguments, or put it in browser/dashboard code.
The HTTP client uses Python's standard library; no extra dependency is needed.

```sh
za research-fit run --limit 20             # Pilot; discovers the whole catalog
za research-fit enrich                    # Cache missing public DOI abstracts, no Typesafe key needed
za research-fit run                       # Resume all uncached work, 4 requests at once
za research-fit run --retry-failed         # Explicitly retry failed items
za research-fit status --json
za research-fit list --label excellent
za research-fit export                    # Private control/research-fit.csv
za research-fit override --item-key ITEMKEY --label good
```

Zotero must be open with its supported local API enabled for `run`; status,
listing, export and manual overrides work offline. Discovery paginates all
personal-library top-level records with titles, excluding notes, attachments,
annotations and deleted items. Group/feed libraries are outside this version's
scope. Items without PDFs are included; multiple attachments do not cause
multiple classifications. The job does not modify Zotero fields/tags or queue
generation/audio/publication.

## Inputs, costs and storage

Each request sends one article's title and abstract plus the compact profile
and five questions to `https://api.typesafe.ai/v1/systemone`. Without an abstract,
the job first reuses cached extracted abstracts, then bounded passages from
existing local research Markdown. For remaining title-only records with a DOI,
it retrieves public Crossref metadata using two concurrent GET requests, checks
the DOI and title match, and caches the abstract or its absence. Only the public
DOI is sent to Crossref. Transient lookup failures retain a provisional input;
cached absent abstracts are checked again after 30 days. The pilot limits DOI
lookups as well as inference; full runs cover all candidates. Otherwise the job
makes a provisional title-only classification. It does
not extract every PDF, send the full application, or send author lists, private
notes, collections, credentials or URL query strings. Abstracts are bounded to
8,000 characters, selected Markdown to 7,000 and request titles to 1,000; full
titles remain in local records and human output. These limits are characters,
not tokenizer estimates. Non-English records should receive extra human review.

The pinned model is `jev-1.13.0`. Results use additive `research_profiles`,
`research_items`, `research_results`, `research_abstract_cache` and `research_overrides` tables in the
existing private `control/library.sqlite3`. The profile/question/model/input
policy and supplied text determine the cache hash. Unchanged inputs are reused;
identical records share a call. Changes create a new result while retaining old
results. Overrides are separate and survive reruns for the same profile version.
Missing/removed records become inactive after a successful discovery snapshot.
Cloud synchronization and dashboard display are not part of this local version.

A process lock prevents overlapping runs. Completed results commit individually;
interrupted work resumes. Transient HTTP/transport failures use bounded retry
and backoff. Authentication, billing and request-configuration errors stop new
requests. Failed records require `--retry-failed`; diagnostics omit upstream
response bodies and private article text. Status reports input-token usage for
current records; retries can incur additional charges not represented by the
successful-result sum. No paid plan or automatic upgrade is configured.

Podcast eligibility remains decided by the existing source-bound license and
publication gates. Research relevance never grants publication permission.
