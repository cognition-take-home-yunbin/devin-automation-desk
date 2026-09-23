"""Publish a facts snapshot to the one fixed report-source issue.

Rules (milestone D):

* The destination is configuration — ``REPORT_GITHUB_REPO`` +
  ``REPORT_DATA_ISSUE_NUMBER`` — never derived from issue content. It is
  *validated* before every write: it must resolve to an open issue (not a
  pull request) in the configured repository.
* One issue body, updated in place — never an unbounded comment stream,
  never a new issue per poll.
* Publish at most once per ``REPORT_PUBLISH_INTERVAL_SECONDS`` unless the
  snapshot's canonical hash changed (a material change always publishes).
* Ambiguous writes are reconciled by *reading the issue back*: the body
  embeds ``report-sha256:<hash>`` so the handler can confirm exactly which
  snapshot is live instead of assuming.
* The body is scrubbed for credential-shaped strings before publishing; it
  contains only derived facts, links, and explicit unknowns — never
  secrets, Slack conversation bodies, or raw logs.
"""

from __future__ import annotations

import sqlite3

from .. import db
from ..clients.base import (
    AmbiguousCreation,
    IssueNotFound,
    RateLimited,
)
from ..transitions import audit
from .context import ServiceContext
from . import jobs, reporting


def _destination(ctx: ServiceContext) -> tuple[str, int]:
    s = ctx.settings
    if ctx.mode == "simulation":
        # The simulated report sink owns a single fixed issue per repo.
        repo = s.report_github_repo or "acme/devin-repair-desk"
        number = s.report_data_issue_number or 1
        return repo, number
    if not s.report_github_repo or s.report_data_issue_number is None:
        raise RuntimeError(
            "live report destination is not configured "
            "(REPORT_GITHUB_REPO / REPORT_DATA_ISSUE_NUMBER)"
        )
    return s.report_github_repo, int(s.report_data_issue_number)


def _record(
    conn: sqlite3.Connection, snapshot_id: int, mode: str,
    repo: str, number: int, status: str, *, observed: str | None = None,
    detail: str = "", external_ref: str | None = None,
) -> None:
    conn.execute(
        """INSERT INTO publication_records
           (snapshot_id, mode, destination_repo, destination_issue_number,
            status, observed_body_hash, detail, external_ref, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (snapshot_id, mode, repo, number, status, observed, detail[:500],
         external_ref, db.now()),
    )


def _observed_snapshot_sha(body: str) -> str | None:
    m = reporting.SHA_MARKER_RE.search(body or "")
    return m.group(1) if m else None


def _validate_destination(ctx: ServiceContext, repo: str, number: int) -> None:
    """The configured destination must be an actual open issue (never a
    pull request) in the configured repository."""
    issue = ctx.clients.report_sink.get_issue(repo, number)
    problems = []
    if issue.is_pull_request:
        problems.append("destination is a pull request, not an issue")
    if issue.state == "closed":
        problems.append("destination issue is closed")
    if problems:
        raise IssueNotFound(
            f"report destination {repo}#{number} rejected: "
            + "; ".join(problems)
        )


def handle_publish(ctx: ServiceContext, job: sqlite3.Row) -> None:
    conn, s = ctx.conn, ctx.settings
    payload = db.loads(job["payload_json"])
    repo, issue_number = _destination(ctx)

    snapshot_id, sha, content = reporting.persist_snapshot(
        conn, ctx.mode, s.report_timezone
    )
    body = reporting.scrub(reporting.render_markdown(content, sha))

    # -- destination validation (fails the job with a recorded reason) ------
    try:
        _validate_destination(ctx, repo, issue_number)
    except IssueNotFound as exc:
        with db.transaction(conn):
            _record(conn, snapshot_id, ctx.mode, repo, issue_number,
                    "failed", detail=f"destination rejected: {exc}")
            audit(conn, action="destination_rejected", mode=ctx.mode,
                  task_id=payload.get("task_id"), detail=str(exc)[:500])
        raise

    # -- publish gate: material change, or interval elapsed -----------------
    control = conn.execute(
        "SELECT last_publish_at FROM control WHERE id = 1"
    ).fetchone()
    last_pub = control["last_publish_at"] if control else None
    last_confirmed = conn.execute(
        "SELECT p.observed_body_hash, s.sha256 AS snapshot_sha "
        "FROM publication_records p JOIN report_snapshots s "
        "ON s.id = p.snapshot_id "
        "WHERE p.mode = ? AND p.status = 'confirmed' "
        "ORDER BY p.id DESC LIMIT 1",
        (ctx.mode,),
    ).fetchone()
    unchanged = (
        last_confirmed is not None
        and last_confirmed["snapshot_sha"] == sha
    )
    within_interval = (
        last_pub is not None
        and db.now() - last_pub < s.report_publish_interval_seconds
    )
    if unchanged and within_interval:
        with db.transaction(conn):
            _record(conn, snapshot_id, ctx.mode, repo, issue_number,
                    "confirmed", observed=last_confirmed["observed_body_hash"],
                    detail="skipped: no material change within publish "
                           "interval",
                    external_ref=f"{repo}#{issue_number}")
            audit(conn, action="publish_skipped", mode=ctx.mode,
                  task_id=payload.get("task_id"),
                  detail=f"no material change ({sha[:12]} unchanged)")
        return

    # -- write ---------------------------------------------------------------
    try:
        observed = ctx.clients.report_sink.publish_snapshot(
            repo, issue_number, body
        )
    except RateLimited as exc:
        with db.transaction(conn):
            _record(conn, snapshot_id, ctx.mode, repo, issue_number,
                    "unknown", detail=f"throttled: {exc}")
        raise jobs.RetryLater(exc.retry_after, "report publish throttled")
    except AmbiguousCreation as exc:
        # Read back the issue body: the embedded sha marker says whether the
        # write actually landed — never re-send on assumption.
        reconciled_sha: str | None = None
        try:
            remote = ctx.clients.report_sink.get_issue(repo, issue_number)
            reconciled_sha = _observed_snapshot_sha(remote.body)
        except Exception:  # noqa: BLE001 - read-back may also fail
            remote = None
        with db.transaction(conn):
            if reconciled_sha == sha:
                _record(conn, snapshot_id, ctx.mode, repo, issue_number,
                        "confirmed", observed=sha,
                        detail=f"reconciled ambiguous write via read-back "
                               f"({exc})", external_ref=f"{repo}#{issue_number}")
                conn.execute(
                    "UPDATE control SET last_publish_at = ? WHERE id = 1",
                    (db.now(),),
                )
                audit(conn, action="publish_reconciled", mode=ctx.mode,
                      task_id=payload.get("task_id"),
                      detail=f"write landed — marker {sha[:12]} on "
                             f"{repo}#{issue_number}")
            else:
                _record(conn, snapshot_id, ctx.mode, repo, issue_number,
                        "unknown",
                        detail=f"ambiguous write ({exc}); observed marker "
                               f"{reconciled_sha or 'none'}")
                audit(conn, action="publish_ambiguous", mode=ctx.mode,
                      task_id=payload.get("task_id"),
                      detail=f"{exc} — observed marker "
                             f"{reconciled_sha or 'none'}, retrying")
        if reconciled_sha == sha:
            return
        raise jobs.RetryLater(
            max(s.poll_interval_seconds, 5.0),
            "publish outcome unconfirmed — will re-read",
        )
    except Exception as exc:
        with db.transaction(conn):
            _record(conn, snapshot_id, ctx.mode, repo, issue_number,
                    "failed", detail=str(exc)[:500])
            audit(conn, action="publish_failed", mode=ctx.mode,
                  task_id=payload.get("task_id"), detail=str(exc)[:500])
        raise

    with db.transaction(conn):
        _record(conn, snapshot_id, ctx.mode, repo, issue_number,
                "confirmed", observed=observed,
                detail="snapshot written to fixed report-source issue",
                external_ref=f"{repo}#{issue_number}")
        conn.execute(
            "UPDATE control SET last_publish_at = ? WHERE id = 1",
            (db.now(),),
        )
        audit(
            conn, action="report_published", mode=ctx.mode,
            task_id=payload.get("task_id"),
            detail=f"snapshot {sha[:12]} -> {repo}#{issue_number} "
                   f"(observed {observed})",
        )
