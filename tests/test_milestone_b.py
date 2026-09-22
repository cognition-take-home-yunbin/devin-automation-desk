"""Milestone B coverage — approval-integrity gate, budget reservations,
operator actions, concurrent claims, restart recovery.

All of it runs on fake providers: sim_* fixture tables stand in for GitHub
and the Devin API, and scripted errors drive the same failure paths the
live clients surface (rate limits, ambiguous writes).
"""

import dataclasses

from conftest import audits, drain, task_by_issue

from app import db
from app.db import transaction
from app.services import jobs
from app.services.simulator import (
    _script_error,
    _seed_issue,
    _seed_label_event,
    seed_scenario,
)


def run_scenario(conn, worker, ctx, name):
    result = seed_scenario(conn, ctx.settings, name)
    drain(worker, ctx, conn)
    return result


def _drain_until(conn, worker, ctx, pred, limit=200):
    """Run jobs until ``pred()`` holds (checked between jobs)."""
    for _ in range(limit):
        if pred():
            return True
        conn.execute("UPDATE jobs SET due_at = 0 WHERE status = 'queued'")
        conn.execute(
            "UPDATE jobs SET lease_expires_at = 0 WHERE status = 'claimed'"
        )
        if not worker.run_once(ctx):
            continue
    return pred()


def _run_kind_first(conn, worker, ctx, kind):
    """Force exactly one job of ``kind`` to run before everything else."""
    conn.execute(
        "UPDATE jobs SET due_at = CASE WHEN kind = ? THEN 0 ELSE ? END "
        "WHERE status = 'queued'",
        (kind, db.now() + 1e6),
    )
    worker.run_once(ctx)


def _task(conn, task_id):
    return conn.execute("SELECT * FROM tasks WHERE id = ?",
                        (task_id,)).fetchone()


def _working_task(conn, worker, ctx, scenario="happy-path", issue=101):
    """Drive a scenario until the task has a live working session."""
    seed_scenario(conn, ctx.settings, scenario)
    ok = _drain_until(
        conn, worker, ctx,
        lambda: (task_by_issue(conn, issue) is not None
                 and task_by_issue(conn, issue)["devin_session_id"]
                 is not None
                 and task_by_issue(conn, issue)["execution"] == "working"),
    )
    assert ok, "task never reached a live working session"
    return task_by_issue(conn, issue)


def _script(conn, session_id):
    r = conn.execute(
        "SELECT script_json FROM sim_sessions WHERE session_id = ?",
        (session_id,),
    ).fetchone()
    return db.loads(r["script_json"], {})


# -- admission integrity --------------------------------------------------------


def test_pull_request_objects_are_rejected(conn, worker, ctx):
    s = ctx.settings
    with transaction(conn):
        _seed_issue(
            conn, s.github_repo, 901, "PR masquerading as an issue",
            labels=[s.candidate_issue_label, s.approval_issue_label],
            approved_by=s.github_allowed_approvers[0],
            approval_label=s.approval_issue_label,
            is_pull_request=True,
        )
        jobs.enqueue(conn, "scan_issues", {}, mode="simulation")
    drain(worker, ctx, conn)
    assert task_by_issue(conn, 901) is None
    detail = " ".join(
        a["detail"] or "" for a in audits(conn)
        if a["action"] == "scan_ineligible"
    )
    assert "pull-request" in detail


def test_reapplied_label_does_not_duplicate(conn, worker, ctx):
    run_scenario(conn, worker, ctx, "happy-path")
    s = ctx.settings
    # "Reapplying a label" — a fresh labeled event must not mint a new task.
    with transaction(conn):
        _seed_label_event(
            conn, s.github_repo, 101, event_id="sim-evt-101-reapply",
            event="labeled", label=s.approval_issue_label,
            actor=s.github_allowed_approvers[0],
        )
        jobs.enqueue(conn, "scan_issues", {}, mode="simulation")
    drain(worker, ctx, conn)
    tasks = conn.execute(
        "SELECT * FROM tasks WHERE issue_number = 101"
    ).fetchall()
    assert len(tasks) == 1
    attempts = conn.execute(
        "SELECT * FROM attempts WHERE task_id = ?", (tasks[0]["id"],)
    ).fetchall()
    assert len(attempts) == 1


def test_approval_withdrawn_stops_for_review(conn, worker, ctx):
    run_scenario(conn, worker, ctx, "approval-withdrawn")
    task = task_by_issue(conn, 109)
    assert task["disposition"] == "blocked"
    acts = [a for a in audits(conn, task["id"])]
    assert any(a["action"] == "dispatch_review_blocked" for a in acts)
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM attempts WHERE task_id = ?",
        (task["id"],),
    ).fetchone()["n"] == 0
    assert task["devin_session_id"] is None


def test_snapshot_drift_stops_for_review(conn, worker, ctx):
    run_scenario(conn, worker, ctx, "snapshot-changed")
    task = task_by_issue(conn, 110)
    assert task["disposition"] == "blocked"
    acts = [a for a in audits(conn, task["id"])
            if a["action"] == "dispatch_review_blocked"]
    assert acts and "snapshot" in (acts[0]["detail"] or "")
    assert task["devin_session_id"] is None


# -- durable claims --------------------------------------------------------------


def test_concurrent_claims_never_hand_out_the_same_job(
    settings, conn, worker, ctx
):
    from app.db import connect

    conn2 = connect(settings.database_path)
    try:
        with transaction(conn):
            for i in range(3):
                jobs.enqueue(conn, "scan_issues", {"n": i},
                             mode="simulation")
        claimed_ids = set()
        for _ in range(3):
            for c in (conn, conn2):
                row = jobs.claim(c, lease_seconds=30, worker_id="w-test")
                if row:
                    assert row["id"] not in claimed_ids
                    claimed_ids.add(row["id"])
        assert len(claimed_ids) == 3
    finally:
        conn2.close()


# -- throttling + uncertain creation ----------------------------------------------


def test_throttled_create_releases_reservation_then_retries(
    conn, worker, ctx
):
    s = ctx.settings
    with transaction(conn):
        _seed_issue(
            conn, s.github_repo, 902, "throttled create",
            labels=[s.candidate_issue_label, s.approval_issue_label],
            approved_by=s.github_allowed_approvers[0],
            approval_label=s.approval_issue_label,
        )
        _script_error(conn, "devin.create_session", "rate_limited", times=1)
        jobs.enqueue(conn, "scan_issues", {}, mode="simulation")
    drain(worker, ctx, conn)
    task = task_by_issue(conn, 902)
    assert task["execution"] == "agent_finished"
    attempts = conn.execute(
        "SELECT * FROM attempts WHERE task_id = ? ORDER BY attempt_number",
        (task["id"],),
    ).fetchall()
    assert len(attempts) == 2
    assert attempts[0]["raw_status"] == "throttled"
    statuses = {
        r["status"]
        for r in conn.execute(
            "SELECT status FROM budget_reservations WHERE task_id = ?",
            (task["id"],),
        ).fetchall()
    }
    assert statuses == {"released", "consumed"}


def test_budget_caps_hold_admission(conn, worker, ctx):
    ctx.settings = dataclasses.replace(
        ctx.settings,
        daily_admission_acu_limit=ctx.settings.repair_acu_limit - 1,
    )
    run_scenario(conn, worker, ctx, "happy-path")
    task = task_by_issue(conn, 101)
    assert task["execution"] == "queued"
    assert task["devin_session_id"] is None
    acts = [a["action"] for a in audits(conn, task["id"])]
    assert "budget_blocked" in acts
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM budget_reservations WHERE task_id = ?",
        (task["id"],),
    ).fetchone()["n"] == 0
    # Raising the cap lets the held admission proceed on the next retry.
    ctx.settings = dataclasses.replace(
        ctx.settings, daily_admission_acu_limit=10_000
    )
    drain(worker, ctx, conn)
    assert task_by_issue(conn, 101)["execution"] == "agent_finished"


def test_message_blocked_when_budget_exhausted(conn, worker, ctx):
    task = _working_task(conn, worker, ctx)
    ctx.settings = dataclasses.replace(
        ctx.settings, project_admission_acu_limit=0
    )
    with transaction(conn):
        jobs.enqueue(
            conn, "send_message",
            {"task_id": task["id"], "text": "ping?", "actor": "operator"},
            mode="simulation",
        )
    _run_kind_first(conn, worker, ctx, "send_message")
    acts = [a["action"] for a in audits(conn, task["id"])]
    assert "budget_exhausted" in acts
    assert "message_sent" not in acts


# -- operator actions -------------------------------------------------------------


def test_message_session_records_and_requeues_poll(conn, worker, ctx):
    task = _working_task(conn, worker, ctx)
    session_id = task["devin_session_id"]
    with transaction(conn):
        jobs.enqueue(
            conn, "send_message",
            {"task_id": task["id"], "text": "status check",
             "actor": "operator"},
            mode="simulation",
        )
    _run_kind_first(conn, worker, ctx, "send_message")
    acts = [a["action"] for a in audits(conn, task["id"])]
    assert "message_sent" in acts
    assert _script(conn, session_id)["messages"][0]["text"] == "status check"


def test_message_rejected_on_stopped_task(conn, worker, ctx):
    task = _working_task(conn, worker, ctx)
    with transaction(conn):
        jobs.enqueue(
            conn, "stop_task",
            {"task_id": task["id"], "reason": "test", "actor": "operator"},
            mode="simulation",
        )
        jobs.enqueue(
            conn, "send_message",
            {"task_id": task["id"], "text": "too late", "actor": "operator"},
            mode="simulation",
        )
    _run_kind_first(conn, worker, ctx, "stop_task")
    _run_kind_first(conn, worker, ctx, "send_message")
    acts = [a["action"] for a in audits(conn, task["id"])]
    assert "message_rejected" in acts
    assert "message_sent" not in acts


def test_stop_terminates_archives_and_tracks_cleanup(conn, worker, ctx):
    task = _working_task(conn, worker, ctx)
    session_id = task["devin_session_id"]
    with transaction(conn):
        jobs.enqueue(
            conn, "stop_task",
            {"task_id": task["id"], "reason": "operator stop",
             "actor": "operator"},
            mode="simulation",
        )
    _run_kind_first(conn, worker, ctx, "stop_task")
    task = _task(conn, task["id"])
    assert task["execution"] == "stopped"
    assert task["disposition"] == "cancelled"
    assert task["cleanup_state"] == "terminated"
    script = _script(conn, session_id)
    assert script["stopped"] and script["archived"]
    rec = conn.execute(
        "SELECT * FROM cleanup_records WHERE task_id = ?", (task["id"],)
    ).fetchone()
    assert rec is not None and rec["action"] == "terminated"
    acts = [a["action"] for a in audits(conn, task["id"])]
    assert "session_terminated" in acts
    # The terminal settle consumed the held reservation — nothing stays held.
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM budget_reservations "
        "WHERE task_id = ? AND status = 'held'", (task["id"],),
    ).fetchone()["n"] == 0


def test_retry_requires_reason(conn, worker, ctx):
    task = _working_task(conn, worker, ctx)
    with transaction(conn):
        jobs.enqueue(
            conn, "stop_task",
            {"task_id": task["id"], "reason": "x", "actor": "operator"},
            mode="simulation",
        )
    _run_kind_first(conn, worker, ctx, "stop_task")
    with transaction(conn):
        jobs.enqueue(
            conn, "retry_task",
            {"task_id": task["id"], "reason": "", "actor": "operator"},
            mode="simulation",
        )
    _run_kind_first(conn, worker, ctx, "retry_task")
    acts = [a["action"] for a in audits(conn, task["id"])]
    assert "retry_rejected" in acts
    assert _task(conn, task["id"])["execution"] == "stopped"


def test_retry_refused_while_session_live(conn, worker, ctx):
    task = _working_task(conn, worker, ctx)
    with transaction(conn):
        jobs.enqueue(
            conn, "retry_task",
            {"task_id": task["id"], "reason": "again", "actor": "operator"},
            mode="simulation",
        )
    _run_kind_first(conn, worker, ctx, "retry_task")
    acts = [a for a in audits(conn, task["id"])
            if a["action"] == "retry_rejected"]
    assert acts and "stop it first" in acts[0]["detail"]
    assert _task(conn, task["id"])["execution"] == "working"


def test_operator_retry_spawns_new_attempt_and_session(conn, worker, ctx):
    task = _working_task(conn, worker, ctx)
    old_session = task["devin_session_id"]
    with transaction(conn):
        jobs.enqueue(
            conn, "stop_task",
            {"task_id": task["id"], "reason": "stop for retry",
             "actor": "operator"},
            mode="simulation",
        )
    _run_kind_first(conn, worker, ctx, "stop_task")
    with transaction(conn):
        jobs.enqueue(
            conn, "retry_task",
            {"task_id": task["id"], "reason": "first run was inconclusive",
             "actor": "operator"},
            mode="simulation",
        )
    drain(worker, ctx, conn)
    task = _task(conn, task["id"])
    assert task["validation"] == "verified"
    attempts = conn.execute(
        "SELECT * FROM attempts WHERE task_id = ? ORDER BY attempt_number",
        (task["id"],),
    ).fetchall()
    assert len(attempts) == 2
    assert attempts[0]["correlation_tag"] != attempts[1]["correlation_tag"]
    assert task["devin_session_id"] != old_session
    acts = [a["action"] for a in audits(conn, task["id"])]
    assert "retry_queued" in acts


def test_operator_reconcile_reattaches_session(conn, worker, ctx):
    task = _working_task(conn, worker, ctx)
    session_id = task["devin_session_id"]
    with transaction(conn):
        conn.execute(
            "UPDATE tasks SET devin_session_id = NULL, "
            "devin_session_url = NULL WHERE id = ?", (task["id"],),
        )
        jobs.enqueue(conn, "reconcile_task", {"task_id": task["id"]},
                     mode="simulation")
    _run_kind_first(conn, worker, ctx, "reconcile_task")
    task = _task(conn, task["id"])
    assert task["devin_session_id"] == session_id


# -- restart recovery --------------------------------------------------------------


def test_restart_recovers_poll_and_finishes(conn, settings, worker, ctx):
    task = _working_task(conn, worker, ctx)
    session_id = task["devin_session_id"]
    # Simulate a crash: the worker dies holding a claimed-but-expired poll
    # lease and the queued follow-up poll never ran.
    with transaction(conn):
        conn.execute(
            "UPDATE jobs SET status = 'claimed', "
            "lease_expires_at = 0 WHERE status = 'queued'"
        )
        conn.execute("DELETE FROM jobs WHERE kind = 'poll_session'")
    from app.services.worker import Worker

    worker2 = Worker(settings)
    worker2.recover(ctx)
    assert conn.execute(
        "SELECT 1 FROM jobs WHERE kind = 'poll_session' "
        "AND status = 'queued' AND payload_json LIKE ?",
        (f"%{session_id}%",),
    ).fetchone(), "recover() must re-enqueue polling for the live session"
    drain(worker2, ctx, conn)
    assert task_by_issue(conn, 101)["execution"] == "agent_finished"
