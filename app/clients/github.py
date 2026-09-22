"""Live GitHub client — milestone A shell.

Implemented enough for factory selection and doctor's read-only checks, but
no live traffic yet: reads raise ``LiveReadsNotImplemented`` and every write
raises ``ExternalWriteDisabled``. Live mode therefore fails closed by
construction rather than by convention.
"""

from __future__ import annotations

from .base import (
    CheckRun,
    ExternalWriteDisabled,
    Issue,
    LabelEvent,
    LiveReadsNotImplemented,
    PullRequest,
)


class LiveGitHubClient:
    mode = "live"

    def __init__(self, repo: str, token: str, api_base_url: str):
        self.repo = repo
        self._token = token
        self.api_base_url = api_base_url

    def list_candidate_issues(self, repo: str, label: str) -> list[Issue]:
        raise LiveReadsNotImplemented("live GitHub reads land in milestone B")

    def get_issue_labels(self, repo: str, number: int) -> list[str]:
        raise LiveReadsNotImplemented("live GitHub reads land in milestone B")

    def list_label_events(self, repo: str, number: int) -> list[LabelEvent]:
        raise LiveReadsNotImplemented("live GitHub reads land in milestone B")

    def get_pull_request(self, repo: str, pr_number: int) -> PullRequest:
        raise LiveReadsNotImplemented("live GitHub reads land in milestone B")

    def get_check_runs(self, repo: str, pr_number: int) -> list[CheckRun]:
        raise LiveReadsNotImplemented("live GitHub reads land in milestone B")

    def update_issue_body(self, repo: str, number: int, body: str) -> str:
        raise ExternalWriteDisabled("live external writes are not implemented")
