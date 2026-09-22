"""Apply state transitions and record audit events."""

from __future__ import annotations

import sqlite3
from typing import Any

from . import db, states


def audit(
    conn: sqlite3.Connection,
    *,
    action: str,
    mode: str,
    task_id: int | None = None,
    dimension: str | None = None,
    old_value: str | None = None,
    new_value: str | None = None,
    detail: str | None = None,
    source: str = "system",
) -> None:
    conn.execute(
        """INSERT INTO audit_events
           (task_id, mode, source, action, dimension, old_value, new_value,
            detail, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (task_id, mode, source, action, dimension, old_value, new_value,
         detail, db.now()),
    )


def transition_task(
    conn: sqlite3.Connection,
    task: sqlite3.Row,
    dimension: str,
    new_state: str,
    *,
    action: str = "state_transition",
    detail: str | None = None,
) -> sqlite3.Row:
    """Move one task dimension to a new state under the transition map and
    audit it. Returns the refreshed task row."""
    old_state = task[dimension]
    states.check_transition(dimension, old_state, new_state)
    if old_state == new_state:
        return task
    conn.execute(
        f"UPDATE tasks SET {dimension} = ?, updated_at = ? WHERE id = ?",
        (new_state, db.now(), task["id"]),
    )
    audit(
        conn,
        action=action,
        mode=task["mode"],
        task_id=task["id"],
        dimension=dimension,
        old_value=old_state,
        new_value=new_state,
        detail=detail,
    )
    return get_task(conn, task["id"])


def add_evidence(
    conn: sqlite3.Connection,
    *,
    mode: str,
    kind: str,
    title: str,
    task_id: int | None = None,
    attempt_id: int | None = None,
    body: Any = None,
    uri: str | None = None,
    synthetic: bool = False,
    verifier: str | None = None,
) -> int:
    if body is not None and not isinstance(body, str):
        body = db.dumps(body)
    cur = conn.execute(
        """INSERT INTO evidence
           (task_id, attempt_id, mode, kind, title, body_json, uri, synthetic,
            verifier, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (task_id, attempt_id, mode, kind, title, body, uri,
         1 if synthetic else 0, verifier, db.now()),
    )
    return int(cur.lastrowid)


def get_task(conn: sqlite3.Connection, task_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if row is None:
        raise KeyError(f"task {task_id} not found")
    return row


def is_paused(conn: sqlite3.Connection) -> bool:
    row = conn.execute("SELECT paused FROM control WHERE id = 1").fetchone()
    return bool(row and row["paused"])


def set_paused(conn: sqlite3.Connection, paused: bool, mode: str) -> None:
    with db.transaction(conn):
        conn.execute(
            "UPDATE control SET paused = ?, updated_at = ? WHERE id = 1",
            (1 if paused else 0, db.now()),
        )
        audit(
            conn,
            action="paused" if paused else "unpaused",
            mode=mode,
            source="operator",
            detail="dispatch pause flag changed; polling is unaffected",
        )
