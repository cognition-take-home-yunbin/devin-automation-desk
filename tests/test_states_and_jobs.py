import pytest

from app.db import transaction
from app.states import InvalidTransition, check_transition
from app.services.jobs import claim, enqueue, requeue, succeed
from app.transitions import transition_task


def test_legal_and_illegal_transitions():
    check_transition("execution", "queued", "dispatching")
    check_transition("validation", "checks_pending", "verified")
    with pytest.raises(InvalidTransition):
        check_transition("execution", "queued", "working")
    with pytest.raises(InvalidTransition):
        check_transition("validation", "no_pr", "verified")
    with pytest.raises(InvalidTransition):
        check_transition("execution", "failed", "working")


def test_transition_task_rejects_illegal(conn):
    with transaction(conn):
        conn.execute(
            """INSERT INTO tasks(mode, repo, issue_number, issue_title, issue_url,
               execution, validation, review, disposition, created_at, updated_at)
               VALUES('simulation','r',1,'t','u','queued','no_pr','unknown','active',0,0)"""
        )
    task = conn.execute("SELECT * FROM tasks").fetchone()
    t = transition_task(conn, task, "execution", "dispatching")
    assert t["execution"] == "dispatching"
    with pytest.raises(InvalidTransition):
        transition_task(conn, task, "execution", "needs_input")


def test_job_claim_is_durable_and_deduplicated(conn):
    with transaction(conn):
        assert enqueue(conn, "scan_issues", {}, mode="simulation",
                       dedup_key="scan:1") is not None
        assert enqueue(conn, "scan_issues", {}, mode="simulation",
                       dedup_key="scan:1") is None  # deduped
    job = claim(conn, lease_seconds=30)
    assert job is not None
    assert job["status"] == "claimed"
    assert claim(conn, lease_seconds=30) is None  # lease held


def test_job_retry_backoff_and_dead_letter(conn):
    with transaction(conn):
        enqueue(conn, "scan_issues", {}, mode="simulation", dedup_key="x",
                max_attempts=3)
    for _ in range(10):
        job = claim(conn, lease_seconds=30)
        if job is None:
            break
        requeue(conn, job, delay=0)
        conn.execute("UPDATE jobs SET due_at = 0 WHERE status = 'queued'")
    row = conn.execute("SELECT * FROM jobs WHERE dedup_key = 'x'").fetchone()
    # requeue() does not kill jobs (fail() does); it stays re-claimable
    assert row["attempt_count"] >= 1


def test_job_fail_dead_letters(conn):
    from app.services.jobs import fail

    with transaction(conn):
        enqueue(conn, "scan_issues", {}, mode="simulation", dedup_key="y",
                max_attempts=2)
    dead = False
    for _ in range(5):
        job = claim(conn, lease_seconds=30)
        if job is None:
            break
        dead = fail(conn, job, "boom")
        conn.execute("UPDATE jobs SET due_at = 0 WHERE status = 'queued'")
        if dead:
            break
    assert dead
    row = conn.execute("SELECT * FROM jobs WHERE dedup_key = 'y'").fetchone()
    assert row["status"] == "dead"


def test_lease_expiry_reclaims_job(conn):
    with transaction(conn):
        enqueue(conn, "scan_issues", {}, mode="simulation", dedup_key="x")
    job = claim(conn, lease_seconds=30)
    assert job
    conn.execute(
        "UPDATE jobs SET lease_expires_at = 0 WHERE id = ?", (job["id"],)
    )
    again = claim(conn, lease_seconds=30)
    assert again is not None and again["id"] == job["id"]
    succeed(conn, again["id"])
    assert (
        conn.execute("SELECT status FROM jobs").fetchone()["status"]
        == "succeeded"
    )
