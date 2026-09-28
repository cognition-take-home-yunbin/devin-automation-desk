"""Per-session ACU usage — read from the consumption API, not the session
record.

Live session responses report ``acus_consumed: 0.0`` even for sessions that
produced merged PRs (the field does not populate), so the desk reads
per-session usage from ``/consumption/daily/sessions/{id}``, falls back to
the session field, and stores NULL — rendered "unknown" — rather than a
false 0. A delayed ``refresh_acu`` job reconciles post-terminal billing lag.
"""

from conftest import audits, drain, task_by_issue
from app.db import transaction
from app.services import jobs
from app.services.simulator import _script_error, seed_scenario


def _attempt(conn, task_id):
    return conn.execute(
        "SELECT * FROM attempts WHERE task_id = ? ORDER BY attempt_number "
        "DESC LIMIT 1",
        (task_id,),
    ).fetchone()


def test_consumption_endpoint_populates_attempt_acu(conn, worker, ctx):
    seed_scenario(conn, ctx.settings, "happy-path")
    drain(worker, ctx, conn)
    task = task_by_issue(conn, 101)
    a = _attempt(conn, task["id"])
    # sim script acu_used=3.5 + per-poll accrual — never the false 0 the
    # session record reports
    assert a["acu_used"] is not None and a["acu_used"] >= 3.5
    # consumed reservation settled with the observed spend, not the held cap
    res = conn.execute(
        "SELECT * FROM budget_reservations WHERE task_id = ? AND "
        "status = 'consumed'",
        (task["id"],),
    ).fetchone()
    assert res["amount"] == a["acu_used"]


def test_consumption_unavailable_falls_back(conn, worker, ctx):
    """No consumption permission → session field value, never a failure."""
    _script_error(conn, "devin.session_acu_usage", "unavailable", times=200)
    seed_scenario(conn, ctx.settings, "happy-path")
    drain(worker, ctx, conn)
    task = task_by_issue(conn, 101)
    assert task["disposition"] == "delivered"
    a = _attempt(conn, task["id"])
    assert a["acu_used"] is not None and a["acu_used"] >= 3.5


def test_refresh_acu_reconciles_post_terminal(conn, worker, ctx):
    seed_scenario(conn, ctx.settings, "happy-path")
    drain(worker, ctx, conn)
    task = task_by_issue(conn, 101)
    a = _attempt(conn, task["id"])
    before = a["acu_used"]

    # The refresh job exists and already ran once during drain.
    refresh_jobs = conn.execute(
        "SELECT * FROM jobs WHERE kind = 'refresh_acu'"
    ).fetchall()
    assert refresh_jobs

    # Billing posts late: the session accrues another 4 ACU after terminal.
    conn.execute(
        "UPDATE sim_sessions SET acu_used = acu_used + 4.0 "
        "WHERE session_id = ?",
        (a["session_id"],),
    )
    with transaction(conn):
        jobs.enqueue(
            conn, "refresh_acu",
            {"task_id": task["id"], "attempt_id": a["id"],
             "session_id": a["session_id"]},
            mode="simulation",
        )
    drain(worker, ctx, conn)
    a = _attempt(conn, task["id"])
    assert a["acu_used"] == before + 4.0
    res = conn.execute(
        "SELECT amount FROM budget_reservations WHERE task_id = ? "
        "AND status = 'consumed'",
        (task["id"],),
    ).fetchone()
    assert res["amount"] == before + 4.0


def test_dispatch_records_unknown_not_zero(conn, worker, ctx):
    """At creation the session record reports 0.0 — stored as NULL so the
    dashboard renders 'unknown' until real usage arrives."""
    seed_scenario(conn, ctx.settings, "happy-path")
    worker.run_once(ctx)  # scan_issues
    # run dispatch only; label sync + polls stay queued
    conn.execute(
        "UPDATE jobs SET due_at = due_at + 100000 "
        "WHERE kind != 'dispatch_task'"
    )
    worker.run_once(ctx)
    conn.execute("UPDATE jobs SET due_at = 0 WHERE status = 'queued'")
    task = task_by_issue(conn, 101)
    a = _attempt(conn, task["id"])
    assert a["acu_used"] is None
    drain(worker, ctx, conn)
    assert _attempt(conn, task["id"])["acu_used"] is not None
