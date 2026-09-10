import { z } from "zod";
import { HttpError } from "./http";
import { getArticle } from "./library";
import type { AppEnv, JobRow } from "./types";
export const jobSchema = z
  .object({
    action: z.enum(["markdown", "full", "brief", "both", "sync"]),
    scope: z.enum(["new", "all", "one"]),
    article_id: z.string().max(128).optional(),
    qa: z.boolean().optional(),
    force: z.boolean().optional(),
  })
  .strict()
  .refine((value) => value.scope !== "one" || !!value.article_id, {
    message: "Choose an article for a single-article job.",
  });
export function jobJson(row: JobRow, bridge = false) {
  const { options, idempotency_key, lease_token, worker_id, ...rest } = row;
  return {
    ...rest,
    ...JSON.parse(options),
    ...(bridge ? { worker_id, lease_token } : {}),
  };
}
export async function getJob(env: AppEnv, id: string) {
  const row = await env.DB.prepare("SELECT * FROM jobs WHERE id=?")
    .bind(id)
    .first<JobRow>();
  if (!row)
    throw new HttpError(
      404,
      "job_not_found",
      "This processing job was not found.",
    );
  return row;
}
export async function createJob(
  env: AppEnv,
  input: unknown,
  idempotency?: string | null,
) {
  const parsed = jobSchema.parse(input),
    now = new Date().toISOString();
  if (idempotency && !/^[a-zA-Z0-9._:-]{1,128}$/.test(idempotency))
    throw new HttpError(
      400,
      "invalid_idempotency_key",
      "Use a short unique Idempotency-Key.",
    );
  if (idempotency) {
    const existing = await env.DB.prepare(
      "SELECT * FROM jobs WHERE idempotency_key=?",
    )
      .bind(idempotency)
      .first<JobRow>();
    if (existing) return jobJson(existing);
  }
  const title =
    parsed.scope === "one"
      ? (await getArticle(env, parsed.article_id!)).title
      : parsed.scope === "new"
        ? "New articles in Zotero"
        : "All articles in Zotero";
  const active = await env.DB.prepare(
    "SELECT count(*) AS count FROM jobs WHERE status IN ('queued','running','cancel_requested')",
  ).first<{ count: number }>();
  if ((active?.count ?? 0) >= Number(env.MAX_ACTIVE_JOBS))
    throw new HttpError(
      429,
      "queue_full",
      "The job queue is full. Let existing jobs finish before adding more.",
    );
  const id = crypto.randomUUID();
  await env.DB.prepare(
    "INSERT INTO jobs(id,action,scope,article_id,title,options,idempotency_key,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(idempotency_key) DO NOTHING",
  )
    .bind(
      id,
      parsed.action,
      parsed.scope,
      parsed.article_id ?? null,
      title,
      JSON.stringify({ qa: parsed.qa, force: parsed.force }),
      idempotency ?? null,
      now,
      now,
    )
    .run();
  const row = idempotency
    ? await env.DB.prepare("SELECT * FROM jobs WHERE idempotency_key=?")
        .bind(idempotency)
        .first<JobRow>()
    : await getJob(env, id);
  return jobJson(row!);
}
export async function listJobs(env: AppEnv) {
  const rows = await env.DB.prepare(
    "SELECT * FROM jobs ORDER BY updated_at DESC LIMIT 100",
  ).all<JobRow>();
  return { jobs: rows.results.map((row) => jobJson(row)) };
}
export async function changeJob(
  env: AppEnv,
  id: string,
  action: "cancel" | "retry",
) {
  const current = await getJob(env, id),
    now = new Date().toISOString();
  if (action === "cancel") {
    await env.DB.prepare(
      `UPDATE jobs SET status=CASE WHEN status='queued' THEN 'cancelled' ELSE 'cancel_requested' END,message='Cancellation requested; the Mac will stop at the next safe checkpoint.',updated_at=? WHERE id=? AND status IN ('queued','running')`,
    )
      .bind(now, id)
      .run();
    return jobJson(await getJob(env, id));
  }
  if (!["failed", "cancelled", "completed"].includes(current.status))
    throw new HttpError(
      409,
      "job_active",
      "This job is already queued or running.",
    );
  return createJob(env, {
    action: current.action,
    scope: current.scope,
    article_id: current.article_id ?? undefined,
    ...JSON.parse(current.options),
  });
}
const claimSchema = z
  .object({
    worker_id: z.string().min(1).max(128),
    lease_seconds: z.number().int().min(60).max(900).default(300),
  })
  .strict();
export async function claimJob(env: AppEnv, input: unknown) {
  const parsed = claimSchema.parse(input),
    now = new Date().toISOString(),
    until = new Date(Date.now() + parsed.lease_seconds * 1000).toISOString(),
    token = crypto.randomUUID();
  await env.DB.prepare(
    "UPDATE jobs SET status='cancelled',stage='cancelled',updated_at=? WHERE status='cancel_requested' AND lease_expires<?",
  )
    .bind(now, now)
    .run();
  const existing = await env.DB.prepare(
    "SELECT * FROM jobs WHERE worker_id=? AND status IN ('running','cancel_requested') AND lease_expires>=? ORDER BY created_at LIMIT 1",
  )
    .bind(parsed.worker_id, now)
    .first<JobRow>();
  if (existing) return { job: jobJson(existing, true) };
  const row = await env.DB.prepare(
    `UPDATE jobs SET status='running',stage='starting',worker_id=?,lease_token=?,lease_expires=?,updated_at=?
    WHERE id=(SELECT id FROM jobs WHERE status='queued' OR (status='running' AND lease_expires<?) ORDER BY created_at LIMIT 1)
    AND NOT EXISTS (SELECT 1 FROM jobs WHERE worker_id=? AND status IN ('running','cancel_requested') AND lease_expires>=?)
    RETURNING *`,
  )
    .bind(parsed.worker_id, token, until, now, now, parsed.worker_id, now)
    .first<JobRow>();
  const claimed =
    row ??
    (await env.DB.prepare(
      "SELECT * FROM jobs WHERE worker_id=? AND status IN ('running','cancel_requested') AND lease_expires>=? ORDER BY created_at LIMIT 1",
    )
      .bind(parsed.worker_id, now)
      .first<JobRow>());
  return { job: claimed ? jobJson(claimed, true) : null };
}
const updateSchema = z
  .object({
    worker_id: z.string().min(1).max(128),
    lease_token: z.string().min(1).max(128),
    status: z.enum(["running", "completed", "failed", "cancelled"]).optional(),
    stage: z.string().max(100).optional(),
    progress: z.number().min(0).max(1).optional(),
    message: z.string().max(4000).optional(),
    title: z.string().min(1).max(2000).optional(),
    lease_seconds: z.number().int().min(60).max(900).default(300),
  })
  .strict();
export async function updateJob(env: AppEnv, id: string, input: unknown) {
  const parsed = updateSchema.parse(input),
    now = new Date().toISOString(),
    until = new Date(Date.now() + parsed.lease_seconds * 1000).toISOString();
  // A late worker cannot resurrect a terminal job or overwrite a newer lease.
  const row = await env.DB.prepare(
    `UPDATE jobs SET
    status=CASE WHEN status='cancel_requested' AND COALESCE(?,'running')='running' THEN status ELSE COALESCE(?,status) END,
    stage=COALESCE(?,stage),progress=COALESCE(?,progress),message=COALESCE(?,message),title=COALESCE(?,title),lease_expires=?,updated_at=?
    WHERE id=? AND worker_id=? AND lease_token=? AND lease_expires>=? AND status IN ('running','cancel_requested') RETURNING *`,
  )
    .bind(
      parsed.status ?? null,
      parsed.status ?? null,
      parsed.stage ?? null,
      parsed.progress ?? null,
      parsed.message ?? null,
      parsed.title ?? null,
      until,
      now,
      id,
      parsed.worker_id,
      parsed.lease_token,
      now,
    )
    .first<JobRow>();
  if (!row)
    throw new HttpError(
      409,
      "lease_lost",
      "This job lease expired or belongs to another Mac process. Stop processing and refresh the queue.",
    );
  return { job: jobJson(row, true) };
}
export async function heartbeat(env: AppEnv, input: unknown) {
  const value = z
    .object({
      worker_id: z.string().min(1).max(128),
      title: z.string().max(2000).optional(),
      current_job_id: z.string().max(128).nullable().optional(),
      local_url: z.string().max(2000).optional(),
      version: z.string().max(100).optional(),
    })
    .strict()
    .parse(input);
  const now = new Date().toISOString();
  await env.DB.prepare(
    "INSERT INTO worker_status(worker_id,title,current_job_id,version,last_seen) VALUES(?,?,?,?,?) ON CONFLICT(worker_id) DO UPDATE SET title=excluded.title,current_job_id=excluded.current_job_id,version=excluded.version,last_seen=excluded.last_seen",
  )
    .bind(
      value.worker_id,
      value.title ?? "Mac",
      value.current_job_id ?? null,
      value.version ?? null,
      now,
    )
    .run();
  return { ok: true, last_seen: now, poll_after_seconds: 60 };
}
export async function status(env: AppEnv) {
  const rows = await env.DB.batch<Record<string, unknown>>([
    env.DB.prepare(
      "SELECT * FROM worker_status ORDER BY last_seen DESC LIMIT 1",
    ),
    env.DB.prepare(
      "SELECT count(*) AS total,sum(markdown_key IS NOT NULL) AS markdown,sum(audio_status IN ('ready','completed','published')) AS audio,sum(qa_status IN ('warnings','failed')) AS warnings FROM articles",
    ),
    env.DB.prepare("SELECT status,count(*) AS count FROM jobs GROUP BY status"),
    env.DB.prepare("SELECT content_bytes,articles FROM budget WHERE id=1"),
  ]);
  const worker = rows[0].results[0];
  return {
    worker: {
      ...worker,
      online:
        !!worker && Date.now() - Date.parse(String(worker.last_seen)) < 180000,
      last_seen: worker?.last_seen ?? null,
    },
    counts: rows[1].results[0] ?? {},
    jobs: Object.fromEntries(rows[2].results.map((r) => [r.status, r.count])),
    storage: {
      ...rows[3].results[0],
      max_bytes: Number(env.MAX_LIBRARY_BYTES),
      max_articles: Number(env.MAX_ARTICLES),
    },
    capabilities: { local_files: false, remote: true, mcp: true, search: true },
    version: "0.1.0",
  };
}
