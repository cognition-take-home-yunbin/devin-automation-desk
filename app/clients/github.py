"""Live GitHub REST client (milestone B).

Read-only against the configured fork: ``update_issue_body`` stays disabled —
the only live issue write is the report-source update, which lives in
``LiveReportSink`` behind ``REPORT_GITHUB_TOKEN``. Throttling surfaces as
:class:`RateLimited` honoring ``Retry-After`` / ``X-RateLimit-Reset`` so the
job layer's bounded retries do the waiting.
"""

from __future__ import annotations

import hashlib
import time
from datetime import datetime, timezone

import httpx

from .base import (
    AmbiguousCreation,
    CheckRun,
    ExternalWriteDisabled,
    Issue,
    IssueNotFound,
    LabelEvent,
    PullRequest,
    RateLimited,
)

MAX_PAGES = 10  # pagination bound — never loop a remote endpoint unbounded


class LiveGitHubClient:
    mode = "live"

    def __init__(
        self,
        repo: str,
        token: str,
        api_base_url: str = "https://api.github.com",
        timeout: float = 30.0,
    ):
        self.repo = repo
        self._client = httpx.Client(
            base_url=api_base_url.rstrip("/"),
            timeout=timeout,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )

    # -- transport -----------------------------------------------------------

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict | None = None,
        json_body: dict | None = None,
    ) -> httpx.Response:
        try:
            resp = self._client.request(
                method, path, params=params, json=json_body
            )
        except httpx.TimeoutException:
            # A timed-out read is safe to retry through job backoff.
            raise RateLimited(retry_after=5.0)
        if resp.status_code in (403, 429):
            retry_after = _parse_retry_after(resp)
            if retry_after is not None or _is_rate_limited(resp):
                raise RateLimited(retry_after=retry_after or 60.0)
        resp.raise_for_status()
        return resp

    def _get_json(self, path: str, params: dict | None = None):
        return self._request("GET", path, params=params).json()

    def _paged_list(
        self, path: str, params: dict, item_key: str | None = None
    ) -> list[dict]:
        """Fetch a page-indexed GitHub list endpoint, bounded by MAX_PAGES."""
        items: list[dict] = []
        for page in range(1, MAX_PAGES + 1):
            body = self._get_json(path, {**params, "per_page": 100, "page": page})
            page_items = body[item_key] if item_key else body
            items.extend(page_items)
            if len(page_items) < 100:
                break
        return items

    # -- GitHubClient protocol -------------------------------------------------

    def list_candidate_issues(self, repo: str, label: str) -> list[Issue]:
        # The issues endpoint returns PRs too — they are rejected downstream
        # and flagged via is_pull_request so the scan audit stays honest.
        items = self._paged_list(
            f"/repos/{repo}/issues",
            {"state": "open", "labels": label},
        )
        return [self._to_issue(repo, it) for it in items]

    def get_issue(self, repo: str, number: int) -> Issue:
        try:
            it = self._get_json(f"/repos/{repo}/issues/{number}")
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                raise IssueNotFound(f"{repo}#{number}") from exc
            raise
        return self._to_issue(repo, it)

    def _to_issue(self, repo: str, it: dict) -> Issue:
        return Issue(
            repo=repo,
            number=int(it["number"]),
            title=it.get("title") or "",
            state=it.get("state") or "",
            labels=[lbl["name"] for lbl in it.get("labels", [])],
            url=it.get("html_url") or "",
            body=it.get("body") or "",
            is_pull_request="pull_request" in it,
        )

    def get_issue_labels(self, repo: str, number: int) -> list[str]:
        rows = self._paged_list(f"/repos/{repo}/issues/{number}/labels", {})
        return [row["name"] for row in rows]

    def list_label_events(self, repo: str, number: int) -> list[LabelEvent]:
        rows = self._paged_list(f"/repos/{repo}/issues/{number}/events", {})
        events = []
        for row in rows:
            if row.get("event") not in ("labeled", "unlabeled"):
                continue
            events.append(
                LabelEvent(
                    repo=repo,
                    issue_number=number,
                    event_id=str(row["id"]),
                    event=row["event"],
                    label=(row.get("label") or {}).get("name"),
                    actor=(row.get("actor") or {}).get("login") or "",
                    created_at=_parse_github_ts(row.get("created_at")),
                )
            )
        return events

    def get_pull_request(self, repo: str, pr_number: int) -> PullRequest:
        pr = self._get_json(f"/repos/{repo}/pulls/{pr_number}")
        return PullRequest(
            repo=repo,
            number=int(pr["number"]),
            url=pr.get("html_url") or "",
            base_branch=(pr.get("base") or {}).get("ref") or "",
            head_sha=(pr.get("head") or {}).get("sha") or "",
            state=pr.get("state") or "open",
        )

    def get_check_runs(self, repo: str, pr_number: int) -> list[CheckRun]:
        head_sha = self.get_pull_request(repo, pr_number).head_sha
        rows = self._paged_list(
            f"/repos/{repo}/commits/{head_sha}/check-runs",
            {},
            item_key="check_runs",
        )
        runs = []
        for row in rows:
            # conclusion is null while the run is still queued/in_progress.
            conclusion = row.get("conclusion") or "pending"
            # Provenance: the app slug that posted the run (e.g. the Actions
            # app). Unattributed runs cannot satisfy trusted_workflows.
            workflow = (row.get("app") or {}).get("slug") or ""
            runs.append(
                CheckRun(
                    repo=repo,
                    pr_number=pr_number,
                    name=row.get("name") or "",
                    workflow=workflow,
                    conclusion=conclusion,
                    head_sha=row.get("head_sha") or head_sha,
                    url=row.get("html_url") or "",
                )
            )
        return runs

    def update_issue_body(self, repo: str, number: int, body: str) -> str:
        raise ExternalWriteDisabled(
            "app GitHub token is read-only; report writes use REPORT_GITHUB_TOKEN"
        )


class LiveReportSink:
    """Publishes the facts snapshot to the fixed report-source issue.

    Uses REPORT_GITHUB_TOKEN — a credential scoped to the report repo — so the
    application's GITHUB_TOKEN never needs write scope.
    """

    mode = "live"

    def __init__(
        self,
        token: str,
        api_base_url: str = "https://api.github.com",
        timeout: float = 30.0,
    ):
        self._client = httpx.Client(
            base_url=api_base_url.rstrip("/"),
            timeout=timeout,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )

    def publish_snapshot(self, repo: str, issue_number: int, body: str) -> str:
        try:
            resp = self._client.patch(
                f"/repos/{repo}/issues/{issue_number}", json={"body": body}
            )
        except httpx.TimeoutException as exc:
            # The update may have been applied — the caller records the
            # publication as 'unknown' and reconciles on the next cycle.
            raise AmbiguousCreation("report publish timed out") from exc
        if resp.status_code in (403, 429) and (
            _parse_retry_after(resp) is not None or _is_rate_limited(resp)
        ):
            raise RateLimited(retry_after=_parse_retry_after(resp) or 60.0)
        resp.raise_for_status()
        observed = resp.json().get("body") or ""
        return _sha_of(observed)


def _sha_of(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _parse_retry_after(resp: httpx.Response) -> float | None:
    raw = resp.headers.get("retry-after")
    if raw:
        try:
            return float(raw)
        except ValueError:
            pass
    reset = resp.headers.get("x-ratelimit-reset")
    if reset:
        try:
            return max(1.0, float(reset) - time.time())
        except ValueError:
            return None
    return None


def _is_rate_limited(resp: httpx.Response) -> bool:
    remaining = resp.headers.get("x-ratelimit-remaining")
    if remaining == "0":
        return True
    # Secondary rate limits come back as 403 with an explanatory body.
    return "rate limit" in (resp.text or "").lower()


def _parse_github_ts(value: str | None) -> float:
    if not value:
        return time.time()
    return (
        datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
        .replace(tzinfo=timezone.utc)
        .timestamp()
    )
