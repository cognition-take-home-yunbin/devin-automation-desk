"""Managed issue status labels — mirror a task's state onto GitHub.

Exactly one of ``MANAGED_LABELS`` may be on a tracked issue at a time;
``desired_label`` maps the four state dimensions to it. These labels are
output only — they carry no authorization weight (the intake/approval labels
remain the only human-controlled signals) and are excluded from the issue
snapshot hash so our own writes never trip the dispatch "snapshot changed"
gate.
"""

from __future__ import annotations

import sqlite3

LABEL_IN_PROGRESS = "devin-in-progress"
LABEL_PR_OPENED = "devin-pr-opened"
LABEL_SUCCEEDED = "devin-succeeded"
LABEL_FAILED = "devin-failed"

MANAGED_LABELS = (
    LABEL_IN_PROGRESS,
    LABEL_PR_OPENED,
    LABEL_SUCCEEDED,
    LABEL_FAILED,
)

SYNC_JOB_KIND = "sync_status_label"


def desired_label(task: sqlite3.Row | dict) -> str | None:
    """The status label the issue should carry for this task, or None when
    the task is untracked and all managed labels should be stripped.

    Precedence: deleted clears; terminal non-success (failed/stopped
    session, failed/cancelled task, or failed required checks) wins over
    success; success (delivered / verified / merged-or-approved review)
    wins over an open PR; a PR wins over plain in-progress.
    """
    if task["disposition"] == "deleted":
        return None
    if (
        task["disposition"] in ("failed", "cancelled")
        or task["execution"] in ("failed", "stopped")
        or task["validation"] == "checks_failed"
    ):
        return LABEL_FAILED
    if (
        task["disposition"] == "delivered"
        or task["validation"] in ("verified", "manually_verified")
        or task["review"] in ("approved", "merged")
    ):
        return LABEL_SUCCEEDED
    if task["pr_number"] is not None or task["validation"] in (
        "pr_found",
        "checks_pending",
    ):
        return LABEL_PR_OPENED
    return LABEL_IN_PROGRESS
