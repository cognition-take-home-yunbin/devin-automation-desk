"""Independent verification of PR evidence (F09).

The agent's own report is testimony; this handler independently fetches the
PR, verifies it targets the configured repository and base branch, reads the
current head SHA, and evaluates the versioned policy's expected checks and
trusted workflow provenance. Missing, failed, untrusted, or stale (wrong-SHA)
checks never yield ``verified`` — and neither does a head that moved between
fetching the checks and finalizing (the head is re-read right before the
verdict). Workflow provenance that changed from what the policy expects, and
out-of-scope PR targets, are flagged on the audit trail and in evidence.
"""

from __future__ import annotations

import sqlite3

from .. import db
from ..transitions import add_evidence, audit, get_task, transition_task
from .context import ServiceContext
from .policy import load_policy
from . import jobs

# Provenance labels on evidence.verifier — keep the agent's claims visibly
# separate from facts this service retrieved itself.
VERIFIER_INDEPENDENT = "github-verifier"
VERIFIER_AGENT = "agent"


def _runs_payload(runs) -> list[dict]:
    return [r.__dict__ for r in runs]


def _fail(
    ctx: ServiceContext,
    task: sqlite3.Row,
    *,
    detail: str,
    body: dict,
    uri: str | None = None,
) -> None:
    task = get_task(ctx.conn, task["id"])
    transition_task(ctx.conn, task, "validation", "checks_failed",
                    detail=detail)
    transition_task(ctx.conn, get_task(ctx.conn, task["id"]),
                    "disposition", "blocked", detail=detail)
    add_evidence(
        ctx.conn, task_id=task["id"], mode=ctx.mode, kind="checks",
        title="Independent verification failed", body=body, uri=uri,
        synthetic=task["synthetic"] == 1, verifier=VERIFIER_INDEPENDENT,
    )


def handle_verify(ctx: ServiceContext, job: sqlite3.Row) -> None:
    conn, s = ctx.conn, ctx.settings
    payload = db.loads(job["payload_json"])
    task = get_task(conn, payload["task_id"])
    policy = load_policy(s.verification_policy_path)
    pr_number = payload["pr_number"]
    # What the agent *claimed* — assertion, not fact. Verified below.
    agent_claimed_head = payload.get("head_sha") or task["head_sha"]

    task = transition_task(conn, task, "validation", "checks_pending")

    # -- scope: the PR must target the configured repo + base branch ---------
    pr = ctx.clients.github.get_pull_request(task["repo"], pr_number)
    scope_flags: list[str] = []
    if pr.base_repo and pr.base_repo != task["repo"]:
        scope_flags.append(
            f"PR targets {pr.base_repo}, not the allowed {task['repo']}"
        )
    if s.github_base_branch and pr.base_branch != s.github_base_branch:
        scope_flags.append(
            f"PR base {pr.base_branch!r} is not {s.github_base_branch!r}"
        )
    if scope_flags:
        reason = "; ".join(scope_flags)
        audit(
            conn, action="pr_out_of_scope", mode=ctx.mode,
            task_id=task["id"], detail=reason,
        )
        add_evidence(
            conn, task_id=task["id"], mode=ctx.mode, kind="flag",
            title=f"Out-of-scope PR target: {reason}",
            body={"flags": scope_flags, "pr_url": pr.url,
                  "base_repo": pr.base_repo,
                  "base_branch": pr.base_branch},
            synthetic=task["synthetic"] == 1,
            verifier=VERIFIER_INDEPENDENT,
        )
        _fail(
            ctx, task,
            detail=f"out-of-scope PR target: {reason}",
            body={"flags": scope_flags, "pr_url": pr.url},
        )
        return

    head_sha = pr.head_sha or agent_claimed_head
    if head_sha and head_sha != task["head_sha"]:
        conn.execute("UPDATE tasks SET head_sha = ? WHERE id = ?",
                     (head_sha, task["id"]))

    # -- checks on the fetched head (client paginates) -----------------------
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

    # Flag workflow changes: provenance on this head that the policy doesn't
    # name. A flag is not itself a failure — required checks are already
    # judged against trusted_workflows — but the audit trail must show the
    # drift so an operator can widen the policy deliberately.
    expected_workflows = {
        c.workflow for c in policy.required_checks if c.workflow
    } | set(policy.trusted_workflows)
    workflow_changes = sorted(
        w for w in {r.workflow for r in runs} - expected_workflows if w
    )
    if workflow_changes:
        audit(
            conn, action="workflow_changed", mode=ctx.mode,
            task_id=task["id"],
            detail=f"check-run provenance outside policy: {workflow_changes}",
        )
        add_evidence(
            conn, task_id=task["id"], mode=ctx.mode, kind="flag",
            title="Workflow provenance changed since the policy was written",
            body={"observed_workflows": workflow_changes,
                  "expected": sorted(expected_workflows)},
            synthetic=task["synthetic"] == 1,
            verifier=VERIFIER_INDEPENDENT,
        )

    # -- staleness: wrong-SHA runs, or a head that moved mid-verification ----
    # Re-check the head before finalizing — a push during evaluation would
    # otherwise verify a commit nobody reviewed.
    recheck = ctx.clients.github.get_pull_request(task["repo"], pr_number)
    head_moved = bool(
        recheck.head_sha and head_sha and recheck.head_sha != head_sha
    )
    stale = bool(
        policy.require_head_sha_match
        and head_sha
        and any(r.head_sha != head_sha for r in runs)
    )
    if stale or head_moved:
        attempt = int(payload.get("verify_attempt", 0)) + 1
        detail = (
            "head moved during verification "
            f"({head_sha[:12]} -> {recheck.head_sha[:12]})"
            if head_moved
            else "check runs do not match current PR head SHA"
        )
        audit(
            conn, action="checks_stale", mode=ctx.mode, task_id=task["id"],
            detail=detail,
        )
        if recheck.head_sha and recheck.head_sha != task["head_sha"]:
            conn.execute("UPDATE tasks SET head_sha = ? WHERE id = ?",
                         (recheck.head_sha, task["id"]))
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
        _fail(
            ctx, task,
            detail=f"missing={missing} failed={failed} untrusted={untrusted}",
            body={
                "missing": missing, "failed": failed, "untrusted": untrusted,
                "head_sha": head_sha,
                "agent_claimed_head": agent_claimed_head,
                "flags": workflow_changes,
                "runs": _runs_payload(runs),
            },
        )
        return

    with db.transaction(conn):
        transition_task(conn, task, "validation", "verified",
                        detail=f"required checks green on head "
                               f"{head_sha[:12] if head_sha else '?'}")
        add_evidence(
            conn, task_id=task["id"], mode=ctx.mode, kind="checks",
            title="Independent verification passed",
            body={
                "policy": policy.name, "policy_version": policy.version,
                "head_sha": head_sha,
                "agent_claimed_head": agent_claimed_head,
                "checks": {c.name: by_name[c.name].conclusion
                           for c in policy.required_checks},
                "trusted_workflows": list(policy.trusted_workflows),
                "flags": workflow_changes,
            },
            synthetic=task["synthetic"] == 1, verifier=VERIFIER_INDEPENDENT,
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
