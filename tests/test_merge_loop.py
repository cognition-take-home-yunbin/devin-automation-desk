"""Post-delivery loop closure — the PR watcher, re-armable session polls,
and bounded ACU reconciliation.

All on fake providers. ``merged_after_reads`` / ``closed_after_reads`` flip
the PR's state once verification's two reads (fetch + head recheck) are
done, so the next watch pass observes the human's outcome.
"""

import dataclasses

from conftest import audits, drain, jobs_rows, task_by_issue
from test_milestone_c import (
    SHA2,
    _drive_to_verified,
    _evidence,
    _seed_verifiable_issue,
)

from app import db
from app.db import transaction
from app.services import jobs
from app.services.context import ServiceContext
from app.services.simulator import (
    _finished_pr_script,
    _script_error,
    _seed_checks,
)


def _watch(conn, worker, ctx):
    """Enqueue one watch pass and run the queue dry (the verify/report
    follow-ups land in the same drain)."""
    with transaction(conn):
        jobs.enqueue(conn, "watch_prs", {"scheduled": True},
                     mode="simulation")
    drain(worker, ctx, conn)


def _with_timeout(ctx, seconds):
    s = dataclasses.replace(
        ctx.settings, verification_pending_timeout_seconds=seconds
    )
    return ServiceContext(conn=ctx.conn, settings=s, clients=ctx.clients)


def _session_script(conn, issue_number):
    row = conn.execute(
        "SELECT * FROM sim_sessions WHERE issue_number = ?",
        (issue_number,),
    ).fetchone()
    return row, db.loads(row["script_json"], {})


# -- watcher: terminal PR states ------------------------------------------------


def test_watcher_lands_merged_review(conn, worker, ctx):
    task = _drive_to_verified(
        conn, worker, ctx, number=301,
        script=_finished_pr_script(
            ctx.settings.github_repo, 301,
            merged_after_reads=2, merged_by="ops-lead",
        ),
    )
    assert task["review"] == "awaiting_review"
    _watch(conn, worker, ctx)
    task = task_by_issue(conn, 301)
    assert task["review"] == "merged"
    assert task["disposition"] == "delivered"
    merged = [
        a for a in audits(conn, task["id"])
        if a["dimension"] == "review" and a["new_value"] == "merged"
    ]
    assert merged and "ops-lead" in (merged[0]["detail"] or "")
    # The merge republishes the report — the static report:{task}:{pr} key
    # was already spent at delivery, so the key must carry the outcome.
    keys = [
        j["dedup_key"] for j in jobs_rows(conn)
        if j["kind"] == "publish_report"
    ]
    assert any(k and k.endswith(":merged") for k in keys)


def test_watcher_lands_closed_unmerged(conn, worker, ctx):
    _drive_to_verified(
        conn, worker, ctx, number=302,
        script=_finished_pr_script(
            ctx.settings.github_repo, 302, closed_after_reads=2,
        ),
    )
    _watch(conn, worker, ctx)
    assert task_by_issue(conn, 302)["review"] == "closed_unmerged"
    # Terminal — a second pass is a no-op, no crash.
    _watch(conn, worker, ctx)
    assert task_by_issue(conn, 302)["review"] == "closed_unmerged"


def test_watcher_lands_review_decisions(conn, worker, ctx):
    # Latest-substantive-review-per-author: a CHANGES_REQUESTED by one
    # reviewer outranks an earlier APPROVED by another.
    _drive_to_verified(
        conn, worker, ctx, number=303,
        script=_finished_pr_script(
            ctx.settings.github_repo, 303,
            reviews=[
                {"state": "APPROVED", "author": "ops-lead",
                 "submitted_at": 1.0},
                {"state": "CHANGES_REQUESTED", "author": "maintainer",
                 "submitted_at": 2.0},
            ],
        ),
    )
    _watch(conn, worker, ctx)
    assert task_by_issue(conn, 303)["review"] == "changes_requested"


def test_watcher_lands_approval(conn, worker, ctx):
    _drive_to_verified(
        conn, worker, ctx, number=304,
        script=_finished_pr_script(
            ctx.settings.github_repo, 304,
            reviews=[{"state": "APPROVED", "author": "ops-lead"}],
        ),
    )
    _watch(conn, worker, ctx)
    assert task_by_issue(conn, 304)["review"] == "approved"


def test_watcher_head_move_reverifies(conn, worker, ctx):
    s = ctx.settings
    task = _drive_to_verified(conn, worker, ctx, number=305)
    assert task["validation"] == "verified"
    row, script = _session_script(conn, 305)
    script["pr_head_sha"] = SHA2
    with transaction(conn):
        conn.execute(
            "UPDATE sim_sessions SET script_json = ? WHERE id = ?",
            (db.dumps(script), row["id"]),
        )
        conn.execute(
            "UPDATE sim_check_runs SET head_sha = ? WHERE pr_number = 1305",
            (SHA2,),
        )
    _watch(conn, worker, ctx)
    task = task_by_issue(conn, 305)
    assert task["validation"] == "verified"
    assert task["head_sha"] == SHA2
    acts = [a for a in audits(conn, task["id"])
            if a["dimension"] == "validation"]
    assert any(
        a["old_value"] == "verified" and a["new_value"] == "checks_pending"
        for a in acts
    )


# -- watcher + verify interplay on terminal PRs ---------------------------------


def test_verify_lands_merged_review(conn, worker, ctx):
    # The PR merged before the verify job even ran — the review outcome
    # lands alongside the verified checks instead of staying unknown.
    task = _drive_to_verified(
        conn, worker, ctx, number=306,
        script=_finished_pr_script(
            ctx.settings.github_repo, 306,
            pr_state="closed", merged=True, merged_by="ops-lead",
        ),
    )
    assert task["validation"] == "verified"
    assert task["review"] == "merged"
    assert task["disposition"] == "delivered"


def test_verify_lands_closed_unmerged_review(conn, worker, ctx):
    task = _drive_to_verified(
        conn, worker, ctx, number=307,
        script=_finished_pr_script(
            ctx.settings.github_repo, 307, pr_state="closed",
        ),
    )
    assert task["validation"] == "verified"
    assert task["review"] == "closed_unmerged"


# -- re-arming the session poller -----------------------------------------------


def test_resumed_session_repolls_without_regressing(conn, worker, ctx):
    task = _drive_to_verified(conn, worker, ctx, number=308)
    head_before = task["head_sha"]
    sid = task["devin_session_id"]
    # A message to the kept session (verification update / operator message)
    # resumes it — the poller must re-arm. With the old static
    # ``poll:{session_id}`` key this enqueue was a silent no-op forever.
    with transaction(conn):
        job_id = jobs.enqueue_session_poll(
            conn, task_id=task["id"], session_id=sid,
            mode="simulation", delay=0, max_attempts=8,
        )
    assert job_id is not None
    drain(worker, ctx, conn)
    task = task_by_issue(conn, 308)
    # The second finish must not regress the verified head the agent's
    # stale claim would have written back, nor crash the state machine.
    assert task["validation"] == "verified"
    assert task["head_sha"] == head_before
    titles = [e["title"] for e in _evidence(conn, task["id"])]
    assert "Resumed session finished again" in titles


def test_second_pending_poll_dedupes(conn, worker, ctx):
    task = _drive_to_verified(conn, worker, ctx, number=312)
    sid = task["devin_session_id"]
    with transaction(conn):
        first = jobs.enqueue_session_poll(
            conn, task_id=task["id"], session_id=sid,
            mode="simulation", delay=0, max_attempts=8,
        )
        second = jobs.enqueue_session_poll(
            conn, task_id=task["id"], session_id=sid,
            mode="simulation", delay=0, max_attempts=8,
        )
    assert first is not None and second is None  # one pending poll max


# -- ACU refresh lifecycle --------------------------------------------------------


def test_acu_refresh_retries_then_reconciles(conn, worker, ctx):
    task = _drive_to_verified(conn, worker, ctx, number=309)
    sid = task["devin_session_id"]
    attempt_id = conn.execute(
        "SELECT id FROM attempts WHERE task_id = ? "
        "ORDER BY attempt_number DESC LIMIT 1",
        (task["id"],),
    ).fetchone()["id"]
    # Consumption unavailable for the first read and every retry — the
    # handler must bound the retries and record the outcome honestly.
    with transaction(conn):
        _script_error(
            conn, "devin.session_acu_usage", "unavailable", times=5
        )
        jobs.enqueue(
            conn, "refresh_acu",
            {"task_id": task["id"], "attempt_id": attempt_id,
             "session_id": sid, "refresh_attempt": 0},
            mode="simulation",
        )
    drain(worker, ctx, conn)
    unavailable = [
        a for a in audits(conn, task["id"])
        if a["action"] == "acu_unavailable"
    ]
    assert unavailable

    # A resumed settle schedules a *fresh* refresh — the old static
    # acuref:{task}:{session} key would have deduped it away forever.
    row, script = _session_script(conn, 309)
    script["acu_used"] = 50.0
    with transaction(conn):
        conn.execute(
            "UPDATE sim_sessions SET script_json = ? WHERE id = ?",
            (db.dumps(script), row["id"]),
        )
        from app.services.monitor import _queue_acu_refresh

        _queue_acu_refresh(conn, "simulation", task["id"], attempt_id, sid)
    drain(worker, ctx, conn)
    attempt = conn.execute(
        "SELECT acu_used FROM attempts WHERE id = ?", (attempt_id,)
    ).fetchone()
    assert attempt["acu_used"] >= 50.0


# -- the settle window ------------------------------------------------------------


def test_pending_checks_retry_then_settle_unknown(conn, worker, ctx):
    ctx2 = _with_timeout(ctx, 3600.0)
    _seed_verifiable_issue(conn, ctx2.settings, 310)
    _seed_checks(
        conn, ctx2.settings.github_repo, 310,
        [("regression-test", "in_progress"), ("lint", "success")],
    )
    drain(worker, ctx2, conn)
    task = task_by_issue(conn, 310)
    assert task["validation"] == "checks_pending"  # inside the window
    # Age the verify job past the window — pending settles honestly.
    with transaction(conn):
        conn.execute(
            "UPDATE jobs SET created_at = 0 WHERE kind = 'verify_task'"
        )
    drain(worker, ctx2, conn)
    task = task_by_issue(conn, 310)
    assert task["validation"] == "unknown"
    assert task["disposition"] == "blocked"


def test_missing_checks_retry_then_fail(conn, worker, ctx):
    ctx2 = _with_timeout(ctx, 3600.0)
    _seed_verifiable_issue(conn, ctx2.settings, 311)
    # No check runs seeded at all — required checks are missing, not failed.
    drain(worker, ctx2, conn)
    task = task_by_issue(conn, 311)
    assert task["validation"] == "checks_pending"  # inside the window
    with transaction(conn):
        conn.execute(
            "UPDATE jobs SET created_at = 0 WHERE kind = 'verify_task'"
        )
    drain(worker, ctx2, conn)
    task = task_by_issue(conn, 311)
    assert task["validation"] == "checks_failed"
    assert task["disposition"] == "blocked"
