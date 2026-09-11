import { z } from "zod";
import type { AppEnv } from "./types";
export const settingsSchema = z
  .object({
    qa_enabled: z.boolean(),
    opening_sound: z.enum(["typing", "none"]),
    closing_sound: z.enum(["typing", "none"]),
    spoken_intro: z.boolean(),
    icloud_folder: z.string().max(2000),
    backup_enabled: z.boolean(),
    backup_folder: z.string().max(2000),
    tts_model: z.literal("kokoro"),
    full_voice: z.string().max(100),
    brief_voice: z.string().max(100),
    speed: z.number().min(0.5).max(2),
    auto_publish: z.boolean(),
    auto_generate: z.enum(["off", "markdown", "full", "brief", "both"]),
  })
  .strict();
export const defaultSettings = {
  qa_enabled: true,
  opening_sound: "typing" as const,
  closing_sound: "none" as const,
  spoken_intro: true,
  icloud_folder: "",
  backup_enabled: false,
  backup_folder: "",
  tts_model: "kokoro" as const,
  full_voice: "am_michael",
  brief_voice: "af_heart",
  speed: 1,
  auto_publish: true,
  auto_generate: "markdown" as const,
};
export async function getSettings(env: AppEnv) {
  const row = await env.DB.prepare(
    "SELECT value, updated_at FROM settings WHERE id=1",
  ).first<{ value: string; updated_at: string }>();
  return {
    ...defaultSettings,
    ...(row ? JSON.parse(row.value) : {}),
    updated_at: row?.updated_at ?? null,
    configured: !!row,
  };
}
export async function patchSettings(env: AppEnv, patch: unknown) {
  const parsed = settingsSchema.partial().parse(patch);
  const current = await getSettings(env);
  const { updated_at: _updated, configured: _configured, ...values } = current;
  const next = settingsSchema.parse({ ...values, ...parsed });
  const updated_at = new Date().toISOString();
  await env.DB.prepare(
    "INSERT INTO settings(id,value,updated_at) VALUES(1,?,?) ON CONFLICT(id) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
  )
    .bind(JSON.stringify(next), updated_at)
    .run();
  return { ...next, updated_at, configured: true };
}
