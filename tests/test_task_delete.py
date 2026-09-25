"""Task deletion — DELETE /api/tasks/{id} as an audited human override.

Deletion is a terminal disposition transition (`deleted`), never a row
removal: the durable (mode, repo, issue_number) constraint still dedupes
re-scans so no duplicate attempt can be created, and audit history is
preserved. Refuses while a session/dispatch may still be live.
"""

from fastapi.testclient import TestClient

from conftest import dashboard_app, drain, task_by_issue

from app.db import transaction
from app.services import jobs
from app.services.simulator import _seed_issue, _seed_task


def _task(conn, task_id):
    return conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,))


def _seed_terminal_task(conn, settings, number=601, execution="agent_finished"):
    task_id = _seed_task(
        conn, repo=settings.github_repo, number=number,
        title=f"task #{number}", snapshot_body="b",
        labels=[settings.candidate_issue_label,
                settings.approval_issue_label],
        approver=settings.github_allowed_approvers[0],
    )
    with transaction(conn):
        conn.execute(
            "UPDATE tasks SET execution = ? WHERE id = ?",
            (execution, task_id),
        )
    return task_id


def test_delete_terminal_task(settings, conn):
    tid = _seed_terminal_task(conn, settings)
    res = TestClient(dashboard_app(settings)).delete(f"/api/tasks/{tid}")
    assert res.status_code == 200
    assert res.json()["disposition"] == "deleted"
    task = _task(conn, tid).fetchone()
    assert task["disposition"] == "deleted"
    # Gone from the list view but the row — and its audit — remain.
    body = TestClient(dashboard_app(settings)).get("/api/tasks").json()
    assert all(t["id"] != tid for t in body)
    audit = conn.execute(
        "SELECT * FROM audit_events WHERE task_id = ? "
        "AND action = 'task_deleted'",
        (tid,),
    ).fetchone()
    assert audit is not None
    assert audit["old_value"] == "active"
    assert audit["new_value"] == "deleted"


def test_delete_is_terminal_and_not_found_afterwards(settings, conn):
    tid = _seed_terminal_task(conn, settings)
    client = TestClient(dashboard_app(settings))
    assert client.delete(f"/api/tasks/{tid}").status_code == 200
    assert client.delete(f"/api/tasks/{tid}").status_code == 404


def test_delete_unknown_task_404s(settings, conn):
    res = TestClient(dashboard_app(settings)).delete("/api/tasks/999")
    assert res.status_code == 404


def test_delete_refuses_live_execution(settings, conn):
    tid = _seed_terminal_task(conn, settings, execution="working")
    res = TestClient(dashboard_app(settings)).delete(f"/api/tasks/{tid}")
    assert res.status_code == 409
    assert "stop the task" in res.text.lower()
    assert _task(conn, tid).fetchone()["disposition"] == "active"


def test_deleted_task_still_dedupes_rescan(settings, conn, ctx, worker):
    """The core guarantee: deleting a task must not let the scanner create
    a duplicate attempt for the same issue."""
    s = ctx.settings
    tid = _seed_terminal_task(conn, settings, number=602)
    res = TestClient(dashboard_app(settings)).delete(f"/api/tasks/{tid}")
    assert res.status_code == 200
    _seed_issue(
        conn, s.github_repo, 602, "synthetic issue #602",
        labels=[s.candidate_issue_label, s.approval_issue_label],
        approved_by=s.github_allowed_approvers[0],
        approval_label=s.approval_issue_label,
    )
    with transaction(conn):
        jobs.enqueue(conn, "scan_issues", {}, mode="simulation")
    drain(worker, ctx, conn)
    n = conn.execute(
        "SELECT COUNT(*) AS n FROM tasks WHERE issue_number = 602"
    ).fetchone()["n"]
    assert n == 1
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM attempts"
    ).fetchone()["n"] == 0


def test_deleted_task_excluded_from_overview_metrics(settings, conn):
    tid = _seed_terminal_task(conn, settings)
    client = TestClient(dashboard_app(settings))
    before = client.get("/api/overview").json()["metrics"]
    client.delete(f"/api/tasks/{tid}")
    after = client.get("/api/overview").json()["metrics"]
    assert after["tasks_total"] == before["tasks_total"] - 1
