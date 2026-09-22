"""Live Devin client — milestone A shell.

Session lifecycle calls raise ``ExternalWriteDisabled`` and reads raise
``LiveReadsNotImplemented``; the v3 org endpoints from the tutorial land in
milestone B against this same interface, so no orchestration code changes
when they do.
"""

from __future__ import annotations

from .base import (
    DevinSession,
    DevinSessionSpec,
    ExternalWriteDisabled,
    LiveReadsNotImplemented,
)


class LiveDevinClient:
    mode = "live"

    def __init__(
        self,
        api_key: str,
        org_id: str,
        api_base_url: str = "https://api.devin.ai",
    ):
        self._api_key = api_key
        self.org_id = org_id
        self.api_base_url = api_base_url

    def create_session(self, spec: DevinSessionSpec) -> DevinSession:
        raise ExternalWriteDisabled("live external writes are not implemented")

    def find_session_by_correlation_tag(self, tag: str) -> DevinSession | None:
        raise LiveReadsNotImplemented("live Devin reads land in milestone B")

    def get_session(self, session_id: str) -> DevinSession:
        raise LiveReadsNotImplemented("live Devin reads land in milestone B")


class LiveReportSink:
    """Writes the facts snapshot to the fixed report-source issue via
    REPORT_GITHUB_TOKEN. Not implemented in milestone A."""

    mode = "live"

    def __init__(self, token: str, api_base_url: str = "https://api.github.com"):
        self._token = token
        self.api_base_url = api_base_url

    def publish_snapshot(self, repo: str, issue_number: int, body: str) -> str:
        raise ExternalWriteDisabled("live external writes are not implemented")
