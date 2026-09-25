"""Session status -> execution mapping (milestone B follow-up).

The live v3 session object reports ``status`` and ``status_detail``.
A session that finished its work and went to sleep arrives as
``suspended`` — treated as finished only once it has produced a
deliverable (a PR); sleep without one stays waiting honestly.
Startup vocabulary (``new``/``claimed``/``resuming``) keeps polling,
``exit`` is normal completion, and anything unrecognized is preserved
as unknown rather than faked into success or failure.
"""

import pytest

from conftest import audits, task_by_issue

from app import db
from app.db import transaction
from app.services import jobs, monitor
from app.services.simulator import _seed_task

ISSUE = 61
SESSION_ID = "sim-map-1"


def _session_row(conn, script, session_id=SESSION_ID):
    conn.execute(
        """INSERT INTO sim_sessions
           (session_id, correlation_tag, repo, issue_number,
            script_json, cursor, acu_used, created_at)
           VALUES (?, ?, 'acme/superset-demo', ?, ?, 0, 0, ?)""",
        (session_id, f"tag-{session_id}", ISSUE,
         db.dumps(script), db.now()),
    )
    return session_id


def _attempt(conn, task_id, session_id=SESSION_ID):
    conn.execute(
        """INSERT INTO attempts
           (task_id, mode, attempt_number, correlation_tag, session_id,
            session_url, started_at)
           VALUES (?, 'simulation', 1, ?, ?, ?, ?)""",
        (task_id, f"tag-{session_id}", session_id,
         f"https://app.devin.example.invalid/sessions/{session_id}",
         db.now()),
    )


def _task_with_session(ctx, conn, script, issue=ISSUE, session_id=SESSION_ID):
    """Accepted task + recorded attempt + scripted sim session."""
    tid = _seed_task(
        conn, repo="acme/superset-demo", number=issue,
        title="sleeping session", snapshot_body="b",
        labels=["devin-approved", "devin-candidate"], approver="ops-lead",
    )
    _session_row(conn, script, session_id)
    _attempt(conn, tid, session_id)
    conn.execute(
        "UPDATE tasks SET execution = 'working', "
        "disposition = 'active' WHERE id = ?", (tid,),
    )
    return tid


def _poll(ctx, task_id, session_id=SESSION_ID):
    monitor.handle_poll(
        ctx,
        {"payload_json": db.dumps(
            {"task_id": task_id, "session_id": session_id})},
    )


def _task(conn, task_id):
    return conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()


def test_suspended_with_finished_detail_completes(ctx, conn):
    tid = _task_with_session(ctx, conn, {
        "sequence": ["suspended"], "status_detail": "finished",
        "pr_number": 12,
        "pr_url": "https://github.com/acme/superset-demo/pull/12",
        "pr_head_sha": "a" * 40,
    })
    _poll(ctx, tid)
    t = _task(conn, tid)
    assert t["execution"] == "agent_finished"
    assert t["validation"] == "pr_found"
    assert t["pr_number"] == 12
    assert t["pr_url"].endswith("/pull/12")
    assert t["head_sha"] == "a" * 40
    # Verification begins from a sleeping session that delivered.
    assert conn.execute(
        "SELECT 1 FROM jobs WHERE kind = 'verify_task'"
    ).fetchone()


def test_suspended_inactivity_with_pr_completes(ctx, conn):
    tid = _task_with_session(ctx, conn, {
        "sequence": ["suspended"], "status_detail": "inactivity",
        "pr_number": 13,
        "pr_url": "https://github.com/acme/superset-demo/pull/13",
    })
    _poll(ctx, tid)
    t = _task(conn, tid)
    assert t["execution"] == "agent_finished"
    assert t["pr_number"] == 13
    assert any(
        a["detail"] == "suspended session produced a PR"
        for a in audits(conn, tid)
    )


def test_suspended_inactivity_without_pr_stays_suspended(ctx, conn):
    tid = _task_with_session(ctx, conn, {
        "sequence": ["suspended"], "status_detail": "inactivity",
    })
    _poll(ctx, tid)
    t = _task(conn, tid)
    assert t["execution"] == "suspended"
    assert t["pr_number"] is None
    assert not conn.execute(
        "SELECT 1 FROM jobs WHERE kind = 'verify_task'"
    ).fetchone()


def test_startup_statuses_keep_polling(ctx, conn):
    for i, status in enumerate(("new", "claimed", "resuming")):
        script = {"sequence": [status], "status_detail": "starting"}
        tid = _task_with_session(ctx, conn, script,
                                 issue=ISSUE + 10 + i,
                                 session_id=f"s-{status}")
        with pytest.raises(jobs.RetryLater):
            _poll(ctx, tid, f"s-{status}")
        assert _task(conn, tid)["execution"] == "working"


def test_exit_is_agent_finished(ctx, conn):
    tid = _task_with_session(ctx, conn, {
        "sequence": ["exit"], "status_detail": "working",
        "pr_number": 14,
        "pr_url": "https://github.com/acme/superset-demo/pull/14",
    })
    _poll(ctx, tid)
    assert _task(conn, tid)["execution"] == "agent_finished"


def test_error_is_failed(ctx, conn):
    tid = _task_with_session(ctx, conn, {
        "sequence": ["error"], "status_detail": "crashed",
    })
    _poll(ctx, tid)
    assert _task(conn, tid)["execution"] == "failed"


def test_unknown_status_is_preserved_not_faked(ctx, conn):
    tid = _task_with_session(ctx, conn, {
        "sequence": ["quuxing"], "status_detail": "quux",
    })
    with pytest.raises(jobs.RetryLater):
        _poll(ctx, tid)
    # Honest evidence of the unfamiliar value; task unchanged.
    assert _task(conn, tid)["execution"] == "working"
    ev = conn.execute(
        "SELECT title FROM evidence WHERE task_id = ? AND kind = 'note' "
        "ORDER BY id DESC LIMIT 1", (tid,),
    ).fetchone()
    assert "'quuxing'" in ev["title"]
    assert "preserved as unknown" in ev["title"]
