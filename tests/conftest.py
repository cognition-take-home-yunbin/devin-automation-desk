import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


@pytest.fixture()
def settings(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_MODE", "simulation")
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "test.sqlite"))
    monkeypatch.setenv("GITHUB_REPO", "acme/superset-demo")
    monkeypatch.setenv("GITHUB_BASE_BRANCH", "master")
    monkeypatch.setenv("GITHUB_ALLOWED_APPROVERS", "ops-lead,dev-oncall")
    monkeypatch.setenv("SCAN_INTERVAL_SECONDS", "60")
    monkeypatch.setenv("POLL_INTERVAL_SECONDS", "15")
    monkeypatch.setenv("MAX_ACTIVE_SESSIONS", "1")
    monkeypatch.setenv(
        "VERIFICATION_POLICY_PATH",
        str(Path(__file__).resolve().parents[1] / "config" / "verification.yaml"),
    )
    monkeypatch.setenv("REPORT_GITHUB_REPO", "acme/devin-repair-desk")
    monkeypatch.setenv("REPORT_DATA_ISSUE_NUMBER", "1")
    from app.config import load_settings

    return load_settings()


@pytest.fixture()
def conn(settings):
    from app.db import connect, init_db

    c = connect(settings.database_path)
    init_db(c)
    yield c
    c.close()


@pytest.fixture()
def ctx(settings, conn):
    from app.clients.factory import build_clients
    from app.services.context import ServiceContext

    return ServiceContext(
        conn=conn, settings=settings, clients=build_clients(settings, conn)
    )


@pytest.fixture()
def worker(settings):
    from app.services.worker import Worker

    return Worker(settings)


def drain(worker, ctx, conn, limit=500):
    """Run jobs until the queue is empty. Requeued/scheduled jobs have future
    due_at values; tests force them claimable to run a bounded loop."""
    ran = 0
    for _ in range(limit):
        conn.execute("UPDATE jobs SET due_at = 0 WHERE status = 'queued'")
        conn.execute(
            "UPDATE jobs SET lease_expires_at = 0 WHERE status = 'claimed'"
        )
        if not worker.run_once(ctx):
            break
        ran += 1
    return ran


def dashboard_app(settings):
    """Minimal app for HTTP-level tests: router only, no worker."""
    from fastapi import FastAPI

    from app.routes.dashboard import router

    app = FastAPI()
    app.include_router(router)
    app.state.settings = settings
    return app


def task_by_issue(conn, issue_number):
    return conn.execute(
        "SELECT * FROM tasks WHERE issue_number = ?", (issue_number,)
    ).fetchone()


def audits(conn, task_id=None):
    q = "SELECT * FROM audit_events"
    args: tuple = ()
    if task_id:
        q += " WHERE task_id = ?"
        args = (task_id,)
    return conn.execute(q + " ORDER BY id", args).fetchall()


def jobs_rows(conn):
    return conn.execute("SELECT * FROM jobs ORDER BY id").fetchall()
