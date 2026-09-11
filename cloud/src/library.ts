import { z } from "zod";
import { digest, HttpError, integer } from "./http";
import type { AppEnv, ArticleRow } from "./types";
import {
  MINIMUM_ARTICLE_YEAR,
  PREFERRED_ARTICLE_YEAR,
  VIKING_RESEARCH_SCOPE,
} from "./article-recency";

const articleSchema = z
  .object({
    id: z.string().optional(),
    title: z.string().min(1).max(2000),
    episode_title: z.string().min(1).max(2000).optional(),
    authors: z.array(z.string().max(500)).max(100).default([]),
    year: z.number().int().min(1).max(3000).nullable().optional(),
    source_url: z.string().max(4000).default(""),
    license_status: z.string().max(100).default("unknown"),
    markdown_status: z.string().max(100).default("pending"),
    audio_status: z.string().max(100).default("pending"),
    qa_status: z.string().max(100).default("unchecked"),
    warnings: z
      .array(z.union([z.string().max(4000), z.record(z.string(), z.unknown())]))
      .max(200)
      .default([]),
    updated_at: z.string().optional(),
    artifacts: z.record(z.string(), z.unknown()).optional(),
    audio_url: z.string().max(4000).optional(),
    editions: z.record(z.string(), z.unknown()).optional(),
    zotero_url: z.string().max(4000).optional(),
    collection: z.string().max(1000).optional(),
    reading_scope: z.enum(["general", VIKING_RESEARCH_SCOPE]).optional(),
    publish_status: z.string().max(100).optional(),
    publication_status: z.string().max(100).optional(),
    icloud_status: z.string().max(100).optional(),
    backup_status: z.string().max(100).optional(),
  })
  .strip();
const ingestSchema = z
  .object({
    article: articleSchema,
    markdown: z.string().optional(),
    review: z.string().optional(),
  })
  .strict();

export function articleJson(row: ArticleRow, origin: string) {
  const extra = JSON.parse(row.metadata) as Record<string, unknown>;
  return {
    ...extra,
    id: row.id,
    title: row.title,
    authors: JSON.parse(row.authors),
    year: row.year,
    source_url: row.source_url,
    license_status: row.license_status,
    markdown_status: row.markdown_status,
    audio_status: row.audio_status,
    qa_status: row.qa_status,
    warnings: JSON.parse(row.warnings),
    updated_at: row.updated_at,
    markdown_hash: row.markdown_hash,
    review_hash: row.review_hash,
    url: `${origin}/api/articles/${encodeURIComponent(row.id)}/markdown`,
    artifacts: {
      markdown: !!row.markdown_key,
      review: !!row.review_key,
      audio:
        row.audio_status === "ready" ||
        row.audio_status === "published" ||
        row.audio_status === "completed" ||
        !!extra.audio_url,
    },
  };
}

export function readableDocumentFilename(title: string): string {
  const normalized = title
    .normalize("NFKC")
    .replace(/[–—]/g, " - ")
    .replace(/[<>:"/\\|?*\u0000-\u001f]/g, " - ")
    .replace(/\s+/g, " ")
    .trim()
    .replace(/[ .]+$/, "")
    .slice(0, 180)
    .trim();
  return `${normalized || "Research article"}.md`;
}
export async function getArticle(env: AppEnv, id: string): Promise<ArticleRow> {
  const row = await env.DB.prepare("SELECT * FROM articles WHERE id=?")
    .bind(id)
    .first<ArticleRow>();
  if (!row)
    throw new HttpError(
      404,
      "article_not_found",
      "This article has not been synced from the Mac.",
    );
  return row;
}
export function searchableBody(markdown: string): string {
  return markdown
    .replace(/^---\r?\n[\s\S]*?\r?\n---(?:\r?\n|$)/, "")
    .replace(/<!--\s*pdf-page:\s*\d+\s*-->/g, "");
}

export function searchExpression(query: string): string {
  return (
    query
      .match(/[\p{L}\p{N}]+/gu)
      ?.slice(0, 20)
      .map((word) => `"${word}"*`)
      .join(" AND ") ?? ""
  );
}
export async function search(
  env: AppEnv,
  query: string,
  origin: string,
  limit = 20,
) {
  const expression = searchExpression(query.slice(0, 500));
  if (!expression) return { results: [] };
  const rows = await env.DB.prepare(
    `SELECT a.id,a.title,a.year,snippet(article_search,2,'','', ' … ',40) AS snippet
    FROM article_search JOIN articles a ON a.id=article_search.article_id
    WHERE article_search MATCH ?
      AND (COALESCE(json_extract(a.metadata, '$.reading_scope'), 'general') != ? OR a.year >= ?)
    ORDER BY
      CASE WHEN COALESCE(json_extract(a.metadata, '$.reading_scope'), 'general') = ? AND a.year = ? THEN 0
           WHEN COALESCE(json_extract(a.metadata, '$.reading_scope'), 'general') = ? THEN 1
           ELSE 2 END,
      CASE WHEN COALESCE(json_extract(a.metadata, '$.reading_scope'), 'general') = ? THEN a.year ELSE 0 END DESC,
      bm25(article_search,0,5,1) LIMIT ?`,
  )
    .bind(
      expression,
      VIKING_RESEARCH_SCOPE,
      MINIMUM_ARTICLE_YEAR,
      VIKING_RESEARCH_SCOPE,
      PREFERRED_ARTICLE_YEAR,
      VIKING_RESEARCH_SCOPE,
      VIKING_RESEARCH_SCOPE,
      Math.min(limit, 50),
    )
    .all<{ id: string; title: string; year: number; snippet: string }>();
  return {
    results: rows.results.map((row) => ({
      ...row,
      url: `${origin}/api/articles/${encodeURIComponent(row.id)}/markdown`,
    })),
  };
}
export async function listArticles(env: AppEnv, url: URL) {
  const limit = integer(url.searchParams.get("limit"), 50, 100),
    offset = integer(url.searchParams.get("offset"), 0, 100000);
  const expression = searchExpression(
    (url.searchParams.get("q") ?? "").slice(0, 500),
  );
  let items: ArticleRow[], total: number;
  if (expression) {
    const [rows, count] = await env.DB.batch<Record<string, unknown>>([
      env.DB.prepare(
        `SELECT a.* FROM article_search JOIN articles a ON a.id=article_search.article_id
         WHERE article_search MATCH ?
           AND (COALESCE(json_extract(a.metadata, '$.reading_scope'), 'general') != ? OR a.year >= ?)
         ORDER BY
           CASE WHEN COALESCE(json_extract(a.metadata, '$.reading_scope'), 'general') = ? AND a.year = ? THEN 0
                WHEN COALESCE(json_extract(a.metadata, '$.reading_scope'), 'general') = ? THEN 1
                ELSE 2 END,
           CASE WHEN COALESCE(json_extract(a.metadata, '$.reading_scope'), 'general') = ? THEN a.year ELSE 0 END DESC,
           bm25(article_search,0,5,1) LIMIT ? OFFSET ?`,
      ).bind(
        expression,
        VIKING_RESEARCH_SCOPE,
        MINIMUM_ARTICLE_YEAR,
        VIKING_RESEARCH_SCOPE,
        PREFERRED_ARTICLE_YEAR,
        VIKING_RESEARCH_SCOPE,
        VIKING_RESEARCH_SCOPE,
        limit,
        offset,
      ),
      env.DB.prepare(
        `SELECT count(*) AS total FROM article_search JOIN articles a ON a.id=article_search.article_id
         WHERE article_search MATCH ?
           AND (COALESCE(json_extract(a.metadata, '$.reading_scope'), 'general') != ? OR a.year >= ?)`,
      ).bind(expression, VIKING_RESEARCH_SCOPE, MINIMUM_ARTICLE_YEAR),
    ]);
    items = rows.results as ArticleRow[];
    total = Number(count.results[0]?.total ?? 0);
  } else {
    const [rows, count] = await env.DB.batch<Record<string, unknown>>([
      env.DB.prepare(
        `SELECT * FROM articles
         WHERE COALESCE(json_extract(metadata, '$.reading_scope'), 'general') != ? OR year >= ?
         ORDER BY
           CASE WHEN COALESCE(json_extract(metadata, '$.reading_scope'), 'general') = ? AND year = ? THEN 0
                WHEN COALESCE(json_extract(metadata, '$.reading_scope'), 'general') = ? THEN 1
                ELSE 2 END,
           CASE WHEN COALESCE(json_extract(metadata, '$.reading_scope'), 'general') = ? THEN year ELSE 0 END DESC,
           updated_at DESC LIMIT ? OFFSET ?`,
      ).bind(
        VIKING_RESEARCH_SCOPE,
        MINIMUM_ARTICLE_YEAR,
        VIKING_RESEARCH_SCOPE,
        PREFERRED_ARTICLE_YEAR,
        VIKING_RESEARCH_SCOPE,
        VIKING_RESEARCH_SCOPE,
        limit,
        offset,
      ),
      env.DB.prepare(
        "SELECT count(*) AS total FROM articles WHERE COALESCE(json_extract(metadata, '$.reading_scope'), 'general') != ? OR year >= ?",
      ).bind(VIKING_RESEARCH_SCOPE, MINIMUM_ARTICLE_YEAR),
    ]);
    items = rows.results as ArticleRow[];
    total = Number(count.results[0]?.total ?? 0);
  }
  return { items: items.map((row) => articleJson(row, url.origin)), total };
}
async function ingestLocked(
  env: AppEnv,
  id: string,
  input: unknown,
  origin: string,
) {
  const data = ingestSchema.parse(input),
    { article } = data;
  if (article.id && article.id !== id)
    throw new HttpError(
      400,
      "id_mismatch",
      "Article ID does not match the upload path.",
    );
  const existing = await env.DB.prepare("SELECT * FROM articles WHERE id=?")
    .bind(id)
    .first<ArticleRow>();
  const encoder = new TextEncoder();
  for (const value of [data.markdown, data.review])
    if (
      value !== undefined &&
      encoder.encode(value).length > Number(env.MAX_DOCUMENT_BYTES)
    )
      throw new HttpError(
        413,
        "document_limit",
        `Each Markdown or review file must be under ${env.MAX_DOCUMENT_BYTES} bytes.`,
      );
  if (data.markdown !== undefined && !data.markdown.trim())
    throw new HttpError(
      400,
      "empty_markdown",
      "Empty Markdown is not a finished research document.",
    );
  const episodeTitle = article.episode_title?.trim() || article.title;
  const mdHash =
    data.markdown === undefined
      ? (existing?.markdown_hash ?? null)
      : await digest(data.markdown);
  const reviewHash =
    data.review === undefined
      ? (existing?.review_hash ?? null)
      : await digest(data.review);
  const nextMdKey = mdHash
      ? `${id}/markdown/${readableDocumentFilename(episodeTitle)}`
      : null,
    nextReviewKey = reviewHash
      ? `${id}/review/${readableDocumentFilename(`${episodeTitle} — AI review`)}`
      : null,
    mdKey =
      data.markdown === undefined
        ? (existing?.markdown_key ?? nextMdKey)
        : nextMdKey,
    reviewKey =
      data.review === undefined
        ? (existing?.review_key ?? nextReviewKey)
        : nextReviewKey;
  const changesMd =
    data.markdown !== undefined &&
    (mdHash !== existing?.markdown_hash || mdKey !== existing?.markdown_key);
  const changesReview =
    data.review !== undefined &&
    (reviewHash !== existing?.review_hash ||
      reviewKey !== existing?.review_key);
  const oldMetadata = existing ? JSON.parse(existing.metadata) : {};
  const mdBytes =
    data.markdown === undefined
      ? Number(oldMetadata.markdown_bytes ?? 0)
      : encoder.encode(data.markdown).length;
  const reviewBytes =
    data.review === undefined
      ? Number(oldMetadata.review_bytes ?? 0)
      : encoder.encode(data.review).length;
  const totalBytes = mdBytes + reviewBytes,
    delta = totalBytes - (existing?.content_bytes ?? 0),
    newArticle = existing ? 0 : 1;
  // Reserve the limited private library space before writing R2. D1 serializes this condition.
  const reserved = await env.DB.prepare(
    "UPDATE budget SET content_bytes=content_bytes+?,articles=articles+? WHERE id=1 AND content_bytes+?<=? AND articles+?<=? RETURNING id",
  )
    .bind(
      delta,
      newArticle,
      delta,
      Number(env.MAX_LIBRARY_BYTES),
      newArticle,
      Number(env.MAX_ARTICLES),
    )
    .first();
  if (!reserved)
    throw new HttpError(
      409,
      "storage_budget",
      "Private library budget reached. Remove obsolete files or review the free-tier budget before syncing more.",
    );
  const uploaded: string[] = [];
  try {
    if (changesMd || changesReview) {
      const day = new Date().toISOString().slice(0, 10);
      const usage = await env.DB.prepare(
        `INSERT INTO daily_usage(day,uploads) VALUES(?,1) ON CONFLICT(day) DO UPDATE SET uploads=uploads+1 WHERE uploads<? RETURNING uploads`,
      )
        .bind(day, Number(env.MAX_DAILY_UPLOADS))
        .first();
      if (!usage)
        throw new HttpError(
          429,
          "upload_budget",
          "Today’s Markdown upload budget is reached; the Mac will retry later.",
        );
    }
    if (changesMd && mdKey) {
      await env.DOCUMENTS.put(mdKey, data.markdown!, {
        httpMetadata: { contentType: "text/markdown; charset=utf-8" },
      });
      uploaded.push(mdKey);
    }
    if (changesReview && reviewKey) {
      await env.DOCUMENTS.put(reviewKey, data.review!, {
        httpMetadata: { contentType: "text/markdown; charset=utf-8" },
      });
      uploaded.push(reviewKey);
    }
    const now = new Date().toISOString();
    const metadata = {
      ...oldMetadata,
      audio_url: article.audio_url ?? oldMetadata.audio_url,
      episode_title: episodeTitle,
      editions: article.editions ?? oldMetadata.editions,
      zotero_url: article.zotero_url,
      collection: article.collection,
      reading_scope: article.reading_scope ?? oldMetadata.reading_scope ?? "general",
      publish_status: article.publish_status,
      publication_status: article.publication_status,
      icloud_status: article.icloud_status,
      backup_status: article.backup_status,
      markdown_bytes: mdBytes,
      review_bytes: reviewBytes,
      index_version:
        data.markdown !== undefined || !existing
          ? 2
          : oldMetadata.index_version,
    };
    const statements = [
      env.DB.prepare(
        `INSERT INTO articles(id,title,authors,year,source_url,license_status,markdown_status,audio_status,qa_status,warnings,metadata,markdown_key,markdown_hash,review_key,review_hash,content_bytes,updated_at)
      VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET title=excluded.title,authors=excluded.authors,year=excluded.year,source_url=excluded.source_url,license_status=excluded.license_status,markdown_status=excluded.markdown_status,audio_status=excluded.audio_status,qa_status=excluded.qa_status,warnings=excluded.warnings,metadata=excluded.metadata,markdown_key=excluded.markdown_key,markdown_hash=excluded.markdown_hash,review_key=excluded.review_key,review_hash=excluded.review_hash,content_bytes=excluded.content_bytes,updated_at=excluded.updated_at`,
      ).bind(
        id,
        article.title,
        JSON.stringify(article.authors),
        article.year ?? null,
        article.source_url,
        article.license_status,
        article.markdown_status,
        article.audio_status,
        article.qa_status,
        JSON.stringify(article.warnings),
        JSON.stringify(metadata),
        mdKey,
        mdHash,
        reviewKey,
        reviewHash,
        totalBytes,
        now,
      ),
    ];
    if (
      changesMd ||
      !existing ||
      (data.markdown !== undefined && oldMetadata.index_version !== 2)
    ) {
      statements.push(
        env.DB.prepare("DELETE FROM article_search WHERE article_id=?").bind(
          id,
        ),
      );
      statements.push(
        env.DB.prepare(
          "INSERT INTO article_search(article_id,title,body) VALUES(?,?,?)",
        ).bind(
          id,
          [article.title, ...article.authors].join(" "),
          searchableBody(data.markdown ?? ""),
        ),
      );
    } else if (
      article.title !== existing.title ||
      JSON.stringify(article.authors) !== existing.authors
    )
      statements.push(
        env.DB.prepare(
          "UPDATE article_search SET title=? WHERE article_id=?",
        ).bind([article.title, ...article.authors].join(" "), id),
      );
    await env.DB.batch(statements);
  } catch (error) {
    await env.DB.prepare(
      "UPDATE budget SET content_bytes=content_bytes-?,articles=articles-? WHERE id=1",
    )
      .bind(delta, newArticle)
      .run();
    for (const key of uploaded) await env.DOCUMENTS.delete(key);
    throw error;
  }
  // R2 is a current-version mirror. Local artifacts retain source versions and QA evidence.
  if (changesMd && existing?.markdown_key)
    await env.DOCUMENTS.delete(existing.markdown_key);
  if (changesReview && existing?.review_key)
    await env.DOCUMENTS.delete(existing.review_key);
  return articleJson(await getArticle(env, id), origin);
}
export async function documentResponse(
  env: AppEnv,
  id: string,
  kind: "markdown" | "review",
) {
  const article = await getArticle(env, id),
    key = kind === "markdown" ? article.markdown_key : article.review_key;
  if (!key)
    throw new HttpError(
      404,
      "document_not_ready",
      `${article.title}: ${kind === "markdown" ? "Markdown" : "Quality report"} is not ready yet.`,
    );
  const object = await env.DOCUMENTS.get(key);
  if (!object)
    throw new HttpError(
      503,
      "document_unavailable",
      "The stored document is unavailable. Retry the Mac sync.",
    );
  const filename = readableDocumentFilename(
    `${JSON.parse(article.metadata).episode_title ?? article.title}${kind === "review" ? " — AI review" : ""}`,
  );
  return new Response(object.body, {
    headers: {
      "Content-Type": "text/markdown; charset=utf-8",
      "Cache-Control": "private, no-store",
      "Content-Disposition": `inline; filename*=UTF-8''${encodeURIComponent(filename)}`,
      ETag: object.httpEtag,
    },
  });
}

export async function ingest(
  env: AppEnv,
  id: string,
  input: unknown,
  origin: string,
) {
  const token = crypto.randomUUID(),
    now = new Date().toISOString(),
    until = new Date(Date.now() + 60000).toISOString();
  const lease = await env.DB.prepare(
    `INSERT INTO article_sync_leases(article_id,lease_token,expires_at) VALUES(?,?,?)
    ON CONFLICT(article_id) DO UPDATE SET lease_token=excluded.lease_token,expires_at=excluded.expires_at WHERE article_sync_leases.expires_at<? RETURNING article_id`,
  )
    .bind(id, token, until, now)
    .first();
  if (!lease)
    throw new HttpError(
      409,
      "article_sync_busy",
      "This article is already syncing. Retry this upload shortly.",
    );
  try {
    return await ingestLocked(env, id, input, origin);
  } finally {
    await env.DB.prepare(
      "DELETE FROM article_sync_leases WHERE article_id=? AND lease_token=?",
    )
      .bind(id, token)
      .run();
  }
}
