"""Post-delivery PR watcher — closes the review loop (F06).

The repair session's job ends at ``agent_finished``; the PR's life
continues: humans review, request changes, merge, or close it. This
handler polls every tracked PR whose review dimension is still open and
lands those facts — ``merged`` / ``closed_unmerged`` / ``approved`` /
``changes_requested`` — plus a head-move that re-opens validation for
re-verification. Scheduled by the worker on the scan cadence.
"""

from __future__ import annotations

import sqlite3

from .. import db
from ..clients.base import RateLimited
from ..transitions import (
    add_evidence,
    advance_review,
    audit,
    get_task,
    transition_task,
)
from .context import ServiceContext
from . import jobs

# Validation states a pushed head re-opens for re-verification. Deliberately
# not "no_pr"/"pr_found"/"checks_pending" — those are mid-pipeline and the
# running verify job detects head moves itself.
_REOPENABLE_VALIDATION = ("verified", "manually_verified", "checks_failed")

# Review states GitHub's review list can still move. "merged" and
# "closed_unmerged" are terminal by definition and never reach the watcher.
_OPEN_REVIEW = ("awaiting_review", "changes_requested")


def handle_watch_prs(ctx: ServiceContext, job: sqlite3.Row) -> None:
    conn = ctx.conn
    rows = conn.execute(
        """SELECT * FROM tasks
           WHERE mode = ? AND pr_number IS NOT NULL
             AND review NOT IN ('merged', 'closed_unmerged')
             AND disposition != 'deleted'""",
        (ctx.mode,),
    ).fetchall()
    for task in rows:
        try:
            _watch_task(ctx, task)
        except RateLimited:
            raise  # whole-job retry — honors the API's Retry-After
        except Exception as exc:  # per-task isolation: one bad PR read must
            # not stall the watch for everyone else
            with db.transaction(conn):
                audit(
                    conn, action="pr_watch_error", mode=ctx.mode,
                    task_id=task["id"],
                    detail=f"{type(exc).__name__}: {exc}"[:400],
                )


def _watch_task(ctx: ServiceContext, task: sqlite3.Row) -> None:
    conn, s = ctx.conn, ctx.settings
    pr = ctx.clients.github.get_pull_request(task["repo"], task["pr_number"])

    with db.transaction(conn):
        task = get_task(conn, task["id"])

        # Terminal PR state wins over everything below — a merged PR is
        # merged whether or not checks or reviews say anything else.
        if pr.state == "closed":
            target = "merged" if pr.merged else "closed_unmerged"
            detail = (
                f"merged by {pr.merged_by or 'a human'}"
                if pr.merged else "PR closed without merging"
            )
            advance_review(conn, task, target, detail=detail)
            add_evidence(
                conn, task_id=task["id"], mode=ctx.mode, kind="review",
                title=(f"PR merged by {pr.merged_by}"
                       if pr.merged and pr.merged_by
                       else "PR merged" if pr.merged
                       else "PR closed without merging"),
                body={"pr_number": pr.number, "pr_url": pr.url,
                      "merged_by": pr.merged_by, "merged_at": pr.merged_at},
                uri=pr.url, synthetic=task["synthetic"] == 1,
                verifier="github-verifier",
            )
            jobs.enqueue(
                conn, "publish_report", {"task_id": task["id"]},
                mode=ctx.mode,
                dedup_key=(
                    f"report:{ctx.mode}:{task['id']}:{task['pr_number']}:"
                    f"{target}"
                ),
                max_attempts=s.job_max_attempts,
            )
            return

        # A pushed head re-opens validation — the desk re-verifies the new
        # head rather than letting a verified badge ride on stale commits.
        if (
            task["validation"] in _REOPENABLE_VALIDATION
            and pr.head_sha
            and pr.head_sha != task["head_sha"]
        ):
            moved_from = task["head_sha"]
            conn.execute(
                "UPDATE tasks SET head_sha = ?, updated_at = ? WHERE id = ?",
                (pr.head_sha, db.now(), task["id"]),
            )
            transition_task(
                conn, task, "validation", "checks_pending",
                detail=f"head moved {(moved_from or '?')[:12]} -> "
                       f"{pr.head_sha[:12]}; re-verifying",
            )
            task = get_task(conn, task["id"])
            jobs.enqueue(
                conn, "verify_task",
                {"task_id": task["id"], "pr_number": task["pr_number"],
                 "head_sha": pr.head_sha, "verify_attempt": 0},
                mode=ctx.mode, delay=s.poll_interval_seconds,
                dedup_key=(
                    f"verify:{ctx.mode}:{task['id']}:{task['pr_number']}:"
                    f"{pr.head_sha}"
                ),
            )

        # Human review decisions: GitHub keeps every submitted review, so
        # collapse to the latest substantive review per author — a stale
        # APPROVED superseded by that author's CHANGES_REQUESTED must not
        # keep the task green.
        if task["review"] in _OPEN_REVIEW:
            reviews = ctx.clients.github.list_pr_reviews(
                task["repo"], task["pr_number"]
            )
            latest: dict[str, str] = {}
            for rv in sorted(reviews, key=lambda r: r.submitted_at):
                if rv.state in ("APPROVED", "CHANGES_REQUESTED"):
                    latest[rv.author] = rv.state
            states = set(latest.values())
            decision = (
                "changes_requested" if "CHANGES_REQUESTED" in states
                else "approved" if "APPROVED" in states else None
            )
            if decision and decision != task["review"]:
                # Reachable either directly or via awaiting_review — e.g.
                # approval arriving while changes are still requested.
                advance_review(
                    conn, task, decision,
                    detail="GitHub review decision observed by watcher",
                )
                jobs.enqueue(
                    conn, "publish_report", {"task_id": task["id"]},
                    mode=ctx.mode,
                    dedup_key=(
                        f"report:{ctx.mode}:{task['id']}:{task['pr_number']}:"
                        f"{decision}"
                    ),
                    max_attempts=s.job_max_attempts,
                )
