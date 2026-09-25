"""Dashboard scan-now human override (POST /api/scan)."""

from fastapi import FastAPI
from fastapi.testclient import TestClient

from conftest import drain, task_by_issue

from app.routes.dashboard import router
from app.services.simulator import _seed_issue


def _app(settings):
    app = FastAPI()
    app.include_router(router)
    app.state.settings = settings
    return app


def test_scan_now_enqueues_durable_scan(settings, conn):
    res = TestClient(_app(settings)).post("/api/scan")
    assert res.status_code == 200
    body = res.json()
    assert body["mode"] == "simulation"
    assert body["queued"] is True
    job = conn.execute(
        "SELECT * FROM jobs WHERE id = ?", (body["job_id"],)
    ).fetchone()
    assert job["kind"] == "scan_issues"
    assert job["status"] == "queued"
    assert conn.execute(
        "SELECT 1 FROM audit_events WHERE action = 'scan_requested'"
    ).fetchone() is not None


def test_scan_now_is_idempotent_while_pending(settings, conn):
    client = TestClient(_app(settings))
    first = client.post("/api/scan").json()
    second = client.post("/api/scan").json()
    assert second["queued"] is False
    assert second["job_id"] == first["job_id"]
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM jobs WHERE kind = 'scan_issues'"
    ).fetchone()["n"] == 1


def test_scan_now_drives_discovery(settings, conn, ctx, worker):
    """The button queues the same durable job the scheduler uses — the
    worker picks it up and admits the approved issue."""
    s = ctx.settings
    _seed_issue(
        conn, s.github_repo, 501, "synthetic issue #501",
        labels=[s.candidate_issue_label, s.approval_issue_label],
        approved_by=s.github_allowed_approvers[0],
        approval_label=s.approval_issue_label,
    )
    res = TestClient(_app(settings)).post("/api/scan")
    assert res.status_code == 200
    drain(worker, ctx, conn)
    task = task_by_issue(conn, 501)
    assert task is not None
    assert task["execution"] in ("working", "agent_finished", "finished")
