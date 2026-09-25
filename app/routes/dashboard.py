"""Dashboard API — read-only views over durable state, the scan-now
human override, and the simulation-only scenario trigger (PRD §11
internal API)."""

from __future__ import annotations

import sqlite3
from typing import Iterator

from fastapi import APIRouter, Depends, HTTPException, Request

from .. import db
from ..models import (
    AttemptOut,
    AuditEventOut,
    EvidenceOut,
    HealthOut,
    NativeSessionOut,
    OverviewOut,
    ReportOut,
    ScanRequestOut,
    SimulationResult,
    TaskDeleteOut,
    TaskDetail,
    TaskSummary,
)
from ..services import jobs, simulator
from ..transitions import audit, transition_task
from ..transitions import is_paused  # noqa: F401  (re-exported for CLI parity)

router = APIRouter()


def get_conn(request: Request) -> Iterator[sqlite3.Connection]:
    """Per-request connection — FastAPI runs sync handlers in a threadpool,
    so sharing one connection is not thread-safe."""
    conn = db.connect(request.app.state.settings.database_path)
    try:
        yield conn
    finally:
        conn.close()


Conn = Depends(get_conn)


def _utc_day_start() -> float:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    ).timestamp()


def _consumed_since(
    conn: sqlite3.Connection, mode: str, since: float | None
) -> float:
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


def _task_summary(row: sqlite3.Row, conn: sqlite3.Connection) -> TaskSummary:
    acu = conn.execute(
        "SELECT MAX(acu_used) AS a FROM attempts WHERE task_id = ?",
        (row["id"],),
    ).fetchone()["a"]
    return TaskSummary(
        id=row["id"],
        mode=row["mode"],
        repo=row["repo"],
        issue_number=row["issue_number"],
        issue_title=row["issue_title"],
        issue_url=row["issue_url"],
        execution=row["execution"],
        validation=row["validation"],
        review=row["review"],
        disposition=row["disposition"],
        approval_actor=row["approval_actor"],
        pr_url=row["pr_url"],
        devin_session_url=row["devin_session_url"],
        slack_link=row["slack_link"],
        cleanup_state=row["cleanup_state"],
        head_sha=row["head_sha"],
        base_sha=row["base_sha"],
        acu_used=acu,
        synthetic=bool(row["synthetic"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


@router.get("/healthz", response_model=HealthOut)
def healthz(request: Request, conn: sqlite3.Connection = Conn) -> HealthOut:
    conn.execute("SELECT 1")
    return HealthOut(status="ok", mode=request.app.state.settings.app_mode)


@router.get("/api/overview", response_model=OverviewOut)
def overview(request: Request, conn: sqlite3.Connection = Conn) -> OverviewOut:
    s = request.app.state.settings
    control = conn.execute("SELECT * FROM control WHERE id = 1").fetchone()
    rows = conn.execute(
        "SELECT * FROM tasks WHERE mode = ? AND disposition != 'deleted' "
        "ORDER BY id",
        (s.app_mode,),
    ).fetchall()

    def count(pred) -> int:
        return sum(1 for t in rows if pred(t))

    acu = conn.execute(
        """SELECT COALESCE(SUM(mx), 0) AS total, COUNT(*) AS n FROM (
               SELECT MAX(acu_used) AS mx FROM attempts
               WHERE mode = ? GROUP BY task_id)""",
        (s.app_mode,),
    ).fetchone()
    # Scanner-downtime visibility: freshness = last_scan_at vs the schedule.
    # The periodic trigger makes no immediate-delivery promise, so "stale"
    # means clearly past-due (3x interval), not merely late.
    last_scan = control["last_scan_at"]
    scan_age = (db.now() - last_scan) if last_scan else None
    scan_fresh = scan_age is not None and scan_age < 3 * s.scan_interval_seconds
    last_pub = control["last_publish_at"]
    publish_age = (db.now() - last_pub) if last_pub else None
    publish_fresh = (
        publish_age is not None
        and publish_age < s.report_stale_after_seconds
    )
    budget_held = conn.execute(
        "SELECT COALESCE(SUM(amount), 0) AS a FROM budget_reservations "
        "WHERE mode = ? AND status = 'held'",
        (s.app_mode,),
    ).fetchone()["a"]
    return OverviewOut(
        mode=s.app_mode,
        paused=bool(control["paused"]),
        repo=s.github_repo or "(unconfigured)",
        last_scan_at=last_scan,
        last_publish_at=control["last_publish_at"],
        scan_age_seconds=scan_age,
        scan_fresh=scan_fresh,
        publish_fresh=publish_fresh,
        last_native_observe_at=control["last_native_observe_at"],
        native_observe_error=control["native_observe_error"],
        generated_at=db.now(),
        metrics={
            "budget_held_acu": float(budget_held),
            "tasks_total": len(rows),
            "active": count(lambda t: t["disposition"] == "active"),
            "needs_intervention": count(
                lambda t: t["execution"] in ("needs_input", "approval_required")
                or t["disposition"] == "blocked"
            ),
            "verified_prs": count(lambda t: t["validation"] == "verified"),
            "manually_verified": count(
                lambda t: t["validation"] == "manually_verified"
            ),
            "blocked": count(lambda t: t["disposition"] == "blocked"),
            "merged_prs": count(lambda t: t["review"] == "merged"),
            "observed_acus": acu["total"],
            "acus_scope": "cumulative ACUs for included sessions",
            "sessions_seen": acu["n"],
            "daily_admission_remaining": round(
                s.daily_admission_acu_limit
                - float(budget_held)
                - _consumed_since(conn, s.app_mode, _utc_day_start()),
                2,
            ),
            "project_admission_remaining": round(
                s.project_admission_acu_limit
                - float(budget_held)
                - _consumed_since(conn, s.app_mode, None),
                2,
            ),
        },
        limits={
            "max_active_sessions": s.max_active_sessions,
            "repair_acu_limit": s.repair_acu_limit,
            "daily_admission_acu_limit": s.daily_admission_acu_limit,
            "project_admission_acu_limit": s.project_admission_acu_limit,
            "scan_interval_seconds": s.scan_interval_seconds,
            "poll_interval_seconds": s.poll_interval_seconds,
        },
    )


@router.get("/api/tasks", response_model=list[TaskSummary])
def list_tasks(
    request: Request,
    state: str | None = None,
    conn: sqlite3.Connection = Conn,
) -> list[TaskSummary]:
    s = request.app.state.settings
    rows = conn.execute(
        "SELECT * FROM tasks WHERE mode = ? AND disposition != 'deleted' "
        "ORDER BY id DESC",
        (s.app_mode,),
    ).fetchall()
    if state:
        rows = [
            r
            for r in rows
            if state
            in (r["execution"], r["validation"], r["review"], r["disposition"])
        ]
    return [_task_summary(r, conn) for r in rows]


# Executions where a Devin session or a queued dispatch may still be live —
# deleting one of those would orphan running work, so the endpoint refuses
# and asks the operator to stop the task first.
_UNDELETABLE_EXECUTIONS = (
    "queued", "dispatching", "creation_unknown", "working", "needs_input",
    "approval_required", "suspended", "stop_requested",
)


@router.delete("/api/tasks/{task_id}", response_model=TaskDeleteOut)
def delete_task(
    request: Request, task_id: int, conn: sqlite3.Connection = Conn
) -> TaskDeleteOut:
    """Human override: remove a task from tracking (disposition=deleted).

    The record stays — the durable (mode, repo, issue_number) constraint
    still dedupes re-scans, and audit history is preserved. Terminal;
    refuses while a session/dispatch may still be live.
    """
    s = request.app.state.settings
    row = conn.execute(
        "SELECT * FROM tasks WHERE id = ? AND mode = ?",
        (task_id, s.app_mode),
    ).fetchone()
    if row is None or row["disposition"] == "deleted":
        raise HTTPException(404, "task not found")
    if row["execution"] in _UNDELETABLE_EXECUTIONS:
        raise HTTPException(
            409,
            f"task execution is '{row['execution']}' — stop the task "
            "(cli stop) before deleting it",
        )
    with db.transaction(conn):
        transition_task(
            conn, row, "disposition", "deleted",
            action="task_deleted", detail="deleted via dashboard",
        )
    return TaskDeleteOut(id=task_id, disposition="deleted")


@router.get("/api/tasks/{task_id}", response_model=TaskDetail)
def task_detail(
    request: Request, task_id: int, conn: sqlite3.Connection = Conn
) -> TaskDetail:
    row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "task not found")
    base = _task_summary(row, conn)
    attempts = [
        AttemptOut(
            id=a["id"],
            attempt_number=a["attempt_number"],
            correlation_tag=a["correlation_tag"],
            session_id=a["session_id"],
            session_url=a["session_url"],
            raw_status=a["raw_status"],
            raw_detail=a["raw_detail"],
            acu_limit=a["acu_limit"],
            acu_used=a["acu_used"],
            prompt_hash=a["prompt_hash"],
            context_hash=a["context_hash"],
            started_at=a["started_at"],
            finished_at=a["finished_at"],
        )
        for a in conn.execute(
            "SELECT * FROM attempts WHERE task_id = ? ORDER BY attempt_number",
            (task_id,),
        )
    ]
    evidence = [
        EvidenceOut(
            id=e["id"],
            created_at=e["created_at"],
            kind=e["kind"],
            title=e["title"],
            body=db.loads(e["body_json"], e["body_json"]),
            uri=e["uri"],
            synthetic=bool(e["synthetic"]),
            verifier=e["verifier"],
        )
        for e in conn.execute(
            "SELECT * FROM evidence WHERE task_id = ? ORDER BY id", (task_id,)
        )
    ]
    audit_rows = [
        AuditEventOut(
            id=a["id"],
            created_at=a["created_at"],
            source=a["source"],
            action=a["action"],
            dimension=a["dimension"],
            old_value=a["old_value"],
            new_value=a["new_value"],
            detail=a["detail"],
        )
        for a in conn.execute(
            "SELECT * FROM audit_events WHERE task_id = ? ORDER BY id",
            (task_id,),
        )
    ]
    return TaskDetail(
        **base.model_dump(),
        issue_snapshot=db.loads(row["issue_snapshot_json"], None),
        last_error=row["last_error"],
        attempts=attempts,
        evidence=evidence,
        audit=audit_rows,
    )


@router.get("/api/reports", response_model=list[ReportOut])
def list_reports(
    request: Request, conn: sqlite3.Connection = Conn
) -> list[ReportOut]:
    s = request.app.state.settings
    rows = conn.execute(
        "SELECT * FROM report_snapshots WHERE mode = ? ORDER BY id DESC",
        (s.app_mode,),
    ).fetchall()
    out = []
    for r in rows:
        pubs = [
            dict(p)
            for p in conn.execute(
                "SELECT destination_repo, destination_issue_number, status, "
                "observed_body_hash, external_ref, created_at "
                "FROM publication_records WHERE snapshot_id = ? ORDER BY id",
                (r["id"],),
            )
        ]
        out.append(
            ReportOut(
                id=r["id"], mode=r["mode"], report_type=r["report_type"],
                schema_version=r["schema_version"], title=r["title"],
                sha256=r["sha256"], generated_at=r["generated_at"],
                native_session_url=r["native_session_url"],
                native_state=r["native_state"],
                slack_link=r["slack_link"],
                publications=pubs,
            )
        )
    return out


@router.get("/api/native-sessions", response_model=list[NativeSessionOut])
def list_native_sessions(
    request: Request, conn: sqlite3.Connection = Conn
) -> list[NativeSessionOut]:
    """Read-only observations of the external reporting automation's
    sessions — deliberately separate from managed repair attempts."""
    s = request.app.state.settings
    rows = conn.execute(
        "SELECT * FROM native_sessions WHERE mode = ? "
        "ORDER BY last_seen_at DESC",
        (s.app_mode,),
    ).fetchall()
    return [
        NativeSessionOut(
            id=r["id"], mode=r["mode"], tag=r["tag"],
            session_id=r["session_id"], url=r["url"],
            status=r["status"], status_detail=r["status_detail"],
            acu_used=r["acu_used"], slack_link=r["slack_link"],
            slack_source=r["slack_source"],
            first_seen_at=r["first_seen_at"],
            last_seen_at=r["last_seen_at"],
        )
        for r in rows
    ]


@router.get("/api/reports/{report_id}")
def report_detail(
    request: Request, report_id: int, conn: sqlite3.Connection = Conn
) -> dict:
    r = conn.execute(
        "SELECT * FROM report_snapshots WHERE id = ?", (report_id,)
    ).fetchone()
    if r is None:
        raise HTTPException(404, "report not found")
    pubs = [
        dict(p)
        for p in conn.execute(
            "SELECT * FROM publication_records WHERE snapshot_id = ?",
            (report_id,),
        )
    ]
    return {
        "mode": r["mode"],
        "snapshot": dict(r),
        "content": db.loads(r["content_json"], {}),
        "publications": pubs,
    }


@router.get("/api/jobs")
def list_jobs(request: Request, conn: sqlite3.Connection = Conn) -> list[dict]:
    rows = conn.execute(
        "SELECT id, kind, status, attempt_count, due_at, last_error, "
        "claimed_by, created_at FROM jobs ORDER BY id DESC LIMIT 100"
    ).fetchall()
    return [dict(r) for r in rows]


@router.post("/api/scan", response_model=ScanRequestOut)
def request_scan(request: Request, conn: sqlite3.Connection = Conn) -> ScanRequestOut:
    """Human override: enqueue a scan_issues job ahead of the schedule.

    Available in both modes — the scan itself only performs external
    reads; dispatch stays gated by pause, budgets and approval checks.
    Idempotent while a scan is already pending.
    """
    s = request.app.state.settings
    with db.transaction(conn):
        pending = conn.execute(
            "SELECT id FROM jobs WHERE kind = 'scan_issues' "
            "AND status IN ('queued', 'running') ORDER BY id LIMIT 1"
        ).fetchone()
        if pending is not None:
            return ScanRequestOut(
                mode=s.app_mode, job_id=int(pending["id"]), queued=False
            )
        job_id = jobs.enqueue(
            conn, "scan_issues", {"scheduled": False}, mode=s.app_mode
        )
        audit(
            conn, action="scan_requested", mode=s.app_mode,
            source="operator", detail="scan now via dashboard",
        )
    return ScanRequestOut(mode=s.app_mode, job_id=job_id, queued=True)


@router.post("/api/simulation/scenarios", response_model=SimulationResult)
def start_scenario(
    request: Request, body: dict, conn: sqlite3.Connection = Conn
) -> SimulationResult:
    s = request.app.state.settings
    if s.app_mode != "simulation":
        raise HTTPException(
            403, "scenario seeding is only available in simulation mode"
        )
    scenario = (body or {}).get("scenario", "")
    try:
        result = simulator.seed_scenario(conn, s, scenario)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return SimulationResult(mode="simulation", **result)


@router.get("/api/control")
def control_state(request: Request, conn: sqlite3.Connection = Conn) -> dict:
    c = conn.execute("SELECT * FROM control WHERE id = 1").fetchone()
    return {
        "mode": request.app.state.settings.app_mode,
        "paused": bool(c["paused"]),
        "last_scan_at": c["last_scan_at"],
        "last_publish_at": c["last_publish_at"],
    }
