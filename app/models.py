"""API response models. Every response carries ``mode`` so consumers can
always tell live data from simulation data (PRD §12)."""

from __future__ import annotations

from pydantic import BaseModel


class TaskSummary(BaseModel):
    id: int
    mode: str
    repo: str
    issue_number: int
    issue_title: str
    issue_url: str
    execution: str
    validation: str
    review: str
    disposition: str
    approval_actor: str | None
    pr_url: str | None
    devin_session_url: str | None
    slack_link: str | None
    cleanup_state: str
    head_sha: str | None
    base_sha: str | None
    acu_used: float | None
    synthetic: bool
    created_at: float
    updated_at: float


class AuditEventOut(BaseModel):
    id: int
    created_at: float
    source: str
    action: str
    dimension: str | None
    old_value: str | None
    new_value: str | None
    detail: str | None


class EvidenceOut(BaseModel):
    id: int
    created_at: float
    kind: str
    title: str
    body: object
    uri: str | None
    synthetic: bool
    verifier: str | None


class AttemptOut(BaseModel):
    id: int
    attempt_number: int
    correlation_tag: str
    session_id: str | None
    session_url: str | None
    raw_status: str | None
    raw_detail: str | None
    acu_limit: int | None
    acu_used: float | None
    prompt_hash: str | None
    context_hash: str | None
    started_at: float
    finished_at: float | None


class TaskDetail(TaskSummary):
    issue_snapshot: object
    last_error: str | None
    attempts: list[AttemptOut]
    evidence: list[EvidenceOut]
    audit: list[AuditEventOut]


class JobOut(BaseModel):
    id: int
    kind: str
    status: str
    attempt_count: int
    due_at: float
    last_error: str | None


class ReportOut(BaseModel):
    id: int
    mode: str
    report_type: str
    schema_version: int
    title: str
    sha256: str
    generated_at: float
    native_session_url: str | None = None
    native_state: str | None = None
    slack_link: str | None = None
    publications: list[dict]


class NativeSessionOut(BaseModel):
    id: int
    mode: str
    tag: str
    session_id: str
    url: str | None
    status: str
    status_detail: str
    acu_used: float | None  # None renders as "unknown" in the UI
    slack_link: str | None
    slack_source: str | None
    first_seen_at: float
    last_seen_at: float


class OverviewOut(BaseModel):
    mode: str
    paused: bool
    repo: str
    last_scan_at: float | None
    last_publish_at: float | None
    scan_age_seconds: float | None
    scan_fresh: bool
    publish_fresh: bool
    last_native_observe_at: float | None
    native_observe_error: str | None
    metrics: dict
    limits: dict
    generated_at: float


class HealthOut(BaseModel):
    status: str
    mode: str


class SimulationResult(BaseModel):
    mode: str
    scenario: str
    issue_number: int
    notes: list[str]
    synthetic: bool
