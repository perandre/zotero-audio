import {
  AuthorizationError,
  type AuthRequest,
} from "@cloudflare/workers-oauth-provider";
import {
  boundedText,
  escaped,
  HttpError,
  sameOrigin,
  verifySecret,
} from "./http";
import type { AppEnv } from "./types";

const COOKIE = "omp_session";
type Session = { exp: number; csrf: string };
function base64(value: Uint8Array) {
  return btoa(String.fromCharCode(...value))
    .replace(/=/g, "")
    .replace(/\+/g, "-")
    .replace(/\//g, "_");
}
function unbase64(value: string) {
  return Uint8Array.from(
    atob(value.replace(/-/g, "+").replace(/_/g, "/")),
    (c) => c.charCodeAt(0),
  );
}
async function sessionKey(env: AppEnv) {
  if (!env.SESSION_SECRET || env.SESSION_SECRET.length < 32)
    throw new HttpError(
      503,
      "setup_required",
      "The server session secret has not been configured.",
    );
  return crypto.subtle.importKey(
    "raw",
    new TextEncoder().encode(env.SESSION_SECRET),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign", "verify"],
  );
}
export async function getSession(
  request: Request,
  env: AppEnv,
): Promise<Session | null> {
  const value = request.headers
    .get("Cookie")
    ?.split(";")
    .map((x) => x.trim())
    .find((x) => x.startsWith(`${COOKIE}=`))
    ?.slice(COOKIE.length + 1);
  if (!value) return null;
  try {
    const [payload, signature] = value.split(".");
    if (!payload || !signature || value.length > 1000) return null;
    if (
      !(await crypto.subtle.verify(
        "HMAC",
        await sessionKey(env),
        unbase64(signature),
        new TextEncoder().encode(payload),
      ))
    )
      return null;
    const parsed = JSON.parse(
      new TextDecoder().decode(unbase64(payload)),
    ) as Session;
    return parsed.exp > Date.now() && typeof parsed.csrf === "string"
      ? parsed
      : null;
  } catch {
    return null;
  }
}
async function cookie(env: AppEnv, request: Request) {
  const payload = base64(
    new TextEncoder().encode(
      JSON.stringify({
        exp: Date.now() + 7 * 86400000,
        csrf: crypto.randomUUID(),
      }),
    ),
  );
  const signature = base64(
    new Uint8Array(
      await crypto.subtle.sign(
        "HMAC",
        await sessionKey(env),
        new TextEncoder().encode(payload),
      ),
    ),
  );
  return `${COOKIE}=${payload}.${signature}; Path=/; HttpOnly; SameSite=Lax; Max-Age=604800${new URL(request.url).protocol === "https:" ? "; Secure" : ""}`;
}
function page(
  title: string,
  content: string,
  status = 200,
  validatedRedirectUri?: string,
) {
  // Browsers also apply form-action to a POST's redirect chain. Consent must
  // allow the callback already validated by the OAuth provider. URL parsing
  // keeps client-controlled query strings out of the policy.
  const redirect = validatedRedirectUri ? new URL(validatedRedirectUri) : null;
  const formAction = redirect
    ? `'self' ${redirect.origin === "null" ? redirect.protocol : redirect.origin}`
    : "'self'";
  return new Response(
    `<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>${escaped(title)} · One More Paper</title><style>body{font:17px/1.6 system-ui,sans-serif;margin:0;background:#f6f4ef;color:#252925}main{max-width:540px;margin:12vh auto;padding:30px}h1{line-height:1.2}label{display:block;margin:20px 0 8px}input[type=password]{box-sizing:border-box;width:100%;padding:12px;border:1px solid #8b958b;border-radius:8px;font:inherit}button{font:inherit;background:#234b40;color:white;border:0;border-radius:8px;padding:12px 20px;margin-top:22px;cursor:pointer}a{color:#234b40}small{display:block;color:#555}li{margin:10px 0}.error{color:#8c2820}</style></head><body><main><p>One More Paper</p><h1>${escaped(title)}</h1>${content}</main></body></html>`,
    {
      status,
      headers: {
        "Content-Type": "text/html; charset=utf-8",
        "Cache-Control": "no-store",
        "Content-Security-Policy": `default-src 'none'; style-src 'unsafe-inline'; form-action ${formAction}; base-uri 'none'; frame-ancestors 'none'`,
        // no-referrer makes browser form POSTs send Origin: null, which our
        // same-origin check rejects. Keep the origin for local forms only.
        "Referrer-Policy": "same-origin",
      },
    },
  );
}
function returnPath(url: URL) {
  const path = url.searchParams.get("return") ?? "/";
  return path.startsWith("/") && !path.startsWith("//") && !path.includes("\\")
    ? path
    : "/";
}
export async function login(request: Request, env: AppEnv) {
  const url = new URL(request.url),
    target = returnPath(url);
  if (request.method === "GET") {
    if (await getSession(request, env))
      return Response.redirect(url.origin + target, 303);
    return page(
      "Your research library",
      `<p>Sign in with your owner access key to read papers, listen and manage your Mac’s jobs.</p><form method="post" action="/login?return=${encodeURIComponent(target)}"><label for="key">Owner access key</label><input id="key" name="key" type="password" autocomplete="current-password" required autofocus><button>Sign in</button></form><small>The key is saved in your local setup credentials.</small>`,
    );
  }
  if (request.method !== "POST")
    throw new HttpError(
      405,
      "method_not_allowed",
      "Use GET or POST for sign in.",
    );
  sameOrigin(request);
  const form = new URLSearchParams(await boundedText(request, 8000));
  if (!(await verifySecret(form.get("key") ?? "", env.OWNER_ACCESS_KEY)))
    return page(
      "Sign in failed",
      '<p class="error">The owner access key was not accepted.</p><p><a href="/login">Try again</a></p>',
      401,
    );
  return new Response(null, {
    status: 303,
    headers: {
      Location: url.origin + target,
      "Set-Cookie": await cookie(env, request),
      "Cache-Control": "no-store",
    },
  });
}
export function logout(request: Request) {
  sameOrigin(request);
  return new Response(null, {
    status: 303,
    headers: {
      Location: "/login",
      "Set-Cookie": `${COOKIE}=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0`,
      "Cache-Control": "no-store",
    },
  });
}
export async function authorize(request: Request, env: AppEnv) {
  const url = new URL(request.url);
  let auth: AuthRequest;
  try {
    auth = await env.OAUTH_PROVIDER.parseAuthRequest(
      new Request(url.toString()),
    );
  } catch (error) {
    if (error instanceof AuthorizationError)
      return page(
        "Connection could not be authorized",
        `<p>${escaped(error.description)}</p>`,
        400,
      );
    throw error;
  }
  const session = await getSession(request, env);
  if (!session)
    return Response.redirect(
      `${url.origin}/login?return=${encodeURIComponent(url.pathname + url.search)}`,
      303,
    );
  const client = await env.OAUTH_PROVIDER.lookupClient(auth.clientId);
  if (!client)
    throw new HttpError(
      400,
      "unknown_client",
      "This MCP client is not registered.",
    );
  const requested = auth.scope.length ? auth.scope : ["library:read"];
  const scopes = requested.filter((s) =>
    ["library:read", "jobs:write", "documents:write"].includes(s),
  );
  if (!scopes.includes("library:read")) scopes.unshift("library:read");
  if (request.method === "GET")
    return page(
      "Connect your research library",
      `<p><strong>${escaped(client.clientName ?? "MCP client")}</strong> is requesting access to your library.</p><p>Access includes private articles and the synced PhD project documents (plans, notes, drafts and extracted source text). Requested content is shared with the connected client.</p><ul><li>Search and read articles, PhD project documents, quality reports and job status.</li>${scopes.includes("jobs:write") ? "<li>Queue, cancel and retry processing jobs on your Mac.</li>" : ""}${scopes.includes("documents:write") ? "<li>Save Markdown notes and update PhD documents on your Mac, with commits and pushes to the project repository.</li>" : ""}</ul><form method="post" action="${escaped(url.pathname + url.search)}"><input type="hidden" name="csrf" value="${escaped(session.csrf)}"><button name="decision" value="allow">Allow connection</button> <button name="decision" value="deny">Cancel</button></form>`,
      200,
      auth.redirectUri,
    );
  if (request.method !== "POST")
    throw new HttpError(
      405,
      "method_not_allowed",
      "Use GET or POST for authorization.",
    );
  sameOrigin(request);
  const form = new URLSearchParams(await boundedText(request, 8000));
  if (!(await verifySecret(form.get("csrf") ?? "", session.csrf)))
    throw new HttpError(
      403,
      "csrf_failed",
      "Refresh the authorization page and try again.",
    );
  if (form.get("decision") !== "allow") {
    const redirect = new URL(auth.redirectUri);
    redirect.searchParams.set("error", "access_denied");
    if (auth.state) redirect.searchParams.set("state", auth.state);
    if (auth.issuer) redirect.searchParams.set("iss", auth.issuer);
    return Response.redirect(redirect.toString(), 303);
  }
  const { redirectTo } = await env.OAUTH_PROVIDER.completeAuthorization({
    request: auth,
    userId: "owner",
    metadata: { clientName: client.clientName ?? "MCP client" },
    scope: scopes,
    props: { userId: "owner", scopes },
  });
  return Response.redirect(redirectTo, 303);
}
