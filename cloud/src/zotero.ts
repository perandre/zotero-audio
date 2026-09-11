import { z } from "zod";
import { boundedText, digest, HttpError } from "./http";
import {
  metadataSchema,
  normalizeDoi,
  normalizedUrl,
  resolveMetadata,
  zoteroPayload,
  type ArticleMetadata,
} from "./article-metadata";
import type { AppEnv } from "./types";

export const zoteroKey = z
  .string()
  .regex(/^[23456789ABCDEFGHIJKLMNPQRSTUVWXYZ]{8}$/);
export const importSchema = z
  .object({
    source: z.string().trim().min(1).max(2048),
    collection_key: zoteroKey.optional(),
    tags: z.array(z.string().trim().min(1).max(200)).max(30).default([]),
    metadata: metadataSchema.optional(),
    request_id: z.string().regex(/^[a-zA-Z0-9][a-zA-Z0-9._:-]{0,127}$/),
  })
  .strict();
type ImportInput = z.infer<typeof importSchema>;
type StoredImport = {
  identity: string;
  item_key: string;
  payload: string;
  creator_request_id: string | null;
};
type Receipt = {
  id: string;
  request_hash: string;
  library_id: string;
  identity: string | null;
  result: string | null;
  created_at: string;
};
const itemSchema = z.object({
  key: zoteroKey,
  version: z.number().int().nonnegative(),
  data: z
    .object({
      title: z.string().default(""),
      itemType: z.string(),
      DOI: z.string().optional(),
      url: z.string().optional(),
      extra: z.string().optional(),
      deleted: z.union([z.boolean(), z.number()]).optional(),
      collections: z.array(zoteroKey).default([]),
      tags: z
        .array(
          z
            .object({ tag: z.string(), type: z.number().optional() })
            .passthrough(),
        )
        .default([]),
    })
    .passthrough(),
});
type ZoteroItem = z.infer<typeof itemSchema>;

function config(env: AppEnv) {
  if (!env.ZOTERO_API_KEY || !/^\d{1,20}$/.test(env.ZOTERO_USER_ID ?? ""))
    throw new HttpError(
      503,
      "zotero_setup_required",
      "The hosted Zotero connection needs its API key and personal library user ID configured.",
    );
  return env.ZOTERO_USER_ID!;
}

async function api(
  env: AppEnv,
  path: string,
  method = "GET",
  data?: unknown,
  version?: number,
): Promise<Response> {
  const user = config(env);
  const state = await env.DB.prepare(
    "SELECT retry_at FROM zotero_api_state WHERE library_id = ?",
  )
    .bind(user)
    .first<{ retry_at: number }>();
  if (state && state.retry_at > Date.now())
    throw new HttpError(
      429,
      "zotero_backoff",
      `Zotero requested a pause. Retry the same request in ${Math.ceil((state.retry_at - Date.now()) / 1000)} seconds.`,
    );
  let response: Response;
  try {
    response = await fetch(`https://api.zotero.org/users/${user}${path}`, {
      method,
      redirect: "error",
      cache: "no-store",
      signal: AbortSignal.timeout(15000),
      headers: {
        "Zotero-API-Key": env.ZOTERO_API_KEY!,
        "Zotero-API-Version": "3",
        Accept: "application/json",
        ...(data === undefined ? {} : { "Content-Type": "application/json" }),
        ...(version === undefined
          ? {}
          : { "If-Unmodified-Since-Version": String(version) }),
      },
      ...(data === undefined ? {} : { body: JSON.stringify(data) }),
    });
  } catch {
    throw new HttpError(
      502,
      "zotero_unavailable",
      "Zotero could not be reached. Retry with the same request_id to check whether the save completed.",
    );
  }
  const delay = Math.max(
    Number(response.headers.get("Backoff")) || 0,
    Number(response.headers.get("Retry-After")) || 0,
    response.status === 429 ? 60 : 0,
  );
  if (delay > 0)
    await env.DB.prepare(
      "INSERT INTO zotero_api_state(library_id, retry_at) VALUES (?, ?) ON CONFLICT(library_id) DO UPDATE SET retry_at = MAX(retry_at, excluded.retry_at)",
    )
      .bind(user, Date.now() + Math.min(delay, 86400) * 1000)
      .run();
  if (!response.ok && ![404, 412].includes(response.status)) {
    await response.body?.cancel();
    throw new HttpError(
      response.status === 429 ? 429 : 502,
      "zotero_rejected",
      response.status === 403 || response.status === 401
        ? "The Zotero key lacks access to this personal library or permission to write. Check its settings."
        : "Zotero did not accept the request. Retry later with the same request_id; no completion has been confirmed.",
    );
  }
  return response;
}

async function readJson(response: Response): Promise<unknown> {
  try {
    return JSON.parse(
      await boundedText(response as unknown as Request, 2_000_000),
    );
  } catch {
    throw new HttpError(
      502,
      "zotero_invalid_response",
      "Zotero returned an unreadable response. Retry with the same request_id.",
    );
  }
}
async function readItem(env: AppEnv, key: string): Promise<ZoteroItem | null> {
  const response = await api(env, `/items/${zoteroKey.parse(key)}`);
  if (response.status === 404) {
    await response.body?.cancel();
    return null;
  }
  const item = itemSchema.parse(await readJson(response));
  return item.data.deleted ? null : item;
}
function identity(metadata: ArticleMetadata) {
  return metadata.DOI
    ? `doi:${metadata.DOI}`
    : `url:${normalizedUrl(metadata.url)}`;
}
function itemDoi(item: ZoteroItem) {
  return (
    normalizeDoi(item.data.DOI ?? "") ??
    normalizeDoi(item.data.extra?.match(/^DOI:\s*(.+)$/im)?.[1] ?? "") ??
    normalizeDoi(item.data.url ?? "")
  );
}
function matches(item: ZoteroItem, metadata: ArticleMetadata) {
  return (
    !item.data.deleted &&
    !["note", "attachment"].includes(item.data.itemType) &&
    ((metadata.DOI && itemDoi(item) === metadata.DOI) ||
      (item.data.url &&
        normalizedUrl(item.data.url) === normalizedUrl(metadata.url)))
  );
}

// Zotero quick search does not reliably search DOI/URL fields. Scan explicit
// fields, bounded to 2,000 references / 20 requests on Workers Free. Fail closed
// for larger or concurrently changing libraries instead of claiming no match.
async function findDuplicate(
  env: AppEnv,
  metadata: ArticleMetadata,
): Promise<ZoteroItem | null> {
  let version: string | null = null;
  for (let start = 0; start < 2000; start += 100) {
    const response = await api(
      env,
      `/items/top?format=json&limit=100&start=${start}&sort=dateAdded&direction=asc`,
    );
    const currentVersion = response.headers.get("Last-Modified-Version");
    if (!currentVersion || (version !== null && currentVersion !== version)) {
      await response.body?.cancel();
      throw new HttpError(
        409,
        "zotero_library_changed",
        "The Zotero library changed during the duplicate check. Retry the same request.",
      );
    }
    version = currentVersion;
    const total = Number(response.headers.get("Total-Results"));
    if (
      !response.headers.has("Total-Results") ||
      !Number.isSafeInteger(total) ||
      total > 2000
    ) {
      await response.body?.cancel();
      throw new HttpError(
        409,
        "zotero_library_limit",
        "Safe duplicate checks currently support up to 2,000 top-level Zotero items. No new reference was created.",
      );
    }
    const items = z.array(itemSchema).parse(await readJson(response));
    const found = items.find((item) => matches(item, metadata));
    if (found) return found;
    if (start + items.length >= total) return null;
    if (items.length !== 100) break;
  }
  throw new HttpError(
    502,
    "zotero_incomplete_scan",
    "The Zotero duplicate check was incomplete. Retry later; no new reference was created.",
  );
}

export async function listZoteroCollections(env: AppEnv, start = 0) {
  const response = await api(
    env,
    `/collections?format=json&limit=100&start=${start}&sort=title&direction=asc`,
  );
  const collections = z
    .array(
      z.object({
        key: zoteroKey,
        data: z.object({
          name: z.string(),
          parentCollection: z.union([zoteroKey, z.literal(false)]),
        }),
      }),
    )
    .parse(await readJson(response));
  return {
    collections: collections.map((c) => ({
      key: c.key,
      name: c.data.name,
      parent_key: c.data.parentCollection || null,
    })),
    next_offset:
      start + collections.length < Number(response.headers.get("Total-Results"))
        ? start + collections.length
        : null,
  };
}

export async function lookupArticle(
  env: AppEnv,
  source: string,
  supplied?: ArticleMetadata,
) {
  config(env);
  const metadata = await resolveMetadata(source, supplied);
  const duplicate = await findDuplicate(env, metadata);
  return {
    metadata,
    existing_item: duplicate ? itemResult(env, duplicate) : null,
    pdf_downloaded: false,
  };
}
function itemResult(env: AppEnv, item: ZoteroItem) {
  return {
    title: item.data.title,
    item_key: item.key,
    zotero_url: `https://www.zotero.org/users/${config(env)}/items/${item.key}`,
    collection_keys: item.data.collections,
    tags: item.data.tags.map((t) => t.tag),
  };
}

async function organize(
  env: AppEnv,
  original: ZoteroItem,
  input: ImportInput,
  metadata: ArticleMetadata,
): Promise<ZoteroItem> {
  let item = original;
  for (let attempt = 0; attempt < 3; attempt++) {
    if (!matches(item, metadata))
      throw new HttpError(
        409,
        "zotero_item_changed",
        "The reserved Zotero item no longer matches this article. Nothing was overwritten.",
      );
    const collections = [
      ...new Set([
        ...item.data.collections,
        ...(input.collection_key ? [input.collection_key] : []),
      ]),
    ];
    const tags = [
      ...item.data.tags,
      ...input.tags
        .filter((tag) => !item.data.tags.some((t) => t.tag === tag))
        .map((tag) => ({ tag })),
    ];
    if (
      collections.length === item.data.collections.length &&
      tags.length === item.data.tags.length
    )
      return item;
    const response = await api(
      env,
      `/items/${item.key}`,
      "PATCH",
      { collections, tags },
      item.version,
    );
    const status = response.status;
    await response.body?.cancel();
    item =
      (await readItem(env, item.key)) ??
      (() => {
        throw new HttpError(
          409,
          "zotero_item_missing",
          "The reference was removed while saving. No replacement was created.",
        );
      })();
    if (
      status === 204 &&
      collections.every((c) => item.data.collections.includes(c)) &&
      tags.every((t) => item.data.tags.some((v) => v.tag === t.tag))
    )
      return item;
    if (![204, 412].includes(status)) break;
  }
  throw new HttpError(
    409,
    "zotero_collection_conflict",
    "The article exists, but its collection/tag update could not be confirmed. Retry the same request_id.",
  );
}

export async function importStatus(env: AppEnv, requestId: string) {
  const receipt = await env.DB.prepare(
    "SELECT * FROM zotero_import_requests WHERE id = ? AND library_id = ?",
  )
    .bind(requestId, config(env))
    .first<Receipt>();
  if (!receipt)
    throw new HttpError(
      404,
      "import_not_found",
      "No Zotero import has this request_id.",
    );
  return receipt.result
    ? (JSON.parse(receipt.result) as Record<string, unknown>)
    : {
        request_id: receipt.id,
        status: "needs_retry",
        created_at: receipt.created_at,
        message:
          "The save has not been confirmed. It may already exist in Zotero; retry add_article with the identical arguments and request_id to finish safely.",
      };
}

export async function addArticle(
  env: AppEnv,
  raw: unknown,
): Promise<Record<string, unknown>> {
  const input = importSchema.parse(raw),
    user = config(env);
  input.tags = [...new Set(input.tags)].sort();
  const hash = await digest(
    JSON.stringify({
      ...input,
      source: normalizeDoi(input.source) ?? normalizedUrl(input.source),
      library_id: user,
    }),
  );
  let receipt = await env.DB.prepare(
    "SELECT * FROM zotero_import_requests WHERE id = ?",
  )
    .bind(input.request_id)
    .first<Receipt>();
  if (!receipt) {
    await env.DB.prepare(
      `INSERT OR IGNORE INTO zotero_import_requests(id, request_hash, library_id, created_at)
      SELECT ?, ?, ?, ? WHERE (SELECT COUNT(*) FROM zotero_import_requests WHERE created_at >= ?) < 50
      AND (SELECT COUNT(*) FROM zotero_import_requests) < 10000`,
    )
      .bind(
        input.request_id,
        hash,
        user,
        new Date().toISOString(),
        new Date().toISOString().slice(0, 10),
      )
      .run();
    receipt = await env.DB.prepare(
      "SELECT * FROM zotero_import_requests WHERE id = ?",
    )
      .bind(input.request_id)
      .first<Receipt>();
  }
  if (!receipt)
    throw new HttpError(
      429,
      "import_limit",
      "The limit of 50 new import requests per UTC day or 10,000 retained receipts has been reached. Existing requests can still be retried.",
    );
  if (receipt.request_hash !== hash || receipt.library_id !== user)
    throw new HttpError(
      409,
      "request_id_conflict",
      "This request_id belongs to different arguments. Use the original arguments to retry, or a new request_id for a different request.",
    );
  if (receipt.result) return JSON.parse(receipt.result);
  if (input.collection_key) {
    const response = await api(env, `/collections/${input.collection_key}`);
    await response.body?.cancel();
    if (response.status !== 200)
      throw new HttpError(
        400,
        "collection_not_found",
        "Select an existing personal-library collection with zotero_collections. No new reference was created.",
      );
  }
  let stored = receipt.identity
    ? await env.DB.prepare(
        "SELECT * FROM zotero_import_items WHERE library_id = ? AND identity = ?",
      )
        .bind(user, receipt.identity)
        .first<StoredImport>()
    : null;
  if (!stored) {
    const metadata = await resolveMetadata(input.source, input.metadata),
      articleIdentity = identity(metadata);
    stored = await env.DB.prepare(
      "SELECT * FROM zotero_import_items WHERE library_id = ? AND identity = ?",
    )
      .bind(user, articleIdentity)
      .first<StoredImport>();
    if (!stored) {
      const duplicate = await findDuplicate(env, metadata);
      const alphabet = "23456789ABCDEFGHIJKLMNPQRSTUVWXYZ";
      const key =
        duplicate?.key ??
        Array.from(
          crypto.getRandomValues(new Uint8Array(8)),
          (n) => alphabet[n % 32],
        ).join("");
      await env.DB.prepare(
        "INSERT OR IGNORE INTO zotero_import_items(library_id, identity, item_key, payload, creator_request_id) VALUES (?, ?, ?, ?, ?)",
      )
        .bind(
          user,
          articleIdentity,
          key,
          JSON.stringify(metadata),
          duplicate ? null : input.request_id,
        )
        .run();
      stored = await env.DB.prepare(
        "SELECT * FROM zotero_import_items WHERE library_id = ? AND identity = ?",
      )
        .bind(user, articleIdentity)
        .first<StoredImport>();
      if (!stored)
        throw new HttpError(
          409,
          "zotero_key_conflict",
          "An item-key reservation conflicted. Retry this request.",
        );
    }
    await env.DB.prepare(
      "UPDATE zotero_import_requests SET identity = ? WHERE id = ?",
    )
      .bind(articleIdentity, input.request_id)
      .run();
  }
  const metadata = metadataSchema.parse(JSON.parse(stored.payload));
  let item = await readItem(env, stored.item_key);
  if (!item) {
    if (stored.creator_request_id !== input.request_id)
      throw new HttpError(
        409,
        "zotero_item_missing",
        "This article has an existing reservation, but its Zotero item is missing. Retry its original import; deleted references are not automatically recreated.",
      );
    const response = await api(env, "/items", "POST", [
      { ...zoteroPayload(metadata), key: stored.item_key, version: 0 },
    ]);
    const outcome =
      response.status === 412
        ? (await response.body?.cancel(),
          { successful: {}, unchanged: {}, failed: { "0": { code: 412 } } })
        : z
            .object({
              successful: z.record(z.string(), z.unknown()).default({}),
              unchanged: z.record(z.string(), z.string()).default({}),
              failed: z
                .record(
                  z.string(),
                  z.object({ code: z.number() }).passthrough(),
                )
                .default({}),
            })
            .parse(await readJson(response));
    // HTTP 200 is not sufficient: Zotero reports per-item failures inside it.
    if (outcome.failed["0"] && outcome.failed["0"].code !== 412)
      throw new HttpError(
        502,
        "zotero_item_rejected",
        "Zotero rejected the reference metadata. No completed save was confirmed. Check the supplied fields and retry with the same request_id when resolved.",
      );
    item = await readItem(env, stored.item_key);
    if (
      !item ||
      (!outcome.successful["0"] &&
        !outcome.unchanged["0"] &&
        outcome.failed["0"]?.code !== 412)
    )
      throw new HttpError(
        502,
        "zotero_save_unconfirmed",
        "The Zotero save could not be confirmed. Retry with the same request_id.",
      );
  }
  item = await organize(env, item, input, metadata);
  const result = {
    request_id: input.request_id,
    status:
      stored.creator_request_id === input.request_id
        ? "created"
        : "already_exists",
    ...itemResult(env, item),
    saved_at: new Date().toISOString(),
    pdf_downloaded: false,
    message:
      "The reference is saved in Zotero online. PDF download, research Markdown and audio are separate steps; Mac processing waits while it is offline.",
  };
  await env.DB.prepare(
    "UPDATE zotero_import_requests SET result = ? WHERE id = ?",
  )
    .bind(JSON.stringify(result), input.request_id)
    .run();
  return result;
}
