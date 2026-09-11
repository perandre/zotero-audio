import { z } from "zod";
import { boundedText, HttpError } from "./http";

const creator = z.union([
  z.object({
    creatorType: z.literal("author"),
    firstName: z.string().max(1000),
    lastName: z.string().min(1).max(1000),
  }),
  z.object({
    creatorType: z.literal("author"),
    name: z.string().min(1).max(1000),
  }),
]);
export const metadataSchema = z.object({
  itemType: z.enum([
    "journalArticle",
    "conferencePaper",
    "preprint",
    "webpage",
  ]),
  title: z.string().trim().min(1).max(4000),
  creators: z.array(creator).max(500).default([]),
  DOI: z.string().max(500).default(""),
  url: z.string().max(2048).default(""),
  date: z.string().max(100).default(""),
  publicationTitle: z.string().max(1000).default(""),
  volume: z.string().max(100).default(""),
  issue: z.string().max(100).default(""),
  pages: z.string().max(100).default(""),
  abstractNote: z.string().max(30000).default(""),
});
export type ArticleMetadata = z.infer<typeof metadataSchema>;

export function normalizeDoi(value: string): string | null {
  let doi = value.trim().replace(/^doi:\s*/i, "");
  if (/^https?:\/\/(?:dx\.)?doi\.org\//i.test(doi)) {
    try {
      doi = decodeURIComponent(new URL(doi).pathname.slice(1));
    } catch {
      return null;
    }
  }
  return /^10\.\d{4,9}\/[^\s\x00-\x1f\x7f]{1,480}$/i.test(doi)
    ? doi.toLowerCase()
    : null;
}

export function publicUrl(value: string): URL {
  let url: URL;
  try {
    url = new URL(value);
  } catch {
    throw new HttpError(
      400,
      "invalid_article_url",
      "Supply a DOI or a public HTTPS article URL.",
    );
  }
  const host = url.hostname.toLowerCase().replace(/\.$/, "");
  // Reject IP literals and local names. Worker global_fetch_strictly_public also
  // rejects DNS resolutions to private addresses; validate every redirect here.
  if (
    url.protocol !== "https:" ||
    url.username ||
    url.password ||
    url.port ||
    !host.includes(".") ||
    /^[\d.]+$/.test(host) ||
    host.includes(":") ||
    /\.(?:localhost|local|internal|test|invalid)$/.test(host)
  )
    throw new HttpError(
      400,
      "invalid_article_url",
      "Use a public HTTPS article URL without credentials or a custom port.",
    );
  url.hash = "";
  return url;
}

export function normalizedUrl(value: string): string {
  try {
    const url = new URL(value);
    url.hash = "";
    url.protocol = "https:";
    // Only discard known tracking parameters; other queries can identify papers.
    for (const key of [...url.searchParams.keys()])
      if (/^utm_/i.test(key) || ["fbclid", "gclid"].includes(key))
        url.searchParams.delete(key);
    url.searchParams.sort();
    return url.toString();
  } catch {
    return value;
  }
}

export async function publicFetch(
  value: string,
  accept: string,
): Promise<Response> {
  let url = publicUrl(value);
  for (let attempt = 0; attempt < 5; attempt++) {
    let response: Response;
    try {
      response = await fetch(url.toString(), {
        headers: { Accept: accept },
        redirect: "manual",
        signal: AbortSignal.timeout(15000),
      });
    } catch {
      throw new HttpError(
        502,
        "metadata_unavailable",
        "The metadata provider could not be reached. Retry later or supply verified metadata.",
      );
    }
    if ([301, 302, 303, 307, 308].includes(response.status)) {
      const location = response.headers.get("Location");
      await response.body?.cancel();
      if (!location) break;
      url = publicUrl(new URL(location, url).toString());
      continue;
    }
    if (!response.ok) {
      await response.body?.cancel();
      throw new HttpError(
        502,
        "metadata_unavailable",
        "The metadata provider did not return an article. Check the DOI/URL, retry later, or supply verified metadata.",
      );
    }
    return response;
  }
  throw new HttpError(
    502,
    "metadata_redirects",
    "The metadata provider redirected too many times. Supply the DOI or verified metadata.",
  );
}

const plain = (value: unknown): string =>
  typeof value === "string"
    ? value
        .replace(/<[^>]*>/g, "")
        .replace(/\s+/g, " ")
        .trim()
    : "";

export function cslMetadata(raw: unknown, doi: string): ArticleMetadata {
  const data = z
    .object({
      title: z.string(),
      type: z.string(),
      DOI: z.string(),
      author: z
        .array(
          z.object({
            given: z.string().optional(),
            family: z.string().optional(),
            literal: z.string().optional(),
          }),
        )
        .default([]),
      issued: z
        .object({ "date-parts": z.array(z.array(z.number().int())).optional() })
        .optional(),
    })
    .passthrough()
    .parse(raw);
  if (normalizeDoi(data.DOI) !== doi)
    throw new HttpError(
      502,
      "metadata_mismatch",
      "The provider returned a different DOI. No article was saved.",
    );
  const type = (
    {
      "article-journal": "journalArticle",
      "journal-article": "journalArticle",
      "paper-conference": "conferencePaper",
      "proceedings-article": "conferencePaper",
      article: "preprint",
      "posted-content": "preprint",
    } as const
  )[data.type];
  if (!type)
    throw new HttpError(
      400,
      "unsupported_reference",
      "This DOI is not an article, conference paper or preprint. Supply verified metadata with the appropriate supported type.",
    );
  return metadataSchema.parse({
    itemType: type,
    title: plain(data.title),
    DOI: doi,
    url: `https://doi.org/${doi}`,
    creators: data.author.flatMap<ArticleMetadata["creators"][number]>((a) =>
      a.family
        ? [
            {
              creatorType: "author",
              firstName: a.given ?? "",
              lastName: a.family,
            },
          ]
        : a.literal
          ? [{ creatorType: "author", name: a.literal }]
          : [],
    ),
    date:
      data.issued?.["date-parts"]?.[0]
        ?.map((v, i) => (i ? String(v).padStart(2, "0") : String(v)))
        .join("-") ?? "",
    publicationTitle: plain(data["container-title"]),
    volume: plain(data.volume),
    issue: plain(data.issue),
    pages: plain(data.page),
    abstractNote: plain(data.abstract),
  });
}

export async function citationTags(
  response: Response,
): Promise<Map<string, string[]>> {
  const html = await boundedText(response as unknown as Request, 2_000_000);
  const tags = new Map<string, string[]>();
  await new HTMLRewriter()
    .on("meta", {
      element(element) {
        const name = (
          element.getAttribute("name") ??
          element.getAttribute("property") ??
          ""
        ).toLowerCase();
        const content = element.getAttribute("content")?.trim();
        if (content && name.startsWith("citation_") && tags.size < 50) {
          const values = tags.get(name) ?? [];
          if (values.length < 500) tags.set(name, [...values, content]);
        }
      },
    })
    .transform(new Response(html))
    .text();
  return tags;
}

export async function resolveMetadata(
  source: string,
  supplied?: ArticleMetadata,
): Promise<ArticleMetadata> {
  const doi = normalizeDoi(source);
  const sourceUrl = doi
    ? `https://doi.org/${doi}`
    : publicUrl(source).toString();
  if (supplied) {
    const value = metadataSchema.parse(supplied);
    const suppliedDoi = normalizeDoi(value.DOI);
    if (
      (value.DOI && !suppliedDoi) ||
      (doi && suppliedDoi && suppliedDoi !== doi)
    )
      throw new HttpError(
        400,
        "metadata_mismatch",
        "Supplied metadata must match the source DOI.",
      );
    // The caller cannot redirect the saved reference to an unrelated URL.
    return { ...value, DOI: doi ?? suppliedDoi ?? "", url: sourceUrl };
  }
  if (doi) {
    const response = await publicFetch(
      sourceUrl,
      "application/vnd.citationstyles.csl+json",
    );
    try {
      return cslMetadata(
        JSON.parse(
          await boundedText(response as unknown as Request, 2_000_000),
        ),
        doi,
      );
    } catch (error) {
      if (error instanceof HttpError) throw error;
      throw new HttpError(
        502,
        "metadata_incomplete",
        "The DOI provider did not return complete article metadata. Supply verified metadata.",
      );
    }
  }
  const response = await publicFetch(sourceUrl, "text/html");
  if (!response.headers.get("Content-Type")?.includes("text/html")) {
    await response.body?.cancel();
    throw new HttpError(
      400,
      "metadata_needed",
      "Use the article landing page or DOI, or supply verified metadata. PDF files are not imported by this tool.",
    );
  }
  const tags = await citationTags(response);
  const first = (key: string) => tags.get(`citation_${key}`)?.[0] ?? "";
  const pageDoi = normalizeDoi(first("doi"));
  if (pageDoi) return { ...(await resolveMetadata(pageDoi)), url: sourceUrl };
  if (!first("title"))
    throw new HttpError(
      400,
      "metadata_needed",
      "This page has no supported article metadata. Supply a DOI or verified metadata; no reference was saved.",
    );
  return metadataSchema.parse({
    itemType: first("journal_title") ? "journalArticle" : "webpage",
    title: first("title"),
    url: sourceUrl,
    creators: (tags.get("citation_author") ?? []).map((name) => ({
      creatorType: "author",
      name,
    })),
    publicationTitle: first("journal_title"),
    date: first("publication_date") || first("date"),
    volume: first("volume"),
    issue: first("issue"),
    pages: [first("firstpage"), first("lastpage")].filter(Boolean).join("-"),
  });
}

export function zoteroPayload(
  metadata: ArticleMetadata,
): Record<string, unknown> {
  const { DOI, publicationTitle, volume, issue, pages, ...common } = metadata;
  const extra: string[] = [];
  if (DOI && ["conferencePaper", "webpage"].includes(metadata.itemType))
    extra.push(`DOI: ${DOI}`);
  return {
    ...common,
    tags: [],
    collections: [],
    relations: {},
    ...(extra.length ? { extra: extra.join("\n") } : {}),
    ...(metadata.itemType === "journalArticle"
      ? { DOI, publicationTitle, volume, issue, pages }
      : {}),
    ...(metadata.itemType === "conferencePaper"
      ? { proceedingsTitle: publicationTitle, volume, pages }
      : {}),
    ...(metadata.itemType === "preprint"
      ? { DOI, repository: publicationTitle }
      : {}),
    ...(metadata.itemType === "webpage"
      ? { websiteTitle: publicationTitle }
      : {}),
  };
}
