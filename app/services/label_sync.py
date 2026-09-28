"""Status-label sync — keep the issue's managed label equal to the task's
desired one (``status_labels.desired_label``).

Runs as a durable job rather than inline in ``transition_task`` so the
external write never sits inside a state transaction and so rate limits get
the job layer's bounded retries. The handler recomputes the desired label
from the *current* task row, so a queued sync always converges to the latest
state — rapid transitions collapse to one write each.

Writes are diffed against the live label set and only touch managed labels;
human labels (including the approval/candidate labels) are never removed.
Every applied change is audited.
"""

from __future__ import annotations

import sqlite3

from .. import db, status_labels
from ..clients.base import IssueNotFound
from ..transitions import audit, get_task
from .context import ServiceContext


def handle_sync(ctx: ServiceContext, job: sqlite3.Row) -> None:
    conn = ctx.conn
    payload = db.loads(job["payload_json"])
    task = get_task(conn, payload["task_id"])
    desired = status_labels.desired_label(task)
    applied = task["status_label"]

    try:
        current = set(
            ctx.clients.github.get_issue_labels(
                task["repo"], task["issue_number"]
            )
        )
        # Add the desired label first so the issue never sits without a
        # status mid-flight, then strip the other managed labels.
        if desired is not None and desired not in current:
            ctx.clients.github.add_label(task["repo"], task["issue_number"],
                                         desired)
        stale = sorted(
            l for l in status_labels.MANAGED_LABELS
            if l != desired and l in current
        )
        for label in stale:
            ctx.clients.github.remove_label(
                task["repo"], task["issue_number"], label
            )
    except IssueNotFound:
        with db.transaction(conn):
            audit(
                conn, action="status_label_skipped", mode=ctx.mode,
                task_id=task["id"],
                detail="issue no longer resolves; managed labels untouched",
            )
        return

    if desired == applied and not stale:
        return  # already in sync — no write, no audit noise

    with db.transaction(conn):
        conn.execute(
            "UPDATE tasks SET status_label = ?, updated_at = ? WHERE id = ?",
            (desired, db.now(), task["id"]),
        )
        audit(
            conn,
            action="status_label_cleared" if desired is None
            else "status_label_synced",
            mode=ctx.mode,
            task_id=task["id"],
            old_value=applied,
            new_value=desired,
            detail=(
                f"set {desired}; removed {stale}" if desired
                else f"removed {stale} (task untracked)"
            ),
        )
