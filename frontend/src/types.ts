export type Mode = "simulation" | "live";

export interface TaskSummary {
  id: number;
  mode: Mode;
  repo: string;
  issue_number: number;
  issue_title: string;
  issue_url: string;
  execution: string;
  validation: string;
  review: string;
  disposition: string;
  approval_actor: string | null;
  pr_url: string | null;
  devin_session_url: string | null;
  slack_link: string | null;
  cleanup_state: string;
  head_sha: string | null;
  base_sha: string | null;
  acu_used: number | null;
  synthetic: boolean;
  created_at: number;
  updated_at: number;
}

export interface Attempt {
  id: number;
  attempt_number: number;
  correlation_tag: string;
  session_id: string | null;
  session_url: string | null;
  raw_status: string | null;
  raw_detail: string | null;
  acu_limit: number | null;
  acu_used: number | null;
  prompt_hash: string | null;
  context_hash: string | null;
  started_at: number;
  finished_at: number | null;
}

export interface Evidence {
  id: number;
  created_at: number;
  kind: string;
  title: string;
  body: unknown;
  uri: string | null;
  synthetic: boolean;
  verifier: string | null;
}

export interface AuditEvent {
  id: number;
  created_at: number;
  source: string;
  action: string;
  dimension: string | null;
  old_value: string | null;
  new_value: string | null;
  detail: string | null;
}

export interface TaskDetail extends TaskSummary {
  issue_snapshot: unknown;
  last_error: string | null;
  attempts: Attempt[];
  evidence: Evidence[];
  audit: AuditEvent[];
}

export interface Overview {
  mode: Mode;
  paused: boolean;
  repo: string;
  last_scan_at: number | null;
  last_publish_at: number | null;
  scan_age_seconds: number | null;
  scan_fresh: boolean;
  publish_fresh: boolean;
  last_native_observe_at: number | null;
  native_observe_error: string | null;
  metrics: Record<string, number | string>;
  limits: Record<string, number>;
  generated_at: number;
}

export interface Publication {
  destination_repo: string;
  destination_issue_number: number;
  status: string;
  observed_body_hash: string | null;
  external_ref: string | null;
  created_at: number;
}

export interface Report {
  id: number;
  mode: Mode;
  report_type: string;
  schema_version: number;
  title: string;
  sha256: string;
  generated_at: number;
  native_session_url: string | null;
  native_state: string | null;
  slack_link: string | null;
  publications: Publication[];
}

export interface NativeSession {
  id: number;
  mode: Mode;
  tag: string;
  session_id: string;
  url: string | null;
  status: string;
  status_detail: string;
  acu_used: number | null;
  slack_link: string | null;
  slack_source: string | null;
  first_seen_at: number;
  last_seen_at: number;
}

export interface ScanRequestResult {
  mode: Mode;
  job_id: number | null;
  queued: boolean;
}
