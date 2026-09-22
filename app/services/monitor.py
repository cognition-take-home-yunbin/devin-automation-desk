"""Session monitor — honestly map provider status onto task state.

A running session can be waiting for input, awaiting approval, suspended, or
finished; unfamiliar ``status`` values are preserved as ``unknown`` rather
than treated as success (PRD F06).
"""

from __future__ import annotations

import sqlite3

from .. import db
from ..clients.base import RateLimited
from ..transitions import add_evidence, audit, get_task, transition_task
from .context import ServiceContext
from . import budget, jobs
from .operator import _record_cleanup

_STATUS_TO_EXECUTION = {
    "working": "working",
    "running": "working",
    "needs_input": "needs_input",
    "approval_required": "approval_required",
    "suspended": "suspended",
    "finished": "agent_finished",
    "succeeded": "agent_finished",
    "failed": "failed",
    "stopped": "stopped",
}


def handle_poll(ctx: ServiceContext, job: sqlite3.Row) -> None:
    conn, s = ctx.conn, ctx.settings
    payload = db.loads(job["payload_json"])
    task = get_task(conn, payload["task_id"])
    session = ctx.clients.devin.get_session(payload["session_id"])

    attempt_id = payload.get("attempt_id")
    if attempt_id is None:
        row = conn.execute(
            "SELECT id FROM attempts WHERE task_id = ? AND session_id = ? "
            "ORDER BY attempt_number DESC LIMIT 1",
            (task["id"], session.session_id),
        ).fetchone()
        attempt_id = row["id"] if row else None
    if attempt_id is not None:
        conn.execute(
            "UPDATE attempts SET raw_status = ?, raw_detail = ?, "
            "last_seen_at = ?, acu_used = COALESCE(?, acu_used) WHERE id = ?",
            (session.status, session.status_detail, db.now(),
             session.acu_used, attempt_id),
        )

    mapped = _STATUS_TO_EXECUTION.get(session.status)
    if mapped is None:
        # Preserve unfamiliar values — uncertainty is not failure or success.
        add_evidence(
            conn, task_id=task["id"], attempt_id=attempt_id, mode=ctx.mode,
            kind="note", title=f"Unrecognized session status "
                               f"{session.status!r}; preserved as unknown",
            body={"status": session.status,
                  "status_detail": session.status_detail},
            synthetic=task["synthetic"] == 1,
        )
        raise jobs.RetryLater(
            max(s.poll_interval_seconds, 1.0), "unknown session status"
        )

    if mapped in ("working",):
        raise jobs.RetryLater(s.poll_interval_seconds, "session still working")

    if mapped in ("needs_input", "approval_required", "suspended"):
        transition_task(
            conn, task, "execution", mapped, action=f"session_{mapped}",
            detail=session.status_detail or mapped,
        )
        if mapped == "needs_input":
            transition_task(conn, get_task(conn, task["id"]), "disposition",
                            "blocked", detail="waiting on human input")
            add_evidence(
                conn, task_id=task["id"], attempt_id=attempt_id,
                mode=ctx.mode, kind="note",
                title="Session requested human input",
                body=session.notes or {"status_detail": session.status_detail},
                uri=session.url, synthetic=task["synthetic"] == 1,
            )
        return

    if mapped == "stopped":
        # Remote termination (operator stop via API/webapp). The task outcome
        # is preserved — a delivered task stays delivered; an active one is
        # honestly cancelled.
        budget.consume(conn, task["id"], session.acu_used)
        transition_task(conn, task, "execution", "stopped",
                        detail=session.status_detail or "session terminated")
        task = get_task(conn, task["id"])
        if task["disposition"] == "active":
            transition_task(conn, task, "disposition", "cancelled",
                            detail="session terminated remotely")
        conn.execute(
            "UPDATE tasks SET cleanup_state = 'terminated', updated_at = ? "
            "WHERE id = ? AND cleanup_state = 'pending'",
            (db.now(), task["id"]),
        )
        _record_cleanup(
            conn, task, "terminated", actor="remote",
            session_id=session.session_id,
            detail="session terminated outside the desk",
        )
        return

    if mapped == "failed":
        budget.consume(conn, task["id"], session.acu_used)
        transition_task(conn, task, "execution", "failed",
                        detail=session.status_detail or session.status)
        transition_task(conn, get_task(conn, task["id"]), "disposition",
                        "failed")
        conn.execute("UPDATE tasks SET last_error = ? WHERE id = ?",
                     (session.status_detail or "session failed", task["id"]))
        conn.execute(
            "UPDATE tasks SET cleanup_state = 'terminated', updated_at = ? "
            "WHERE id = ? AND cleanup_state = 'pending'",
            (db.now(), task["id"]),
        )
        _record_cleanup(
            conn, task, "terminated", actor="provider",
            session_id=session.session_id,
            detail="session ended in a failed state",
        )
        return

    # agent_finished — settle the reservation with observed ACU spend,
    # record the PR and hand off to verification.
    with db.transaction(conn):
        budget.consume(conn, task["id"], session.acu_used)
        if session.pr_url:
            conn.execute(
                """UPDATE tasks SET pr_number = ?, pr_url = ?, head_sha = ?,
                   updated_at = ? WHERE id = ?""",
                (session.pr_number, session.pr_url, session.pr_head_sha,
                 db.now(), task["id"]),
            )
        task = get_task(conn, task["id"])
        transition_task(conn, task, "execution", "agent_finished",
                        detail="session reported finished")
        # Handoff policy: keep the session available — the planned native
        # Slack conversation and the verification update still need it.
        conn.execute(
            "UPDATE tasks SET cleanup_state = 'kept', updated_at = ? "
            "WHERE id = ? AND cleanup_state = 'pending'",
            (db.now(), task["id"]),
        )
        _record_cleanup(
            conn, task, "kept", actor="system",
            session_id=session.session_id,
            detail="retained for native conversation + verification update",
        )
        if session.pr_number:
            transition_task(conn, task, "validation", "pr_found",
                            detail=f"PR #{session.pr_number}")
            add_evidence(
                conn, task_id=task["id"], attempt_id=attempt_id,
                mode=ctx.mode, kind="artifact",
                title=f"Pull request opened: {session.pr_url}",
                uri=session.pr_url, synthetic=task["synthetic"] == 1,
            )
            jobs.enqueue(
                conn, "verify_task",
                {"task_id": task["id"], "pr_number": session.pr_number,
                 "head_sha": session.pr_head_sha, "verify_attempt": 0},
                mode=ctx.mode,
                dedup_key=f"verify:{ctx.mode}:{task['id']}:{session.pr_number}",
            )
        else:
            transition_task(conn, task, "validation", "no_pr",
                            detail="session finished without a PR")
            transition_task(conn, get_task(conn, task["id"]), "disposition",
                            "blocked", detail="finished without a PR")
