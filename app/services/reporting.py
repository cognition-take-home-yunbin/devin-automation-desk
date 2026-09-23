"""Facts snapshot generation (PRD §15–16, milestone D).

A snapshot is deterministic for a given database state: the canonical hash
covers periods + metrics + tasks + coverage but **not** ``generated_at`` —
identical data yields an identical sha256, which is what makes publication
idempotent (the publisher only rewrites the report issue on a material
change or once per configured interval). Windows are computed in
REPORT_TIMEZONE with explicit UTC boundaries; unknown fields stay
explicitly unknown (``null``) rather than reading as zero.

``coverage`` keeps managed repair accounting strictly separate from the
external Devin Automation's native report sessions: native rows are
read-only observations, and a native session's status says nothing about
Slack delivery (which the desk never infers — only an operator-recorded
link can say a post landed).
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from .. import db

SCHEMA_VERSION = 2

_TERMINAL_EXECUTION = ("agent_finished", "failed", "stopped")
_NEEDS_INTERVENTION = ("needs_input", "approval_required")

# Marker embedded in the published body so a read-back can identify exactly
# which snapshot is live — the reconcile path compares it to the sha it
# tried to write, never to a guess.
SHA_MARKER_RE = re.compile(r"<!--\s*report-sha256:([0-9a-f]{64})\s*-->")

# Credential-shaped strings must never reach the published body, even if a
# field upstream picked one up by accident.
_SECRET_PATTERNS = re.compile(
    r"(sk-[A-Za-z0-9_-]{8,}|ghp_[A-Za-z0-9]{8,}|gho_[A-Za-z0-9]{8,}|"
    r"github_pat_[A-Za-z0-9_]{8,}|Bearer\s+[A-Za-z0-9._-]{10,}|"
    r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,})"
)


def scrub(text: str) -> str:
    """Remove credential-shaped strings from publishable content."""
    return _SECRET_PATTERNS.sub("[redacted]", text)


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


def _task_evidence_links(conn: sqlite3.Connection, task_id: int) -> list[dict]:
    """URIs + labels for a task's evidence — links and provenance only,
    never evidence bodies (which may embed issue text or operator notes)."""
    rows = conn.execute(
        "SELECT kind, title, uri, verifier FROM evidence "
        "WHERE task_id = ? AND uri IS NOT NULL ORDER BY id",
        (task_id,),
    ).fetchall()
    return [
        {
            "kind": r["kind"],
            "title": scrub(r["title"]),
            "uri": r["uri"],
            "verifier": r["verifier"],
        }
        for r in rows
    ]


def _native_sessions(conn: sqlite3.Connection, mode: str) -> list[dict]:
    rows = conn.execute(
        "SELECT session_id, url, status, status_detail, acu_used, "
        "slack_link, slack_source, last_seen_at FROM native_sessions "
        "WHERE mode = ? ORDER BY session_id",
        (mode,),
    ).fetchall()
    return [
        {
            "session_id": r["session_id"],
            "url": r["url"],
            "status": r["status"],
            "status_detail": r["status_detail"],
            "acu_used": r["acu_used"],  # None -> rendered as unknown
            "slack_link": r["slack_link"],
            "last_seen_at": r["last_seen_at"],
        }
        for r in rows
    ]


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
    # Zero sessions = nothing observed yet → usage is unknown, not zero.
    acu_known = bool(rows)
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
                "title": scrub(t["issue_title"]),
                "execution": t["execution"],
                "validation": t["validation"],
                "review": t["review"],
                "disposition": t["disposition"],
                "pr_url": t["pr_url"],
                "session_url": t["devin_session_url"],
                "slack_link": t["slack_link"],
                "accepted_at": t["accepted_at"],
                "acu_used": acu,  # None -> unknown, not zero
                "evidence_links": _task_evidence_links(conn, t["id"]),
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
            "manually_verified": sum(
                1 for t in subset
                if t["validation"] == "manually_verified"
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

    native = _native_sessions(conn, mode)
    native_acus = [n["acu_used"] for n in native]
    native_total = (
        round(sum(a for a in native_acus if a is not None), 2)
        if native and all(a is not None for a in native_acus)
        else None
    )

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
            "manually_verified": sum(
                1 for t in tasks
                if t["validation"] == "manually_verified"
            ),
            "merged": sum(1 for t in tasks if t["review"] == "merged"),
            "blocked": sum(1 for t in tasks if t["disposition"] == "blocked"),
            "observed_acus": (
                round(acu_total, 2) if acu_known else None
            ),
            "acus_scope": "cumulative ACUs for included sessions"
                          if acu_known else "incomplete usage data",
        },
        "coverage": {
            "managed": {
                "sessions": len(tasks),
                "observed_acus": (
                    round(acu_total, 2) if acu_known else None
                ),
            },
            "native": {
                "sessions": native,
                "sessions_seen": len(native),
                "observed_acus": native_total,
                "note": (
                    "native sessions are read-only observations; a "
                    "session's status does not imply Slack delivery"
                ),
            },
        },
        "tasks": tasks,
    }


def canonical_hash(content: dict) -> str:
    """sha256 over the canonical content minus wall-clock fields.

    ``generated_at`` (and per-task ``last_seen_at``) would make every run
    produce a new hash and break idempotent publication.
    """
    canonical = {
        k: v for k, v in content.items() if k != "generated_at"
    }
    for n in canonical["coverage"]["native"]["sessions"]:
        n.pop("last_seen_at", None)
    return hashlib.sha256(db.dumps(canonical).encode()).hexdigest()


def persist_snapshot(
    conn: sqlite3.Connection, mode: str, tz_name: str,
    report_type: str = "summary",
) -> tuple[int, str, dict]:
    content = snapshot_content(conn, mode, tz_name)
    canonical = db.dumps(content)
    sha = canonical_hash(content)
    existing = conn.execute(
        "SELECT id FROM report_snapshots WHERE sha256 = ?", (sha,)
    ).fetchone()
    native = content["coverage"]["native"]["sessions"]
    latest_native = native[-1] if native else None
    if existing:
        return int(existing["id"]), sha, content
    cur = conn.execute(
        """INSERT INTO report_snapshots
           (mode, report_type, period_json, timezone, schema_version, title,
            content_json, sha256, generated_at, native_session_url,
            native_state, slack_link)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            mode, report_type, db.dumps(content["periods"]), tz_name,
            SCHEMA_VERSION,
            f"Repair Desk facts snapshot {sha[:12]}", canonical, sha,
            db.now(),
            latest_native["url"] if latest_native else None,
            latest_native["status"] if latest_native else None,
            latest_native["slack_link"] if latest_native else None,
        ),
    )
    return int(cur.lastrowid), sha, content


def render_markdown(content: dict, sha: str) -> str:
    lines = [
        "<!-- repairdesk-report-data -->",
        f"<!-- report-sha256:{sha} -->",
        "## Repair Desk report data (generated)",
        "",
        f"- schema_version: {content['schema_version']}",
        f"- generated_at: {content['generated_at']}",
        f"- timezone: {content['timezone']}",
        f"- mode: {'SIMULATION — synthetic data' if content['synthetic'] else 'live'}",
        "",
        "| period | UTC start | UTC end | accepted | verified | manual | merged | needs intervention |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for p in content["periods"]:
        lines.append(
            f"| {p['name']} | {p['utc_start']} | {p['utc_end']} | "
            f"{p['accepted_tasks']} | {p['verified_deliveries']} | "
            f"{p['manually_verified']} | {p['merged_prs']} | "
            f"{p['needs_intervention']} |"
        )
    totals = content["totals"]
    lines += [
        "",
        f"- tasks: {totals['tasks']} (active {totals['active']}, "
        f"verified {totals['verified']}, manual "
        f"{totals['manually_verified']}, merged {totals['merged']}, "
        f"blocked {totals['blocked']})",
        f"- observed ACUs (managed): "
        f"{totals['observed_acus'] if totals['observed_acus'] is not None else 'unknown'} "
        f"({totals['acus_scope']})",
        "",
        "### Coverage",
    ]
    native = content["coverage"]["native"]
    lines.append(
        f"- managed repair sessions: "
        f"{content['coverage']['managed']['sessions']} "
        f"(ACUs {content['coverage']['managed']['observed_acus'] if content['coverage']['managed']['observed_acus'] is not None else 'unknown'})"
    )
    lines.append(
        f"- native report sessions observed: {native['sessions_seen']} "
        f"(ACUs {native['observed_acus'] if native['observed_acus'] is not None else 'unknown'}) — "
        "session status does not imply Slack delivery"
    )
    for n in native["sessions"]:
        slack = f" · slack {n['slack_link']}" if n["slack_link"] else ""
        lines.append(
            f"  - {n['url'] or n['session_id']} — {n['status']}"
            f" ({n['status_detail']}), ACUs "
            f"{n['acu_used'] if n['acu_used'] is not None else 'unknown'}"
            f"{slack}"
        )
    lines += ["", "### Tasks"]
    for t in content["tasks"]:
        lines.append(
            f"- #{t['id']} {t['issue']} — {t['title']} "
            f"[exec={t['execution']} val={t['validation']} "
            f"review={t['review']} disp={t['disposition']}] "
            f"PR={t['pr_url'] or 'n/a'}"
        )
        for e in t["evidence_links"]:
            lines.append(
                f"  - {e['kind']} ({e['verifier'] or 'recorded'}): "
                f"{e['title']} — {e['uri']}"
            )
    if content["synthetic"]:
        lines += ["", "_SYNTHETIC — generated by simulation mode._"]
    return "\n".join(lines)
