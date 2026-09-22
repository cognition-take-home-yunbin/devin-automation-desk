"""Issue scanner — durable intake for approved issues (F02–F04).

Scans the configured fork for ``CANDIDATE_ISSUE_LABEL``, dedupes on
(mode, repo, issue_number), freezes the issue snapshot, and requires *both*
labels plus a latest ``APPROVAL_ISSUE_LABEL`` event from an allowed approver
before persisting an approval receipt and enqueueing dispatch. Labels alone
are not authorization.
"""

from __future__ import annotations

import hashlib
import sqlite3

from .. import db
from ..clients.base import Issue
from ..transitions import add_evidence, audit, get_task, transition_task
from .context import ServiceContext
from . import jobs


def issue_snapshot_hash(issue: Issue) -> str:
    canonical = db.dumps(
        {
            "title": issue.title,
            "body": issue.body,
            "labels": sorted(issue.labels),
            "state": issue.state,
        }
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def _latest_approval_event(ctx: ServiceContext, issue: Issue):
    """The latest ``labeled`` event for the approval label decides the actor."""
    events = ctx.clients.github.list_label_events(issue.repo, issue.number)
    relevant = [
        e for e in events
        if e.event == "labeled" and e.label == ctx.settings.approval_issue_label
    ]
    return relevant[-1] if relevant else None


def handle_scan(ctx: ServiceContext, job: sqlite3.Row) -> None:
    conn, s = ctx.conn, ctx.settings
    repo = s.github_repo or "example/superset-demo"
    issues = ctx.clients.github.list_candidate_issues(repo, s.candidate_issue_label)

    created = skipped = ineligible = 0
    for issue in issues:
        if issue.is_pull_request or issue.state != "open":
            ineligible += 1
            with db.transaction(conn):
                audit(
                    conn, action="scan_ineligible", mode=ctx.mode,
                    detail=f"#{issue.number}: closed or pull-request object",
                )
            continue

        existing = conn.execute(
            "SELECT * FROM tasks WHERE mode = ? AND repo = ? AND issue_number = ?",
            (ctx.mode, issue.repo, issue.number),
        ).fetchone()
        if existing is not None:
            skipped += 1
            continue

        snapshot_hash = issue_snapshot_hash(issue)
        labels = ctx.clients.github.get_issue_labels(issue.repo, issue.number)
        has_candidate = s.candidate_issue_label in labels
        approval_event = _latest_approval_event(ctx, issue)
        has_approval_label = s.approval_issue_label in labels
        approver = (
            approval_event.actor
            if approval_event and approval_event.actor in s.github_allowed_approvers
            else None
        )

        with db.transaction(conn):
            if not (has_candidate and has_approval_label and approver):
                # No task row, no session — an unapproved/untrusted input is
                # simply not admitted. The scan summary records the outcome.
                ineligible += 1
                reason = []
                if not has_approval_label:
                    reason.append(f"missing {s.approval_issue_label}")
                elif not approval_event:
                    reason.append(f"no label event for {s.approval_issue_label}")
                elif approval_event.actor not in s.github_allowed_approvers:
                    reason.append(
                        f"approver {approval_event.actor!r} not in "
                        "GITHUB_ALLOWED_APPROVERS"
                    )
                audit(
                    conn, action="approval_insufficient", mode=ctx.mode,
                    detail=f"issue #{issue.number}: {'; '.join(reason)}",
                )
                continue

            ts = db.now()
            cur = conn.execute(
                """INSERT INTO tasks
                   (mode, repo, issue_number, issue_title, issue_url,
                    issue_snapshot_hash, issue_snapshot_json, approval_actor,
                    accepted_at, synthetic, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    ctx.mode, issue.repo, issue.number, issue.title, issue.url,
                    snapshot_hash,
                    db.dumps({
                        "title": issue.title, "body": issue.body,
                        "labels": labels, "state": issue.state,
                        "url": issue.url,
                    }),
                    approver,
                    ts,
                    1 if ctx.mode == "simulation" else 0,
                    ts, ts,
                ),
            )
            task_id = int(cur.lastrowid)
            task = get_task(conn, task_id)
            conn.execute(
                """INSERT INTO approval_receipts
                   (mode, task_id, repo, issue_number, label_event_id, actor,
                    observed_at, issue_snapshot_hash, processing_state,
                    payload_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'accepted', ?)""",
                (
                    ctx.mode, task_id, issue.repo, issue.number,
                    approval_event.event_id, approver, ts, snapshot_hash,
                    db.dumps({"event": approval_event.event,
                              "label": approval_event.label,
                              "actor": approver,
                              "labels_at_scan": labels}),
                ),
            )
            audit(
                conn, action="task_created", mode=ctx.mode, task_id=task_id,
                detail=f"issue #{issue.number}: {issue.title}",
            )
            audit(
                conn, action="approval_received", mode=ctx.mode,
                task_id=task_id, source=approver,
                detail=(
                    f"{s.approval_issue_label} applied by {approver} "
                    f"(event {approval_event.event_id}); snapshot "
                    f"{snapshot_hash[:12]}"
                ),
            )
            add_evidence(
                conn, task_id=task_id, mode=ctx.mode, kind="approval",
                title=f"Approval receipt: {approver} applied "
                      f"{s.approval_issue_label}",
                body={"labels": sorted(labels), "actor": approver,
                      "event_id": approval_event.event_id},
                synthetic=task["synthetic"] == 1,
            )
            jobs.enqueue(
                conn, "dispatch_task", {"task_id": task_id},
                mode=ctx.mode,
                dedup_key=f"dispatch:{ctx.mode}:{repo}:{issue.number}",
                max_attempts=s.job_max_attempts,
            )
            created += 1

    with db.transaction(conn):
        conn.execute("UPDATE control SET last_scan_at = ? WHERE id = 1", (db.now(),))
        audit(
            conn, action="scan_completed", mode=ctx.mode,
            detail=f"candidates={len(issues)} created={created} "
                   f"deduplicated={skipped} ineligible={ineligible}",
        )
        add_evidence(
            conn, mode=ctx.mode, kind="scan",
            title=f"Scan: {len(issues)} candidate(s), {created} new task(s), "
                  f"{skipped} already tracked",
            body={"candidates": len(issues), "created": created,
                  "deduplicated": skipped, "ineligible": ineligible},
            synthetic=ctx.mode == "simulation",
        )
