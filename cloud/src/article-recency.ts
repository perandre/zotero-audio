export const VIKING_RESEARCH_SCOPE = "viking_research";
export const MINIMUM_ARTICLE_YEAR = 2025;
export const PREFERRED_ARTICLE_YEAR = 2026;

export function articleYear(value: unknown): number | null {
  if (
    typeof value === "number" &&
    Number.isInteger(value) &&
    value >= 1 &&
    value <= 3000
  )
    return value;
  const match = String(value ?? "").match(/(?<!\d)(?:19|20)\d{2}(?!\d)/);
  return match ? Number(match[0]) : null;
}

export function readableArticleYear(value: unknown): boolean {
  const year = articleYear(value);
  return year !== null && year >= MINIMUM_ARTICLE_YEAR;
}

export function readableArticle(article: {
  year: unknown;
  reading_scope?: unknown;
}): boolean {
  return article.reading_scope !== VIKING_RESEARCH_SCOPE || readableArticleYear(article.year);
}

export function unreadableArticleMessage(
  title: string,
  value: unknown,
): string {
  const year = articleYear(value);
  return `${title}: VIKING research-article reading is limited to publications from ${MINIMUM_ARTICLE_YEAR} onward (prefer ${PREFERRED_ARTICLE_YEAR}); this record has ${year ?? "an unknown year"}.`;
}
