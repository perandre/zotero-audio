import type { OAuthHelpers } from "@cloudflare/workers-oauth-provider";

export type AppEnv = Env & {
  PROJECT_GITHUB_TOKEN?: string;
  OWNER_ACCESS_KEY: string;
  SESSION_SECRET: string;
  BRIDGE_TOKEN: string;
  ZOTERO_API_KEY?: string;
  ZOTERO_USER_ID?: string;
  OAUTH_PROVIDER: OAuthHelpers;
};
export type Permissions = { userId: "owner"; scopes: string[] };
export type ArticleRow = {
  id: string;
  title: string;
  authors: string;
  year: number | null;
  source_url: string;
  license_status: string;
  markdown_status: string;
  audio_status: string;
  qa_status: string;
  warnings: string;
  metadata: string;
  markdown_key: string | null;
  markdown_hash: string | null;
  review_key: string | null;
  review_hash: string | null;
  content_bytes: number;
  updated_at: string;
};
export type JobRow = {
  id: string;
  action: string;
  scope: string;
  article_id: string | null;
  title: string;
  status: string;
  stage: string;
  progress: number;
  message: string;
  options: string;
  idempotency_key: string | null;
  worker_id: string | null;
  lease_token: string | null;
  lease_expires: string | null;
  created_at: string;
  updated_at: string;
};
