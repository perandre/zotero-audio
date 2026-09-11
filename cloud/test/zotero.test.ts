import { afterEach, test } from "node:test";
import assert from "node:assert/strict";
import { DatabaseSync, type SQLInputValue } from "node:sqlite";
import { readFileSync } from "node:fs";
import {
  addArticle,
  importStatus,
  listZoteroCollections,
  lookupArticle,
} from "../src/zotero";
import {
  cslMetadata,
  metadataSchema,
  normalizeDoi,
  publicFetch,
  publicUrl,
  resolveMetadata,
  zoteroPayload,
} from "../src/article-metadata";
import { mcp } from "../src/mcp";
import type { AppEnv } from "../src/types";

const realFetch = globalThis.fetch;
afterEach(() => {
  globalThis.fetch = realFetch;
});
const doi = "10.1234/synthetic-paper";
const metadata = metadataSchema.parse({
  itemType: "journalArticle",
  title: "Synthetic article: preserving research across interruptions",
  DOI: doi,
  url: `https://doi.org/${doi}`,
});
const input = { source: doi, metadata, request_id: "synthetic-request-1" };
function fixture() {
  const db = new DatabaseSync(":memory:");
  db.exec(
    readFileSync(
      new URL("../migrations/0006_zotero_imports.sql", import.meta.url),
      "utf8",
    ),
  );
  const statement = (sql: string, args: SQLInputValue[] = []) => ({
    bind: (...values: SQLInputValue[]) => statement(sql, values),
    first: async () => db.prepare(sql).get(...args) ?? null,
    run: async () => ({ meta: db.prepare(sql).run(...args), success: true }),
  });
  const env = {
    ZOTERO_API_KEY: "synthetic-secret-never-return",
    ZOTERO_USER_ID: "12345",
    DB: { prepare: statement },
  } as unknown as AppEnv;
  const items = new Map<string, any>();
  const state = {
    writes: 0,
    patches: 0,
    scans: 0,
    lostResponse: false,
    failItem: false,
    rejectGet: false,
    conflictPatch: false,
    backoff: false,
    redirect: false,
    total: 0,
    drift: false,
  };
  globalThis.fetch = async (raw, init) => {
    const url = new URL(String(raw));
    assert.equal(url.origin, "https://api.zotero.org");
    assert.ok(url.pathname.startsWith("/users/12345/"));
    assert.equal(
      new Headers(init?.headers).get("Zotero-API-Key"),
      env.ZOTERO_API_KEY,
    );
    assert.equal(init?.redirect, "manual");
    assert.equal(url.searchParams.has("key"), false);
    if (state.redirect) return new Response(null, { status: 302, headers: { Location: "https://elsewhere.example.org/collect" } });
    const method = init?.method ?? "GET";
    const data = init?.body ? JSON.parse(String(init.body)) : null;
    if (state.rejectGet && method === "GET")
      return new Response("synthetic-secret-never-return", { status: 403 });
    if (url.pathname.endsWith("/collections"))
      return Response.json(
        [
          {
            key: "ABCDEFGH",
            data: {
              name: "Synthetic research collection",
              parentCollection: false,
            },
          },
        ],
        { headers: { "Total-Results": "1" } },
      );
    if (url.pathname.includes("/collections/"))
      return new Response(null, {
        status: url.pathname.endsWith("ABCDEFGH") ? 200 : 404,
      });
    if (url.pathname.endsWith("/items/top")) {
      state.scans++;
      const start = Number(url.searchParams.get("start"));
      const values = state.total
        ? Array.from({ length: 100 }, () => ({
            key: "ZYXWVUTS",
            version: 1,
            data: {
              title: "Another synthetic article",
              itemType: "journalArticle",
              collections: [],
              tags: [],
            },
          }))
        : [...items.values()].slice(start, start + 100);
      return Response.json(values, {
        headers: {
          "Total-Results": String(state.total || items.size),
          "Last-Modified-Version": String(state.drift ? state.scans : 10),
          ...(state.backoff ? { Backoff: "60" } : {}),
        },
      });
    }
    if (url.pathname.endsWith("/items") && method === "POST") {
      state.writes++;
      assert.equal(data[0].version, 0);
      if (state.failItem)
        return Response.json({
          successful: {},
          failed: {
            "0": { code: 400, message: "synthetic-secret-never-return" },
          },
        });
      if (items.has(data[0].key))
        return Response.json({ failed: { "0": { code: 412 } } });
      const saved = {
        key: data[0].key,
        version: 2,
        data: { ...data[0], version: 2 },
      };
      items.set(saved.key, saved);
      if (state.lostResponse) {
        state.lostResponse = false;
        throw new Error("synthetic-secret-never-return");
      }
      return Response.json({ successful: { "0": saved }, failed: {} });
    }
    const key = url.pathname.split("/").at(-1)!;
    const item = items.get(key);
    if (!item) return new Response(null, { status: 404 });
    if (method === "GET") return Response.json(item);
    assert.equal(method, "PATCH");
    state.patches++;
    if (state.conflictPatch) {
      state.conflictPatch = false;
      item.version++;
      item.data.collections.push("ZYXWVUTS");
      item.data.tags.push({ tag: "Concurrent user tag", type: 1 });
      return new Response(null, { status: 412 });
    }
    assert.equal(
      Number(new Headers(init?.headers).get("If-Unmodified-Since-Version")),
      item.version,
    );
    item.data = { ...item.data, ...data };
    item.version++;
    return new Response(null, { status: 204 });
  };
  const existing = (key = "BCDEFGHJ") => {
    const item = {
      key,
      version: 1,
      data: {
        ...zoteroPayload(metadata),
        title: "Existing full article title edited by the owner",
        collections: ["JKLMNPQR"],
        tags: [{ tag: "Existing tag", type: 1 }],
        abstractNote: "Owner edits must survive",
      },
    };
    items.set(key, item);
    return item;
  };
  return { env, db, items, state, existing };
}

test("Cloud save survives a lost response and preserves immutable request arguments", async () => {
  const { env, state, items } = fixture();
  state.lostResponse = true;
  await assert.rejects(addArticle(env, input), /same request_id/);
  assert.equal(
    (await importStatus(env, input.request_id)).status,
    "needs_retry",
  );
  const result = await addArticle(env, input);
  assert.equal(result.status, "created");
  assert.equal(result.title, metadata.title);
  assert.equal(result.pdf_downloaded, false);
  assert.equal(items.size, 1);
  assert.equal(state.writes, 1);
  assert.deepEqual(await addArticle(env, input), result);
  await assert.rejects(
    addArticle(env, { ...input, tags: ["changed"] }),
    /different arguments/,
  );
});

test("Duplicates keep edited metadata and add collections/tags after a version conflict", async () => {
  const { env, existing, state, items } = fixture();
  const old = existing();
  state.conflictPatch = true;
  const result = await addArticle(env, {
    ...input,
    collection_key: "ABCDEFGH",
    tags: ["New tag"],
  });
  assert.equal(result.status, "already_exists");
  assert.equal(result.title, old.data.title);
  assert.equal(state.writes, 0);
  assert.equal(state.patches, 2);
  assert.deepEqual(result.collection_keys, [
    "JKLMNPQR",
    "ZYXWVUTS",
    "ABCDEFGH",
  ]);
  assert.deepEqual(result.tags, [
    "Existing tag",
    "Concurrent user tag",
    "New tag",
  ]);
  assert.equal(
    items.get(old.key).data.abstractNote,
    "Owner edits must survive",
  );
});

test("Concurrent MCP requests reserve one item and retries never create a second reference", async () => {
  const { env, state, items } = fixture();
  const second = { ...input, request_id: "synthetic-request-2" };
  const results = await Promise.allSettled([
    addArticle(env, input),
    addArticle(env, second),
  ]);
  assert.ok(results.some((r) => r.status === "fulfilled"));
  await addArticle(env, input);
  await addArticle(env, second);
  assert.equal(items.size, 1);
  assert.equal(state.writes, 1);
});

test("HTTP 200 with per-item failure is not a successful import and does not leak errors", async () => {
  const { env, state } = fixture();
  state.failItem = true;
  await assert.rejects(addArticle(env, input), (error) => {
    assert.match(String(error), /rejected the reference metadata/);
    assert.doesNotMatch(String(error), /synthetic-secret/);
    return true;
  });
  assert.equal(
    (await importStatus(env, input.request_id)).status,
    "needs_retry",
  );
});

test("Incomplete, oversized or changing duplicate scans fail before a write", async () => {
  for (const total of [201, 2001]) {
    const { env, state } = fixture();
    state.total = total;
    state.drift = total === 201;
    await assert.rejects(addArticle(env, input), /changed during|2,000/);
    assert.equal(state.writes, 0);
  }
});

test("Duplicate checking paginates instead of relying on Zotero quick search", async () => {
  const { env, existing, items, state } = fixture();
  for (let i = 0; i < 100; i++)
    items.set(`synthetic-${i}`, {
      key: "ZYXWVUTS",
      version: 1,
      data: {
        itemType: "webpage",
        title: `Different synthetic title ${i}`,
        url: `https://example.org/${i}`,
      },
    });
  existing();
  assert.equal((await addArticle(env, input)).status, "already_exists");
  assert.equal(state.scans, 2);
  assert.equal(state.writes, 0);
});

test("Bad collection, missing credentials and rejected API access cannot create items", async () => {
  const { env, state } = fixture();
  await assert.rejects(
    addArticle(env, { ...input, collection_key: "ZYXWVUTS" }),
    /existing personal-library collection/,
  );
  assert.equal(state.writes, 0);
  await assert.rejects(
    lookupArticle({ ...env, ZOTERO_API_KEY: undefined }, doi, metadata),
    /API key/,
  );
  state.rejectGet = true;
  await assert.rejects(lookupArticle(env, doi, metadata), (error) => {
    assert.doesNotMatch(String(error), /synthetic-secret/);
    return true;
  });
});

test("Zotero backoff survives requests and preserves unfinished receipts", async () => {
  const { env, state } = fixture();
  state.backoff = true;
  await assert.rejects(addArticle(env, input), /requested a pause/);
  await assert.rejects(listZoteroCollections(env), /requested a pause/);
  assert.equal(state.writes, 0);
});

test("Request quotas allow recovery of existing saves", async () => {
  const { env, db } = fixture();
  const result = await addArticle(env, input);
  const insert = db.prepare(
    "INSERT INTO zotero_import_requests(id, request_hash, library_id, created_at) VALUES (?, 'synthetic', '12345', ?)",
  );
  for (let i = 0; i < 49; i++)
    insert.run(`quota-${i}`, new Date().toISOString());
  await assert.rejects(
    addArticle(env, { ...input, request_id: "beyond-quota" }),
    /50 new import requests/,
  );
  assert.deepEqual(await addArticle(env, input), result);
});

test("DOI metadata preserves titles/authors and rejects mismatched or unsupported records", async () => {
  const raw = {
    type: "article-journal",
    DOI: doi,
    title: "Synthetic <i>research</i> &amp; evidence title",
    author: [
      { given: "Ada", family: "Example" },
      { literal: "Example Research Group" },
    ],
    issued: { "date-parts": [[2025, 3, 2]] },
    "container-title": "Synthetic Journal",
  };
  const result = cslMetadata(raw, doi);
  assert.equal(result.title, "Synthetic research & evidence title");
  assert.equal(result.date, "2025-03-02");
  assert.equal(result.creators.length, 2);
  assert.equal(
    cslMetadata({ ...raw, type: "proceedings-article" }, doi).itemType,
    "conferencePaper",
  );
  assert.equal(
    cslMetadata({ ...raw, type: "journal-article" }, doi).itemType,
    "journalArticle",
  );
  assert.equal(normalizeDoi("https://doi.org/10.1234/SYNTHETIC-PAPER"), doi);
  assert.throws(
    () => cslMetadata({ ...raw, DOI: "10.1234/different" }, doi),
    /different DOI/,
  );
  assert.throws(
    () => cslMetadata({ ...raw, type: "book" }, doi),
    /not an article/,
  );
});

test("Metadata fetching rejects local URLs and credential-bearing redirects without sending Zotero secrets", async () => {
  for (const url of [
    "http://example.org",
    "https://localhost/a",
    "https://127.0.0.1",
    "https://2130706433",
    "https://[::1]",
    "https://example.org:8080",
    "https://user:secret@example.org",
    "https://x.local/a",
  ])
    assert.throws(() => publicUrl(url));
  let calls = 0;
  globalThis.fetch = async (_url, init) => {
    calls++;
    assert.equal(new Headers(init?.headers).has("Zotero-API-Key"), false);
    return new Response(null, {
      status: 302,
      headers: { Location: "https://127.0.0.1/private" },
    });
  };
  await assert.rejects(
    publicFetch("https://example.org/paper", "text/html"),
    /public HTTPS/,
  );
  assert.equal(calls, 1);
  await assert.rejects(
    resolveMetadata(doi, { ...metadata, DOI: "10.1234/other" }),
    /match the source DOI/,
  );
});

test("Read/jobs/document scopes cannot mutate Zotero; Zotero scope does not grant other writes", async () => {
  const { env } = fixture();
  for (const scopes of [
    ["library:read"],
    ["library:read", "jobs:write", "documents:write"],
    ["library:read", "zotero:write"],
  ]) {
    const response = await mcp(
      new Request("https://one-more-paper.example/mcp", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          Accept: "application/json, text/event-stream",
        },
        body: JSON.stringify({
          jsonrpc: "2.0",
          id: 1,
          method: "tools/list",
          params: {},
        }),
      }),
      env,
      { userId: "owner", scopes },
    );
    const tools = ((await response.json()) as any).result.tools.map(
      (t: any) => t.name,
    );
    assert.equal(
      tools.includes("add_article"),
      scopes.includes("zotero:write"),
    );
    assert.equal(tools.includes("create_job"), scopes.includes("jobs:write"));
    assert.equal(
      tools.includes("save_document"),
      scopes.includes("documents:write"),
    );
    assert.ok(tools.includes("lookup_article"));
  }
});

test("Zotero redirects are rejected without forwarding the credential", async () => {
  const {env,state}=fixture();state.redirect=true;
  await assert.rejects(listZoteroCollections(env), /did not accept/);
  assert.equal(state.writes,0);
});
