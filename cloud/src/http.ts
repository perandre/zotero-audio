export class HttpError extends Error {
  constructor(
    public status: number,
    public code: string,
    message: string,
  ) {
    super(message);
  }
}
export function json(data: unknown, status = 200): Response {
  return Response.json(data, {
    status,
    headers: { "Cache-Control": "no-store" },
  });
}
export async function boundedText(
  request: Pick<Request, "headers" | "body">,
  limit = 1_100_000,
): Promise<string> {
  if (Number(request.headers.get("Content-Length") || 0) > limit)
    throw new HttpError(413, "too_large", "Request exceeds the upload limit.");
  if (!request.body) return "";
  const reader = request.body.getReader();
  const decoder = new TextDecoder("utf-8", { fatal: true, ignoreBOM: false });
  let length = 0,
    text = "";
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      length += value.byteLength;
      if (length > limit) {
        await reader.cancel();
        throw new HttpError(
          413,
          "too_large",
          "Request exceeds the upload limit.",
        );
      }
      text += decoder.decode(value, { stream: true });
    }
    return text + decoder.decode();
  } finally {
    reader.releaseLock();
  }
}
export async function body(request: Request, limit?: number): Promise<unknown> {
  try {
    return JSON.parse(await boundedText(request, limit));
  } catch (e) {
    if (e instanceof HttpError) throw e;
    throw new HttpError(400, "invalid_json", "Expected a valid JSON body.");
  }
}
export function sameOrigin(request: Request): void {
  const origin = request.headers.get("Origin");
  if (origin !== new URL(request.url).origin)
    throw new HttpError(
      403,
      "origin_mismatch",
      "This action must come from this application.",
    );
}
export function escaped(value: unknown): string {
  return String(value ?? "").replace(
    /[&<>"']/g,
    (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[
        c
      ]!,
  );
}
export async function digest(value: string): Promise<string> {
  const bytes = await crypto.subtle.digest(
    "SHA-256",
    new TextEncoder().encode(value),
  );
  return Array.from(new Uint8Array(bytes), (x) =>
    x.toString(16).padStart(2, "0"),
  ).join("");
}
export async function verifySecret(
  provided: string,
  expected: string | undefined,
): Promise<boolean> {
  if (!expected || expected.length < 32) return false;
  const [a, b] = await Promise.all([
    crypto.subtle.digest("SHA-256", new TextEncoder().encode(provided)),
    crypto.subtle.digest("SHA-256", new TextEncoder().encode(expected)),
  ]);
  return crypto.subtle.timingSafeEqual(a, b);
}
export function idFromPath(value: string): string {
  let id: string;
  try {
    id = decodeURIComponent(value);
  } catch {
    throw new HttpError(
      400,
      "invalid_id",
      "Invalid article or job identifier.",
    );
  }
  if (!/^[a-zA-Z0-9][a-zA-Z0-9._:-]{0,127}$/.test(id))
    throw new HttpError(
      400,
      "invalid_id",
      "Invalid article or job identifier.",
    );
  return id;
}
export function integer(
  value: string | null,
  fallback: number,
  max: number,
): number {
  const n = value === null ? fallback : Number(value);
  return Number.isFinite(n)
    ? Math.max(0, Math.min(max, Math.floor(n)))
    : fallback;
}
