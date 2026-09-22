"""External-service interfaces.

Job handlers only ever see these protocols. ``factory.build_clients`` picks
the implementation from APP_MODE: scripted fakes in simulation, real HTTP
clients in live (whose writes are not implemented in milestone A — they fail
closed). Keeping the interfaces shared means the orchestration logic is
identical once live clients land.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


class RateLimited(Exception):
    """Remote API throttled the call; the job retries honoring Retry-After."""

    def __init__(self, retry_after: float = 5.0):
        super().__init__("rate limited")
        self.retry_after = retry_after


class AmbiguousCreation(Exception):
    """A create call's outcome is unknown (e.g. timed out after submission).

    Callers must not blindly retry — the resource may already exist.
    """


class ExternalWriteDisabled(RuntimeError):
    """Milestone A performs no live external writes."""


class LiveReadsNotImplemented(NotImplementedError):
    pass


@dataclass
class Issue:
    repo: str
    number: int
    title: str
    state: str
    labels: list[str]
    url: str
    body: str = ""
    is_pull_request: bool = False


@dataclass
class LabelEvent:
    repo: str
    issue_number: int
    event_id: str
    event: str            # e.g. "labeled"
    label: str | None
    actor: str
    created_at: float


@dataclass
class CheckRun:
    repo: str
    pr_number: int
    name: str
    workflow: str         # provenance — the workflow that produced the check
    conclusion: str       # success | failure | pending | skipped | neutral
    head_sha: str
    url: str = ""


@dataclass
class PullRequest:
    repo: str
    number: int
    url: str
    base_branch: str
    head_sha: str
    state: str = "open"


@dataclass
class DevinSessionSpec:
    repo: str
    issue_number: int
    title: str
    correlation_tag: str  # stable task/attempt tag persisted before the call
    issue_snapshot_json: str = ""
    acceptance_criteria: str = ""
    base_sha: str = ""
    acu_limit: int | None = None
    playbook_id: str = ""
    knowledge_ids: tuple[str, ...] = ()
    repo_ref: str = ""


@dataclass
class DevinSession:
    session_id: str
    status: str           # working | needs_input | approval_required |
                          # finished | suspended | failed | unknown
    status_detail: str = ""
    url: str = ""
    pr_number: int | None = None
    pr_url: str | None = None
    pr_head_sha: str | None = None
    acu_used: float | None = None
    notes: dict = field(default_factory=dict)


class IssueNotFound(Exception):
    """The configured issue no longer resolves (deleted/closed view)."""


class GitHubClient(Protocol):
    def list_candidate_issues(self, repo: str, label: str) -> list[Issue]: ...
    def get_issue(self, repo: str, number: int) -> Issue: ...
    def get_issue_labels(self, repo: str, number: int) -> list[str]: ...
    def list_label_events(self, repo: str, number: int) -> list[LabelEvent]: ...
    def get_pull_request(self, repo: str, pr_number: int) -> PullRequest: ...
    def get_check_runs(self, repo: str, pr_number: int) -> list[CheckRun]: ...
    def update_issue_body(
        self, repo: str, number: int, body: str
    ) -> str: ...  # returns observed body hash


class BudgetBlocked(Exception):
    """Managed dispatch/follow-up is blocked by a spending reservation."""


class DevinClient(Protocol):
    def create_session(self, spec: DevinSessionSpec) -> DevinSession: ...
    def find_session_by_correlation_tag(self, tag: str) -> DevinSession | None: ...
    def get_session(self, session_id: str) -> DevinSession: ...
    def message_session(self, session_id: str, text: str) -> None: ...
    def stop_session(self, session_id: str, archive: bool = True) -> DevinSession: ...
    def daily_acu_usage(self, time_after: float, time_before: float) -> float | None: ...
    # daily_acu_usage returns None when the deployment lacks a consumption API —
    # callers must fall back to local reservation accounting.


class ReportSink(Protocol):
    """Writes the facts snapshot to the fixed report-source issue."""

    def publish_snapshot(
        self, repo: str, issue_number: int, body: str
    ) -> str: ...  # returns observed body hash / revision ref
