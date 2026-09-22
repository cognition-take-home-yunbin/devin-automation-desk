"""Facts snapshot generation (PRD §15–16).

Snapshots are deterministic for a given database state: the same tasks yield
the same canonical content and sha256, so publishing is idempotent and a
snapshot hash always identifies exactly what was reported. Windows are
computed in REPORT_TIMEZONE with explicit UTC boundaries; unknown fields stay
explicitly unknown rather than zero.
"""

from __future__ import annotations

import hashlib
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from .. import db

SCHEMA_VERSION = 1

_TERMINAL_EXECUTION = ("agent_finished", "failed", "stopped")
_NEEDS_INTERVENTION = ("needs_input", "approval_required")


def _local_day_bounds(tz: ZoneInfo, day_offset: int = 0) -> tuple[str, str]:
    """UTC ISO boundaries of the local calendar day (offset days back)."""
    now_local = datetime.now(tz)
    start_local = (now_local - timedelta(days=day_offset)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    end_local = start_local + timedelta(days=1)
    return (
        start_local.astimezone(timezone.utc).isoformat(),
        end_local.astimezone(timezone.utc).isoformat(),
    )


def _count_between(
    conn: sqlite3.Connection, mode: str, where: str, start: str, end: str
) -> int:
    row = conn.execute(
        f"SELECT COUNT(*) AS n FROM tasks WHERE mode = ? AND {where} "
        "AND created_at >= ? AND created_at < ?",
        (mode, datetime.fromisoformat(start).timestamp(),
         datetime.fromisoformat(end).timestamp()),
    ).fetchone()
    return int(row["n"])


def snapshot_content(
    conn: sqlite3.Connection, mode: str, tz_name: str
) -> dict[str, Any]:
    tz = ZoneInfo(tz_name)
    generated_at = datetime.now(timezone.utc)

    rows = conn.execute(
        "SELECT * FROM tasks WHERE mode = ? ORDER BY id", (mode,)
    ).fetchall()
    tasks = []
    acu_total = 0.0
    acu_known = True
    for t in rows:
        acu = conn.execute(
            "SELECT MAX(acu_used) AS a FROM attempts WHERE task_id = ?",
            (t["id"],),
        ).fetchone()["a"]
        if acu is None:
            acu_known = False
        else:
            acu_total += acu
        tasks.append(
            {
                "id": t["id"],
                "issue": f"{t['repo']}#{t['issue_number']}",
                "title": t["issue_title"],
                "execution": t["execution"],
                "validation": t["validation"],
                "review": t["review"],
                "disposition": t["disposition"],
                "pr_url": t["pr_url"],
                "session_url": t["devin_session_url"],
                "accepted_at": t["accepted_at"],
                "acu_used": acu,
                "synthetic": bool(t["synthetic"]),
            }
        )

    def period(name: str, start_iso: str, end_iso: str) -> dict[str, Any]:
        subset = [
            t for t in tasks
            if t["accepted_at"]
            and start_iso <= datetime.fromtimestamp(
                t["accepted_at"], timezone.utc
            ).isoformat() < end_iso
        ]
        return {
            "name": name,
            "utc_start": start_iso,
            "utc_end": end_iso,
            "accepted_tasks": len(subset),
            "verified_deliveries": sum(
                1 for t in subset if t["validation"] == "verified"
            ),
            "merged_prs": sum(1 for t in subset if t["review"] == "merged"),
            "needs_intervention": sum(
                1 for t in subset
                if t["execution"] in _NEEDS_INTERVENTION
                or t["disposition"] == "blocked"
            ),
        }

    today_start, today_end = _local_day_bounds(tz, 0)
    yest_start, yest_end = _local_day_bounds(tz, 1)
    week_start = (
        datetime.fromisoformat(today_start) - timedelta(days=7)
    ).isoformat()

    return {
        "schema_version": SCHEMA_VERSION,
        "synthetic": mode == "simulation",
        "generated_at": generated_at.isoformat(),
        "timezone": tz_name,
        "periods": [
            period("today_to_date", today_start, today_end),
            period("previous_day", yest_start, yest_end),
            period("rolling_7d", week_start, today_end),
        ],
        "totals": {
            "tasks": len(tasks),
            "active": sum(
                1 for t in tasks
                if t["disposition"] == "active"
            ),
            "verified": sum(1 for t in tasks if t["validation"] == "verified"),
            "merged": sum(1 for t in tasks if t["review"] == "merged"),
            "blocked": sum(1 for t in tasks if t["disposition"] == "blocked"),
            "observed_acus": (
                round(acu_total, 2) if acu_known else None
            ),
            "acus_scope": "cumulative ACUs for included sessions"
                          if acu_known else "incomplete usage data",
        },
        "tasks": tasks,
    }


def persist_snapshot(
    conn: sqlite3.Connection, mode: str, tz_name: str,
    report_type: str = "summary",
) -> tuple[int, str, dict]:
    content = snapshot_content(conn, mode, tz_name)
    canonical = db.dumps(content)
    sha = hashlib.sha256(canonical.encode()).hexdigest()
    existing = conn.execute(
        "SELECT id FROM report_snapshots WHERE sha256 = ?", (sha,)
    ).fetchone()
    if existing:
        return int(existing["id"]), sha, content
    cur = conn.execute(
        """INSERT INTO report_snapshots
           (mode, report_type, period_json, timezone, schema_version, title,
            content_json, sha256, generated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            mode, report_type, db.dumps(content["periods"]), tz_name,
            SCHEMA_VERSION,
            f"Repair Desk facts snapshot {sha[:12]}", canonical, sha,
            db.now(),
        ),
    )
    return int(cur.lastrowid), sha, content


def render_markdown(content: dict) -> str:
    lines = [
        "## Repair Desk report data (generated)",
        "",
        f"- schema_version: {content['schema_version']}",
        f"- generated_at: {content['generated_at']}",
        f"- timezone: {content['timezone']}",
        f"- mode: {'SIMULATION — synthetic data' if content['synthetic'] else 'live'}",
        "",
        "| period | UTC start | UTC end | accepted | verified | merged | needs intervention |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for p in content["periods"]:
        lines.append(
            f"| {p['name']} | {p['utc_start']} | {p['utc_end']} | "
            f"{p['accepted_tasks']} | {p['verified_deliveries']} | "
            f"{p['merged_prs']} | {p['needs_intervention']} |"
        )
    totals = content["totals"]
    lines += [
        "",
        f"- tasks: {totals['tasks']} (active {totals['active']}, "
        f"verified {totals['verified']}, merged {totals['merged']}, "
        f"blocked {totals['blocked']})",
        f"- observed ACUs: {totals['observed_acus'] if totals['observed_acus'] is not None else 'unknown'} "
        f"({totals['acus_scope']})",
        "",
        "### Tasks",
    ]
    for t in content["tasks"]:
        lines.append(
            f"- #{t['id']} {t['issue']} — {t['title']} "
            f"[exec={t['execution']} val={t['validation']} "
            f"review={t['review']} disp={t['disposition']}] "
            f"PR={t['pr_url'] or 'n/a'}"
        )
    if content["synthetic"]:
        lines += ["", "_SYNTHETIC — generated by simulation mode._"]
    return "\n".join(lines)
