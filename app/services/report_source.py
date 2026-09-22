"""Publish a facts snapshot to the one fixed report-source issue.

The destination is configuration, never derived from issue content
(PRD §16). The publication record stores the observed body hash so an
ambiguous write can be reconciled by reading back rather than resent.
"""

from __future__ import annotations

import sqlite3

from .. import db
from ..clients.base import RateLimited
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


def handle_publish(ctx: ServiceContext, job: sqlite3.Row) -> None:
    conn, s = ctx.conn, ctx.settings
    payload = db.loads(job["payload_json"])
    repo, issue_number = _destination(ctx)

    snapshot_id, sha, content = reporting.persist_snapshot(
        conn, ctx.mode, s.report_timezone
    )
    body = reporting.render_markdown(content)
    try:
        observed = ctx.clients.report_sink.publish_snapshot(
            repo, issue_number, body
        )
    except RateLimited as exc:
        conn.execute(
            """INSERT INTO publication_records
               (snapshot_id, mode, destination_repo, destination_issue_number,
                status, detail, created_at)
               VALUES (?, ?, ?, ?, 'unknown', ?, ?)""",
            (snapshot_id, ctx.mode, repo, issue_number,
             f"throttled: {exc}", db.now()),
        )
        raise jobs.RetryLater(exc.retry_after, "report publish throttled")
    except Exception as exc:
        with db.transaction(conn):
            conn.execute(
                """INSERT INTO publication_records
                   (snapshot_id, mode, destination_repo,
                    destination_issue_number, status, detail, created_at)
                   VALUES (?, ?, ?, ?, 'failed', ?, ?)""",
                (snapshot_id, ctx.mode, repo, issue_number,
                 str(exc)[:500], db.now()),
            )
            audit(
                conn, action="publish_failed", mode=ctx.mode,
                task_id=payload.get("task_id"), detail=str(exc)[:500],
            )
        raise

    with db.transaction(conn):
        conn.execute(
            """INSERT INTO publication_records
               (snapshot_id, mode, destination_repo, destination_issue_number,
                status, observed_body_hash, detail, external_ref, created_at)
               VALUES (?, ?, ?, ?, 'confirmed', ?, ?, ?, ?)""",
            (snapshot_id, ctx.mode, repo, issue_number, observed,
             "snapshot written to fixed report-source issue",
             f"{repo}#{issue_number}", db.now()),
        )
        conn.execute(
            "UPDATE control SET last_publish_at = ? WHERE id = 1", (db.now(),)
        )
        audit(
            conn, action="report_published", mode=ctx.mode,
            task_id=payload.get("task_id"),
            detail=f"snapshot {sha[:12]} -> {repo}#{issue_number} "
                   f"(observed {observed})",
        )
