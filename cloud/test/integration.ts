import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createHash, randomBytes } from "node:crypto";

const base = process.env.TEST_BASE_URL ?? "http://127.0.0.1:8797";
if (
  !["127.0.0.1", "localhost"].includes(new URL(base).hostname) &&
  !process.env.ALLOW_REMOTE_INTEGRATION
)
  throw new Error(
    "Integration tests create fixtures. Set ALLOW_REMOTE_INTEGRATION explicitly for a remote test.",
  );
const secrets = Object.fromEntries(
  readFileSync(process.env.TEST_SECRETS_FILE ?? ".dev.vars", "utf8")
    .split("\n")
    .filter(Boolean)
    .map((line) => {
      const i = line.indexOf("=");
      return [line.slice(0, i), line.slice(i + 1)];
    }),
);
const bridge = {
  Authorization: `Bearer ${secrets.BRIDGE_TOKEN}`,
  "Content-Type": "application/json",
};
const send = (path: string, init: RequestInit = {}) =>
  fetch(base + path, { redirect: "manual", ...init });
async function parsed(response: Response) {
  const value = await response.json();
  assert.ok(response.ok, `HTTP ${response.status}: ${JSON.stringify(value)}`);
  return value as Record<string, any>;
}
let r = await send("/api/library");
assert.equal(r.status, 401);
assert.equal(r.headers.get("Referrer-Policy"), "no-referrer");
r = await send("/mcp");
assert.equal(r.status, 401);
assert.match(r.headers.get("WWW-Authenticate") ?? "", /resource_metadata/);
const meta = await parsed(
  await send("/.well-known/oauth-protected-resource/mcp"),
);
assert.equal(meta.resource, `${base}/mcp`);
const authMeta = await parsed(
  await send("/.well-known/oauth-authorization-server"),
);
assert.ok(authMeta.code_challenge_methods_supported.includes("S256"));
r = await send("/login");
assert.equal(r.status, 200);
// Check the final response after the Worker wrapper: no-referrer causes real
// browser form submissions to send Origin: null, unlike the fetches below.
assert.equal(r.headers.get("Referrer-Policy"), "same-origin");
assert.match(
  r.headers.get("Content-Security-Policy") ?? "",
  /form-action 'self';/,
);
for (const origin of [undefined, "null", "https://evil.example"]) {
  const headers = new Headers({
    "Content-Type": "application/x-www-form-urlencoded",
  });
  if (origin !== undefined) headers.set("Origin", origin);
  r = await send("/login", {
    method: "POST",
    headers,
    body: new URLSearchParams({ key: secrets.OWNER_ACCESS_KEY }),
  });
  assert.equal(r.status, 403);
  assert.equal(((await r.json()) as any).error.code, "origin_mismatch");
  assert.equal(r.headers.get("Set-Cookie"), null);
}
r = await send("/login", {
  method: "POST",
  headers: {
    Origin: base,
    "Content-Type": "application/x-www-form-urlencoded",
  },
  body: new URLSearchParams({ key: secrets.OWNER_ACCESS_KEY }),
});
assert.equal(r.status, 303);
const cookie = r.headers.get("Set-Cookie")!.split(";")[0];
assert.ok(cookie);
const owner = {
  Cookie: cookie,
  Origin: base,
  "Content-Type": "application/json",
};
r = await send("/api/settings", {
  method: "PATCH",
  headers: owner,
  body: JSON.stringify({
    qa_enabled: true,
    opening_sound: "typing",
    closing_sound: "none",
  }),
});
assert.equal(r.status, 200, await r.text());
r = await send("/api/settings", {
  method: "PATCH", headers: owner, body: JSON.stringify({ auto_generate: "both" }),
});
assert.equal((await parsed(r)).auto_generate, "both");
r = await send("/api/settings", {
  method: "PATCH", headers: owner, body: JSON.stringify({ qa_enabled: false }),
});
assert.equal((await parsed(r)).auto_generate, "both", "Unrelated settings edits must preserve automation");
r = await send("/api/bridge/settings", {
  method: "PATCH", headers: bridge, body: JSON.stringify({ auto_generate: "markdown", qa_enabled: true }),
});
assert.equal((await parsed(r)).auto_generate, "markdown");
r = await send("/api/settings", {
  method: "PATCH",
  headers: {
    Cookie: cookie,
    Origin: "https://evil.example",
    "Content-Type": "application/json",
  },
  body: "{}",
});
assert.equal(r.status, 403);
const id = `test-${randomBytes(5).toString("hex")}`;
const article = {
  id,
  title: "How Example Company improved AI adoption — Research fixture",
  authors: ["Test Author"],
  year: 2025,
  license_status: "private",
  markdown_status: "ready",
  audio_status: "pending",
  qa_status: "warnings",
  warnings: ["Citation formatting needs review."],
  audio_url: "https://audio.example/brief.m4a",
  editions: { brief: { audio_url: "https://audio.example/brief.m4a" } },
};
const markdown =
  "# How Example Company improved AI adoption\n\nExample Company improved AI adoption through weekly staff training.\n\n## References\n\n[1] Source citation.";
const ingestion = await parsed(
  await send(`/api/bridge/articles/${id}`, {
    method: "PUT",
    headers: bridge,
    body: JSON.stringify({
      article,
      markdown,
      review: "# Quality report\n\nWarning: inspect citation formatting.\n",
    }),
  }),
);
assert.equal(ingestion.article.title, article.title);
r = await send(`/api/articles/${id}/markdown`);
assert.equal(r.status, 401);
r = await send(`/api/articles/${id}/markdown`, { headers: { Cookie: cookie } });
assert.equal(await r.text(), markdown);
r = await send(`/api/articles/${id}/audio?edition=full`, { headers: owner });
assert.equal(
  r.status,
  409,
  "A missing Full must never redirect to Brief audio",
);
r = await send(`/api/articles/${id}/audio?edition=brief`, { headers: owner });
assert.equal(r.status, 302);
assert.equal(r.headers.get("Location"), article.editions.brief.audio_url);
const found = await parsed(
  await send("/api/search?q=weekly%20staff", { headers: owner }),
);
assert.ok(found.results.some((row: any) => row.id === id));
const key = `request-${id}`;
const jobBody = JSON.stringify({
  action: "markdown",
  scope: "one",
  article_id: id,
  qa: true,
});
const created = await parsed(
  await send("/api/jobs", {
    method: "POST",
    headers: { ...owner, "Idempotency-Key": key },
    body: jobBody,
  }),
);
assert.equal(created.job.title, article.title);
const repeated = await parsed(
  await send("/api/jobs", {
    method: "POST",
    headers: { ...owner, "Idempotency-Key": key },
    body: jobBody,
  }),
);
assert.equal(created.job.id, repeated.job.id);
const claims = await Promise.all(
  [1, 2].map((worker) =>
    send("/api/bridge/jobs/claim", {
      method: "POST",
      headers: bridge,
      body: JSON.stringify({ worker_id: `test-${worker}` }),
    }).then(parsed),
  ),
);
const claimed = claims.find((value) => value.job?.id === created.job.id);
assert.ok(claimed, "Expected test job claim");
assert.equal(
  claims.filter((value) => value.job?.id === created.job.id).length,
  1,
);
const repeatedClaim = await parsed(
  await send("/api/bridge/jobs/claim", {
    method: "POST",
    headers: bridge,
    body: JSON.stringify({ worker_id: claimed.job.worker_id }),
  }),
);
assert.equal(repeatedClaim.job.id, claimed.job.id);
assert.equal(repeatedClaim.job.lease_token, claimed.job.lease_token);
r = await send(`/api/bridge/jobs/${created.job.id}`, {
  method: "PATCH",
  headers: bridge,
  body: JSON.stringify({
    worker_id: claimed.job.worker_id,
    lease_token: "wrong-lease",
    status: "completed",
  }),
});
assert.equal(r.status, 409);
await parsed(
  await send(`/api/jobs/${created.job.id}/cancel`, {
    method: "POST",
    headers: owner,
    body: "{}",
  }),
);
const cancelled = await parsed(
  await send(`/api/bridge/jobs/${created.job.id}`, {
    method: "PATCH",
    headers: bridge,
    body: JSON.stringify({
      worker_id: claimed.job.worker_id,
      lease_token: claimed.job.lease_token,
      status: "running",
      stage: "markdown",
    }),
  }),
);
assert.equal(cancelled.job.status, "cancel_requested");
await parsed(
  await send(`/api/bridge/jobs/${created.job.id}`, {
    method: "PATCH",
    headers: bridge,
    body: JSON.stringify({
      worker_id: claimed.job.worker_id,
      lease_token: claimed.job.lease_token,
      status: "cancelled",
    }),
  }),
);
async function oauth(scopes: string) {
  const client = await parsed(
    await send("/oauth/register", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        client_name: "Local integration test",
        redirect_uris: ["http://127.0.0.1:3099/callback"],
        grant_types: ["authorization_code", "refresh_token"],
        response_types: ["code"],
        token_endpoint_auth_method: "none",
      }),
    }),
  );
  const verifier = randomBytes(32).toString("base64url"),
    challenge = createHash("sha256").update(verifier).digest("base64url");
  const params = new URLSearchParams({
    client_id: client.client_id,
    redirect_uri: "http://127.0.0.1:3099/callback",
    response_type: "code",
    scope: scopes,
    state: "integration-state",
    code_challenge: challenge,
    code_challenge_method: "S256",
    resource: base + "/mcp",
  });
  const path = "/authorize?" + params;
  const consent = await send(path, { headers: { Cookie: cookie } });
  assert.equal(consent.status, 200, await consent.clone().text());
  assert.equal(consent.headers.get("Referrer-Policy"), "same-origin");
  assert.match(
    consent.headers.get("Content-Security-Policy") ?? "",
    /form-action 'self' http:\/\/127\.0\.0\.1:3099;/,
  );
  const invalidParams = new URLSearchParams(params);
  invalidParams.set("redirect_uri", "https://evil.example/callback");
  const invalidRedirect = await send("/authorize?" + invalidParams, {
    headers: { Cookie: cookie },
  });
  assert.equal(invalidRedirect.status, 400);
  assert.match(
    invalidRedirect.headers.get("Content-Security-Policy") ?? "",
    /form-action 'self';/,
  );
  const html = await consent.text(),
    csrf = html.match(/name="csrf" value="([^"]+)"/)?.[1];
  assert.ok(csrf, "Missing CSRF token");
  for (const origin of [undefined, "null", "https://evil.example"]) {
    const headers = new Headers({
      Cookie: cookie,
      "Content-Type": "application/x-www-form-urlencoded",
    });
    if (origin !== undefined) headers.set("Origin", origin);
    const rejected = await send(path, {
      method: "POST",
      headers,
      body: new URLSearchParams({ csrf, decision: "allow" }),
    });
    assert.equal(rejected.status, 403);
    assert.equal(
      ((await rejected.json()) as any).error.code,
      "origin_mismatch",
    );
    assert.equal(rejected.headers.get("Location"), null);
  }
  const badCsrf = await send(path, {
    method: "POST",
    headers: {
      Cookie: cookie,
      Origin: base,
      "Content-Type": "application/x-www-form-urlencoded",
    },
    body: new URLSearchParams({ csrf: "wrong-token", decision: "allow" }),
  });
  assert.equal(badCsrf.status, 403);
  assert.equal(((await badCsrf.json()) as any).error.code, "csrf_failed");
  assert.equal(badCsrf.headers.get("Location"), null);
  const approval = await send(path, {
    method: "POST",
    headers: {
      Cookie: cookie,
      Origin: base,
      "Content-Type": "application/x-www-form-urlencoded",
    },
    body: new URLSearchParams({ csrf, decision: "allow" }),
  });
  assert.equal(approval.status, 303, await approval.text());
  const redirect = new URL(approval.headers.get("Location")!);
  assert.equal(redirect.searchParams.get("state"), "integration-state");
  const token = await parsed(
    await send("/oauth/token", {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body: new URLSearchParams({
        grant_type: "authorization_code",
        code: redirect.searchParams.get("code")!,
        client_id: client.client_id,
        redirect_uri: "http://127.0.0.1:3099/callback",
        code_verifier: verifier,
        resource: base + "/mcp",
      }),
    }),
  );
  return {
    access_token: String(token.access_token),
    refresh_token: String(token.refresh_token),
    client_id: String(client.client_id),
  };
}
async function rpc(
  token: string,
  method: string,
  params: Record<string, unknown> = {},
) {
  return parsed(
    await send("/mcp", {
      method: "POST",
      headers: {
        Authorization: `Bearer ${token}`,
        "Content-Type": "application/json",
        Accept: "application/json, text/event-stream",
        "MCP-Protocol-Version": "2025-11-25",
      },
      body: JSON.stringify({ jsonrpc: "2.0", id: 1, method, params }),
    }),
  );
}
const readGrant = await oauth("library:read"),
  readToken = readGrant.access_token;
const initialized = await rpc(readToken, "initialize", {
  protocolVersion: "2025-11-25",
  capabilities: {},
  clientInfo: { name: "Integration test", version: "1.0" },
});
assert.equal(initialized.result.serverInfo.name, "one-more-paper");
const readTools = await rpc(readToken, "tools/list");
assert.ok(readTools.result.tools.some((tool: any) => tool.name === "fetch"));
assert.ok(
  !readTools.result.tools.some((tool: any) => tool.name === "create_job"),
);
const fetched = await rpc(readToken, "tools/call", {
  name: "fetch",
  arguments: { id },
});
assert.equal(fetched.result.structuredContent.text, markdown);
const writeDenied = await rpc(readToken, "tools/call", {
  name: "create_job",
  arguments: { action: "markdown", scope: "new" },
});
assert.ok(writeDenied.error || writeDenied.result?.isError);
const writeGrant = await oauth("library:read jobs:write"),
  writeToken = writeGrant.access_token;
const writeTools = await rpc(writeToken, "tools/list");
assert.ok(
  writeTools.result.tools.some((tool: any) => tool.name === "create_job"),
);
r = await send("/api/bridge/jobs/claim", {
  method: "POST",
  headers: {
    Authorization: `Bearer ${writeToken}`,
    "Content-Type": "application/json",
  },
  body: '{"worker_id":"forbidden"}',
});
assert.equal(r.status, 401);
const narrowed = await parsed(
  await send("/oauth/token", {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams({
      grant_type: "refresh_token",
      refresh_token: writeGrant.refresh_token,
      client_id: writeGrant.client_id,
      scope: "library:read",
      resource: base + "/mcp",
    }),
  }),
);
const narrowedTools = await rpc(narrowed.access_token, "tools/list");
assert.ok(
  !narrowedTools.result.tools.some((tool: any) => tool.name === "create_job"),
);
// Project documents use the same private MCP, with separate document-write consent.
const projectWorker = "integration-project-worker";
const projectPath = "NOW.md";
const projectText =
  "# Synthetic PhD priorities\n\nReview the synthetic interview guide.\n";
const projectRevision = createHash("sha256").update(projectText).digest("hex");
await parsed(
  await send("/api/bridge/project/manifest", {
    method: "POST",
    headers: bridge,
    body: JSON.stringify({
      worker_id: projectWorker,
      title: "Synthetic PhD project",
      paths: [projectPath],
      warnings: ["diagram.png: inspect the original image."],
    }),
  }),
);
await parsed(
  await send("/api/bridge/project/document", {
    method: "PUT",
    headers: bridge,
    body: JSON.stringify({
      worker_id: projectWorker,
      document: {
        path: projectPath,
        title: "Synthetic PhD priorities",
        text: projectText,
        revision: projectRevision,
        format: "markdown",
        source_modified_at: new Date().toISOString(),
      },
    }),
  }),
);
r = await send("/api/project/document?path=NOW.md");
assert.equal(r.status, 401);
r = await send("/api/project/document?path=NOW.md", { headers: owner });
assert.equal(await r.text(), projectText);
async function callProject(
  token: string,
  name: string,
  args: Record<string, unknown> = {},
) {
  return rpc(token, "tools/call", { name, arguments: args });
}
const priorities = await callProject(readToken, "whats_next");
assert.equal(
  priorities.result.structuredContent.documents[0].text,
  projectText,
);
assert.deepEqual(priorities.result.structuredContent.missing, ["NEXT.md"]);
const projectList = await callProject(readToken, "list_documents");
assert.equal(projectList.result.structuredContent.documents[0].path, "NOW.md");
assert.ok(projectList.result.structuredContent.warnings.length);
const projectRead = await callProject(readToken, "read_document", {
  path: projectPath,
});
assert.equal(projectRead.result.structuredContent.revision, projectRevision);
const projectSearch = await callProject(readToken, "search", {
  query: "synthetic interview",
});
assert.ok(
  projectSearch.result.structuredContent.results.some(
    (row: any) => row.id === "project:phd:NOW.md",
  ),
);
const projectFetch = await callProject(readToken, "fetch", {
  id: "project:phd:NOW.md",
});
assert.equal(projectFetch.result.structuredContent.text, projectText);
const traversal = await callProject(readToken, "read_document", {
  path: "../private.md",
});
assert.ok(traversal.error || traversal.result?.isError);
const docChange = {
  path: projectPath,
  text: "# Synthetic PhD priorities\n\nUpdated synthetic plan.\n",
  expected_revision: projectRevision,
  request_id: `project-${id}`,
};
const jobGrantDenied = await callProject(
  writeToken,
  "save_document",
  docChange,
);
assert.ok(jobGrantDenied.error || jobGrantDenied.result?.isError);
const docGrant = await oauth("library:read documents:write");
const docTools = await rpc(docGrant.access_token, "tools/list");
assert.ok(
  docTools.result.tools.some((tool: any) => tool.name === "save_document"),
);
assert.ok(
  !docTools.result.tools.some((tool: any) => tool.name === "create_job"),
);
const saved = await callProject(
  docGrant.access_token,
  "save_document",
  docChange,
);
assert.equal(saved.result.structuredContent.status, "queued");
const savedAgain = await callProject(
  docGrant.access_token,
  "save_document",
  docChange,
);
assert.equal(
  savedAgain.result.structuredContent.request_id,
  docChange.request_id,
);
const collision = await callProject(docGrant.access_token, "save_document", {
  ...docChange,
  text: "Different text",
});
assert.equal(collision.result.isError, true);
const stale = await callProject(docGrant.access_token, "save_document", {
  ...docChange,
  expected_revision: "a".repeat(64),
  request_id: `stale-${id}`,
});
assert.equal(stale.result.isError, true);
const pending = await parsed(
  await send(`/api/bridge/project/changes?worker_id=${projectWorker}`, {
    headers: bridge,
  }),
);
assert.equal(pending.changes[0].id, docChange.request_id);
r = await send("/api/bridge/project/changes?worker_id=wrong", {
  headers: bridge,
});
assert.equal(r.status, 409);
await parsed(
  await send(`/api/bridge/project/changes/${docChange.request_id}`, {
    method: "PATCH",
    headers: bridge,
    body: JSON.stringify({
      worker_id: projectWorker,
      status: "completed",
      result: { message: "Synthetic save acknowledged", pushed: true },
    }),
  }),
);
const receipt = await callProject(
  docGrant.access_token,
  "save_document",
  docChange,
);
assert.equal(receipt.result.structuredContent.status, "completed");
const statusReceipt = await callProject(readToken, "document_change_status", {
  request_id: docChange.request_id,
});
assert.equal(statusReceipt.result.structuredContent.result.pushed, true);
await parsed(
  await send("/api/bridge/project/manifest", {
    method: "POST",
    headers: bridge,
    body: JSON.stringify({
      worker_id: projectWorker,
      title: "Synthetic PhD project",
      paths: [],
      warnings: [],
    }),
  }),
);
const removed = await callProject(readToken, "read_document", {
  path: projectPath,
});
assert.equal(removed.result.isError, true);
const removedSearch = await callProject(readToken, "search_documents", {
  query: "synthetic interview",
});
assert.equal(removedSearch.result.structuredContent.results.length, 0);
console.log(
  "PASS: private PhD browse/read/search/fetch, NOW/NEXT, document consent, traversal rejection, optimistic revisions, idempotent saves/receipts, writer isolation and removed-file reconciliation.",
);

const state = await parsed(await send("/api/status", { headers: owner }));
assert.equal(state.capabilities.local_files, false);
assert.equal(typeof state.counts.catalog_revision, "string");
await parsed(await send(`/api/bridge/articles/${id}`, {
  method: "PUT", headers: bridge,
  body: JSON.stringify({ article: { ...article, title: article.title + " — Updated" } }),
}));
const revisedState = await parsed(await send("/api/status", { headers: owner }));
assert.equal(revisedState.counts.total, state.counts.total);
assert.notEqual(revisedState.counts.catalog_revision, state.counts.catalog_revision);
console.log(
  "PASS: authenticated Markdown/R2 + D1 search, owner CSRF, idempotent jobs, atomic leases, cancellation, OAuth discovery/S256 consent/code/token, MCP initialize/search/fetch and read/write/bridge scope isolation.",
);
