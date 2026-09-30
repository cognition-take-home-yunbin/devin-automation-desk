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
    base_repo: str = ""       # repository the PR targets — out-of-scope when
                              # it isn't the configured repair repo
    head_repo: str = ""       # repository the head branch lives on
    state: str = "open"       # open | closed — a merged PR reports "closed";
                              # `merged` carries the distinction
    merged: bool = False
    merged_at: float | None = None
    merged_by: str = ""


@dataclass
class PullReview:
    """One submitted PR review — the unit the watcher aggregates into a
    review-decision signal."""
    repo: str
    pr_number: int
    state: str                # APPROVED | CHANGES_REQUESTED | COMMENTED |
                              # DISMISSED | PENDING
    author: str = ""
    submitted_at: float = 0.0


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
    def list_pr_reviews(
        self, repo: str, pr_number: int
    ) -> list[PullReview]: ...
    def update_issue_body(
        self, repo: str, number: int, body: str
    ) -> str: ...  # returns observed body hash
    def add_label(self, repo: str, number: int, label: str) -> None: ...
    def remove_label(self, repo: str, number: int, label: str) -> None: ...
    # Label writes power the managed status labels (see status_labels.py);
    # remove_label is idempotent — an absent label is a successful remove.


class BudgetBlocked(Exception):
    """Managed dispatch/follow-up is blocked by a spending reservation."""


class DevinClient(Protocol):
    def create_session(self, spec: DevinSessionSpec) -> DevinSession: ...
    def find_session_by_correlation_tag(self, tag: str) -> DevinSession | None: ...
    def list_sessions_by_tag(self, tag: str) -> list[DevinSession]: ...
    def get_session(self, session_id: str) -> DevinSession: ...
    def message_session(self, session_id: str, text: str) -> None: ...
    def stop_session(self, session_id: str, archive: bool = True) -> DevinSession: ...
    def daily_acu_usage(self, time_after: float, time_before: float) -> float | None: ...
    # daily_acu_usage returns None when the deployment lacks a consumption API —
    # callers must fall back to local reservation accounting.
    def session_acu_usage(self, session_id: str) -> float | None: ...
    # session_acu_usage reads the consumption endpoint for one session. The
    # session record's own acus_consumed field stays 0 in practice, so this
    # is the authoritative source; None when the endpoint is unavailable or
    # the token lacks the consumption permission.


class ReportSink(Protocol):
    """Reads and writes the fixed report-source issue's body."""

    def get_issue(self, repo: str, issue_number: int) -> Issue: ...
    # Read-back: validates the destination exists and lets the publisher
    # reconcile ambiguous writes by hashing the observed body.

    def publish_snapshot(
        self, repo: str, issue_number: int, body: str
    ) -> str: ...  # returns observed body hash / revision ref
