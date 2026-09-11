import { afterEach, test } from "node:test";
import assert from "node:assert/strict";
import { DatabaseSync, type SQLInputValue } from "node:sqlite";
import { readFileSync } from "node:fs";
import { createHash } from "node:crypto";
import * as projects from "../src/projects";
import { mcp } from "../src/mcp";
import { digest } from "../src/http";
import type { AppEnv } from "../src/types";

const originalFetch = globalThis.fetch;
afterEach(() => {
  globalThis.fetch = originalFetch;
});
const hash = (text: string) => createHash("sha1").update(text).digest("hex");
type Entry = {
  path: string;
  type: string;
  mode: string;
  sha: string;
  size?: number;
};
type Commit = { tree: { sha: string }; parents: string[] };
function fixture() {
  const db = new DatabaseSync(":memory:");
  // Node SQLite omits FTS5; that legacy index is exercised by Wrangler integration.
  db.exec(
    readFileSync(
      new URL("../migrations/0004_project_documents.sql", import.meta.url),
      "utf8",
    ).replace(/^CREATE VIRTUAL TABLE project_search.*$/m, ""),
  );
  db.exec(
    readFileSync(
      new URL("../migrations/0005_project_github.sql", import.meta.url),
      "utf8",
    ),
  );
  const statement = (sql: string, args: SQLInputValue[] = []) => ({
    bind: (...values: SQLInputValue[]) => statement(sql, values),
    first: async () => db.prepare(sql).get(...args) ?? null,
    all: async () => ({ results: db.prepare(sql).all(...args), success: true }),
    run: async () => ({ meta: db.prepare(sql).run(...args), success: true }),
  });
  const env = {
    PROJECT_GITHUB_REPO: "perandre/phd",
    PROJECT_GITHUB_BRANCH: "main",
    PROJECT_GITHUB_TOKEN: "synthetic-repository-token",
    DB: {
      prepare: statement,
      batch: async (statements: ReturnType<typeof statement>[]) =>
        Promise.all(statements.map((s) => s.run())),
    },
  } as unknown as AppEnv;
  const blobs = new Map<string, string>();
  const trees = new Map<string, Entry[]>();
  const commits = new Map<string, Commit>();
  const initial = "# Current PhD work\n\nA synthetic priority.\n";
  const entry = (path: string, text: string, mode = "100644") => {
    const sha = hash(`blob ${Buffer.byteLength(text)}\0${text}`);
    blobs.set(sha, text);
    return { path, sha, type: "blob", mode, size: Buffer.byteLength(text) };
  };
  const firstTree = hash("first-tree"),
    first = hash("first-commit");
  trees.set(firstTree, [
    entry("NOW.md", initial),
    entry("PROJECT.md", "# Full project title\n\nSynthetic facts."),
    entry(".env", "synthetic hidden value"),
    entry("alias.md", "NOW.md", "120000"),
  ]);
  commits.set(first, { tree: { sha: firstTree }, parents: [] });
  const state = {
    head: first,
    puts: 0,
    writes: 0,
    loseResponse: false,
    failBeforePublish: false,
    race: false,
    failGet: false,
    calls: [] as string[],
  };
  const advance = (path: string, text: string) => {
    const tree = trees.get(commits.get(state.head)!.tree.sha)!;
    const newTree = hash(JSON.stringify([path, text, state.head]));
    trees.set(newTree, [
      ...tree.filter((e) => e.path !== path),
      entry(path, text),
    ]);
    const next = hash(newTree + state.head);
    commits.set(next, { tree: { sha: newTree }, parents: [state.head] });
    state.head = next;
  };
  const ancestor = (parent: string, child: string): boolean =>
    child === parent ||
    (commits.get(child)?.parents.some((p) => ancestor(parent, p)) ?? false);
  globalThis.fetch = async (input, init) => {
    const url = new URL(String(input));
    assert.equal(url.origin, "https://api.github.com");
    assert.equal(
      new Headers(init?.headers).get("Authorization"),
      "Bearer synthetic-repository-token",
    );
    assert.equal(init?.redirect, "error");
    assert.equal(init?.cache, "no-store");
    const method = init?.method ?? "GET",
      path = url.pathname.replace("/repos/perandre/phd", "");
    state.calls.push(`${method} ${path}`);
    if (state.failGet && method === "GET")
      return Response.json(
        { message: "synthetic private error must not leak" },
        { status: 403 },
      );
    const data = init?.body ? JSON.parse(String(init.body)) : null;
    if (path === "/git/ref/heads/main")
      return Response.json({ object: { sha: state.head } });
    if (path.startsWith("/git/commits/") && method === "GET")
      return Response.json(commits.get(path.split("/").pop()!));
    if (path.startsWith("/git/trees/") && method === "GET")
      return Response.json({
        tree: trees.get(path.split("/").pop()!),
        truncated: false,
      });
    if (path.startsWith("/git/blobs/")) {
      const sha = path.split("/").pop()!;
      return Response.json({
        sha,
        encoding: "base64",
        content: Buffer.from(blobs.get(sha)!).toString("base64"),
      });
    }
    if (path === "/graphql") {
      const repository: Record<string, unknown> = {};
      for (const match of data.query.matchAll(
        /(b\d+): object\(oid: "([a-f0-9]+)"\)/g,
      )) {
        const text = blobs.get(match[2])!;
        repository[match[1]] = {
          oid: match[2],
          text,
          isBinary: false,
          byteSize: Buffer.byteLength(text),
        };
      }
      return Response.json({ data: { repository } });
    }
    if (path === "/git/trees" && method === "POST") {
      const old = trees.get(data.base_tree)!;
      const value = data.tree[0];
      assert.equal(data.tree.length, 1);
      const sha = hash(JSON.stringify(data));
      trees.set(sha, [
        ...old.filter((e) => e.path !== value.path),
        entry(value.path, value.content, value.mode),
      ]);
      return Response.json({ sha });
    }
    if (path === "/git/commits" && method === "POST") {
      const sha = hash(JSON.stringify(data));
      commits.set(sha, { tree: { sha: data.tree }, parents: data.parents });
      state.writes++;
      return Response.json({ sha });
    }
    if (path.startsWith("/compare/")) {
      const [a, b] = path.slice("/compare/".length).split("...");
      return Response.json({
        status:
          a === b
            ? "identical"
            : ancestor(a, b)
              ? "ahead"
              : ancestor(b, a)
                ? "behind"
                : "diverged",
      });
    }
    if (path === "/git/refs/heads/main" && method === "PATCH") {
      assert.equal(data.force, false);
      if (state.failBeforePublish) {
        state.failBeforePublish = false;
        throw new Error("synthetic disconnect");
      }
      if (state.race) {
        state.race = false;
        advance("OTHER.md", "# Concurrent edit");
      }
      if (!ancestor(state.head, data.sha))
        return Response.json({}, { status: 422 });
      state.head = data.sha;
      state.puts++;
      if (state.loseResponse) {
        state.loseResponse = false;
        throw new Error("synthetic lost success response");
      }
      return Response.json({ object: { sha: data.sha } });
    }
    throw new Error(`Unexpected GitHub request: ${method} ${path}`);
  };
  return { env, db, state, advance, initial, trees, commits, entry };
}
const origin = "https://mcp.example.test";
const save = async (
  f: ReturnType<typeof fixture>,
  id = "synthetic-save-001",
  text = "# Revised PhD priorities\n\nNext documented action.\n",
) =>
  projects.saveDocument(f.env, {
    path: "NOW.md",
    text,
    expected_revision: await digest(f.initial),
    request_id: id,
  });

test("Remote read/list/search use current GitHub, exclude symlinks and ignore stale Mac uploads", async () => {
  const f = fixture();
  assert.equal(
    (await projects.readDocument(f.env, "NOW.md", origin)).text,
    f.initial,
  );
  const listed = await projects.listDocuments(f.env);
  assert.deepEqual(
    listed.documents.map((d) => d.path),
    ["NOW.md", "PROJECT.md"],
  );
  assert.match(listed.warnings.join(" "), /symlinks/);
  await projects.syncManifest(f.env, { paths: [] });
  await projects.uploadDocument(f.env, {
    worker_id: "old-mac",
    document: {
      path: "NOW.md",
      title: "Old Mac",
      text: "stale text",
      revision: "a".repeat(64),
      format: "markdown",
      source_modified_at: new Date().toISOString(),
    },
  });
  f.advance("NOW.md", "# New GitHub priority\n\nNorwegian læring.\n");
  assert.match(
    (await projects.readDocument(f.env, "NOW.md", origin)).text,
    /læring/,
  );
  const found = await projects.searchDocuments(
    f.env,
    "Norwegian læring",
    origin,
  );
  assert.equal(found.results[0].path, "NOW.md");
  assert.match(found.results[0].snippet, /læring/);
  assert.deepEqual((await projects.whatsNext(f.env, origin)).missing, [
    "NEXT.md",
  ]);
  assert.deepEqual(await projects.pendingChanges(f.env, "old-mac"), {
    changes: [],
    source: "github",
  });
});
test("A completed save is already committed and immediately readable with the Mac absent", async () => {
  const f = fixture(),
    before = f.state.head;
  const result = await save(f);
  assert.equal(result.status, "completed");
  assert.equal(result.result.commit, f.state.head);
  assert.notEqual(f.state.head, before);
  assert.equal(result.result.pushed, true);
  assert.match(result.result.commit_url, /github.com\/perandre\/phd\/commit/);
  assert.match(
    (await projects.readDocument(f.env, "NOW.md", origin)).text,
    /Next documented action/,
  );
  assert.equal(
    (await projects.readDocument(f.env, "PROJECT.md", origin)).title,
    "Full project title",
  );
  assert.equal((await save(f)).result.commit, result.result.commit);
  assert.equal(f.state.puts, 1);
  await assert.rejects(
    save(f, "synthetic-save-001", "# Different request"),
    /new request_id/,
  );
});
test("Concurrent branch changes and stale revisions never overwrite work", async () => {
  const f = fixture();
  f.state.race = true;
  assert.equal((await save(f)).status, "conflict");
  assert.equal(
    (await projects.readDocument(f.env, "NOW.md", origin)).text,
    f.initial,
  );
  assert.equal(f.state.puts, 0);
  f.advance("NOW.md", "# Newer user text");
  assert.equal((await save(f, "synthetic-save-002")).status, "conflict");
  assert.equal(
    (await projects.readDocument(f.env, "NOW.md", origin)).text,
    "# Newer user text",
  );
});
test("Lost success responses recover the exact commit even after a later edit", async () => {
  const f = fixture();
  f.state.loseResponse = true;
  const result = await save(f);
  assert.equal(result.status, "completed");
  f.advance("NOW.md", "# Later edit remains authoritative");
  assert.equal((await save(f)).result.commit, result.result.commit);
  assert.equal(f.state.puts, 1);
  assert.equal(
    (await projects.readDocument(f.env, "NOW.md", origin)).text,
    "# Later edit remains authoritative",
  );
});
test("Interruption before publication retries the durable prepared commit", async () => {
  const f = fixture();
  f.state.failBeforePublish = true;
  await assert.rejects(save(f), /Retry the identical save/);
  assert.equal(
    (await projects.getChange(f.env, "synthetic-save-001")).status,
    "prepared",
  );
  const result = await save(f);
  assert.equal(result.status, "completed");
  assert.equal(f.state.writes, 1);
  assert.equal(f.state.puts, 1);
});
test("Concurrent identical requests share a commit and status can recover a lost database acknowledgement", async () => {
  const f = fixture();
  const results = await Promise.all([save(f), save(f)]);
  assert.equal(results[0].result.commit, results[1].result.commit);
  f.db.exec("UPDATE project_github_changes SET status='prepared',result='{}'");
  f.advance("OTHER.md", "# An unrelated later edit");
  assert.equal(
    (await projects.getChange(f.env, "synthetic-save-001")).status,
    "completed",
  );
});
test("New notes are created directly and invalid or occupied paths fail closed", async () => {
  const f = fixture();
  const request = {
    path: "admin/meetings/2026-09-11-synthetic.md",
    text: "# Synthetic meeting\n",
    expected_revision: null,
    request_id: "synthetic-note-001",
  };
  assert.equal(
    (await projects.saveDocument(f.env, request)).status,
    "completed",
  );
  for (const path of [
    "../outside.md",
    ".github/config.md",
    "source.pdf",
    "alias.md",
    "alias.md/new.md",
  ])
    await assert.rejects(
      projects.saveDocument(f.env, {
        ...request,
        path,
        request_id: `invalid-${hash(path)}`,
      }),
    );
  assert.equal(
    (
      await projects.saveDocument(f.env, {
        ...request,
        path: "NOW.md",
        request_id: "occupied-path-001",
      })
    ).status,
    "conflict",
  );
});
test("Repository failure is explicit and never falls back to the Mac's text or queue", async () => {
  const f = fixture();
  await projects.readDocument(f.env, "NOW.md", origin);
  f.state.failGet = true;
  await assert.rejects(
    projects.readDocument(f.env, "NOW.md", origin),
    (e) =>
      e instanceof Error &&
      /HTTP 403/.test(e.message) &&
      !e.message.includes("private error"),
  );
  await assert.rejects(save(f), /GitHub returned HTTP 403/);
  assert.equal(
    f.db.prepare("SELECT count(*) AS n FROM project_changes").get()!.n,
    0,
  );
  delete f.env.PROJECT_GITHUB_TOKEN;
  await assert.rejects(save(f), /repository credential/);
});
test("PDF extraction is served only when its source blob equals current GitHub", async () => {
  const f = fixture();
  f.advance("source.pdf", "synthetic-pdf-bytes");
  const entry = f.trees
    .get(f.commits.get(f.state.head)!.tree.sha)!
    .find((e) => e.path === "source.pdf")!;
  const doc = {
    path: "source.pdf",
    title: "Synthetic PDF",
    text: "## Page 1\n\nExtracted words.",
    revision: await digest("synthetic-pdf-bytes"),
    format: "pdf",
    source_modified_at: new Date().toISOString(),
    source_blob_sha: entry.sha,
  };
  await projects.uploadDocument(f.env, { worker_id: "old-mac", document: doc });
  assert.match(
    (await projects.readDocument(f.env, doc.path, origin)).text,
    /Extracted words/,
  );
  f.advance("source.pdf", "new-synthetic-pdf-bytes");
  await assert.rejects(
    projects.readDocument(f.env, doc.path, origin),
    /no matching extracted text/,
  );
  const result = await projects.searchDocuments(f.env, "Extracted", origin);
  assert.equal(result.results.length, 0);
  assert.match(result.warnings.join(" "), /no matching extracted text/);
});
test("MCP exposes direct saves only under documents:write and returns a completed commit", async () => {
  const f = fixture();
  const rpc = async (scopes: string[], method: string, params: object) => {
    const response = await mcp(
      new Request(origin + "/mcp", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          Accept: "application/json, text/event-stream",
        },
        body: JSON.stringify({ jsonrpc: "2.0", id: 1, method, params }),
      }),
      f.env,
      { userId: "owner", scopes },
    );
    return response.json() as Promise<any>;
  };
  const read = await rpc(["library:read", "jobs:write"], "tools/list", {});
  assert.ok(
    !read.result.tools.some(
      (tool: { name: string }) => tool.name === "save_document",
    ),
  );
  const write = await rpc(
    ["library:read", "documents:write"],
    "tools/list",
    {},
  );
  assert.match(
    write.result.tools.find(
      (tool: { name: string }) => tool.name === "save_document",
    ).description,
    /directly to GitHub/,
  );
  const result = await rpc(["library:read", "documents:write"], "tools/call", {
    name: "save_meeting_note",
    arguments: {
      title: "Synthetic supervision",
      date: "2026-09-11",
      body_markdown: "A documented action.",
      request_id: "meeting-test-001",
    },
  });
  assert.equal(result.result.structuredContent.status, "completed");
  assert.equal(result.result.structuredContent.result.commit, f.state.head);
});
