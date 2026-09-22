"""Independent verification of PR evidence (F09).

The agent's own report is testimony; this handler independently fetches the
PR, checks repository/base branch, reads the current head SHA and evaluates
the versioned policy's expected checks and trusted workflow provenance.
Missing, failed, untrusted, or stale (wrong-SHA) checks never yield
``verified``.
"""

from __future__ import annotations

import sqlite3

from .. import db
from ..transitions import add_evidence, audit, get_task, transition_task
from .context import ServiceContext
from .policy import load_policy
from . import jobs


def handle_verify(ctx: ServiceContext, job: sqlite3.Row) -> None:
    conn, s = ctx.conn, ctx.settings
    payload = db.loads(job["payload_json"])
    task = get_task(conn, payload["task_id"])
    policy = load_policy(s.verification_policy_path)
    pr_number = payload["pr_number"]
    recorded_head = payload.get("head_sha") or task["head_sha"]

    transition_task(conn, task, "validation", "checks_pending")

    # Repository and base-branch provenance.
    pr = ctx.clients.github.get_pull_request(task["repo"], pr_number)
    if s.github_base_branch and pr.base_branch != s.github_base_branch:
        transition_task(
            conn, task, "validation", "checks_failed",
            detail=f"PR base {pr.base_branch!r} is not {s.github_base_branch!r}",
        )
        return
    head_sha = pr.head_sha or recorded_head
    if head_sha and head_sha != task["head_sha"]:
        conn.execute("UPDATE tasks SET head_sha = ? WHERE id = ?",
                     (head_sha, task["id"]))

    runs = ctx.clients.github.get_check_runs(task["repo"], pr_number)
    by_name = {r.name: r for r in runs}
    missing = [c.name for c in policy.required_checks if c.name not in by_name]
    failed: list[str] = []
    untrusted: list[str] = []
    pending: list[str] = []
    for c in policy.required_checks:
        run = by_name.get(c.name)
        if run is None:
            continue
        if c.workflow and run.workflow != c.workflow:
            untrusted.append(c.name)
        elif policy.trusted_workflows and run.workflow not in policy.trusted_workflows:
            untrusted.append(c.name)
        elif run.conclusion == "success":
            continue
        elif run.conclusion in ("pending", "in_progress", "queued"):
            pending.append(c.name)
        else:
            # failure, skipped, cancelled, neutral — none of these pass
            failed.append(c.name)

    stale = bool(
        policy.require_head_sha_match
        and head_sha
        and any(r.head_sha != head_sha for r in runs)
    )

    if stale:
        attempt = int(payload.get("verify_attempt", 0)) + 1
        audit(
            conn, action="checks_stale", mode=ctx.mode, task_id=task["id"],
            detail="check runs do not match current PR head SHA",
        )
        if attempt <= policy.max_reverify_attempts:
            jobs.enqueue(
                conn, "verify_task", {**payload, "verify_attempt": attempt},
                mode=ctx.mode, delay=s.poll_interval_seconds,
            )
        else:
            transition_task(conn, get_task(conn, task["id"]), "validation",
                            "unknown",
                            detail="check runs stayed stale past re-verify "
                                   "limit; operator review required")
            transition_task(conn, get_task(conn, task["id"]), "disposition",
                            "blocked")
        return

    if pending:
        raise jobs.RetryLater(
            s.poll_interval_seconds, f"checks pending: {pending}"
        )

    if missing or failed or untrusted:
        task = get_task(conn, task["id"])
        transition_task(
            conn, task, "validation", "checks_failed",
            detail=f"missing={missing} failed={failed} untrusted={untrusted}",
        )
        transition_task(conn, get_task(conn, task["id"]), "disposition",
                        "blocked", detail="required checks did not pass")
        add_evidence(
            conn, task_id=task["id"], mode=ctx.mode, kind="checks",
            title="Independent verification failed",
            body={
                "missing": missing, "failed": failed, "untrusted": untrusted,
                "head_sha": head_sha,
                "runs": [r.__dict__ for r in runs],
            },
            synthetic=task["synthetic"] == 1, verifier="verification-policy",
        )
        return

    with db.transaction(conn):
        transition_task(conn, task, "validation", "verified",
                        detail=f"required checks green on head {head_sha[:12] if head_sha else '?'}")
        add_evidence(
            conn, task_id=task["id"], mode=ctx.mode, kind="checks",
            title="Independent verification passed",
            body={
                "policy": policy.name, "policy_version": policy.version,
                "head_sha": head_sha,
                "checks": {c.name: by_name[c.name].conclusion
                            for c in policy.required_checks},
                "trusted_workflows": list(policy.trusted_workflows),
            },
            synthetic=task["synthetic"] == 1, verifier="verification-policy",
        )
        task = get_task(conn, task["id"])
        transition_task(conn, task, "review", "awaiting_review",
                        detail="verified PR awaiting human review")
        task = get_task(conn, task["id"])
        transition_task(conn, task, "disposition", "delivered")
        jobs.enqueue(
            conn, "publish_report", {"task_id": task["id"]},
            mode=ctx.mode,
            dedup_key=f"report:{ctx.mode}:{task['id']}:{task['pr_number']}",
            max_attempts=s.job_max_attempts,
        )
