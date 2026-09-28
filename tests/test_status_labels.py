"""Managed status labels — the issue's ``devin-*`` label mirrors task state.

A durable ``sync_status_label`` job is enqueued by every ``transition_task``
(and reconciled on worker recovery); the handler diffs the desired label
against the issue's live labels and only ever touches managed ones. The desk
never writes human labels (``devin-candidate``/``devin-approved`` stay the
operator's authorization surface), and managed labels are excluded from the
frozen snapshot hash so our own writes can't trip the dispatch re-verify.
"""

from fastapi.testclient import TestClient

from conftest import audits, dashboard_app, drain, task_by_issue
from app import db, status_labels
from app.db import transaction
from app.services import jobs
from app.services.scanner import issue_snapshot_hash
from app.services.simulator import (
    _script_error,
    _seed_issue,
    _seed_task,
    seed_scenario,
)

MANAGED = set(status_labels.MANAGED_LABELS)


def issue_labels(conn, repo, number):
    r = conn.execute(
        "SELECT labels_json FROM sim_issues WHERE repo = ? AND number = ?",
        (repo, number),
    ).fetchone()
    return set(db.loads(r["labels_json"], []))


def managed_labels(conn, repo, number):
    return issue_labels(conn, repo, number) & MANAGED


def test_desired_label_mapping():
    def task(**over):
        t = {
            "execution": "queued", "validation": "no_pr", "review": "unknown",
            "disposition": "active", "pr_number": None,
        }
        t.update(over)
        return t

    L = status_labels
    assert L.desired_label(task()) == L.LABEL_IN_PROGRESS
    assert L.desired_label(task(execution="working")) == L.LABEL_IN_PROGRESS
    assert L.desired_label(task(execution="needs_input")) == L.LABEL_IN_PROGRESS
    assert L.desired_label(task(disposition="blocked")) == L.LABEL_IN_PROGRESS
    assert L.desired_label(task(pr_number=5)) == L.LABEL_PR_OPENED
    assert L.desired_label(task(validation="pr_found")) == L.LABEL_PR_OPENED
    assert L.desired_label(task(validation="checks_pending")) == L.LABEL_PR_OPENED
    assert L.desired_label(task(validation="verified")) == L.LABEL_SUCCEEDED
    assert L.desired_label(task(validation="manually_verified")) == L.LABEL_SUCCEEDED
    assert L.desired_label(task(review="merged")) == L.LABEL_SUCCEEDED
    assert L.desired_label(task(disposition="delivered")) == L.LABEL_SUCCEEDED
    assert L.desired_label(task(execution="failed")) == L.LABEL_FAILED
    assert L.desired_label(task(execution="stopped")) == L.LABEL_FAILED
    assert L.desired_label(task(validation="checks_failed")) == L.LABEL_FAILED
    assert L.desired_label(task(disposition="failed")) == L.LABEL_FAILED
    assert L.desired_label(task(disposition="cancelled")) == L.LABEL_FAILED
    # failure outranks a recorded PR; deleted means "strip the label"
    assert L.desired_label(task(pr_number=5, execution="failed")) == L.LABEL_FAILED
    assert L.desired_label(task(disposition="deleted")) is None


def test_happy_path_label_lifecycle(conn, worker, ctx):
    seed_scenario(conn, ctx.settings, "happy-path")
    drain(worker, ctx, conn)

    task = task_by_issue(conn, 101)
    assert task["disposition"] == "delivered"
    assert task["status_label"] == status_labels.LABEL_SUCCEEDED

    labels = issue_labels(conn, task["repo"], 101)
    assert managed_labels(conn, task["repo"], 101) == {status_labels.LABEL_SUCCEEDED}
    # human labels are never removed by the sync
    assert ctx.settings.candidate_issue_label in labels
    assert ctx.settings.approval_issue_label in labels

    syncs = [
        a for a in audits(conn, task["id"])
        if a["action"] == "status_label_synced"
    ]
    progress = [a["new_value"] for a in syncs]
    assert progress[0] == status_labels.LABEL_IN_PROGRESS
    assert progress[-1] == status_labels.LABEL_SUCCEEDED
    assert status_labels.LABEL_PR_OPENED in progress

    # label writes are recorded like real GitHub label events, attributed
    # to the desk bot (never an approver)
    evts = conn.execute(
        "SELECT event, label, actor FROM sim_issue_events "
        "WHERE issue_number = 101 ORDER BY id",
    ).fetchall()
    evts = [e for e in evts if e["label"] in MANAGED]
    assert any(e["event"] == "labeled" for e in evts)
    assert any(e["event"] == "unlabeled" for e in evts)
    assert all(e["actor"] == "repairdesk[bot]" for e in evts)


def test_checks_failed_maps_to_failed_label(conn, worker, ctx):
    seed_scenario(conn, ctx.settings, "checks-failed")
    drain(worker, ctx, conn)
    task = task_by_issue(conn, 103)
    assert task["validation"] == "checks_failed"
    assert task["status_label"] == status_labels.LABEL_FAILED
    assert managed_labels(conn, task["repo"], 103) == {
        status_labels.LABEL_FAILED
    }


def test_label_write_retries_on_rate_limit(conn, worker, ctx):
    seed_scenario(conn, ctx.settings, "happy-path")
    _script_error(conn, "github.add_label", "rate_limited", times=2)
    drain(worker, ctx, conn)
    task = task_by_issue(conn, 101)
    assert task["status_label"] == status_labels.LABEL_SUCCEEDED
    assert managed_labels(conn, task["repo"], 101) == {
        status_labels.LABEL_SUCCEEDED
    }
    # the scripted rate limit forced the sync job through real retries
    sync_jobs = conn.execute(
        "SELECT * FROM jobs WHERE kind = ?", (status_labels.SYNC_JOB_KIND,)
    ).fetchall()
    assert any(
        j["attempt_count"] > 1 and "rate limited" in (j["last_error"] or "")
        for j in sync_jobs
    )


def test_managed_labels_never_trip_dispatch_reverify(conn, worker, ctx):
    """Regression: our own label write between intake and dispatch must not
    poison the frozen snapshot hash (approval drift → blocked)."""
    seed_scenario(conn, ctx.settings, "happy-path")
    worker.run_once(ctx)  # scan_issues → task + dispatch + label jobs
    # Force the label sync ahead of dispatch so devin-in-progress lands on
    # the issue before the approval re-verification reads it.
    conn.execute(
        "UPDATE jobs SET due_at = due_at + 100000 WHERE kind = 'dispatch_task'"
    )
    worker.run_once(ctx)  # sync_status_label
    assert managed_labels(conn, ctx.settings.github_repo, 101) == {
        status_labels.LABEL_IN_PROGRESS
    }
    conn.execute("UPDATE jobs SET due_at = 0 WHERE kind = 'dispatch_task'")
    drain(worker, ctx, conn)
    task = task_by_issue(conn, 101)
    assert task["disposition"] == "delivered"
    assert task["validation"] == "verified"
    acts = [a["action"] for a in audits(conn, task["id"])]
    assert "approval_drift" not in acts and "task_stopped" not in acts


def test_snapshot_hash_ignores_managed_labels():
    class _Issue:
        title, body, state = "t", "b", "open"

    human = ["devin-candidate", "devin-approved", "bug"]
    a = _Issue()
    a.labels = list(human)
    b = _Issue()
    b.labels = human + list(status_labels.MANAGED_LABELS)
    assert issue_snapshot_hash(a) == issue_snapshot_hash(b)
    c = _Issue()
    c.labels = human + ["regression"]
    assert issue_snapshot_hash(a) != issue_snapshot_hash(c)


def test_delete_strips_status_label(settings, conn, worker, ctx):
    tid = _seed_task(
        conn, repo=settings.github_repo, number=603,
        title="task #603", snapshot_body="b",
        labels=[settings.candidate_issue_label, settings.approval_issue_label],
        approver=settings.github_allowed_approvers[0],
    )
    _seed_issue(
        conn, settings.github_repo, 603, "synthetic issue #603",
        labels=[settings.candidate_issue_label,
                settings.approval_issue_label],
        approved_by=settings.github_allowed_approvers[0],
        approval_label=settings.approval_issue_label,
    )
    with transaction(conn):
        conn.execute(
            "UPDATE tasks SET execution = 'agent_finished' WHERE id = ?",
            (tid,),
        )
        jobs.enqueue(
            conn, status_labels.SYNC_JOB_KIND, {"task_id": tid},
            mode="simulation",
        )
    drain(worker, ctx, conn)
    assert managed_labels(conn, settings.github_repo, 603) == {
        status_labels.LABEL_IN_PROGRESS
    }

    res = TestClient(dashboard_app(settings)).delete(f"/api/tasks/{tid}")
    assert res.status_code == 200
    drain(worker, ctx, conn)
    task = conn.execute("SELECT * FROM tasks WHERE id = ?", (tid,)).fetchone()
    assert task["disposition"] == "deleted"
    assert task["status_label"] is None
    assert managed_labels(conn, settings.github_repo, 603) == set()
    # human labels survive; only the managed one was removed
    labels = issue_labels(conn, settings.github_repo, 603)
    assert settings.approval_issue_label in labels


def test_missing_issue_skips_sync(conn, worker, ctx, settings):
    tid = _seed_task(
        conn, repo=settings.github_repo, number=604,
        title="task #604", snapshot_body="b",
        labels=[settings.candidate_issue_label],
        approver=settings.github_allowed_approvers[0],
    )
    with transaction(conn):
        jobs.enqueue(
            conn, status_labels.SYNC_JOB_KIND, {"task_id": tid},
            mode="simulation",
        )
    drain(worker, ctx, conn)
    acts = [a["action"] for a in audits(conn, tid)]
    assert "status_label_skipped" in acts
