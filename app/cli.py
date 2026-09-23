"""``python -m app.cli`` — operator CLI.

Every command works directly against the durable SQLite store. The CLI never
starts a worker: ``simulate`` only seeds fixtures and enqueues jobs, and the
application's own worker picks them up.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import db
from .clients.factory import build_clients
from .config import ConfigError, Settings, doctor_report, load_settings
from .services import jobs, native, simulator
from .services.context import ServiceContext
from .transitions import audit, is_paused, set_paused


def _ts(ts: float | None) -> str:
    if ts is None:
        return "-"
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")


def _settings() -> Settings:
    try:
        return load_settings()
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc


def _connect(settings: Settings):
    conn = db.connect(settings.database_path)
    db.init_db(conn)
    return conn


def cmd_doctor(_args) -> int:
    ok, checks = doctor_report()
    mode = "?"
    try:
        mode = load_settings().app_mode
    except ConfigError:
        pass
    print(f"Devin Repair Desk doctor — mode={mode}")
    for c in checks:
        mark = "ok" if c["ok"] else "FAIL"
        detail = f" — {c['detail']}" if c["detail"] else ""
        print(f"  [{mark}] {c['name']}{detail}")
    print("doctor is read-only: no sessions created, nothing published, "
          "no secrets printed")
    return 0 if ok else 1


def cmd_simulate(args) -> int:
    settings = _settings()
    if settings.app_mode != "simulation":
        print("simulate is only available with APP_MODE=simulation",
              file=sys.stderr)
        return 2
    conn = _connect(settings)
    result = simulator.seed_scenario(conn, settings, args.scenario)
    print(json.dumps(result, indent=2))
    print("Seeded synthetic fixtures and queued a scan job — the running "
          "app's worker will pick it up. Existing state was preserved.")
    return 0


def cmd_scan(args) -> int:
    """`scan now` — enqueue a scan job ahead of the schedule."""
    settings = _settings()
    conn = _connect(settings)
    with db.transaction(conn):
        job_id = jobs.enqueue(
            conn, "scan_issues", {"scheduled": False},
            mode=settings.app_mode,
        )
        audit(conn, action="scan_requested", mode=settings.app_mode,
              source="operator", detail="manual scan now")
    print(f"queued scan_issues job #{job_id} "
          "(the running worker will pick it up)")
    return 0


def _find_task(conn, settings: Settings, task_id: int):
    row = conn.execute(
        "SELECT * FROM tasks WHERE id = ? AND mode = ?",
        (task_id, settings.app_mode),
    ).fetchone()
    if row is None:
        print(f"task {task_id} not found (mode={settings.app_mode})",
              file=sys.stderr)
        raise SystemExit(2)
    return row


def cmd_message(args) -> int:
    """`message TASK_ID TEXT` — send an operator message to the session."""
    settings = _settings()
    conn = _connect(settings)
    task = _find_task(conn, settings, args.task_id)
    if not task["devin_session_id"]:
        print(f"task {task['id']} has no Devin session yet", file=sys.stderr)
        return 2
    with db.transaction(conn):
        job_id = jobs.enqueue(
            conn, "send_message",
            {"task_id": task["id"], "text": args.text, "actor": "operator"},
            mode=settings.app_mode,
        )
    print(f"queued send_message job #{job_id} for task {task['id']} "
          f"(session {task['devin_session_id']})")
    return 0


def cmd_stop(args) -> int:
    """`stop TASK_ID` — permanently terminate the session (archive=true)."""
    settings = _settings()
    conn = _connect(settings)
    task = _find_task(conn, settings, args.task_id)
    with db.transaction(conn):
        job_id = jobs.enqueue(
            conn, "stop_task",
            {"task_id": task["id"], "reason": args.reason or "operator stop",
             "actor": "operator"},
            mode=settings.app_mode,
        )
    print(f"queued stop_task job #{job_id} for task {task['id']} — "
          "permanent, preserves task outcome")
    return 0


def cmd_retry(args) -> int:
    """`retry TASK_ID --reason TEXT` — intentional re-dispatch."""
    settings = _settings()
    conn = _connect(settings)
    task = _find_task(conn, settings, args.task_id)
    with db.transaction(conn):
        job_id = jobs.enqueue(
            conn, "retry_task",
            {"task_id": task["id"], "reason": args.reason,
             "actor": "operator"},
            mode=settings.app_mode,
        )
    print(f"queued retry_task job #{job_id} for task {task['id']} "
          f"(reason: {args.reason})")
    return 0


def cmd_reconcile(args) -> int:
    """`reconcile TASK_ID` — locate a session by correlation tag."""
    settings = _settings()
    conn = _connect(settings)
    task = _find_task(conn, settings, args.task_id)
    with db.transaction(conn):
        job_id = jobs.enqueue(
            conn, "reconcile_task", {"task_id": task["id"]},
            mode=settings.app_mode,
        )
    print(f"queued reconcile_task job #{job_id} for task {task['id']}")
    return 0


def cmd_verify_manual(args) -> int:
    """`verify-manual TASK_ID ...` — record operator-performed verification.

    Fallback for when CI is unavailable. This records an operator's
    verification evidence — never 'CI verified'. The task's validation moves
    to ``manually_verified`` (a distinct state), the evidence row carries the
    operator, the exact head SHA, the command and results, and the evidence
    location so a reviewer can re-run it.
    """
    from .transitions import add_evidence, get_task, transition_task

    settings = _settings()
    conn = _connect(settings)
    task = _find_task(conn, settings, args.task_id)

    if not task["pr_number"]:
        print(
            f"task {task['id']} has no PR — nothing to manually verify",
            file=sys.stderr,
        )
        return 2
    if task["validation"] in ("verified", "manually_verified"):
        print(
            f"task {task['id']} is already {task['validation']}; "
            "refusing to overwrite an existing verification",
            file=sys.stderr,
        )
        return 2
    if task["validation"] == "no_pr":
        print(
            f"task {task['id']} has validation=no_pr — verify needs a "
            "PR first",
            file=sys.stderr,
        )
        return 2

    head_mismatch = (
        task["head_sha"]
        and task["head_sha"].lower() != args.head_sha.lower()
    )
    if head_mismatch:
        print(
            f"warning: --head-sha {args.head_sha[:12]} differs from the "
            f"task's recorded head {task['head_sha'][:12]} — recording the "
            "operator's SHA as a claim, review carefully",
            file=sys.stderr,
        )

    with db.transaction(conn):
        add_evidence(
            conn, task_id=task["id"], mode=settings.app_mode,
            kind="manual_verification",
            title=(
                f"Manually verified by {args.operator} — NOT CI verified"
            ),
            body={
                "operator": args.operator,
                "head_sha": args.head_sha,
                "command": args.command,
                "results": args.results,
                "evidence_uri": args.evidence,
                "recorded_head_sha": task["head_sha"],
                "head_matches_record": not head_mismatch,
                "caveat": (
                    "operator-recorded verification; CI verification "
                    "was unavailable — this is not 'CI verified'"
                ),
            },
            uri=args.evidence,
            synthetic=task["synthetic"] == 1,
            verifier=f"manual:{args.operator}",
        )
        audit(
            conn, action="manual_verification_recorded",
            mode=settings.app_mode, task_id=task["id"], source="operator",
            detail=(
                f"{args.operator} verified head {args.head_sha} via "
                f"`{args.command}` — evidence: {args.evidence}"
            ),
        )
        transition_task(
            conn, get_task(conn, task["id"]), "validation",
            "manually_verified",
            detail="operator-recorded evidence — not CI verified",
        )
        row = get_task(conn, task["id"])
        if row["review"] == "unknown":
            transition_task(
                conn, row, "review", "awaiting_review",
                detail="manually verified — awaiting human review",
            )
            row = get_task(conn, task["id"])
        if row["disposition"] in ("active", "blocked"):
            transition_task(
                conn, row, "disposition", "delivered",
                detail="delivered on manual verification",
            )
        jobs.enqueue(
            conn, "publish_report", {"task_id": task["id"]},
            mode=settings.app_mode,
            dedup_key=f"report:{settings.app_mode}:{task['id']}:"
                      f"{task['pr_number']}",
            max_attempts=settings.job_max_attempts,
        )
    print(
        f"task {task['id']}: validation -> manually_verified "
        f"(operator={args.operator}, head={args.head_sha[:12]}) — "
        "recorded as manual evidence, not CI verified"
    )
    return 0


def cmd_slack_link(args) -> int:
    """Record the native Slack thread link on a task (manual bookkeeping)."""
    settings = _settings()
    conn = _connect(settings)
    task = _find_task(conn, settings, args.task_id)
    with db.transaction(conn):
        conn.execute(
            "UPDATE tasks SET slack_link = ?, updated_at = ? WHERE id = ?",
            (args.url, db.now(), task["id"]),
        )
        audit(conn, action="slack_link_recorded", mode=settings.app_mode,
              task_id=task["id"], source="operator", detail=args.url)
    print(f"task {task['id']}: slack_link -> {args.url}")
    return 0


def cmd_tasks(_args) -> int:
    settings = _settings()
    conn = _connect(settings)
    rows = conn.execute(
        "SELECT * FROM tasks WHERE mode = ? ORDER BY id",
        (settings.app_mode,),
    ).fetchall()
    if not rows:
        print(f"no tasks (mode={settings.app_mode})")
        return 0
    print(f"tasks (mode={settings.app_mode}, paused={is_paused(conn)})")
    for t in rows:
        print(
            f"  #{t['id']:>3} {t['repo']}#{t['issue_number']} "
            f"{t['issue_title'][:48]}"
        )
        print(
            f"       exec={t['execution']}  validation={t['validation']}  "
            f"review={t['review']}  disposition={t['disposition']}"
        )
        print(
            f"       pr={t['pr_url'] or '-'}  session="
            f"{t['devin_session_id'] or '-'}  approver={t['approval_actor'] or '-'}"
            f"{'  SYNTHETIC' if t['synthetic'] else ''}"
        )
    return 0


def cmd_publish_report_source(_args) -> int:
    """Queue a report-source publish ahead of the interval — the worker
    still enforces destination validation and the material-change gate."""
    settings = _settings()
    conn = _connect(settings)
    with db.transaction(conn):
        job_id = jobs.enqueue(
            conn, "publish_report",
            {"task_id": None, "source": "operator"},
            mode=settings.app_mode,
            dedup_key=f"report:operator:{db.now()}",
        )
        audit(conn, action="report_source_requested",
              mode=settings.app_mode, source="operator",
              detail="manual publish-report-source")
    print(f"queued publish_report job #{job_id} — updates the fixed "
          "report-source issue only (never a new issue/comment)")
    return 0


def cmd_native_link(args) -> int:
    """Record an operator-verified Slack/session link on a native report
    session. This is the only delivery evidence the desk accepts — a
    session's status never implies Slack delivery."""
    settings = _settings()
    conn = _connect(settings)
    ctx = ServiceContext(
        conn=conn, settings=settings,
        clients=build_clients(settings, conn),
    )
    row_id = native.record_link(
        ctx, session_id=args.session_id, slack_link=args.url,
        session_url=args.session_url, operator=args.operator,
    )
    print(f"native_sessions #{row_id}: slack_link -> {args.url} "
          f"(manual, recorded by {args.operator})")
    return 0


def cmd_reports(_args) -> int:
    settings = _settings()
    conn = _connect(settings)
    control = conn.execute(
        "SELECT last_publish_at, last_native_observe_at, "
        "native_observe_error FROM control WHERE id = 1"
    ).fetchone()
    rows = conn.execute(
        "SELECT * FROM report_snapshots WHERE mode = ? ORDER BY id DESC",
        (settings.app_mode,),
    ).fetchall()
    print(
        f"reports (mode={settings.app_mode}, "
        f"last_publish={_ts(control['last_publish_at'])}, "
        f"last_native_observe={_ts(control['last_native_observe_at'])}"
        + (f", observe_error={control['native_observe_error']}"
           if control["native_observe_error"] else "")
        + ")"
    )
    if not rows:
        print("  no report snapshots")
    for r in rows:
        pubs = conn.execute(
            "SELECT status, destination_repo, destination_issue_number, "
            "external_ref, detail FROM publication_records "
            "WHERE snapshot_id = ? ORDER BY id",
            (r["id"],),
        ).fetchall()
        print(
            f"  #{r['id']:>3} {r['title']}  sha256={r['sha256'][:16]}  "
            f"generated={_ts(r['generated_at'])}"
        )
        for p in pubs:
            print(
                f"       {p['status']} -> {p['destination_repo']}#"
                f"{p['destination_issue_number']} ({p['external_ref'] or '-'})"
            )
    native_rows = conn.execute(
        "SELECT session_id, url, status, status_detail, acu_used, "
        "slack_link, last_seen_at FROM native_sessions "
        "WHERE mode = ? ORDER BY last_seen_at DESC",
        (settings.app_mode,),
    ).fetchall()
    if native_rows:
        print("  native report sessions (read-only observations; a "
              "session's status does not imply Slack delivery):")
        for n in native_rows:
            acu = "unknown" if n["acu_used"] is None else n["acu_used"]
            print(
                f"    {n['session_id']}  status={n['status']} "
                f"({n['status_detail']})  acu={acu}  "
                f"slack={n['slack_link'] or '-'}  "
                f"seen={_ts(n['last_seen_at'])}"
            )
    return 0


def cmd_pause(_args) -> int:
    settings = _settings()
    conn = _connect(settings)
    set_paused(conn, True, settings.app_mode)
    print("paused — new dispatch jobs will wait; polling continues")
    return 0


def cmd_unpause(_args) -> int:
    settings = _settings()
    conn = _connect(settings)
    set_paused(conn, False, settings.app_mode)
    print("unpaused")
    return 0


def cmd_export_evidence(args) -> int:
    settings = _settings()
    conn = _connect(settings)
    out_dir = Path(args.output or settings.evidence_export_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    tasks = conn.execute(
        "SELECT * FROM tasks WHERE mode = ?", (settings.app_mode,)
    ).fetchall()
    bundle = {
        "mode": settings.app_mode,
        "exported_at": _ts(db.now()),
        "synthetic": settings.app_mode == "simulation",
        "tasks": [],
    }
    for t in tasks:
        tid = t["id"]
        bundle["tasks"].append(
            {
                "task": dict(t),
                "approval_receipts": [
                    dict(r) for r in conn.execute(
                        "SELECT * FROM approval_receipts WHERE task_id = ?",
                        (tid,),
                    )
                ],
                "attempts": [
                    dict(r) for r in conn.execute(
                        "SELECT * FROM attempts WHERE task_id = ?", (tid,)
                    )
                ],
                "evidence": [
                    dict(r) for r in conn.execute(
                        "SELECT * FROM evidence WHERE task_id = ?", (tid,)
                    )
                ],
                "audit_events": [
                    dict(r) for r in conn.execute(
                        "SELECT * FROM audit_events WHERE task_id = ?",
                        (tid,),
                    )
                ],
            }
        )
    bundle["report_snapshots"] = [
        dict(r) for r in conn.execute(
            "SELECT * FROM report_snapshots WHERE mode = ?",
            (settings.app_mode,),
        )
    ]
    bundle["publication_records"] = [
        dict(r) for r in conn.execute("SELECT * FROM publication_records")
    ]
    bundle["native_sessions"] = [
        dict(r) for r in conn.execute(
            "SELECT * FROM native_sessions WHERE mode = ?",
            (settings.app_mode,),
        )
    ]
    bundle["budget_reservations"] = [
        dict(r) for r in conn.execute(
            "SELECT * FROM budget_reservations WHERE mode = ?",
            (settings.app_mode,),
        )
    ]
    bundle["control"] = dict(
        conn.execute("SELECT * FROM control WHERE id = 1").fetchone()
    )
    bundle["jobs"] = [dict(r) for r in conn.execute("SELECT * FROM jobs")]

    path = out_dir / f"evidence-{settings.app_mode}-{int(db.now())}.json"
    path.write_text(json.dumps(bundle, indent=2, default=str))
    print(f"wrote {path} ({len(bundle['tasks'])} task(s))")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.cli",
        description="Devin Repair Desk operator CLI (never starts a worker)",
    )
    # dest must not collide with verify-manual's own --command option.
    sub = parser.add_subparsers(dest="action", required=True)

    sub.add_parser("doctor", help="read-only configuration/health check")
    sim = sub.add_parser("simulate", help="seed a simulation scenario")
    sim.add_argument("scenario", choices=simulator.SCENARIOS)
    sub.add_parser("tasks", help="list tasks")
    sub.add_parser("reports", help="list report snapshots/publications"
                   " and native sessions")
    sub.add_parser(
        "publish-report-source",
        help="queue a publish to the fixed report-source issue now",
    )
    nl = sub.add_parser(
        "native-link",
        help="record an operator-verified slack link on a native "
             "report session",
    )
    nl.add_argument("session_id", help="native session id")
    nl.add_argument("url", help="slack permalink")
    nl.add_argument("--session-url", dest="session_url", default=None,
                    help="native session URL (if not yet observed)")
    nl.add_argument("--operator", default="operator",
                    help="who recorded the link")
    sub.add_parser("pause", help="pause new dispatch (polling continues)")
    sub.add_parser("unpause", help="resume dispatch")
    exp = sub.add_parser("export-evidence", help="export an evidence bundle")
    exp.add_argument("--output", "-o", default=None,
                     help="directory for the evidence bundle")

    scan = sub.add_parser("scan", help="enqueue a scan ahead of the schedule")
    scan.add_argument("when", nargs="?", default="now", choices=["now"])
    msg = sub.add_parser("message", help="message TASK_ID TEXT to the session")
    msg.add_argument("task_id", type=int)
    msg.add_argument("text")
    stop = sub.add_parser(
        "stop", help="terminate the task's session (archive=true)")
    stop.add_argument("task_id", type=int)
    stop.add_argument("--reason", default="")
    retry = sub.add_parser(
        "retry", help="intentional retry — requires --reason")
    retry.add_argument("task_id", type=int)
    retry.add_argument("--reason", required=True)
    rec = sub.add_parser(
        "reconcile", help="find the task's session by correlation tag")
    rec.add_argument("task_id", type=int)
    vm = sub.add_parser(
        "verify-manual",
        help="record operator verification when CI is unavailable "
             "(never 'CI verified')",
    )
    vm.add_argument("task_id", type=int)
    vm.add_argument("--operator", required=True,
                    help="operator name accountable for the record")
    vm.add_argument("--head-sha", required=True, dest="head_sha",
                    help="exact commit SHA that was verified")
    vm.add_argument("--command", required=True,
                    help="command(s) the operator ran")
    vm.add_argument("--results", required=True,
                    help="observed results of the command(s)")
    vm.add_argument("--evidence", required=True,
                    help="URL/path where the verification evidence lives")
    slack = sub.add_parser(
        "slack-link", help="record the native Slack thread URL on a task")
    slack.add_argument("task_id", type=int)
    slack.add_argument("url")

    args = parser.parse_args(argv)
    handlers = {
        "doctor": cmd_doctor,
        "simulate": cmd_simulate,
        "tasks": cmd_tasks,
        "reports": cmd_reports,
        "publish-report-source": cmd_publish_report_source,
        "native-link": cmd_native_link,
        "pause": cmd_pause,
        "unpause": cmd_unpause,
        "export-evidence": cmd_export_evidence,
        "scan": cmd_scan,
        "message": cmd_message,
        "stop": cmd_stop,
        "retry": cmd_retry,
        "reconcile": cmd_reconcile,
        "verify-manual": cmd_verify_manual,
        "slack-link": cmd_slack_link,
    }
    return handlers[args.action](args)


if __name__ == "__main__":
    raise SystemExit(main())
