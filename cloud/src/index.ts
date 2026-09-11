import { OAuthProvider } from "@cloudflare/workers-oauth-provider";
import { z } from "zod";
import { authorize, getSession, login, logout } from "./auth";
import {
  body,
  HttpError,
  idFromPath,
  json,
  sameOrigin,
  verifySecret,
} from "./http";
import {
  articleJson,
  documentResponse,
  getArticle,
  ingest,
  listArticles,
  search,
} from "./library";
import {
  changeJob,
  claimJob,
  createJob,
  getJob,
  heartbeat,
  jobJson,
  listJobs,
  status,
  updateJob,
} from "./jobs";
import { getSettings, patchSettings } from "./settings";
import { mcp } from "./mcp";
import {
  syncManifest,
  uploadDocument,
  pendingChanges,
  finishChange,
  readDocument,
} from "./projects";
import type { AppEnv, Permissions } from "./types";

async function bridge(request: Request, env: AppEnv) {
  const url = new URL(request.url),
    path = url.pathname;
  if (
    !(await verifySecret(
      request.headers.get("Authorization")?.replace(/^Bearer /, "") ?? "",
      env.BRIDGE_TOKEN,
    ))
  )
    throw new HttpError(
      401,
      "bridge_unauthorized",
      "The Mac bridge credential was not accepted.",
    );
  if (path === "/api/bridge/project/manifest" && request.method === "POST")
    return json(await syncManifest(env, await body(request, 400000)));
  if (path === "/api/bridge/project/document" && request.method === "PUT")
    return json(await uploadDocument(env, await body(request)));
  if (path === "/api/bridge/project/changes" && request.method === "GET")
    return json(
      await pendingChanges(env, url.searchParams.get("worker_id") ?? ""),
    );
  const documentChange = path.match(
    /^\/api\/bridge\/project\/changes\/([^/]+)$/,
  );
  if (documentChange && request.method === "PATCH")
    return json(
      await finishChange(
        env,
        idFromPath(documentChange[1]),
        await body(request, 8000),
      ),
    );
  if (path === "/api/bridge/heartbeat" && request.method === "POST")
    return json(await heartbeat(env, await body(request, 16000)));
  if (path === "/api/bridge/jobs/claim" && request.method === "POST")
    return json(await claimJob(env, await body(request, 16000)));
  if (path === "/api/bridge/settings" && request.method === "GET")
    return json(await getSettings(env));
  if (path === "/api/bridge/settings" && request.method === "PATCH")
    return json(await patchSettings(env, await body(request, 16000)));
  const article = path.match(/^\/api\/bridge\/articles\/([^/]+)$/);
  if (article && request.method === "PUT")
    return json({
      article: await ingest(
        env,
        idFromPath(article[1]),
        await body(request),
        url.origin,
      ),
    });
  const job = path.match(/^\/api\/bridge\/jobs\/([^/]+)$/);
  if (job && request.method === "PATCH")
    return json(
      await updateJob(env, idFromPath(job[1]), await body(request, 16000)),
    );
  throw new HttpError(404, "not_found", "Bridge endpoint not found.");
}
async function api(request: Request, env: AppEnv) {
  const url = new URL(request.url),
    path = url.pathname;
  if (!["GET", "HEAD"].includes(request.method)) sameOrigin(request);
  if (path === "/api/project/document" && request.method === "GET") {
    const document = await readDocument(
      env,
      url.searchParams.get("path") ?? "",
      url.origin,
    );
    return new Response(document.text, {
      headers: {
        "Content-Type": "text/plain; charset=utf-8",
        "Cache-Control": "no-store",
      },
    });
  }
  if (path === "/api/library" && request.method === "GET")
    return json(await listArticles(env, url));
  if (path === "/api/search" && request.method === "GET")
    return json(await search(env, url.searchParams.get("q") ?? "", url.origin));
  if (path === "/api/status" && request.method === "GET")
    return json(await status(env));
  if (path === "/api/settings" && request.method === "GET")
    return json(await getSettings(env));
  if (path === "/api/settings" && request.method === "PATCH")
    return json(await patchSettings(env, await body(request, 16000)));
  if (path === "/api/jobs" && request.method === "GET")
    return json(await listJobs(env));
  if (path === "/api/jobs" && request.method === "POST")
    return json(
      {
        job: await createJob(
          env,
          await body(request, 16000),
          request.headers.get("Idempotency-Key"),
        ),
      },
      202,
    );
  const job = path.match(/^\/api\/jobs\/([^/]+)(?:\/(cancel|retry))?$/);
  if (job) {
    const id = idFromPath(job[1]);
    if (job[2] && request.method === "POST")
      return json({
        job: await changeJob(env, id, job[2] as "cancel" | "retry"),
      });
    if (!job[2] && request.method === "GET")
      return json({ job: jobJson(await getJob(env, id)) });
  }
  const article = path.match(
    /^\/api\/articles\/([^/]+)(?:\/(markdown|review|audio))?$/,
  );
  if (article && request.method === "GET") {
    const id = idFromPath(article[1]);
    if (article[2] === "markdown" || article[2] === "review")
      return documentResponse(env, id, article[2]);
    const row = await getArticle(env, id);
    if (article[2] === "audio") {
      const extra = JSON.parse(row.metadata),
        edition = url.searchParams.get("edition") ?? "full";
      if (!["full", "brief"].includes(edition))
        throw new HttpError(
          400,
          "invalid_edition",
          "Choose Full or Brief audio.",
        );
      const audio = extra.editions?.[edition]?.audio_url;
      if (typeof audio === "string" && audio.startsWith("https://"))
        return Response.redirect(audio, 302);
      throw new HttpError(
        409,
        "audio_on_mac",
        `${row.title}: audio is available on your Mac or in your selected iCloud folder.`,
      );
    }
    return json(articleJson(row, url.origin));
  }
  throw new HttpError(404, "not_found", "API endpoint not found.");
}
const defaultHandler: ExportedHandler<AppEnv> = {
  async fetch(request, env) {
    const url = new URL(request.url),
      path = url.pathname;
    if (path === "/login") return login(request, env);
    if (path === "/logout" && request.method === "POST") return logout(request);
    if (path === "/authorize") return authorize(request, env);
    if (path.startsWith("/api/bridge/")) return bridge(request, env);
    const session = await getSession(request, env);
    if (!session) {
      if (
        path.startsWith("/api/") &&
        !request.headers.get("Accept")?.includes("text/html")
      )
        return json(
          {
            error: {
              code: "unauthorized",
              message: "Sign in to your research library.",
            },
            login_url: "/login",
          },
          401,
        );
      return Response.redirect(
        `${url.origin}/login?return=${encodeURIComponent(path + url.search)}`,
        303,
      );
    }
    if (path.startsWith("/api/")) return api(request, env);
    if (request.method === "GET" && /^\/articles\/[^/]+\/?$/.test(path)) {
      return env.ASSETS.fetch(new Request(new URL("/", request.url), request));
    }
    return env.ASSETS.fetch(request);
  },
};
export default {
  async fetch(
    request: Request,
    env: AppEnv,
    ctx: ExecutionContext,
  ): Promise<Response> {
    const requestId = crypto.randomUUID(),
      url = new URL(request.url);
    try {
      const provider = new OAuthProvider<AppEnv>({
        apiRoute: "/mcp",
        apiHandler: {
          fetch: (req, bindings, context) =>
            mcp(req, bindings, context.props as Permissions),
        },
        defaultHandler,
        authorizeEndpoint: "/authorize",
        tokenEndpoint: "/oauth/token",
        clientRegistrationEndpoint: "/oauth/register",
        scopesSupported: [
          "library:read",
          "jobs:write",
          "documents:write",
          "zotero:write",
        ],
        resourceMetadata: {
          resource: `${url.origin}/mcp`,
          authorization_servers:
            url.protocol === "https:" ? [url.origin] : undefined,
          scopes_supported: [
            "library:read",
            "jobs:write",
            "documents:write",
            "zotero:write",
          ],
          resource_name: "One More Paper research library",
        },
        clientIdMetadataDocumentEnabled: true,
        allowImplicitFlow: false,
        allowPlainPKCE: false,
        accessTokenTTL: 3600,
        refreshTokenTTL: 30 * 86400,
        tokenExchangeCallback: (options) => ({
          accessTokenProps: { userId: "owner", scopes: options.requestedScope },
        }),
      });
      const response = await provider.fetch(request, env, ctx);
      const secured = new Response(response.body, response);
      secured.headers.set("X-Content-Type-Options", "nosniff");
      if (!secured.headers.has("Referrer-Policy"))
        secured.headers.set("Referrer-Policy", "no-referrer");
      secured.headers.set("X-Request-ID", requestId);
      if (
        !secured.headers.has("Content-Security-Policy") &&
        secured.headers.get("Content-Type")?.includes("text/html")
      )
        secured.headers.set(
          "Content-Security-Policy",
          "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; media-src 'self' https:; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'",
        );
      if (!secured.headers.has("Cache-Control"))
        secured.headers.set("Cache-Control", "private, no-store");
      if (url.protocol === "https:")
        secured.headers.set("Strict-Transport-Security", "max-age=31536000");
      return secured;
    } catch (error) {
      const status =
        error instanceof HttpError
          ? error.status
          : error instanceof z.ZodError
            ? 400
            : 500;
      const code =
        error instanceof HttpError
          ? error.code
          : error instanceof z.ZodError
            ? "invalid_input"
            : "internal_error";
      const message =
        error instanceof HttpError
          ? error.message
          : error instanceof z.ZodError
            ? error.issues
                .map((issue) => `${issue.path.join(".")}: ${issue.message}`)
                .join("; ")
            : "The operation failed. Use the request ID to inspect server logs.";
      // Do not log document contents, URLs with private query strings, passwords or tokens.
      console.error(
        JSON.stringify({
          event: "request_failed",
          request_id: requestId,
          status,
          code,
          path: url.pathname,
          error_type: error instanceof Error ? error.name : "unknown",
        }),
      );
      return json({ error: { code, message, request_id: requestId } }, status);
    }
  },
} satisfies ExportedHandler<AppEnv>;
