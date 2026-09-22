"""Scenario seeding for simulation mode.

Every scenario inserts synthetic fixture rows (``sim_*`` tables — labelled
synthetic throughout the UI and exports) and enqueues a ``scan_issues`` job;
the durable worker then drives the normal orchestration. Seeding is strictly
additive: ``INSERT OR IGNORE`` on natural keys means re-running a scenario
never resets or deletes existing state, and a second scan of the same issue
dedupes instead of duplicating work.
"""

from __future__ import annotations

import sqlite3

from .. import db
from ..config import Settings
from . import jobs

SCENARIOS = (
    "happy-path",
    "duplicate-scan",
    "needs-input",
    "checks-failed",
    "creation-unknown",
    "throttled",
    "stale-checks",
    "report-failure",
)

# Fixed, disjoint issue numbers keep repeated scenario runs idempotent.
_ISSUE = {
    "happy-path": 101,
    "duplicate-scan": 108,
    "needs-input": 102,
    "checks-failed": 103,
    "creation-unknown": 104,
    "throttled": 105,
    "stale-checks": 106,
    "report-failure": 107,
}

WORKFLOW = "pilot-validation"
CHECKS_OK = [("regression-test", "success"), ("lint", "success")]


def _seed_issue(
    conn: sqlite3.Connection,
    repo: str,
    number: int,
    title: str,
    *,
    labels: list[str],
    approved_by: str | None,
    approval_label: str,
    session_script: dict | None = None,
) -> None:
    conn.execute(
        """INSERT OR IGNORE INTO sim_issues
           (repo, number, title, state, labels_json, body, session_script_json)
           VALUES (?, ?, ?, 'open', ?, ?, ?)""",
        (
            repo, number, title,
            db.dumps(labels),
            "SYNTHETIC issue body — simulation fixture, not a real report.",
            db.dumps(session_script) if session_script else None,
        ),
    )
    if approved_by:
        conn.execute(
            """INSERT OR IGNORE INTO sim_issue_events
               (repo, issue_number, event_id, event, label, actor, created_at)
               VALUES (?, ?, ?, 'labeled', ?, ?, ?)""",
            (repo, number, f"sim-evt-{number}-approve", approval_label,
             approved_by, db.now()),
        )


def _seed_checks(
    conn: sqlite3.Connection,
    repo: str,
    issue_number: int,
    checks: list[tuple[str, str]],
    head_sha: str | None = None,
) -> None:
    pr_number = 1000 + issue_number
    sha = head_sha or f"simsha{pr_number:034d}"[:40]
    for name, conclusion in checks:
        conn.execute(
            """INSERT OR IGNORE INTO sim_check_runs
               (repo, pr_number, name, workflow, conclusion, head_sha, url)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (repo, pr_number, name, WORKFLOW, conclusion, sha,
             f"https://github.example.invalid/{repo}/checks/{pr_number}/{name}"),
        )


def _script_error(
    conn: sqlite3.Connection, operation: str, error: str, times: int
) -> None:
    conn.execute(
        """INSERT INTO sim_call_scripts (operation, remaining, error)
           VALUES (?, ?, ?)
           ON CONFLICT(operation) DO UPDATE
             SET remaining = sim_call_scripts.remaining + excluded.remaining,
                 error = excluded.error""",
        (operation, times, error),
    )


def seed_scenario(
    conn: sqlite3.Connection, settings: Settings, scenario: str
) -> dict:
    """Seed fixtures and enqueue the scan job. Returns a summary dict.
    Never resets or deletes existing state."""
    if scenario not in SCENARIOS:
        raise ValueError(
            f"unknown scenario {scenario!r}; choices: {', '.join(SCENARIOS)}"
        )
    repo = settings.github_repo or "acme/superset-demo"
    approver = (
        settings.github_allowed_approvers[0]
        if settings.github_allowed_approvers
        else "ops-lead"
    )
    cand, appr = settings.candidate_issue_label, settings.approval_issue_label
    n = _ISSUE[scenario]
    notes: list[str] = []

    with db.transaction(conn):
        # Every deployment has one fixed report-source issue the publisher
        # writes to (the fake counterpart of REPORT_DATA_ISSUE_NUMBER).
        conn.execute(
            """INSERT OR IGNORE INTO sim_report_issue
               (repo, issue_number, revision, body)
               VALUES (?, ?, 0, 'SYNTHETIC report-source issue (empty)')""",
            (settings.report_github_repo or "acme/devin-repair-desk",
             settings.report_data_issue_number or 1),
        )

        if scenario == "happy-path":
            _seed_issue(
                conn, repo, n,
                "Mixed Chart keeps metric prefixes when Truncate Metric is on",
                labels=[cand, appr], approved_by=approver, approval_label=appr,
            )
            _seed_checks(conn, repo, n, CHECKS_OK)

        elif scenario == "duplicate-scan":
            _seed_issue(
                conn, repo, n,
                "Dashboard export drops native filter names",
                labels=[cand, appr], approved_by=approver, approval_label=appr,
            )
            _seed_checks(conn, repo, n, CHECKS_OK)
            # Two scans in one run: the second must dedupe, not duplicate.
            jobs.enqueue(conn, "scan_issues", {"scenario": scenario},
                         mode="simulation", delay=0.05)
            notes.append("an extra scan runs immediately; the task must dedupe")

        elif scenario == "needs-input":
            _seed_issue(
                conn, repo, n,
                "Time-range filter ignores dashboard default",
                labels=[cand, appr], approved_by=approver, approval_label=appr,
                session_script={
                    "sequence": ["working", "needs_input"],
                    "notes": {"question": "Which baseline SHA should I use?"},
                },
            )

        elif scenario == "checks-failed":
            _seed_issue(
                conn, repo, n,
                "CSV export truncates UTF-8 headers",
                labels=[cand, appr], approved_by=approver, approval_label=appr,
            )
            _seed_checks(conn, repo, n,
                         [("regression-test", "failure"), ("lint", "success")])

        elif scenario == "creation-unknown":
            _seed_issue(
                conn, repo, n,
                "Alert modal submits twice on Enter",
                labels=[cand, appr], approved_by=approver, approval_label=appr,
            )
            _seed_checks(conn, repo, n, CHECKS_OK)
            _script_error(conn, "devin.create_session",
                          "ambiguous_creation", times=1)
            notes.append(
                "create_session is ambiguous once; reconcile resolves it via "
                "correlation tag rather than retrying"
            )

        elif scenario == "throttled":
            _seed_issue(
                conn, repo, n,
                "Chart tooltip shows stale data after refresh",
                labels=[cand, appr], approved_by=approver, approval_label=appr,
            )
            _seed_checks(conn, repo, n, CHECKS_OK)
            _script_error(conn, "github.list_candidate_issues",
                          "rate_limited", times=2)
            notes.append("scan is rate-limited twice; job retry recovers")

        elif scenario == "stale-checks":
            _seed_issue(
                conn, repo, n,
                "Dashboard link share ignores permalink tab",
                labels=[cand, appr], approved_by=approver, approval_label=appr,
            )
            # Check runs exist but belong to an older head SHA -> stale.
            _seed_checks(conn, repo, n, CHECKS_OK, head_sha="0" * 40)
            notes.append(
                "check-run head_sha mismatches PR head; verification goes "
                "stale, retries, then lands validation=unknown"
            )

        elif scenario == "report-failure":
            _seed_issue(
                conn, repo, n,
                "SQL Lab stops polling query results",
                labels=[cand, appr], approved_by=approver, approval_label=appr,
            )
            _seed_checks(conn, repo, n, CHECKS_OK)
            _script_error(conn, "report.publish_snapshot", "boom", times=3)
            notes.append(
                "report publication fails 3x; failed publication records are "
                "kept, the job retries, then succeeds"
            )

        # Scan jobs dedupe at the task level, so a plain enqueue is correct —
        # re-running a scenario must not be blocked by a consumed dedup key.
        jobs.enqueue(conn, "scan_issues", {"scenario": scenario},
                     mode="simulation")

    return {
        "scenario": scenario,
        "issue_number": n,
        "repo": repo,
        "notes": notes,
        "synthetic": True,
    }
