"""Simulated external clients ("fakes").

All state comes from the ``sim_*`` fixture tables — every record these
clients return is synthetic and labelled as such downstream (``mode =
'simulation'``, ``synthetic = 1`` on evidence). They never open sockets.
Scenario scripts inject rate limits, ambiguous creations and publication
failures through ``sim_call_scripts`` so the shared job/state code exercises
the same failure paths the live clients will hit.
"""

from __future__ import annotations

import hashlib
import sqlite3

from .. import db
from .base import (
    AmbiguousCreation,
    CheckRun,
    DevinSession,
    DevinSessionSpec,
    ExternalWriteDisabled,
    Issue,
    IssueNotFound,
    LabelEvent,
    PullRequest,
    RateLimited,
)


def _scripted_error(conn: sqlite3.Connection, operation: str) -> str | None:
    """Return the scripted error name for an operation (and decrement its
    counter), or None when no scripted failure remains."""
    row = conn.execute(
        "SELECT remaining, error FROM sim_call_scripts WHERE operation = ?",
        (operation,),
    ).fetchone()
    if row is None or row["remaining"] <= 0:
        return None
    conn.execute(
        "UPDATE sim_call_scripts SET remaining = remaining - 1 "
        "WHERE operation = ?",
        (operation,),
    )
    return str(row["error"])


def _raise_if_scripted(conn: sqlite3.Connection, operation: str) -> None:
    err = _scripted_error(conn, operation)
    if err is None:
        return
    if err == "rate_limited":
        raise RateLimited(retry_after=0.05)
    if err == "ambiguous_creation":
        raise AmbiguousCreation(f"simulated ambiguous {operation}")
    raise RuntimeError(f"simulated {err} on {operation}")


def sha_of(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


class FakeGitHubClient:
    mode = "simulation"

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def list_candidate_issues(self, repo: str, label: str) -> list[Issue]:
        _raise_if_scripted(self.conn, "github.list_candidate_issues")
        rows = self.conn.execute(
            "SELECT * FROM sim_issues WHERE repo = ? AND state = 'open' "
            "ORDER BY number",
            (repo,),
        ).fetchall()
        return [
            self._to_issue(r)
            for r in rows
            if label in db.loads(r["labels_json"], [])
        ]

    def get_issue(self, repo: str, number: int) -> Issue:
        _raise_if_scripted(self.conn, "github.get_issue")
        r = self.conn.execute(
            "SELECT * FROM sim_issues WHERE repo = ? AND number = ?",
            (repo, number),
        ).fetchone()
        if r is None or r["state"] != "open":
            raise IssueNotFound(f"simulated issue {repo}#{number} gone")
        return self._to_issue(r)

    def _to_issue(self, r: sqlite3.Row) -> Issue:
        return Issue(
            repo=r["repo"],
            number=r["number"],
            title=r["title"],
            state=r["state"],
            labels=db.loads(r["labels_json"], []),
            url=f"https://github.example.invalid/{r['repo']}/issues/{r['number']}",
            body=r["body"] or "",
            is_pull_request=bool(r["is_pull_request"]) if "is_pull_request" in r.keys() else False,
        )

    def get_issue_labels(self, repo: str, number: int) -> list[str]:
        r = self.conn.execute(
            "SELECT labels_json FROM sim_issues WHERE repo = ? AND number = ?",
            (repo, number),
        ).fetchone()
        return db.loads(r["labels_json"], []) if r else []

    def list_label_events(self, repo: str, number: int) -> list[LabelEvent]:
        rows = self.conn.execute(
            """SELECT * FROM sim_issue_events
               WHERE repo = ? AND issue_number = ? ORDER BY id""",
            (repo, number),
        ).fetchall()
        return [
            LabelEvent(
                repo=r["repo"],
                issue_number=r["issue_number"],
                event_id=r["event_id"],
                event=r["event"],
                label=r["label"],
                actor=r["actor"],
                created_at=r["created_at"],
            )
            for r in rows
        ]

    def get_pull_request(self, repo: str, pr_number: int) -> PullRequest:
        r = self.conn.execute(
            """SELECT s.* FROM sim_sessions s
               JOIN tasks t ON t.devin_session_id = s.session_id
               WHERE t.repo = ? AND t.pr_number = ?""",
            (repo, pr_number),
        ).fetchone()
        script = db.loads(r["script_json"], {}) if r else {}
        sha = script.get("pr_head_sha", f"simsha{pr_number:034d}"[:40])
        seq = script.get("pr_head_sha_seq") or []
        if seq:
            # A scripted head history — each read observes the next head, so
            # tests can script a push mid-verification (stale-head race).
            sha = seq[0]
            if len(seq) > 1:
                script["pr_head_sha_seq"] = seq[1:]
                self.conn.execute(
                    "UPDATE sim_sessions SET script_json = ? WHERE id = ?",
                    (db.dumps(script), r["id"]),
                )
        return PullRequest(
            repo=repo,
            number=pr_number,
            url=f"https://github.example.invalid/{repo}/pull/{pr_number}",
            base_branch=script.get("base_branch", "master"),
            base_repo=script.get("base_repo", repo),
            head_sha=sha,
            head_repo=script.get("head_repo", repo),
            state="open",
        )

    def get_check_runs(self, repo: str, pr_number: int) -> list[CheckRun]:
        _raise_if_scripted(self.conn, "github.get_check_runs")
        rows = self.conn.execute(
            "SELECT * FROM sim_check_runs WHERE repo = ? AND pr_number = ?",
            (repo, pr_number),
        ).fetchall()
        return [
            CheckRun(
                repo=r["repo"],
                pr_number=r["pr_number"],
                name=r["name"],
                workflow=r["workflow"],
                conclusion=r["conclusion"],
                head_sha=r["head_sha"],
                url=r["url"] or "",
            )
            for r in rows
        ]

    def update_issue_body(self, repo: str, number: int, body: str) -> str:
        raise ExternalWriteDisabled(
            "fake GitHub client does not write issue bodies; the report "
            "source goes through the simulated report sink"
        )


class FakeDevinClient:
    mode = "simulation"

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def create_session(self, spec: DevinSessionSpec) -> DevinSession:
        # Ambiguous creation means the remote side may hold the session even
        # though the caller never received an answer — so the row is written
        # first and only then is the error raised.
        err = _scripted_error(self.conn, "devin.create_session")
        existing = self.find_session_by_correlation_tag(spec.correlation_tag)
        if existing is not None:
            return existing
        session_id = (
            f"sim-{spec.issue_number}-{sha_of(spec.correlation_tag)[:8]}"
        )
        script = self._script_for(spec)
        self.conn.execute(
            """INSERT INTO sim_sessions
               (session_id, correlation_tag, repo, issue_number, script_json,
                created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                session_id,
                spec.correlation_tag,
                spec.repo,
                spec.issue_number,
                db.dumps(script),
                db.now(),
            ),
        )
        if err == "ambiguous_creation":
            raise AmbiguousCreation(
                f"simulated ambiguous create for issue #{spec.issue_number}"
            )
        if err == "rate_limited":
            raise RateLimited(retry_after=0.05)
        if err:
            raise RuntimeError(f"simulated {err} on devin.create_session")
        return DevinSession(
            session_id=session_id,
            status="working",
            status_detail="session started",
            url=f"https://app.devin.example.invalid/sessions/{session_id}",
            acu_used=0.0,
        )

    def _script_for(self, spec: DevinSessionSpec) -> dict:
        r = self.conn.execute(
            "SELECT session_script_json FROM sim_issues "
            "WHERE repo = ? AND number = ?",
            (spec.repo, spec.issue_number),
        ).fetchone()
        if r is not None and r["session_script_json"]:
            return db.loads(r["session_script_json"], {})
        pr_number = 1000 + spec.issue_number
        return {
            "sequence": ["working", "finished"],
            "pr_number": pr_number,
            "pr_url": (
                f"https://github.example.invalid/{spec.repo}/pull/{pr_number}"
            ),
            "pr_head_sha": f"simsha{pr_number:034d}"[:40],
            "acu_used": 3.5,
        }

    def find_session_by_correlation_tag(self, tag: str) -> DevinSession | None:
        r = self.conn.execute(
            "SELECT * FROM sim_sessions WHERE correlation_tag = ?", (tag,)
        ).fetchone()
        if r is None:
            return None
        return self._advance(r, peek=True)

    def get_session(self, session_id: str) -> DevinSession:
        _raise_if_scripted(self.conn, "devin.get_session")
        r = self.conn.execute(
            "SELECT * FROM sim_sessions WHERE session_id = ?", (session_id,)
        ).fetchone()
        if r is None:
            raise KeyError(f"unknown simulated session {session_id}")
        return self._advance(r)

    def _advance(self, r: sqlite3.Row, peek: bool = False) -> DevinSession:
        script = db.loads(r["script_json"], {})
        seq = script.get("sequence", ["finished"])
        idx = r["cursor"]
        status = "stopped" if script.get("stopped") else seq[min(idx, len(seq) - 1)]
        if not peek and not script.get("stopped") and idx < len(seq) - 1:
            self.conn.execute(
                "UPDATE sim_sessions SET cursor = cursor + 1, "
                "acu_used = acu_used + 1.25 WHERE id = ?",
                (r["id"],),
            )
        session = DevinSession(
            session_id=r["session_id"],
            status=status,
            status_detail=script.get("status_detail", status),
            url=f"https://app.devin.example.invalid/sessions/{r['session_id']}",
            acu_used=script.get("acu_used", 0.0) + r["acu_used"],
            notes=script.get("notes", {}),
        )
        if status == "finished":
            session.pr_number = script.get("pr_number")
            session.pr_url = script.get("pr_url")
            session.pr_head_sha = script.get("pr_head_sha")
        return session

    def message_session(self, session_id: str, text: str) -> None:
        _raise_if_scripted(self.conn, "devin.message_session")
        r = self.conn.execute(
            "SELECT * FROM sim_sessions WHERE session_id = ?", (session_id,)
        ).fetchone()
        if r is None:
            raise KeyError(f"unknown simulated session {session_id}")
        script = db.loads(r["script_json"], {})
        if script.get("stopped"):
            raise RuntimeError(
                f"simulated session {session_id} is stopped — cannot message"
            )
        script.setdefault("messages", []).append(
            {"text": text, "sent_at": db.now()}
        )
        self.conn.execute(
            "UPDATE sim_sessions SET script_json = ? WHERE id = ?",
            (db.dumps(script), r["id"]),
        )

    def stop_session(self, session_id: str, archive: bool = True) -> DevinSession:
        _raise_if_scripted(self.conn, "devin.stop_session")
        r = self.conn.execute(
            "SELECT * FROM sim_sessions WHERE session_id = ?", (session_id,)
        ).fetchone()
        if r is None:
            raise KeyError(f"unknown simulated session {session_id}")
        script = db.loads(r["script_json"], {})
        script["stopped"] = True
        script["archived"] = bool(archive)
        self.conn.execute(
            "UPDATE sim_sessions SET script_json = ? WHERE id = ?",
            (db.dumps(script), r["id"]),
        )
        return DevinSession(
            session_id=session_id,
            status="stopped",
            status_detail="terminated" if archive else "stopped",
            url=f"https://app.devin.example.invalid/sessions/{session_id}",
        )

    def daily_acu_usage(
        self, time_after: float, time_before: float
    ) -> float | None:
        # Simulation has no remote consumption API — callers exercise the
        # local-reservation accounting path instead.
        return None


class FakeReportSink:
    """Stands in for the fixed report-source GitHub issue. Writes land in
    ``sim_report_issue`` — visible, durable, and obviously synthetic."""

    mode = "simulation"

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def publish_snapshot(self, repo: str, issue_number: int, body: str) -> str:
        _raise_if_scripted(self.conn, "report.publish_snapshot")
        row = self.conn.execute(
            "SELECT id, revision FROM sim_report_issue WHERE repo = ? AND issue_number = ?",
            (repo, issue_number),
        ).fetchone()
        if row is None:
            self.conn.execute(
                """INSERT INTO sim_report_issue
                   (repo, issue_number, revision, body, updated_at)
                   VALUES (?, ?, 1, ?, ?)""",
                (repo, issue_number, body, db.now()),
            )
            revision = 1
        else:
            revision = row["revision"] + 1
            self.conn.execute(
                "UPDATE sim_report_issue SET revision = ?, body = ?, "
                "updated_at = ? WHERE id = ?",
                (revision, body, db.now(), row["id"]),
            )
        return f"{sha_of(body)[:16]}@r{revision}"
