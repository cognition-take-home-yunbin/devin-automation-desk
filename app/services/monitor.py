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
    "new": "working",
    "claimed": "working",
    "resuming": "working",
    "resume_requested": "working",
    "needs_input": "needs_input",
    "approval_required": "approval_required",
    "suspended": "suspended",
    "sleep": "suspended",
    "finished": "agent_finished",
    "succeeded": "agent_finished",
    "exit": "agent_finished",
    "failed": "failed",
    "error": "failed",
    "expired": "failed",
    "stopped": "stopped",
}


def _map_session(session) -> str | None:
    """status/status_detail pair -> task execution, honestly resolved.

    A session that finished its work and went to sleep arrives as
    ``suspended`` — sometimes with detail ``finished``, sometimes
    ``inactivity``/``sleep``. ``suspended`` alone keeps the task waiting
    (it can be resumed); but once the session has produced its
    deliverable the repair is effectively complete — verification can
    begin while it sleeps, and the session is retained for the native
    conversation regardless. Sleep *without* a deliverable stays
    suspended rather than being treated as success.
    """
    if session.status == "suspended":
        if session.status_detail == "finished" or session.pr_number:
            return "agent_finished"
        return "suspended"
    return _STATUS_TO_EXECUTION.get(session.status)


def handle_poll(ctx: ServiceContext, job: sqlite3.Row) -> None:
    conn, s = ctx.conn, ctx.settings
    payload = db.loads(job["payload_json"])
    task = get_task(conn, payload["task_id"])
    session = ctx.clients.devin.get_session(payload["session_id"])

    # The session record's acus_consumed field is not populated in practice
    # (finished sessions with merged PRs report 0.0), so authoritative
    # per-session usage comes from the consumption endpoint; fall back to
    # the session field, then to None (renders "unknown", never a false 0).
    acu = (
        ctx.clients.devin.session_acu_usage(session.session_id)
        or session.acu_used
        or None
    )

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
             acu, attempt_id),
        )

    mapped = _map_session(session)
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
                # A session-reported question is the agent's assertion —
                # stored and shown separately from verified facts.
                verifier="agent",
            )
        return

    if mapped == "stopped":
        # Remote termination (operator stop via API/webapp). The task outcome
        # is preserved — a delivered task stays delivered; an active one is
        # honestly cancelled.
        budget.consume(conn, task["id"], acu)
        _queue_acu_refresh(conn, ctx.mode, task["id"], attempt_id,
                           session.session_id)
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
        budget.consume(conn, task["id"], acu)
        _queue_acu_refresh(conn, ctx.mode, task["id"], attempt_id,
                           session.session_id)
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
        budget.consume(conn, task["id"], acu)
        _queue_acu_refresh(conn, ctx.mode, task["id"], attempt_id,
                           session.session_id)
        if session.pr_url:
            # Seed the PR fields from the agent's claim — but only before
            # verification. Once verified, task.head_sha is the
            # independently observed remote head; the session's stale
            # claimed head (it reports what it pushed, which verification
            # may have advanced past) must not write back over it. A
            # resumed session's genuinely-new head is picked up by the
            # watcher's remote read instead.
            if task["validation"] not in ("verified", "manually_verified"):
                conn.execute(
                    """UPDATE tasks SET pr_number = ?, pr_url = ?,
                       head_sha = ?, updated_at = ? WHERE id = ?""",
                    (session.pr_number, session.pr_url, session.pr_head_sha,
                     db.now(), task["id"]),
                )
        task = get_task(conn, task["id"])
        finish_detail = (
            "suspended session produced a PR"
            if session.status == "suspended" and session.pr_number
            and session.status_detail != "finished"
            else "session reported finished"
        )
        transition_task(conn, task, "execution", "agent_finished",
                        detail=finish_detail)
        # Handoff policy: keep the session available — the planned native
        # Slack conversation and the verification update still need it.
        # A resumed session can finish more than once — only the first
        # finish writes the cleanup record.
        cur = conn.execute(
            "UPDATE tasks SET cleanup_state = 'kept', updated_at = ? "
            "WHERE id = ? AND cleanup_state = 'pending'",
            (db.now(), task["id"]),
        )
        if cur.rowcount:
            _record_cleanup(
                conn, task, "kept", actor="system",
                session_id=session.session_id,
                detail="retained for native conversation + verification "
                       "update",
            )
        if session.pr_number:
            if task["validation"] in ("no_pr", "pr_found"):
                transition_task(conn, task, "validation", "pr_found",
                                detail=f"PR #{session.pr_number}")
            elif (
                task["pr_number"] == session.pr_number
                or task["validation"] != "verified"
            ):
                # A resumed session finished again carrying the same PR —
                # the new head is re-verified below via the head-scoped key.
                add_evidence(
                    conn, task_id=task["id"], attempt_id=attempt_id,
                    mode=ctx.mode, kind="note",
                    title="Resumed session finished again",
                    body={"validation": task["validation"],
                          "pr_number": session.pr_number,
                          "head_sha": session.pr_head_sha},
                    synthetic=task["synthetic"] == 1,
                )
            else:
                # A second, different PR after verification — the first one
                # stays canonical; the extra work is evidence, not a state
                # change (multiple PRs are not modelled).
                add_evidence(
                    conn, task_id=task["id"], attempt_id=attempt_id,
                    mode=ctx.mode, kind="flag",
                    title=f"Additional PR opened after verification: "
                          f"{session.pr_url}",
                    body={"pr_number": session.pr_number,
                          "pr_url": session.pr_url,
                          "canonical_pr": task["pr_number"]},
                    uri=session.pr_url,
                    synthetic=task["synthetic"] == 1,
                    verifier="agent",
                )
            add_evidence(
                conn, task_id=task["id"], attempt_id=attempt_id,
                mode=ctx.mode, kind="artifact",
                title=f"Pull request opened: {session.pr_url}",
                body={
                    # Agent-reported assertions — the verifier independently
                    # re-fetches all of these before 'verified' is granted.
                    "pr_number": session.pr_number,
                    "pr_url": session.pr_url,
                    "claimed_head_sha": session.pr_head_sha,
                },
                uri=session.pr_url, synthetic=task["synthetic"] == 1,
                verifier="agent",
            )
            jobs.enqueue(
                conn, "verify_task",
                {"task_id": task["id"], "pr_number": session.pr_number,
                 "head_sha": session.pr_head_sha, "verify_attempt": 0},
                mode=ctx.mode,
                # Head-scoped: re-pushes after verification must re-verify;
                # a static key would dedup the re-run away forever.
                dedup_key=(
                    f"verify:{ctx.mode}:{task['id']}:{session.pr_number}:"
                    f"{session.pr_head_sha or 'unknown'}"
                ),
            )
        elif task["pr_number"]:
            # A resumed session finished without a new PR while a PR is
            # already tracked — note it, keep the verified state.
            add_evidence(
                conn, task_id=task["id"], attempt_id=attempt_id,
                mode=ctx.mode, kind="note",
                title="Resumed session finished without a new PR",
                body={"canonical_pr": task["pr_number"]},
                synthetic=task["synthetic"] == 1,
            )
        else:
            transition_task(conn, task, "validation", "no_pr",
                            detail="session finished without a PR")
            transition_task(conn, get_task(conn, task["id"]), "disposition",
                            "blocked", detail="finished without a PR")


_ACU_REFRESH_DELAY = 900.0
_ACU_REFRESH_MAX_RETRIES = 4


def _queue_acu_refresh(
    conn: sqlite3.Connection,
    mode: str,
    task_id: int,
    attempt_id: int | None,
    session_id: str,
) -> None:
    """Consumption settles after the session ends — schedule a delayed
    refresh so the recorded spend reconciles to the billed value. One
    pending refresh per session; a *later* settle (resumed session) needs a
    fresh key — dedup keys are permanent."""
    if jobs.pending_exists(conn, "refresh_acu", "session_id", session_id):
        return
    jobs.enqueue(
        conn, "refresh_acu",
        {"task_id": task_id, "attempt_id": attempt_id,
         "session_id": session_id, "refresh_attempt": 0},
        mode=mode, delay=_ACU_REFRESH_DELAY,
        dedup_key=f"acuref:{mode}:{task_id}:{session_id}:{db.now()}",
    )


def handle_refresh_acu(ctx: ServiceContext, job: sqlite3.Row) -> None:
    """Re-read a finished session's consumption and reconcile the attempt +
    consumed reservation upward when the billed total exceeds the recorded
    one. Never writes down. Billing posts late, so a ``None`` read retries
    a bounded number of times before being recorded honestly as
    unavailable."""
    conn = ctx.conn
    payload = db.loads(job["payload_json"])
    usage = ctx.clients.devin.session_acu_usage(payload["session_id"])
    if usage is None:
        attempt = int(payload.get("refresh_attempt", 0)) + 1
        if attempt <= _ACU_REFRESH_MAX_RETRIES:
            jobs.enqueue(
                conn, "refresh_acu",
                {**payload, "refresh_attempt": attempt},
                mode=ctx.mode, delay=600.0,
                dedup_key=(
                    f"acuref:{ctx.mode}:{payload['task_id']}:"
                    f"{payload['session_id']}:{db.now()}"
                ),
            )
            return
        audit(
            conn, action="acu_unavailable", mode=ctx.mode,
            task_id=payload["task_id"],
            detail="consumption endpoint returned no usage after "
                   f"{_ACU_REFRESH_MAX_RETRIES} refresh retries; recorded "
                   "spend stays at last observed value",
        )
        return
    with db.transaction(conn):
        if payload.get("attempt_id") is not None:
            conn.execute(
                "UPDATE attempts SET acu_used = ? WHERE id = ? "
                "AND (acu_used IS NULL OR acu_used < ?)",
                (usage, payload["attempt_id"], usage),
            )
        conn.execute(
            "UPDATE budget_reservations SET amount = ? WHERE task_id = ? "
            "AND status = 'consumed' AND amount < ?",
            (usage, payload["task_id"], usage),
        )
