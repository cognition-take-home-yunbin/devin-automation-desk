"""Operator actions — message / stop / retry / reconcile (milestone B).

Each CLI command enqueues a durable job; handlers run inside the same worker
loop as everything else, so a ``stop`` issued while a poll is in flight is
serialized by the job claim, not by luck.

Policy notes:

* ``stop`` is permanent: the session is terminated with ``archive=true`` and
  the task outcome is preserved (a delivered task stays delivered; an active
  one is cancelled). The cleanup ledger records the termination.
* ``retry`` is only ever operator-driven, requires a recorded reason, refuses
  to run while a session or PR already exists, and creates a new attempt with
  a new correlation tag. Sessions are never auto-resumed.
* ``message`` is a budgeted follow-up: capacity and remaining cap are checked
  before the API call, and it never targets a stopped/terminated session.
"""

from __future__ import annotations

import sqlite3

from .. import db
from ..transitions import add_evidence, audit, get_task, transition_task
from .context import ServiceContext
from . import budget, jobs


def _record_cleanup(
    conn: sqlite3.Connection,
    task: sqlite3.Row,
    action: str,
    *,
    actor: str = "system",
    session_id: str | None = None,
    detail: str | None = None,
) -> None:
    conn.execute(
        """INSERT INTO cleanup_records
           (task_id, mode, action, actor, session_id, detail, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (task["id"], task["mode"], action, actor,
         session_id or task["devin_session_id"], detail, db.now()),
    )


# -- send_message ---------------------------------------------------------------

def handle_send_message(ctx: ServiceContext, job: sqlite3.Row) -> None:
    conn = ctx.conn
    payload = db.loads(job["payload_json"])
    task = get_task(conn, payload["task_id"])
    text = (payload.get("text") or "").strip()
    actor = payload.get("actor") or "operator"

    with db.transaction(conn):
        if not text:
            audit(conn, action="message_rejected", mode=ctx.mode,
                  task_id=task["id"], source=actor, detail="empty message")
            return
        session_id = task["devin_session_id"]
        if not session_id or task["execution"] in (
            "stop_requested", "stopped", "failed",
        ):
            audit(conn, action="message_rejected", mode=ctx.mode,
                  task_id=task["id"], source=actor,
                  detail=f"no live session to message "
                         f"(execution={task['execution']})")
            return
        # Budgeted API follow-up — verify remaining capacity first.
        if not budget.can_follow_up(ctx, task["id"]):
            return

    ctx.clients.devin.message_session(session_id, text)
    with db.transaction(conn):
        audit(conn, action="message_sent", mode=ctx.mode,
              task_id=task["id"], source=actor,
              detail=f"sent to session {session_id}")
        add_evidence(
            conn, task_id=task["id"], mode=ctx.mode, kind="note",
            title=f"Operator message to {session_id}",
            body={"text": text}, synthetic=task["synthetic"] == 1,
        )
        # The session may now be working again — make sure a poll is queued.
        jobs.enqueue(
            conn, "poll_session",
            {"task_id": task["id"], "session_id": session_id},
            mode=ctx.mode, delay=ctx.settings.poll_interval_seconds,
            dedup_key=f"poll:{session_id}",
            max_attempts=ctx.settings.job_max_attempts * 10,
        )


# -- stop_task ------------------------------------------------------------------

def handle_stop_task(ctx: ServiceContext, job: sqlite3.Row) -> None:
    conn = ctx.conn
    payload = db.loads(job["payload_json"])
    reason = (payload.get("reason") or "").strip() or "operator stop"
    actor = payload.get("actor") or "operator"
    task = get_task(conn, payload["task_id"])

    with db.transaction(conn):
        if task["execution"] == "stopped":
            audit(conn, action="stop_skipped", mode=ctx.mode,
                  task_id=task["id"], source=actor,
                  detail="already stopped")
            return
        if task["execution"] in ("failed", "agent_finished"):
            audit(conn, action="stop_skipped", mode=ctx.mode,
                  task_id=task["id"], source=actor,
                  detail=f"nothing to terminate "
                         f"(execution={task['execution']})")
            return
        task = transition_task(
            conn, task, "execution", "stop_requested",
            action="stop_requested", detail=reason,
        )
        audit(conn, action="stop_requested", mode=ctx.mode,
              task_id=task["id"], source=actor, detail=reason)

    session_id = task["devin_session_id"]
    if session_id:
        ctx.clients.devin.stop_session(session_id, archive=True)

    with db.transaction(conn):
        task = get_task(conn, task["id"])
        # Settle at observed spend — the last poll recorded the session's
        # acu_used on the attempt row; absent any observation the full
        # reservation stands as the conservative charge.
        latest = conn.execute(
            "SELECT acu_used FROM attempts WHERE task_id = ? "
            "ORDER BY attempt_number DESC LIMIT 1",
            (task["id"],),
        ).fetchone()
        observed = latest["acu_used"] if latest else None
        budget.consume(conn, task["id"], observed)
        transition_task(
            conn, task, "execution", "stopped",
            action="session_terminated",
            detail="terminated with archive=true",
        )
        audit(conn, action="session_terminated", mode=ctx.mode,
              task_id=task["id"], source=actor, detail=reason)
        task = get_task(conn, task["id"])
        if task["disposition"] == "active":
            transition_task(
                conn, task, "disposition", "cancelled",
                detail="operator stop",
            )
        conn.execute(
            "UPDATE tasks SET cleanup_state = 'terminated', updated_at = ? "
            "WHERE id = ?",
            (db.now(), task["id"]),
        )
        _record_cleanup(
            conn, task, "terminated", actor=actor,
            session_id=session_id, detail=reason,
        )
        add_evidence(
            conn, task_id=task["id"], mode=ctx.mode, kind="note",
            title=f"Session terminated (archive=true) — {reason}",
            body={"reason": reason},
            synthetic=task["synthetic"] == 1,
        )


# -- retry_task -----------------------------------------------------------------

def handle_retry_task(ctx: ServiceContext, job: sqlite3.Row) -> None:
    conn = ctx.conn
    payload = db.loads(job["payload_json"])
    reason = (payload.get("reason") or "").strip()
    actor = payload.get("actor") or "operator"
    task = get_task(conn, payload["task_id"])

    with db.transaction(conn):
        if not reason:
            audit(conn, action="retry_rejected", mode=ctx.mode,
                  task_id=task["id"], source=actor,
                  detail="an operator reason is required")
            return
        if task["pr_number"]:
            audit(conn, action="retry_rejected", mode=ctx.mode,
                  task_id=task["id"], source=actor,
                  detail=f"PR #{task['pr_number']} already exists — "
                         "retry would duplicate work")
            return
        if task["execution"] not in ("failed", "stopped", "agent_finished"):
            audit(conn, action="retry_rejected", mode=ctx.mode,
                  task_id=task["id"], source=actor,
                  detail=f"execution is {task['execution']}; a live session "
                         "must not be retried — stop it first")
            return
        if not budget.can_follow_up(ctx, task["id"]):
            return

    # Existing-session check beyond local rows: a session recorded under the
    # latest correlation tag may still be alive remotely.
    session_id = task["devin_session_id"]
    if session_id:
        try:
            session = ctx.clients.devin.get_session(session_id)
        except Exception:  # noqa: BLE001 - gone remotely counts as no session
            session = None
        if session is not None and session.status not in (
            "stopped", "failed", "finished",
        ):
            with db.transaction(conn):
                audit(conn, action="retry_rejected", mode=ctx.mode,
                      task_id=task["id"], source=actor,
                      detail=f"session {session_id} still {session.status}")
            return

    with db.transaction(conn):
        task = get_task(conn, task["id"])
        transition_task(
            conn, task, "execution", "queued",
            action="operator_retry", detail=reason,
        )
        task = get_task(conn, task["id"])
        if task["disposition"] != "active":
            transition_task(
                conn, task, "disposition", "active",
                detail=f"operator retry: {reason}",
            )
        conn.execute(
            "UPDATE approval_receipts SET processing_state = 'accepted' "
            "WHERE task_id = ?",
            (task["id"],),
        )
        # The new attempt gets a fresh correlation tag and a fresh session —
        # the old session id must be detached or dispatch would reattach it.
        # Cleanup state is per-session-lifecycle: the new session starts
        # 'pending' again (its eventual 'kept'/'terminated' is recorded then).
        conn.execute(
            "UPDATE tasks SET devin_session_id = NULL, "
            "devin_session_url = NULL, cleanup_state = 'pending', "
            "updated_at = ? WHERE id = ?",
            (db.now(), task["id"]),
        )
        add_evidence(
            conn, task_id=task["id"], mode=ctx.mode, kind="note",
            title=f"Operator retry requested: {reason}",
            body={"reason": reason}, synthetic=task["synthetic"] == 1,
        )
        jobs.enqueue(
            conn, "dispatch_task", {"task_id": task["id"]},
            mode=ctx.mode,
            dedup_key=f"retry:{ctx.mode}:{task['id']}:{db.now()}",
            max_attempts=ctx.settings.job_max_attempts,
        )
        audit(conn, action="retry_queued", mode=ctx.mode,
              task_id=task["id"], source=actor, detail=reason)


# -- reconcile_task ---------------------------------------------------------------

def handle_reconcile_task(ctx: ServiceContext, job: sqlite3.Row) -> None:
    """Operator-driven reconcile: locate the session for the latest attempt's
    correlation tag when the local row lost track of it."""
    conn = ctx.conn
    payload = db.loads(job["payload_json"])
    task = get_task(conn, payload["task_id"])
    attempt = conn.execute(
        "SELECT * FROM attempts WHERE task_id = ? "
        "ORDER BY attempt_number DESC LIMIT 1",
        (task["id"],),
    ).fetchone()
    if attempt is None:
        with db.transaction(conn):
            audit(conn, action="reconcile_skipped", mode=ctx.mode,
                  task_id=task["id"], detail="no attempts recorded")
        return
    session = ctx.clients.devin.find_session_by_correlation_tag(
        attempt["correlation_tag"]
    )
    with db.transaction(conn):
        if session is None:
            audit(conn, action="reconcile_miss", mode=ctx.mode,
                  task_id=task["id"],
                  detail=f"no session under tag "
                         f"{attempt['correlation_tag']}")
            return
        conn.execute(
            "UPDATE tasks SET devin_session_id = ?, devin_session_url = ?, "
            "updated_at = ? WHERE id = ?",
            (session.session_id, session.url, db.now(), task["id"]),
        )
        conn.execute(
            "UPDATE attempts SET session_id = ?, session_url = ?, "
            "raw_status = 'reconciled', last_seen_at = ? WHERE id = ?",
            (session.session_id, session.url, db.now(), attempt["id"]),
        )
        task = get_task(conn, task["id"])
        if task["execution"] in ("creation_unknown", "dispatching", "queued"):
            transition_task(
                conn, task, "execution", "working",
                action="creation_reconciled",
                detail=f"operator reconcile found {session.session_id}",
            )
        jobs.enqueue(
            conn, "poll_session",
            {"task_id": task["id"], "session_id": session.session_id},
            mode=ctx.mode, delay=ctx.settings.poll_interval_seconds,
            dedup_key=f"poll:{session.session_id}",
            max_attempts=ctx.settings.job_max_attempts * 10,
        )
