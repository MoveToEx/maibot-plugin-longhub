export type Rating = 'none' | 'moderate' | 'violent';
export type JobState = 'idle' | 'queued' | 'running' | 'done' | 'failed' | 'cancelled';
export interface Meme {
  id: string;
  text: string;
  rating: Rating;
  image_url: string;
  hub_tags: string[];
  ai_tags: string[];
  manual_tags: string[];
  excluded_tags: string[];
  effective_tags: string[];
  description: string;
  ai_description: string;
  manual_description: string | null;
  enabled: boolean;
  annotation_fresh: boolean;
  tag_job: { status: JobState; error?: string | null } | null;
}
export interface Status {
  total: number;
  ratings: Partial<Record<Rating, number>>;
  pending_notifications: number;
  tag_jobs: Partial<Record<JobState, number>>;
  tagging_paused: boolean;
  ready: boolean;
  vision_configured: boolean;
  receiver: { started: boolean; error: string | null; listen: string; path: string };
  sync: { id?: string; status: JobState; imported?: number; refreshed?: number; error?: string };
  last_sync: { completed_at: number } | null;
  last_webhook: number | null;
  sync_error?: string | null;
  background_error?: string | null;
}
export interface WebhookLog {
  id: number;
  received: number;
  image_id: string | null;
  event: string | null;
  signature_valid: boolean;
  http_status: number;
  processed: boolean;
  attempts: number;
  processing_error?: string | null;
  body: string;
  error?: string | null;
}
export interface PageData<T> {
  items: T[];
  total: number;
  page: number;
}
export interface Settings {
  api_base: string;
  app_key_configured: boolean;
  webhook_secret_configured: boolean;
  webhook_host: string;
  webhook_port: number;
  webhook_ready: boolean;
  sync_interval_hours: number;
  image_hosts: string[];
  vision_provider_id: string;
  tagging_rpm: number;
  tagging_concurrency: number;
  candidate_limit: number;
  max_image_mb: number;
  auto_tagging: boolean;
  allowed_ratings: Rating[];
  webhook_log_days: number;
  webhook_log_limit: number;
}
export interface SettingsData {
  settings: Settings;
  providers: { id: string; name: string }[];
}
export interface Bridge {
  ready(): Promise<unknown>;
  apiGet<T>(path: string, params?: Record<string, string | number>): Promise<T>;
  apiPost<T>(path: string, body: unknown): Promise<T>;
}
declare global {
  interface Window {
    AstrBotPluginView?: Bridge;
    AstrBotPluginPage?: Bridge;
  }
}
