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
from .config import ConfigError, Settings, doctor_report, load_settings
from .services import simulator
from .transitions import is_paused, set_paused


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


def cmd_reports(_args) -> int:
    settings = _settings()
    conn = _connect(settings)
    rows = conn.execute(
        "SELECT * FROM report_snapshots WHERE mode = ? ORDER BY id DESC",
        (settings.app_mode,),
    ).fetchall()
    if not rows:
        print(f"no report snapshots (mode={settings.app_mode})")
        return 0
    print(f"report snapshots (mode={settings.app_mode})")
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
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("doctor", help="read-only configuration/health check")
    sim = sub.add_parser("simulate", help="seed a simulation scenario")
    sim.add_argument("scenario", choices=simulator.SCENARIOS)
    sub.add_parser("tasks", help="list tasks")
    sub.add_parser("reports", help="list report snapshots/publications")
    sub.add_parser("pause", help="pause new dispatch (polling continues)")
    sub.add_parser("unpause", help="resume dispatch")
    exp = sub.add_parser("export-evidence", help="export an evidence bundle")
    exp.add_argument("--output", "-o", default=None,
                     help="directory for the evidence bundle")

    args = parser.parse_args(argv)
    handlers = {
        "doctor": cmd_doctor,
        "simulate": cmd_simulate,
        "tasks": cmd_tasks,
        "reports": cmd_reports,
        "pause": cmd_pause,
        "unpause": cmd_unpause,
        "export-evidence": cmd_export_evidence,
    }
    return handlers[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
