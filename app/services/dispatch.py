"""Dispatch — create a bounded Devin API session for an approved task.

PRD dispatch rules implemented here:

* A creation intent (attempt row with a stable correlation tag) is persisted
  *before* the Devin call.
* A returned session ID is stored before any follow-up work.
* An ambiguous create (e.g. timeout after submission) is never blindly
  retried: the task moves to ``creation_unknown`` and a reconcile job
  searches sessions by correlation tag.
* Admission is bounded by MAX_ACTIVE_SESSIONS and the configured ACU cap;
  the operator pause flag blocks new dispatch while polling continues.
"""

from __future__ import annotations

import hashlib
import sqlite3

from .. import db
from ..clients.base import (
    AmbiguousCreation,
    DevinSessionSpec,
    IssueNotFound,
    RateLimited,
)
from ..transitions import add_evidence, audit, get_task, transition_task
from .context import ServiceContext
from . import budget, jobs, scanner


def _next_attempt_number(conn: sqlite3.Connection, task_id: int) -> int:
    row = conn.execute(
        "SELECT COALESCE(MAX(attempt_number), 0) + 1 AS n FROM attempts "
        "WHERE task_id = ?",
        (task_id,),
    ).fetchone()
    return int(row["n"])


def _correlation_tag(mode: str, task_id: int, attempt: int) -> str:
    return f"repairdesk:{mode}:task-{task_id}:attempt-{attempt}"


def _verify_approval_intact(
    ctx: ServiceContext, task: sqlite3.Row
) -> str | None:
    """Re-verify intake facts before dispatch spends money.

    Returns a human-readable stop reason, or None when the stored approval
    still holds: the issue still resolves as open, its frozen snapshot is
    unchanged, the approval label is still present, and the latest label
    event for it remains an allowed approver's ``labeled`` (an ``unlabeled``
    or a different actor means the approval was withdrawn or superseded).
    """
    s = ctx.settings
    try:
        issue = ctx.clients.github.get_issue(
            task["repo"], task["issue_number"]
        )
    except IssueNotFound:
        return "issue no longer resolves (closed or deleted)"
    if issue.is_pull_request:
        return "issue resolved as a pull-request object"
    if issue.state != "open":
        return f"issue state is {issue.state!r}, not open"
    # Approval withdrawal is checked before content drift: the snapshot hash
    # includes labels, so a removed label would otherwise misreport as a
    # content change instead of a withdrawn approval.
    labels = ctx.clients.github.get_issue_labels(
        task["repo"], task["issue_number"]
    )
    if s.approval_issue_label not in labels:
        return f"approval label {s.approval_issue_label} was removed"
    events = [
        e
        for e in ctx.clients.github.list_label_events(
            task["repo"], task["issue_number"]
        )
        if e.label == s.approval_issue_label
    ]
    latest = events[-1] if events else None
    if latest is None or latest.event != "labeled":
        return "latest approval-label event is an unlabeling"
    if latest.actor not in s.github_allowed_approvers:
        return (
            f"latest approval actor {latest.actor!r} not in "
            "GITHUB_ALLOWED_APPROVERS"
        )
    current_hash = scanner.issue_snapshot_hash(issue)
    stored_hash = task["issue_snapshot_hash"] or ""
    if current_hash != stored_hash:
        return (
            f"accepted snapshot changed "
            f"({stored_hash[:12]} -> {current_hash[:12]})"
        )
    return None


def _stop_for_review(
    ctx: ServiceContext, task: sqlite3.Row, reason: str
) -> None:
    """'Stop for review' — the accepted facts changed before dispatch."""
    with db.transaction(ctx.conn):
        ctx.conn.execute(
            "UPDATE approval_receipts SET processing_state = 'rejected' "
            "WHERE task_id = ? AND processing_state = 'accepted'",
            (task["id"],),
        )
        audit(
            ctx.conn, action="dispatch_review_blocked", mode=ctx.mode,
            task_id=task["id"], detail=reason,
        )
        transition_task(
            ctx.conn, task, "disposition", "blocked",
            detail=f"stopped for review: {reason}",
        )
        add_evidence(
            ctx.conn, task_id=task["id"], mode=ctx.mode, kind="note",
            title=f"Dispatch stopped for review: {reason}",
            synthetic=task["synthetic"] == 1,
        )


def _active_session_count(conn: sqlite3.Connection, mode: str) -> int:
    row = conn.execute(
        """SELECT COUNT(*) AS n FROM tasks
           WHERE mode = ? AND execution IN
           ('dispatching', 'working', 'needs_input', 'approval_required',
            'suspended')""",
        (mode,),
    ).fetchone()
    return int(row["n"])


def handle_dispatch(ctx: ServiceContext, job: sqlite3.Row) -> None:
    conn, s = ctx.conn, ctx.settings
    payload = db.loads(job["payload_json"])
    task = get_task(conn, payload["task_id"])

    if task["disposition"] not in ("active",):
        audit(conn, action="dispatch_blocked", mode=ctx.mode,
              task_id=task["id"],
              detail=f"disposition is {task['disposition']}")
        return
    if task["execution"] not in ("queued", "dispatching", "failed"):
        # Already past dispatch — e.g. a replayed job after restart.
        audit(conn, action="dispatch_skipped", mode=ctx.mode,
              task_id=task["id"],
              detail=f"execution is {task['execution']}")
        return
    if conn.execute(
        "SELECT 1 FROM approval_receipts WHERE task_id = ? AND "
        "processing_state = 'accepted'",
        (task["id"],),
    ).fetchone() is None:
        audit(conn, action="dispatch_blocked", mode=ctx.mode,
              task_id=task["id"], detail="no accepted approval receipt")
        return

    from ..transitions import is_paused

    if is_paused(conn):
        raise jobs.RetryLater(
            s.poll_interval_seconds,
            "dispatch paused by operator; polling continues",
        )

    if task["devin_session_id"]:
        audit(conn, action="dispatch_reattach", mode=ctx.mode,
              task_id=task["id"],
              detail=f"session {task['devin_session_id']} already recorded")
        jobs.enqueue(
            conn, "poll_session",
            {"task_id": task["id"], "session_id": task["devin_session_id"]},
            mode=ctx.mode, delay=s.poll_interval_seconds,
            dedup_key=f"poll:{task['devin_session_id']}",
            max_attempts=s.job_max_attempts * 10,
        )
        return

    # Re-verify before spending: the frozen snapshot and the approval must
    # still hold, otherwise the task stops for review instead of dispatching.
    if task["execution"] == "queued":
        stop_reason = _verify_approval_intact(ctx, task)
        if stop_reason:
            _stop_for_review(ctx, task, stop_reason)
            return

    if _active_session_count(conn, ctx.mode) >= s.max_active_sessions:
        raise jobs.RetryLater(
            s.poll_interval_seconds,
            "MAX_ACTIVE_SESSIONS reached; serial dispatch",
        )

    # Reservation-based admission: hold REPAIR_ACU_LIMIT before creating.
    if task["execution"] == "queued":
        budget.check_admission(ctx, task["id"], float(s.repair_acu_limit))

    # Persist the creation intent BEFORE calling Devin.
    attempt_number = _next_attempt_number(conn, task["id"])
    tag = _correlation_tag(ctx.mode, task["id"], attempt_number)
    prompt_body = db.dumps(
        {
            "issue_url": task["issue_url"],
            "issue_snapshot": db.loads(task["issue_snapshot_json"], {}),
            "playbook_id": s.devin_remediation_playbook_id or None,
            "knowledge_ids": list(s.devin_knowledge_ids),
            "acu_limit": s.repair_acu_limit,
        }
    )
    with db.transaction(conn):
        cur = conn.execute(
            """INSERT INTO attempts
               (task_id, mode, attempt_number, correlation_tag, acu_limit,
                prompt_hash, context_hash, started_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                task["id"], ctx.mode, attempt_number, tag, s.repair_acu_limit,
                hashlib.sha256(prompt_body.encode()).hexdigest(),
                hashlib.sha256(
                    (s.devin_remediation_playbook_id
                     + ",".join(s.devin_knowledge_ids)).encode()
                ).hexdigest(),
                db.now(),
            ),
        )
        attempt_id = int(cur.lastrowid)
        budget.reserve(
            conn, task["id"], ctx.mode, float(s.repair_acu_limit),
            scope="session", note=f"attempt {attempt_number}",
        )
        transition_task(conn, task, "execution", "dispatching",
                        detail=f"attempt {attempt_number}, tag {tag}")

    spec = DevinSessionSpec(
        repo=task["repo"],
        issue_number=task["issue_number"],
        title=task["issue_title"],
        correlation_tag=tag,
        issue_snapshot_json=task["issue_snapshot_json"] or "",
        base_sha=task["base_sha"] or "",
        acu_limit=s.repair_acu_limit,
        playbook_id=s.devin_remediation_playbook_id,
        knowledge_ids=s.devin_knowledge_ids,
        repo_ref=s.devin_repo_ref,
    )
    try:
        session = ctx.clients.devin.create_session(spec)
    except AmbiguousCreation:
        with db.transaction(conn):
            task = get_task(conn, task["id"])
            transition_task(conn, task, "execution", "creation_unknown",
                            action="creation_unknown",
                            detail="create_session outcome ambiguous; "
                                   "reconciling by correlation tag")
            add_evidence(
                conn, task_id=task["id"], attempt_id=attempt_id,
                mode=ctx.mode, kind="note",
                title="Session creation ambiguous — searching by "
                      "correlation tag instead of retrying",
                synthetic=task["synthetic"] == 1,
            )
            conn.execute(
                "UPDATE attempts SET raw_status = 'creation_unknown', "
                "finished_at = ? WHERE id = ?",
                (db.now(), attempt_id),
            )
            jobs.enqueue(
                conn, "reconcile_creation",
                {"task_id": task["id"], "correlation_tag": tag},
                mode=ctx.mode, delay=0.2,
                dedup_key=f"reconcile:{tag}",
            )
        return
    except RateLimited:
        conn.execute(
            "UPDATE attempts SET raw_status = 'throttled', finished_at = ? "
            "WHERE id = ?",
            (db.now(), attempt_id),
        )
        budget.release(conn, task["id"])
        transition_task(
            conn, get_task(conn, task["id"]), "execution", "queued",
            detail="create_session throttled; released reservation",
        )
        raise

    with db.transaction(conn):
        conn.execute(
            """UPDATE tasks SET devin_session_id = ?, devin_session_url = ?,
               updated_at = ? WHERE id = ?""",
            (session.session_id, session.url, db.now(), task["id"]),
        )
        conn.execute(
            """UPDATE attempts SET session_id = ?, session_url = ?,
               raw_status = ?, last_seen_at = ?, acu_used = ? WHERE id = ?""",
            (session.session_id, session.url, session.status,
             db.now(), session.acu_used, attempt_id),
        )
        task = get_task(conn, task["id"])
        transition_task(conn, task, "execution", "working",
                        detail=f"session {session.session_id} created")
        add_evidence(
            conn, task_id=task["id"], attempt_id=attempt_id, mode=ctx.mode,
            kind="session", title=f"Devin session {session.session_id} created",
            uri=session.url, synthetic=task["synthetic"] == 1,
        )
        jobs.enqueue(
            conn, "poll_session",
            {"task_id": task["id"], "session_id": session.session_id,
             "attempt_id": attempt_id},
            mode=ctx.mode, delay=s.poll_interval_seconds,
            dedup_key=f"poll:{session.session_id}",
            max_attempts=s.job_max_attempts * 10,
        )


def handle_reconcile(ctx: ServiceContext, job: sqlite3.Row) -> None:
    conn, s = ctx.conn, ctx.settings
    payload = db.loads(job["payload_json"])
    task = get_task(conn, payload["task_id"])
    session = ctx.clients.devin.find_session_by_correlation_tag(
        payload["correlation_tag"]
    )
    with db.transaction(conn):
        if session is None:
            if job["attempt_count"] >= 5:
                transition_task(conn, task, "execution", "failed",
                                detail="no session found for correlation tag; "
                                       "operator resolution required")
                transition_task(conn, task, "disposition", "failed")
                conn.execute(
                    "UPDATE tasks SET last_error = ? WHERE id = ?",
                    ("creation could not be reconciled", task["id"]),
                )
                return
            raise jobs.RetryLater(
                0.5, "session not yet visible under correlation tag"
            )
        conn.execute(
            """UPDATE tasks SET devin_session_id = ?, devin_session_url = ?,
               updated_at = ? WHERE id = ?""",
            (session.session_id, session.url, db.now(), task["id"]),
        )
        conn.execute(
            "UPDATE attempts SET session_id = ?, session_url = ?, "
            "raw_status = 'reconciled', last_seen_at = ? WHERE task_id = ? "
            "AND correlation_tag = ?",
            (session.session_id, session.url, db.now(), task["id"],
             payload["correlation_tag"]),
        )
        task = get_task(conn, task["id"])
        transition_task(conn, task, "execution", "working",
                        action="creation_reconciled",
                        detail=f"reattached session {session.session_id} via "
                               "correlation tag")
        jobs.enqueue(
            conn, "poll_session",
            {"task_id": task["id"], "session_id": session.session_id},
            mode=ctx.mode, delay=s.poll_interval_seconds,
            dedup_key=f"poll:{session.session_id}",
            max_attempts=s.job_max_attempts * 10,
        )
