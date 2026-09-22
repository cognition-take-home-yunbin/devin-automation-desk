"""The single asynchronous background worker.

Runs inside the one application process. Each iteration:

1. Recovers leases — claimed jobs whose lease expired are re-claimed (the
   ``claim`` query already treats expired leases as claimable).
2. Reattaches in-flight work — tasks that had a session are re-polled, never
   re-created; ``creation_unknown`` tasks are re-reconciled.
3. Enqueues the periodic scan when due (SCAN_INTERVAL_SECONDS) if no scan is
   already queued — so the schedule never piles up.
4. Claims one due job and runs its handler.

The pause flag blocks new dispatch (the dispatch handler requeues) while
polling, verification and reporting continue — matching the PRD's pause
semantics. CLI commands never start this loop.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import uuid

from .. import db
from ..clients.factory import build_clients
from ..config import Settings
from ..clients.base import RateLimited
from ..transitions import audit, is_paused
from .context import ServiceContext
from . import (
    dispatch,
    jobs,
    monitor,
    operator,
    report_source,
    scanner,
    verification,
)

log = logging.getLogger("repairdesk.worker")

HANDLERS = {
    "scan_issues": scanner.handle_scan,
    "dispatch_task": dispatch.handle_dispatch,
    "reconcile_creation": dispatch.handle_reconcile,
    "poll_session": monitor.handle_poll,
    "verify_task": verification.handle_verify,
    "publish_report": report_source.handle_publish,
    "send_message": operator.handle_send_message,
    "stop_task": operator.handle_stop_task,
    "retry_task": operator.handle_retry_task,
    "reconcile_task": operator.handle_reconcile_task,
}

JOB_KINDS = tuple(HANDLERS)


class Worker:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.worker_id = f"worker-{uuid.uuid4().hex[:8]}"
        self._stop = asyncio.Event()
        self._conn: sqlite3.Connection | None = None

    # -- lifecycle -----------------------------------------------------------

    def connect(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = db.connect(self.settings.database_path)
            db.init_db(self._conn)
        return self._conn

    def recover(self, ctx: ServiceContext) -> None:
        """Reattach in-flight work after a restart — never recreate it."""
        conn = ctx.conn
        with db.transaction(conn):
            # Tasks that already have a session get a poll job (dedup makes
            # this a no-op when one is already queued).
            rows = conn.execute(
                """SELECT id, devin_session_id FROM tasks
                   WHERE mode = ? AND devin_session_id IS NOT NULL
                     AND execution IN ('working', 'needs_input',
                                       'approval_required', 'suspended',
                                       'dispatching')""",
                (ctx.mode,),
            ).fetchall()
            for r in rows:
                jobs.enqueue(
                    conn, "poll_session",
                    {"task_id": r["id"], "session_id": r["devin_session_id"]},
                    mode=ctx.mode, delay=self.settings.poll_interval_seconds,
                    dedup_key=f"poll:{r['devin_session_id']}",
                    max_attempts=self.settings.job_max_attempts * 10,
                )
            unknowns = conn.execute(
                """SELECT a.task_id, a.correlation_tag FROM attempts a
                   JOIN tasks t ON t.id = a.task_id
                   WHERE t.mode = ? AND t.execution = 'creation_unknown'
                     AND t.devin_session_id IS NULL""",
                (ctx.mode,),
            ).fetchall()
            for r in unknowns:
                jobs.enqueue(
                    conn, "reconcile_creation",
                    {"task_id": r["task_id"], "correlation_tag": r["correlation_tag"]},
                    mode=ctx.mode, delay=0.5,
                    dedup_key=f"reconcile:{r['correlation_tag']}",
                )
            if unknowns or rows:
                audit(
                    conn, action="worker_recovered", mode=ctx.mode,
                    detail=f"reattached {len(rows)} session(s), "
                           f"{len(unknowns)} pending creation(s)",
                )

    def _maybe_schedule_scan(self, ctx: ServiceContext) -> None:
        conn = ctx.conn
        row = conn.execute(
            "SELECT last_scan_at FROM control WHERE id = 1"
        ).fetchone()
        last = row["last_scan_at"] if row else None
        if last is not None and db.now() - last < self.settings.scan_interval_seconds:
            return
        pending = conn.execute(
            "SELECT 1 FROM jobs WHERE kind = 'scan_issues' "
            "AND status IN ('queued', 'claimed') LIMIT 1"
        ).fetchone()
        if pending:
            return
        jobs.enqueue(conn, "scan_issues", {"scheduled": True}, mode=ctx.mode)

    # -- main loop ------------------------------------------------------------

    def run_once(self, ctx: ServiceContext) -> bool:
        """Claim and run a single job. Returns True if work was done."""
        job = jobs.claim(
            ctx.conn, self.settings.job_lease_seconds, self.worker_id
        )
        if job is None:
            return False
        handler = HANDLERS.get(job["kind"])
        if handler is None:
            jobs.fail(ctx.conn, job, f"unknown job kind {job['kind']}")
            return True
        try:
            handler(ctx, job)
        except jobs.RetryLater as exc:
            jobs.requeue(ctx.conn, job, exc.delay, str(exc))
        except RateLimited as exc:
            jobs.requeue(ctx.conn, job, exc.retry_after, "rate limited")
        except Exception as exc:  # noqa: BLE001 - job failure path
            log.exception("job %s (%s) failed", job["id"], job["kind"])
            dead = jobs.fail(ctx.conn, job, f"{type(exc).__name__}: {exc}")
            if dead:
                audit(
                    ctx.conn, action="job_dead", mode=ctx.mode,
                    task_id=db.loads(job["payload_json"]).get("task_id"),
                    detail=f"{job['kind']}: {exc}",
                )
        else:
            jobs.succeed(ctx.conn, job["id"])
        return True

    async def run(self) -> None:
        conn = self.connect()
        ctx = ServiceContext(conn=conn, settings=self.settings,
                             clients=build_clients(self.settings, conn))
        if self.settings.dispatch_paused_on_first_start:
            paused_row = conn.execute(
                "SELECT paused FROM control WHERE id = 1"
            ).fetchone()
            if paused_row is not None and not paused_row["paused"]:
                # Only honour the flag on a virgin database — a returning
                # volume keeps the operator's choice.
                ever_audited = conn.execute(
                    "SELECT 1 FROM audit_events LIMIT 1"
                ).fetchone()
                if ever_audited is None:
                    conn.execute(
                        "UPDATE control SET paused = 1, updated_at = ? "
                        "WHERE id = 1",
                        (db.now(),),
                    )
                    audit(conn, action="paused", mode=ctx.mode,
                          source="system",
                          detail="DISPATCH_PAUSED_ON_FIRST_START")
        self.recover(ctx)
        log.info("worker %s running (mode=%s)", self.worker_id, ctx.mode)
        while not self._stop.is_set():
            try:
                self._maybe_schedule_scan(ctx)
                did_work = self.run_once(ctx)
            except Exception:  # noqa: BLE001 - keep the loop alive
                log.exception("worker loop error")
                did_work = False
            if not did_work:
                try:
                    await asyncio.wait_for(
                        self._stop.wait(),
                        timeout=self.settings.worker_idle_seconds,
                    )
                except asyncio.TimeoutError:
                    pass
        log.info("worker %s stopped", self.worker_id)

    def stop(self) -> None:
        self._stop.set()
