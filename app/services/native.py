"""Native-report session observation (milestone D).

The daily report is generated and posted by a *native Devin Automation* —
the desk never schedules it, never talks to Slack, and never infers
delivery from a session's state. What the app does is strictly read-only:
when ``list_sessions_by_tag`` is reachable with the configured API
permissions, sessions carrying the ``NATIVE_REPORT_SESSION_TAG`` are
recorded in ``native_sessions`` (separate from managed repair attempts),
with ``acu_used=None`` preserved as *unknown* rather than zero. A failure
to observe is recorded on the control row so the dashboard can show the
native feed as unavailable — it never blocks the repair pipeline.

Operators may attach a Slack permalink manually
(``slack_link``/``slack_source='manual'``); that link is the *only*
evidence of delivery the desk will ever display.
"""

from __future__ import annotations

import sqlite3

from .. import db
from ..transitions import audit
from .context import ServiceContext
from . import jobs


def handle_observe(ctx: ServiceContext, job: sqlite3.Row) -> None:
    conn, s = ctx.conn, ctx.settings
    try:
        sessions = ctx.clients.devin.list_sessions_by_tag(
            s.native_report_session_tag
        )
    except jobs.RetryLater:
        raise
    except Exception as exc:  # noqa: BLE001 - observation may be unavailable
        with db.transaction(conn):
            conn.execute(
                "UPDATE control SET native_observe_error = ? WHERE id = 1",
                (f"{type(exc).__name__}: {exc}"[:500],),
            )
            audit(conn, action="native_observe_failed", mode=ctx.mode,
                  detail=f"{type(exc).__name__}: {exc}"[:500])
        raise jobs.RetryLater(
            max(s.poll_interval_seconds, 15.0),
            "native observation unavailable",
        )

    with db.transaction(conn):
        now = db.now()
        for sess in sessions:
            existing = conn.execute(
                "SELECT id FROM native_sessions "
                "WHERE mode = ? AND session_id = ?",
                (ctx.mode, sess.session_id),
            ).fetchone()
            if existing is None:
                conn.execute(
                    """INSERT INTO native_sessions
                       (mode, tag, session_id, url, status, status_detail,
                        acu_used, first_seen_at, last_seen_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        ctx.mode, s.native_report_session_tag,
                        sess.session_id, sess.url, sess.status,
                        sess.status_detail, sess.acu_used, now, now,
                    ),
                )
            else:
                conn.execute(
                    """UPDATE native_sessions
                       SET url = ?, status = ?, status_detail = ?,
                           acu_used = ?, last_seen_at = ?
                       WHERE id = ?""",
                    (
                        sess.url, sess.status, sess.status_detail,
                        sess.acu_used, now, existing["id"],
                    ),
                )
        conn.execute(
            "UPDATE control SET last_native_observe_at = ?, "
            "native_observe_error = NULL WHERE id = 1",
            (now,),
        )
        audit(
            conn, action="native_observed", mode=ctx.mode,
            detail=f"{len(sessions)} native session(s) observed for "
                   f"tag {s.native_report_session_tag}",
        )


def record_link(
    ctx: ServiceContext, session_id: str, slack_link: str,
    session_url: str | None, operator: str,
) -> int:
    """Manually recorded evidence links — the only delivery evidence the
    desk accepts, since native Slack posting is owned by the automation."""
    conn = ctx.conn
    with db.transaction(conn):
        row = conn.execute(
            "SELECT id FROM native_sessions WHERE mode = ? AND session_id = ?",
            (ctx.mode, session_id),
        ).fetchone()
        now = db.now()
        if row is None:
            cur = conn.execute(
                """INSERT INTO native_sessions
                   (mode, tag, session_id, url, status, status_detail,
                    acu_used, slack_link, slack_source,
                    first_seen_at, last_seen_at)
                   VALUES (?, ?, ?, ?, 'unknown', 'manually recorded',
                           NULL, ?, 'manual', ?, ?)""",
                (
                    ctx.mode, ctx.settings.native_report_session_tag,
                    session_id, session_url, slack_link, now, now,
                ),
            )
            row_id = int(cur.lastrowid)
        else:
            row_id = int(row["id"])
            conn.execute(
                """UPDATE native_sessions
                   SET slack_link = ?, slack_source = 'manual',
                       url = COALESCE(?, url), last_seen_at = ?
                   WHERE id = ?""",
                (slack_link, session_url, now, row_id),
            )
        audit(
            conn, action="native_link_recorded", mode=ctx.mode,
            source="operator",
            detail=f"operator {operator} recorded slack link for native "
                   f"session {session_id}: {slack_link}",
        )
    return row_id
