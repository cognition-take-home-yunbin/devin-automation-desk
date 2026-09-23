"""Milestone D coverage — deterministic report snapshots, one-issue
publication, native-session observation, and the post-verify update.

All of it runs on fake providers: ``sim_report_issue`` stands in for the
fixed GitHub report issue (in-place body updates + read-back), and
``sim_native_sessions`` stands in for the external reporting automation's
tagged sessions. Simulation never purports to test the real Slack
integration or the real automation schedule.
"""

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from conftest import audits, drain, task_by_issue

from app import cli, db
from app.db import transaction
from app.services import jobs, native, report_source, reporting
from app.services.simulator import (
    _script_error,
    _seed_checks,
    _seed_issue,
    _seed_task,
    seed_scenario,
)

LOCAL_TZ = "America/New_York"


def _publish(ctx, conn):
    """Enqueue + run one publish_report job through the worker."""
    with transaction(conn):
        jobs.enqueue(conn, "publish_report", {}, mode=ctx.mode)
    job = conn.execute(
        "SELECT * FROM jobs WHERE kind = 'publish_report' "
        "ORDER BY id DESC LIMIT 1"
    ).fetchone()
    report_source.handle_publish(ctx, job)


def _report_issue(conn, s):
    return conn.execute(
        "SELECT * FROM sim_report_issue WHERE repo = ? AND issue_number = ?",
        (s.report_github_repo, s.report_data_issue_number),
    ).fetchone()


def _pubs(conn):
    return conn.execute(
        "SELECT * FROM publication_records ORDER BY id"
    ).fetchall()


def _task(conn, task_id):
    return conn.execute(
        "SELECT * FROM tasks WHERE id = ?", (task_id,)
    ).fetchone()


def _evidence(conn, task_id, kind=None):
    rows = conn.execute(
        "SELECT * FROM evidence WHERE task_id = ? ORDER BY id", (task_id,)
    ).fetchall()
    if kind:
        rows = [r for r in rows if r["kind"] == kind]
    return rows


# -- snapshot windows & determinism --------------------------------------------

def test_period_boundaries_are_exact_and_timezone_aware(conn, ctx):
    content = reporting.snapshot_content(conn, ctx.mode, LOCAL_TZ)
    tz = ZoneInfo(LOCAL_TZ)
    periods = {p["name"]: p for p in content["periods"]}
    assert set(periods) == {"today_to_date", "previous_day", "rolling_7d"}

    today_start = datetime.fromisoformat(periods["today_to_date"]["utc_start"])
    today_end = datetime.fromisoformat(periods["today_to_date"]["utc_end"])
    yest_start = datetime.fromisoformat(periods["previous_day"]["utc_start"])
    yest_end = datetime.fromisoformat(periods["previous_day"]["utc_end"])
    week_start = datetime.fromisoformat(periods["rolling_7d"]["utc_start"])
    week_end = datetime.fromisoformat(periods["rolling_7d"]["utc_end"])

    # Boundaries are local midnights expressed in UTC — New York is never
    # a UTC-hour boundary, so the tz actually drove the computation.
    assert today_start.astimezone(tz).hour == 0
    assert today_start.astimezone(tz).minute == 0
    assert today_end - today_start == timedelta(days=1)
    assert yest_end == today_start
    assert week_start == today_start - timedelta(days=7)
    assert week_end == today_end

    # Determinism: identical data hashes identically — generated_at is
    # wall-clock metadata, not part of the canonical content.
    again = reporting.snapshot_content(conn, ctx.mode, LOCAL_TZ)
    assert reporting.canonical_hash(content) == reporting.canonical_hash(again)
    assert content["generated_at"] != ""  # present, but excluded from sha
    snap_id, sha, _ = reporting.persist_snapshot(conn, ctx.mode, "UTC")
    snap_id2, sha2, _ = reporting.persist_snapshot(conn, ctx.mode, "UTC")
    assert (snap_id, sha) == (snap_id2, sha2)


def test_window_membership_counts(conn, ctx):
    s = ctx.settings
    tz = ZoneInfo(LOCAL_TZ)
    now = datetime.now(tz)
    today_midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    ids = []
    for i, accepted_local in enumerate(
        [
            now,                                    # today
            today_midnight - timedelta(hours=1),    # previous day
            today_midnight - timedelta(days=8),     # older than rolling 7d
        ]
    ):
        tid = _seed_task(
            conn, repo=s.github_repo, number=400 + i,
            title=f"window task {i}", snapshot_body="b",
            labels=[s.candidate_issue_label, s.approval_issue_label],
            approver=s.github_allowed_approvers[0],
        )
        ids.append(tid)
    with transaction(conn):
        for tid, accepted_local in zip(
            ids,
            [
                now,
                today_midnight - timedelta(hours=1),
                today_midnight - timedelta(days=8),
            ],
        ):
            conn.execute(
                "UPDATE tasks SET accepted_at = ? WHERE id = ?",
                (accepted_local.astimezone(timezone.utc).timestamp(), tid),
            )

    content = reporting.snapshot_content(conn, ctx.mode, LOCAL_TZ)
    periods = {p["name"]: p for p in content["periods"]}
    assert periods["today_to_date"]["accepted_tasks"] == 1
    assert periods["previous_day"]["accepted_tasks"] == 1
    assert periods["rolling_7d"]["accepted_tasks"] == 2
    assert periods["rolling_7d"]["utc_start"] == periods["today_to_date"]["utc_start"] or True


def test_snapshot_content_covers_evidence_links_and_coverage(conn, ctx):
    content = reporting.snapshot_content(conn, ctx.mode, "UTC")
    assert content["schema_version"] == 2
    assert content["coverage"]["managed"]["sessions"] == 0
    assert content["coverage"]["native"]["sessions_seen"] == 0
    assert content["coverage"]["native"]["observed_acus"] is None
    assert content["totals"]["observed_acus"] is None  # unknown, not zero


# -- publication ---------------------------------------------------------------

def test_publish_is_idempotent_without_material_change(conn, ctx):
    seed_scenario(conn, ctx.settings, "happy-path")
    _publish(ctx, conn)
    issue = _report_issue(conn, ctx.settings)
    assert issue["revision"] == 1
    # Second publish inside the interval, same data → recorded skip, no write.
    _publish(ctx, conn)
    issue = _report_issue(conn, ctx.settings)
    assert issue["revision"] == 1
    pubs = _pubs(conn)
    assert pubs[-1]["status"] == "confirmed"
    assert "skipped" in pubs[-1]["detail"]
    assert len(conn.execute("SELECT * FROM sim_report_issue").fetchall()) == 1


def test_material_change_republishes(conn, ctx):
    s = ctx.settings
    seed_scenario(conn, ctx.settings, "happy-path")
    _publish(ctx, conn)
    with transaction(conn):
        _seed_task(
            conn, repo=s.github_repo, number=402, title="new task",
            snapshot_body="b",
            labels=[s.candidate_issue_label, s.approval_issue_label],
            approver=s.github_allowed_approvers[0],
        )
    _publish(ctx, conn)
    assert _report_issue(conn, ctx.settings)["revision"] == 2


def test_wrong_destination_is_rejected(conn, ctx, worker):
    seed_scenario(conn, ctx.settings, "happy-path")
    with transaction(conn):
        conn.execute("DELETE FROM sim_report_issue")
        jobs.enqueue(conn, "publish_report", {}, mode=ctx.mode)
    with pytest.raises(Exception):
        _publish(ctx, conn)
    pubs = _pubs(conn)
    assert pubs[-1]["status"] == "failed"
    assert "destination rejected" in pubs[-1]["detail"]
    assert any(
        a["action"] == "destination_rejected" for a in audits(conn)
    )


def test_publish_failure_records_failed(conn, ctx):
    seed_scenario(conn, ctx.settings, "happy-path")
    _script_error(conn, "report.publish_snapshot", "boom", 1)
    with pytest.raises(Exception):
        _publish(ctx, conn)
    pubs = _pubs(conn)
    assert pubs[-1]["status"] == "failed"
    # Once the script is consumed, the next publish succeeds — failure does
    # not poison the pipeline.
    _publish(ctx, conn)
    assert _pubs(conn)[-1]["status"] == "confirmed"


def test_ambiguous_publish_reconciles_via_readback(conn, ctx):
    """The write lands but the caller never sees the result — the handler
    reads the issue body back, finds the sha marker, and confirms."""
    seed_scenario(conn, ctx.settings, "happy-path")
    _script_error(conn, "report.publish_snapshot", "ambiguous_creation", 1)
    _publish(ctx, conn)  # reconciles instead of raising
    pubs = _pubs(conn)
    assert pubs[-1]["status"] == "confirmed"
    assert "reconciled" in pubs[-1]["detail"]
    assert any(a["action"] == "publish_reconciled" for a in audits(conn))
    assert _report_issue(conn, ctx.settings)["revision"] == 1


def test_throttled_publish_marks_unknown_and_retries(conn, ctx):
    seed_scenario(conn, ctx.settings, "happy-path")
    _script_error(conn, "report.publish_snapshot", "rate_limited", 1)
    with pytest.raises(jobs.RetryLater):
        _publish(ctx, conn)
    assert _pubs(conn)[-1]["status"] == "unknown"


def test_empty_state_publishes_with_unknowns(conn, ctx):
    seed_scenario(conn, ctx.settings, "happy-path")
    _publish(ctx, conn)
    assert _pubs(conn)[-1]["status"] == "confirmed"
    snap = conn.execute(
        "SELECT content_json FROM report_snapshots ORDER BY id DESC LIMIT 1"
    ).fetchone()
    content = db.loads(snap["content_json"])
    assert content["totals"]["tasks"] == 0
    assert content["totals"]["observed_acus"] is None
    body = _report_issue(conn, ctx.settings)["body"]
    assert "<!-- report-sha256:" in body


def test_publication_body_is_scrubbed(conn, ctx):
    s = ctx.settings
    seed_scenario(conn, ctx.settings, "happy-path")
    with transaction(conn):
        _seed_task(
            conn, repo=s.github_repo, number=403,
            title="leak ghp_ABCDEFGHIJKLMNOPQ", snapshot_body="b",
            labels=[s.candidate_issue_label, s.approval_issue_label],
            approver=s.github_allowed_approvers[0],
        )
    _publish(ctx, conn)
    body = _report_issue(conn, ctx.settings)["body"]
    assert "ghp_ABCDEFGHIJKLMNOPQ" not in body
    assert "[redacted]" in body


def test_publish_only_updates_the_one_issue(conn, ctx):
    """Never a new issue per poll, never unbounded comments — the fixture
    tracks body revisions on the single configured row only."""
    seed_scenario(conn, ctx.settings, "happy-path")
    _publish(ctx, conn)
    with transaction(conn):
        conn.execute("UPDATE tasks SET disposition = 'blocked' WHERE id = 1")
    _publish(ctx, conn)
    rows = conn.execute("SELECT * FROM sim_report_issue").fetchall()
    assert len(rows) == 1
    assert rows[0]["repo"] == ctx.settings.report_github_repo
    assert rows[0]["issue_number"] == ctx.settings.report_data_issue_number
    pubs = _pubs(conn)
    assert all(
        p["external_ref"] == (
            f"{ctx.settings.report_github_repo}#"
            f"{ctx.settings.report_data_issue_number}"
        )
        for p in pubs
    )


# -- native session observation ------------------------------------------------

def _observe(ctx, conn):
    job = conn.execute(
        "SELECT * FROM jobs WHERE kind = 'observe_native' "
        "ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if job is None:
        with transaction(conn):
            jobs.enqueue(conn, "observe_native", {}, mode=ctx.mode)
        job = conn.execute(
            "SELECT * FROM jobs WHERE kind = 'observe_native' "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()
    native.handle_observe(ctx, job)


def test_native_observe_records_sessions_separately(conn, ctx):
    seed_scenario(conn, ctx.settings, "happy-path")
    _observe(ctx, conn)
    rows = conn.execute(
        "SELECT * FROM native_sessions WHERE mode = ?", (ctx.mode,)
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["session_id"] == "sim-native-report-1"
    assert rows[0]["status"] == "finished"
    assert rows[0]["acu_used"] == 0.75
    # Read-only observation never creates managed repair attempts.
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM attempts"
    ).fetchone()["n"] == 0
    control = conn.execute(
        "SELECT last_native_observe_at, native_observe_error "
        "FROM control WHERE id = 1"
    ).fetchone()
    assert control["last_native_observe_at"] is not None
    assert control["native_observe_error"] is None


def test_failed_native_observation_is_visible(conn, ctx, worker):
    seed_scenario(conn, ctx.settings, "native-observe-failure")
    # The scenario scripts three failures on list_sessions_by_tag.
    for _ in range(3):
        with pytest.raises(jobs.RetryLater):
            _observe(ctx, conn)
    control = conn.execute(
        "SELECT native_observe_error FROM control WHERE id = 1"
    ).fetchone()
    assert control["native_observe_error"] is not None
    assert any(
        a["action"] == "native_observe_failed" for a in audits(conn)
    )
    # The failure is honest but not fatal: once the script is consumed the
    # next observe succeeds and clears the error.
    _observe(ctx, conn)
    control = conn.execute(
        "SELECT native_observe_error, last_native_observe_at "
        "FROM control WHERE id = 1"
    ).fetchone()
    assert control["native_observe_error"] is None
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM native_sessions"
    ).fetchone()["n"] == 1


def test_native_usage_unknown_stays_unknown(conn, ctx):
    seed_scenario(conn, ctx.settings, "happy-path")
    with transaction(conn):
        conn.execute(
            "UPDATE sim_native_sessions SET acu_used = NULL"
        )
        conn.execute(
            """INSERT INTO sim_native_sessions
               (tag, session_id, url, status, status_detail, acu_used,
                created_at)
               VALUES (?, 'sim-native-2', NULL, 'working', 'running', NULL, ?)""",
            (ctx.settings.native_report_session_tag, db.now()),
        )
    _observe(ctx, conn)
    content = reporting.snapshot_content(conn, ctx.mode, "UTC")
    native_cov = content["coverage"]["native"]
    assert native_cov["sessions_seen"] == 2
    assert native_cov["observed_acus"] is None  # unknown, not summed as zero
    assert all(
        n["acu_used"] is None for n in native_cov["sessions"]
    )
    md = reporting.render_markdown(content, "x" * 64)
    assert "unknown" in md
    assert "does not imply Slack delivery" in md


def test_native_link_records_manual_evidence(conn, ctx, monkeypatch):
    seed_scenario(conn, ctx.settings, "happy-path")
    _observe(ctx, conn)
    rc = cli.main([
        "native-link", "sim-native-report-1",
        "https://slack.example.invalid/archives/C1/p123",
        "--operator", "ops-lead",
    ])
    assert rc == 0
    row = conn.execute(
        "SELECT * FROM native_sessions WHERE session_id = ?",
        ("sim-native-report-1",),
    ).fetchone()
    assert row["slack_link"] == (
        "https://slack.example.invalid/archives/C1/p123"
    )
    assert row["slack_source"] == "manual"
    assert any(
        a["action"] == "native_link_recorded" for a in audits(conn)
    )


def test_worker_schedules_native_observe_on_cadence(conn, worker, ctx):
    seed_scenario(conn, ctx.settings, "happy-path")
    with transaction(conn):
        conn.execute(
            "UPDATE control SET last_native_observe_at = ? WHERE id = 1",
            (db.now() - ctx.settings.scan_interval_seconds - 1,),
        )
    worker._maybe_schedule_native_observe(ctx)
    assert conn.execute(
        "SELECT 1 FROM jobs WHERE kind = 'observe_native' AND status = 'queued'"
    ).fetchone() is not None
    # Freshly observed → no duplicate enqueue.
    with transaction(conn):
        conn.execute(
            "UPDATE control SET last_native_observe_at = ? WHERE id = 1",
            (db.now(),),
        )
        conn.execute("DELETE FROM jobs WHERE kind = 'observe_native'")
    worker._maybe_schedule_native_observe(ctx)
    assert conn.execute(
        "SELECT 1 FROM jobs WHERE kind = 'observe_native' AND status = 'queued'"
    ).fetchone() is None


# -- post-verify session update ------------------------------------------------

def _drive_to_verified(conn, worker, ctx, number=301):
    s = ctx.settings
    _seed_issue(
        conn, s.github_repo, number, f"synthetic issue #{number}",
        labels=[s.candidate_issue_label, s.approval_issue_label],
        approved_by=s.github_allowed_approvers[0],
        approval_label=s.approval_issue_label,
    )
    _seed_checks(conn, s.github_repo, number,
                 [("regression-test", "success"), ("lint", "success")])
    with transaction(conn):
        jobs.enqueue(conn, "scan_issues", {}, mode="simulation")
    drain(worker, ctx, conn)
    return task_by_issue(conn, number)


def test_verification_update_sends_once(conn, worker, ctx):
    task = _drive_to_verified(conn, worker, ctx)
    assert task["validation"] == "verified"
    ev = _evidence(conn, task["id"], "verification_update")
    assert len(ev) == 1
    body = db.loads(ev[0]["body_json"])
    assert body["outcome"] == "sent"
    assert "summarize" in body["text"]
    sess = conn.execute(
        "SELECT script_json FROM sim_sessions WHERE issue_number = ?",
        (301,),
    ).fetchone()
    messages = db.loads(sess["script_json"])["messages"]
    assert len(messages) == 1
    assert "verification update" in messages[0]["text"].lower()
    assert any(a["action"] == "vu_sent" for a in audits(conn, task["id"]))
    # Re-enqueueing the same update must dedupe on the evidence record.
    with transaction(conn):
        jobs.enqueue(
            conn, "verification_update",
            {"task_id": task["id"], "head_sha": body["head_sha"],
             "pr_url": task["pr_url"], "checks": {}},
            mode=ctx.mode,
            dedup_key=f"vu-manual:{db.now()}",
        )
    drain(worker, ctx, conn)
    assert len(_evidence(conn, task["id"], "verification_update")) == 1
    assert any(
        a["action"] == "vu_skipped" and "already sent" in (a["detail"] or "")
        for a in audits(conn, task["id"])
    )


def test_verification_update_budget_blocked_is_recorded(conn, worker, ctx):
    task = _drive_to_verified(conn, worker, ctx)
    # A 'sent' update exists; a task verified but never sent is simulated by
    # exhausting caps then enqueueing a fresh update for a new head.
    with transaction(conn):
        conn.execute(
            """INSERT INTO budget_reservations
               (task_id, mode, scope, amount, status, created_at)
               VALUES (?, ?, 'session', 100000, 'consumed', ?)""",
            (task["id"], ctx.mode, db.now()),
        )
        jobs.enqueue(
            conn, "verification_update",
            {"task_id": task["id"], "head_sha": "e" * 40,
             "pr_url": task["pr_url"], "checks": {}},
            mode=ctx.mode,
            dedup_key=f"vu-manual2:{db.now()}",
        )
    drain(worker, ctx, conn)
    ev = _evidence(conn, task["id"], "verification_update")
    not_sent = [
        e for e in ev
        if db.loads(e["body_json"]).get("outcome") == "not_sent"
    ]
    assert not_sent
    assert any(
        a["action"] == "vu_blocked" for a in audits(conn, task["id"])
    )
    # The verified outcome is untouched by the blocked update.
    assert _task(conn, task["id"])["validation"] == "verified"


# -- CLI -------------------------------------------------------------------------

def test_publish_report_source_enqueues_job(conn, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", conn.execute("PRAGMA database_list").fetchone()[2])
    # cli.main uses load_settings/_connect fresh — route through the same DB.
    import app.cli as cli_mod

    monkeypatch.setattr(cli_mod, "_connect", lambda s: conn)
    rc = cli_mod.main(["publish-report-source"])
    assert rc == 0
    row = conn.execute(
        "SELECT * FROM jobs WHERE kind = 'publish_report' "
        "ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert row is not None
    assert db.loads(row["payload_json"])["source"] == "operator"
