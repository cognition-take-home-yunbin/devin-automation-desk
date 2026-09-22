"""Durable job queue backed by SQLite.

Jobs are rows, not in-memory tasks: they survive restarts, carry a dedup key
so producers enqueue idempotently, are claimed atomically
(``UPDATE ... RETURNING`` inside BEGIN IMMEDIATE) with a lease so a dead
worker forfeits its claim, and retry with bounded backoff until
``max_attempts`` lands them in ``dead``.
"""

from __future__ import annotations

import sqlite3
import uuid

from .. import db


class RetryLater(Exception):
    """Handler asks for the job to be requeued after ``delay`` seconds."""

    def __init__(self, delay: float, reason: str = ""):
        super().__init__(reason or f"retry after {delay}s")
        self.delay = delay


def enqueue(
    conn: sqlite3.Connection,
    kind: str,
    payload: dict | None = None,
    *,
    mode: str,
    delay: float = 0.0,
    dedup_key: str | None = None,
    max_attempts: int = 8,
) -> int | None:
    """Insert a queued job; returns the job id, or None when the dedup key
    already exists (idempotent no-op)."""
    ts = db.now()
    try:
        with db.transaction(conn):
            cur = conn.execute(
                """INSERT INTO jobs
                   (mode, kind, payload_json, status, dedup_key, due_at,
                    attempt_count, max_attempts, created_at, updated_at)
                   VALUES (?, ?, ?, 'queued', ?, ?, 0, ?, ?, ?)""",
                (mode, kind, db.dumps(payload or {}), dedup_key,
                 ts + delay, max_attempts, ts, ts),
            )
            return int(cur.lastrowid)
    except sqlite3.IntegrityError:
        return None


def claim(
    conn: sqlite3.Connection, lease_seconds: float, worker_id: str | None = None
) -> sqlite3.Row | None:
    """Atomically claim the next due job, setting a lease so a crashed worker
    forfeits the claim once the lease expires."""
    worker = worker_id or f"worker-{uuid.uuid4().hex[:8]}"
    ts = db.now()
    with db.transaction(conn):
        row = conn.execute(
            """UPDATE jobs
               SET status = 'claimed', claimed_by = ?, claimed_at = ?,
                   lease_expires_at = ?, attempt_count = attempt_count + 1,
                   updated_at = ?
               WHERE id = (
                   SELECT id FROM jobs
                   WHERE status = 'queued' AND due_at <= ?
                      OR (status = 'claimed' AND lease_expires_at <= ?)
                   ORDER BY due_at, id LIMIT 1
               )
               RETURNING *""",
            (worker, ts, ts + lease_seconds, ts, ts, ts),
        ).fetchone()
    return row


def succeed(conn: sqlite3.Connection, job_id: int) -> None:
    with db.transaction(conn):
        conn.execute(
            """UPDATE jobs SET status = 'succeeded', claimed_by = NULL,
               lease_expires_at = NULL, updated_at = ? WHERE id = ?""",
            (db.now(), job_id),
        )


def requeue(
    conn: sqlite3.Connection,
    job: sqlite3.Row,
    delay: float,
    error: str | None = None,
) -> None:
    with db.transaction(conn):
        conn.execute(
            """UPDATE jobs
               SET status = 'queued', due_at = ?, last_error = ?,
                   claimed_by = NULL, lease_expires_at = NULL, updated_at = ?
               WHERE id = ?""",
            (db.now() + delay, error, db.now(), job["id"]),
        )


def fail(conn: sqlite3.Connection, job: sqlite3.Row, error: str) -> bool:
    """Record a failure; requeue with bounded backoff or mark dead.
    Returns True when the job is dead."""
    dead = job["attempt_count"] >= job["max_attempts"]
    status = "dead" if dead else "queued"
    backoff = min(2.0 ** job["attempt_count"], 300.0)
    with db.transaction(conn):
        conn.execute(
            """UPDATE jobs
               SET status = ?, due_at = ?, last_error = ?,
                   claimed_by = NULL, lease_expires_at = NULL, updated_at = ?
               WHERE id = ?""",
            (status, db.now() + backoff, error[:2000], db.now(), job["id"]),
        )
        conn.execute(
            """INSERT INTO audit_events
               (task_id, mode, source, action, detail, created_at)
               VALUES (?, ?, 'worker', ?, ?, ?)""",
            (
                db.loads(job["payload_json"]).get("task_id"),
                job["mode"],
                "job_dead" if dead else "job_retry",
                f"{job['kind']}: {error[:500]}",
                db.now(),
            ),
        )
    return dead
