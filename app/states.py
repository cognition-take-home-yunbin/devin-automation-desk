"""Task state dimensions — the PRD's separation of execution, validation,
review and disposition (§12). One flat status would hide, e.g., an
agent-finished repair whose independent checks failed, or a verified PR still
awaiting human review.

Every change goes through ``transitions.transition_task``, which enforces the
transition map and writes an audit event, so the audit trail is complete no
matter which job handler moved the task.
"""

from __future__ import annotations

EXECUTION_STATES = (
    "queued",
    "dispatching",
    "creation_unknown",
    "working",
    "needs_input",
    "approval_required",
    "agent_finished",
    "suspended",
    "failed",
    "stop_requested",
    "stopped",
)
VALIDATION_STATES = (
    "no_pr",
    "pr_found",
    "checks_pending",
    "checks_failed",
    "verified",
    "unknown",
)
REVIEW_STATES = (
    "awaiting_review",
    "changes_requested",
    "approved",
    "merged",
    "closed_unmerged",
    "unknown",
)
DISPOSITION_STATES = ("active", "delivered", "blocked", "failed", "cancelled")

DIMENSIONS = ("execution", "validation", "review", "disposition")

ALLOWED: dict[str, dict[str, tuple[str, ...]]] = {
    "execution": {
        "queued": ("dispatching", "stop_requested"),
        "dispatching": ("working", "creation_unknown", "failed", "queued",
                        "stop_requested"),
        "creation_unknown": ("working", "failed", "queued", "stop_requested"),
        "working": (
            "needs_input",
            "approval_required",
            "agent_finished",
            "suspended",
            "failed",
            "stop_requested",
            "stopped",
        ),
        "needs_input": ("working", "failed", "stop_requested", "stopped"),
        "approval_required": ("working", "failed", "stop_requested",
                              "stopped"),
        "agent_finished": ("queued",),
        "suspended": ("working", "failed", "stop_requested", "stopped"),
        "failed": ("queued",),
        "stop_requested": ("stopped",),
        "stopped": ("queued",),
    },
    "validation": {
        "no_pr": ("pr_found", "unknown"),
        "pr_found": ("checks_pending", "checks_failed", "unknown"),
        "checks_pending": ("verified", "checks_failed", "unknown"),
        "checks_failed": ("checks_pending", "unknown"),
        "verified": ("checks_pending",),  # a new head push re-opens validation
        "unknown": ("pr_found", "checks_pending", "verified", "checks_failed"),
    },
    "review": {
        "unknown": ("awaiting_review",),
        "awaiting_review": (
            "changes_requested",
            "approved",
            "merged",
            "closed_unmerged",
        ),
        "changes_requested": ("awaiting_review", "closed_unmerged"),
        "approved": ("merged", "closed_unmerged"),
        "merged": (),
        "closed_unmerged": (),
    },
    "disposition": {
        "active": ("delivered", "blocked", "failed", "cancelled"),
        "blocked": ("active", "failed", "cancelled"),
        "delivered": (),
        "failed": ("active", "cancelled"),
        "cancelled": ("active",),
    },
}

STATES_BY_DIMENSION = {
    "execution": EXECUTION_STATES,
    "validation": VALIDATION_STATES,
    "review": REVIEW_STATES,
    "disposition": DISPOSITION_STATES,
}


class InvalidTransition(RuntimeError):
    pass


def check_transition(dimension: str, old: str, new: str) -> None:
    if new not in STATES_BY_DIMENSION[dimension]:
        raise InvalidTransition(f"{new!r} is not a valid {dimension} state")
    if old == new:
        return
    if new not in ALLOWED[dimension].get(old, ()):
        raise InvalidTransition(f"{dimension}: {old} -> {new} is not allowed")
