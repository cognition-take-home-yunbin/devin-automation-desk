"""Reservation-based managed spending limits (milestone B).

Three configured caps drive admission:

* ``REPAIR_ACU_LIMIT`` — per-session reservation amount, held at dispatch.
* ``DAILY_ADMISSION_ACU_LIMIT`` — max held+consumed reservations per UTC day.
* ``PROJECT_ADMISSION_ACU_LIMIT`` — max held+consumed over the project.

A dispatch *reserves* ``REPAIR_ACU_LIMIT`` before creating the session; the
reservation is consumed with the session's observed ``acus_consumed`` on a
terminal state, or released when no session spend happened. Operator
follow-ups (message/retry) verify remaining capacity *before* the API call —
budgets govern managed dispatch, not native Slack interactions.
"""

from __future__ import annotations

import sqlite3

from .. import db
from ..transitions import audit
from .context import ServiceContext


def _utc_day_start(ts: float | None = None) -> float:
    import datetime as _dt

    now = _dt.datetime.now(_dt.timezone.utc) if ts is None else (
        _dt.datetime.fromtimestamp(ts, _dt.timezone.utc)
    )
    return now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


def held_amount(conn: sqlite3.Connection, mode: str) -> float:
    row = conn.execute(
        "SELECT COALESCE(SUM(amount), 0) AS a FROM budget_reservations "
        "WHERE mode = ? AND status = 'held'",
        (mode,),
    ).fetchone()
    return float(row["a"])


def consumed_since(conn: sqlite3.Connection, mode: str, since: float | None) -> float:
    if since is None:
        row = conn.execute(
            "SELECT COALESCE(SUM(amount), 0) AS a FROM budget_reservations "
            "WHERE mode = ? AND status = 'consumed'",
            (mode,),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT COALESCE(SUM(amount), 0) AS a FROM budget_reservations "
            "WHERE mode = ? AND status = 'consumed' AND resolved_at >= ?",
            (mode, since),
        ).fetchone()
    return float(row["a"])


def remaining_daily(ctx: ServiceContext) -> float:
    return ctx.settings.daily_admission_acu_limit - (
        held_amount(ctx.conn, ctx.mode)
        + consumed_since(ctx.conn, ctx.mode, _utc_day_start())
    )


def remaining_project(ctx: ServiceContext) -> float:
    return ctx.settings.project_admission_acu_limit - (
        held_amount(ctx.conn, ctx.mode) + consumed_since(ctx.conn, ctx.mode, None)
    )


def check_admission(ctx: ServiceContext, task_id: int, amount: float) -> None:
    """Raise ``jobs.RetryLater`` when the reservation would exceed a cap."""
    from . import jobs

    if remaining_project(ctx) < amount or remaining_daily(ctx) < amount:
        audit(
            ctx.conn, action="budget_blocked", mode=ctx.mode, task_id=task_id,
            detail=(
                f"admission reservation {amount} ACU exceeds remaining caps "
                f"(daily {remaining_daily(ctx):.1f}, "
                f"project {remaining_project(ctx):.1f})"
            ),
        )
        raise jobs.RetryLater(
            ctx.settings.poll_interval_seconds,
            "budget caps reached; holding admission",
        )


def can_follow_up(ctx: ServiceContext, task_id: int) -> bool:
    """Capacity check before a budgeted API follow-up (message/retry)."""
    if remaining_project(ctx) <= 0 or remaining_daily(ctx) <= 0:
        audit(
            ctx.conn, action="budget_exhausted", mode=ctx.mode,
            task_id=task_id,
            detail="managed follow-up skipped: no remaining ACU cap",
        )
        return False
    return True


def reserve(
    conn: sqlite3.Connection,
    task_id: int,
    mode: str,
    amount: float,
    *,
    scope: str = "session",
    session_id: str | None = None,
    note: str | None = None,
) -> int:
    cur = conn.execute(
        """INSERT INTO budget_reservations
           (task_id, mode, scope, amount, session_id, note, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (task_id, mode, scope, amount, session_id, note, db.now()),
    )
    return int(cur.lastrowid)


def consume(
    conn: sqlite3.Connection, task_id: int, observed_acu: float | None
) -> None:
    """Settle held reservations with the session's observed ACU spend."""
    rows = conn.execute(
        "SELECT id FROM budget_reservations WHERE task_id = ? "
        "AND status = 'held'",
        (task_id,),
    ).fetchall()
    for r in rows:
        conn.execute(
            "UPDATE budget_reservations SET status = 'consumed', "
            "amount = COALESCE(?, amount), resolved_at = ? WHERE id = ?",
            (observed_acu, db.now(), r["id"]),
        )


def release(conn: sqlite3.Connection, task_id: int) -> None:
    conn.execute(
        "UPDATE budget_reservations SET status = 'released', "
        "resolved_at = ? WHERE task_id = ? AND status = 'held'",
        (db.now(), task_id),
    )
