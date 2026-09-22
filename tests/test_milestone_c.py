"""Milestone C coverage — the hardened GitHub verifier, the manual-verification
fallback, and assertion/fact provenance.

All of it runs on fake providers: ``sim_*`` fixture tables stand in for GitHub
and the Devin API, and ``pr_head_sha_seq`` scripts a mid-verification push
(the stale-head race).
"""

from conftest import audits, drain, task_by_issue

from app import cli, db
from app.db import transaction
from app.services import jobs
from app.services.simulator import (
    _seed_checks,
    _seed_issue,
    seed_scenario,
)

SHA1 = "a" * 40
SHA2 = "b" * 40
SHA3 = "c" * 40


def run_scenario(conn, worker, ctx, name):
    result = seed_scenario(conn, ctx.settings, name)
    drain(worker, ctx, conn)
    return result


def _task(conn, task_id):
    return conn.execute(
        "SELECT * FROM tasks WHERE id = ?", (task_id,)
    ).fetchone()


def _evidence(conn, task_id):
    return conn.execute(
        "SELECT * FROM evidence WHERE task_id = ? ORDER BY id", (task_id,)
    ).fetchall()


def _seed_verifiable_issue(conn, s, number, *, session_script=None):
    _seed_issue(
        conn, s.github_repo, number, f"synthetic issue #{number}",
        labels=[s.candidate_issue_label, s.approval_issue_label],
        approved_by=s.github_allowed_approvers[0],
        approval_label=s.approval_issue_label,
        session_script=session_script,
    )
    with transaction(conn):
        jobs.enqueue(conn, "scan_issues", {}, mode="simulation")


def _drive_to_verified(conn, worker, ctx, number=201, *, script=None):
    _seed_verifiable_issue(conn, ctx.settings, number, session_script=script)
    _seed_checks(conn, ctx.settings.github_repo, number,
                 [("regression-test", "success"), ("lint", "success")])
    drain(worker, ctx, conn)
    return task_by_issue(conn, number)


# -- verifier -------------------------------------------------------------------


def test_successful_checks_verify(conn, worker, ctx):
    task = _drive_to_verified(conn, worker, ctx)
    assert task["validation"] == "verified"
    assert task["disposition"] == "delivered"
    ev = _evidence(conn, task["id"])
    passed = [e for e in ev if e["title"] == "Independent verification passed"]
    assert passed and passed[0]["verifier"] == "github-verifier"


def test_no_checks_never_verify(conn, worker, ctx):
    _seed_verifiable_issue(conn, ctx.settings, 202)
    drain(worker, ctx, conn)
    task = task_by_issue(conn, 202)
    assert task["validation"] == "checks_failed"
    assert task["disposition"] == "blocked"
    ev = db.loads(
        [e for e in _evidence(conn, task["id"])
         if e["title"] == "Independent verification failed"][0]["body_json"]
    )
    assert set(ev["missing"]) == {"regression-test", "lint"}


def test_failed_checks_never_verify(conn, worker, ctx):
    _seed_verifiable_issue(conn, ctx.settings, 203)
    _seed_checks(conn, ctx.settings.github_repo, 203,
                 [("regression-test", "failure"), ("lint", "success")])
    drain(worker, ctx, conn)
    task = task_by_issue(conn, 203)
    assert task["validation"] == "checks_failed"
    assert task["disposition"] == "blocked"


def test_skipped_and_neutral_checks_never_verify(conn, worker, ctx):
    _seed_verifiable_issue(conn, ctx.settings, 204)
    _seed_checks(conn, ctx.settings.github_repo, 204,
                 [("regression-test", "skipped"), ("lint", "neutral")])
    drain(worker, ctx, conn)
    task = task_by_issue(conn, 204)
    assert task["validation"] == "checks_failed"


def test_wrong_repository_is_flagged_out_of_scope(conn, worker, ctx):
    task = _drive_to_verified(
        conn, worker, ctx, number=205,
        script={"sequence": ["working", "finished"], "pr_number": 1205,
                "pr_url": "https://github.example.invalid/acme/superset-demo/pull/1205",
                "pr_head_sha": SHA1,
                "base_repo": "acme/other-repo"},
    )
    assert task["validation"] == "checks_failed"
    assert task["disposition"] == "blocked"
    acts = [a for a in audits(conn, task["id"])]
    assert any(a["action"] == "pr_out_of_scope" for a in acts)
    flags = [e for e in _evidence(conn, task["id"]) if e["kind"] == "flag"]
    assert flags and "acme/other-repo" in (flags[0]["title"] + str(flags[0]["body_json"]))


def test_untrusted_check_provenance_never_verifies(conn, worker, ctx):
    s = ctx.settings
    _seed_verifiable_issue(conn, s, 206)
    pr_number = 1000 + 206
    sha = f"simsha{pr_number:034d}"[:40]
    with transaction(conn):
        for name in ("regression-test", "lint"):
            conn.execute(
                "INSERT OR IGNORE INTO sim_check_runs "
                "(repo, pr_number, name, workflow, conclusion, head_sha) "
                "VALUES (?, ?, ?, 'rogue-ci', 'success', ?)",
                (s.github_repo, pr_number, name, sha),
            )
    drain(worker, ctx, conn)
    task = task_by_issue(conn, 206)
    assert task["validation"] == "checks_failed"
    ev = db.loads(
        [e for e in _evidence(conn, task["id"])
         if e["title"] == "Independent verification failed"][0]["body_json"]
    )
    assert ev["untrusted"] == ["regression-test", "lint"]


def test_head_moving_mid_verification_reverifies(conn, worker, ctx):
    """Stale-head race: checks green on sha1, but the head moved to sha2
    before the verdict — sha1 must never be 'verified'."""
    s = ctx.settings
    _seed_verifiable_issue(
        conn, s, 207,
        session_script={
            "sequence": ["working", "finished"],
            "pr_number": 1207,
            "pr_url": "https://github.example.invalid/acme/superset-demo/pull/1207",
            "pr_head_sha": SHA1,
            "pr_head_sha_seq": [SHA1, SHA2],
        },
    )
    _seed_checks(conn, s.github_repo, 207,
                 [("regression-test", "success"), ("lint", "success")],
                 head_sha=SHA1)
    # Drain until the first stale verdict, then let CI catch up to sha2.
    for _ in range(200):
        conn.execute("UPDATE jobs SET due_at = 0 WHERE status = 'queued'")
        if not worker.run_once(ctx):
            continue
        t = task_by_issue(conn, 207)
        if t and any(
            a["action"] == "checks_stale" for a in audits(conn, t["id"])
        ):
            break
    task = task_by_issue(conn, 207)
    assert task["validation"] in ("checks_pending", "pr_found")
    assert any(
        "moved" in (a["detail"] or "")
        for a in audits(conn, task["id"])
        if a["action"] == "checks_stale"
    )
    # New head gets its own green runs, then verification lands on sha2.
    with transaction(conn):
        conn.execute(
            "UPDATE sim_check_runs SET head_sha = ? "
            "WHERE pr_number = 1207", (SHA2,),
        )
    drain(worker, ctx, conn)
    task = task_by_issue(conn, 207)
    assert task["validation"] == "verified"
    assert task["head_sha"] == SHA2


def test_new_push_after_success_reopens_validation(conn, worker, ctx):
    s = ctx.settings
    task = _drive_to_verified(conn, worker, ctx, number=208)
    assert task["validation"] == "verified"
    # A push to the PR head re-opens verification on the new SHA.
    session = conn.execute(
        "SELECT * FROM sim_sessions WHERE issue_number = 208"
    ).fetchone()
    script = db.loads(session["script_json"], {})
    script["pr_head_sha"] = SHA3
    with transaction(conn):
        conn.execute(
            "UPDATE sim_sessions SET script_json = ? WHERE id = ?",
            (db.dumps(script), session["id"]),
        )
        conn.execute(
            "UPDATE sim_check_runs SET head_sha = ? "
            "WHERE pr_number = 1208", (SHA3,),
        )
        jobs.enqueue(
            conn, "verify_task",
            {"task_id": task["id"], "pr_number": 1208, "head_sha": SHA3},
            mode="simulation",
        )
    drain(worker, ctx, conn)
    task = _task(conn, task["id"])
    assert task["validation"] == "verified"
    assert task["head_sha"] == SHA3
    acts = [a for a in audits(conn, task["id"])
            if a["dimension"] == "validation"]
    assert any(
        a["old_value"] == "verified" and a["new_value"] == "checks_pending"
        for a in acts
    )


def test_workflow_change_is_flagged_but_does_not_block(conn, worker, ctx):
    s = ctx.settings
    task = _drive_to_verified(conn, worker, ctx, number=209)
    assert task["validation"] == "verified"
    # Provenance the policy doesn't name gets flagged even on a pass.
    pr_number = 1209
    sha = task["head_sha"]
    with transaction(conn):
        conn.execute(
            "INSERT INTO sim_check_runs "
            "(repo, pr_number, name, workflow, conclusion, head_sha) "
            "VALUES (?, ?, 'coverage-bot', 'third-party-ci', 'success', ?)",
            (s.github_repo, pr_number, sha),
        )
        jobs.enqueue(
            conn, "verify_task",
            {"task_id": task["id"], "pr_number": pr_number},
            mode="simulation",
        )
    drain(worker, ctx, conn)
    task = _task(conn, task["id"])
    assert task["validation"] == "verified"
    assert any(
        a["action"] == "workflow_changed" for a in audits(conn, task["id"])
    )
    assert any(
        e["kind"] == "flag" for e in _evidence(conn, task["id"])
    )


# -- agent assertions vs verified facts -------------------------------------------

def test_agent_assertions_are_labelled_separately(conn, worker, ctx):
    task = _drive_to_verified(conn, worker, ctx, number=210)
    ev = _evidence(conn, task["id"])
    pr_evidence = [
        e for e in ev if e["title"].startswith("Pull request opened")
    ]
    assert pr_evidence and pr_evidence[0]["verifier"] == "agent"
    body = db.loads(pr_evidence[0]["body_json"])
    assert body["claimed_head_sha"]
    verified = [e for e in ev if e["verifier"] == "github-verifier"]
    assert verified


# -- manual verification fallback --------------------------------------------------


def _manual_args(task_id, **over):
    import argparse

    defaults = dict(
        task_id=task_id, operator="ops-lead", head_sha="f" * 40,
        command="pytest tests/test_repair.py -q", results="12 passed",
        evidence="https://paste.example.invalid/run-42",
    )
    defaults.update(over)
    return argparse.Namespace(**defaults)


def test_verify_manual_records_labeled_evidence(conn, worker, ctx):
    """CI failed → operator records a manual verification. The task lands
    'manually_verified' — never 'verified'."""
    run_scenario(conn, worker, ctx, "checks-failed")
    task = task_by_issue(conn, 103)
    assert task["validation"] == "checks_failed"

    # Through the real parser + dispatch — not just the handler.
    rc = cli.main([
        "verify-manual", str(task["id"]),
        "--operator", "ops-lead", "--head-sha", "f" * 40,
        "--command", "pytest tests/test_repair.py -q",
        "--results", "12 passed",
        "--evidence", "https://paste.example.invalid/run-42",
    ])
    assert rc == 0
    task = _task(conn, task["id"])
    assert task["validation"] == "manually_verified"
    assert task["review"] == "awaiting_review"
    assert task["disposition"] == "delivered"
    ev = _evidence(conn, task["id"])
    manual = [e for e in ev if e["kind"] == "manual_verification"]
    assert len(manual) == 1
    assert manual[0]["verifier"] == "manual:ops-lead"
    body = db.loads(manual[0]["body_json"])
    assert body["head_sha"] == "f" * 40
    assert body["command"] and body["results"] and body["evidence_uri"]
    assert "NOT CI verified" in manual[0]["title"]
    assert any(
        a["action"] == "manual_verification_recorded"
        for a in audits(conn, task["id"])
    )


def test_verify_manual_refuses_verified_task(conn, worker, ctx):
    task = _drive_to_verified(conn, worker, ctx, number=211)
    assert task["validation"] == "verified"
    rc = cli.cmd_verify_manual(_manual_args(task["id"]))
    assert rc == 2
    assert _task(conn, task["id"])["validation"] == "verified"


def test_verify_manual_requires_a_pr(conn, worker, ctx):
    s = ctx.settings
    with transaction(conn):
        _seed_issue(
            conn, s.github_repo, 212, "queued only",
            labels=[s.candidate_issue_label, s.approval_issue_label],
            approved_by=s.github_allowed_approvers[0],
            approval_label=s.approval_issue_label,
        )
        jobs.enqueue(conn, "scan_issues", {}, mode="simulation")
    # Only run the scan — the task is queued with no PR yet.
    for _ in range(50):
        conn.execute("UPDATE jobs SET due_at = 0 WHERE status = 'queued'")
        t = task_by_issue(conn, 212)
        if t is not None:
            break
        worker.run_once(ctx)
    task = task_by_issue(conn, 212)
    rc = cli.cmd_verify_manual(_manual_args(task["id"]))
    assert rc == 2


def test_verify_manual_requires_all_fields():
    """The CLI contract: operator, exact SHA, command/results, evidence URI."""
    import pytest

    for argv in (
        ["verify-manual", "1"],
        ["verify-manual", "1", "--operator", "x"],
        ["verify-manual", "1", "--operator", "x", "--head-sha", "f" * 40],
        ["verify-manual", "1", "--operator", "x", "--head-sha", "f" * 40,
         "--command", "make check"],
        ["verify-manual", "1", "--operator", "x", "--head-sha", "f" * 40,
         "--command", "make check", "--results", "pass"],
    ):
        with pytest.raises(SystemExit):
            cli.main(argv)
