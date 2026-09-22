from conftest import audits, drain, jobs_rows, task_by_issue
from app.db import transaction
from app.services.simulator import _seed_issue, seed_scenario
from app.services.jobs import enqueue


def run_scenario(conn, worker, ctx, name):
    result = seed_scenario(conn, ctx.settings, name)
    drain(worker, ctx, conn)
    return result


def test_happy_path_end_to_end(conn, worker, ctx):
    run_scenario(conn, worker, ctx, "happy-path")
    task = task_by_issue(conn, 101)
    assert task is not None
    assert task["execution"] == "agent_finished"
    assert task["validation"] == "verified"
    assert task["disposition"] == "delivered"
    assert task["devin_session_id"]
    assert task["pr_number"] == 1101
    assert task["synthetic"] == 1
    pubs = conn.execute("SELECT * FROM publication_records").fetchall()
    assert pubs and pubs[-1]["status"] == "confirmed"


def test_repeated_discovery_dedupes(conn, worker, ctx):
    run_scenario(conn, worker, ctx, "duplicate-scan")
    run_scenario(conn, worker, ctx, "duplicate-scan")
    tasks = conn.execute(
        "SELECT * FROM tasks WHERE issue_number = 108"
    ).fetchall()
    assert len(tasks) == 1
    attempts = conn.execute(
        "SELECT * FROM attempts WHERE task_id = ?", (tasks[0]["id"],)
    ).fetchall()
    assert len(attempts) == 1


def test_labels_alone_are_not_authorization(conn, worker, ctx):
    s = ctx.settings
    with transaction(conn):
        _seed_issue(
            conn, s.github_repo, 900, "suspicious claim",
            labels=[s.candidate_issue_label, s.approval_issue_label],
            approved_by="mallory",  # not in GITHUB_ALLOWED_APPROVERS
            approval_label=s.approval_label if hasattr(s, 'approval_label') else s.approval_issue_label,
        )
        enqueue(conn, "scan_issues", {}, mode="simulation")
    drain(worker, ctx, conn)
    assert task_by_issue(conn, 900) is None
    acts = [a["action"] for a in audits(conn)]
    assert "approval_insufficient" in acts


def test_needs_input_blocks_and_keeps_evidence(conn, worker, ctx):
    run_scenario(conn, worker, ctx, "needs-input")
    task = task_by_issue(conn, 102)
    assert task["execution"] == "needs_input"
    assert task["disposition"] == "blocked"
    acts = [a["action"] for a in audits(conn, task["id"])]
    assert "session_needs_input" in acts


def test_checks_failed_never_verifies(conn, worker, ctx):
    run_scenario(conn, worker, ctx, "checks-failed")
    task = task_by_issue(conn, 103)
    assert task["validation"] == "checks_failed"
    assert task["disposition"] == "blocked"


def test_creation_unknown_reconciles_to_real_session(conn, worker, ctx):
    run_scenario(conn, worker, ctx, "creation-unknown")
    task = task_by_issue(conn, 104)
    assert task["execution"] == "agent_finished"
    assert task["validation"] == "verified"
    acts = [a["action"] for a in audits(conn, task["id"])]
    assert "creation_reconciled" in acts
    assert "creation_unknown" in acts


def test_throttled_dispatch_retries_then_succeeds(conn, worker, ctx):
    run_scenario(conn, worker, ctx, "throttled")
    task = task_by_issue(conn, 105)
    assert task["validation"] == "verified"
    # The scan job was rate-limited twice before succeeding — its durable
    # row shows the retries (last_error retained from the requeue path).
    scan_jobs = conn.execute(
        "SELECT * FROM jobs WHERE kind = 'scan_issues' AND attempt_count > 1"
    ).fetchall()
    assert scan_jobs, "expected the rate-limited scan to retry"


def test_stale_checks_never_verify(conn, worker, ctx):
    run_scenario(conn, worker, ctx, "stale-checks")
    task = task_by_issue(conn, 106)
    assert task["validation"] == "unknown"
    assert task["disposition"] == "blocked"
    acts = [a["action"] for a in audits(conn, task["id"])]
    assert "checks_stale" in acts


def test_report_failure_keeps_records_and_recovers(conn, worker, ctx):
    run_scenario(conn, worker, ctx, "report-failure")
    task = task_by_issue(conn, 107)
    assert task["validation"] == "verified"
    assert task["disposition"] == "delivered"
    pubs = conn.execute(
        "SELECT * FROM publication_records ORDER BY id"
    ).fetchall()
    assert any(p["status"] == "failed" for p in pubs)
    assert pubs[-1]["status"] == "confirmed"


def test_pause_blocks_dispatch_but_not_polling(conn, worker, ctx):
    from app.transitions import set_paused

    seed_scenario(conn, ctx.settings, "happy-path")
    with transaction(conn):
        set_paused(conn, True, mode="simulation")
    drain(worker, ctx, conn)
    task = task_by_issue(conn, 101)
    assert task["execution"] in ("queued", "dispatching")
    assert task["devin_session_id"] is None
    with transaction(conn):
        set_paused(conn, False, mode="simulation")
    drain(worker, ctx, conn)
    task = task_by_issue(conn, 101)
    assert task["validation"] == "verified"


def test_state_persists_across_reopen(settings, conn, worker, ctx):
    run_scenario(conn, worker, ctx, "happy-path")
    conn.close()
    from app.db import connect

    conn2 = connect(settings.database_path)
    task = task_by_issue(conn2, 101)
    assert task["validation"] == "verified"
    assert len(jobs_rows(conn2)) > 0
    conn2.close()
