"""SQLite persistence layer.

One database file per deployment keeps durable domain state (approval
receipts, tasks, attempts, jobs, evidence, audit events, report snapshots,
publication records) plus the simulation fixture tables (``sim_*``). Live and
simulation use separate database files/volumes (DATABASE_PATH +
COMPOSE_PROJECT_NAME); every domain row additionally carries a ``mode``
column so exported data and API responses always state which world produced
it.

Connections run autocommit (``isolation_level=None``); writes go through
``transaction()`` which issues BEGIN IMMEDIATE and nests via SAVEPOINT, so
job claims are atomic and helpers may be called inside a handler's own
transaction.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from contextlib import contextmanager
from typing import Any, Iterator

SCHEMA_VERSION = 2

SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- single-row operator control state (pause flag, scheduled-scan bookkeeping)
CREATE TABLE IF NOT EXISTS control (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    paused INTEGER NOT NULL DEFAULT 0,
    last_scan_at REAL,
    last_publish_at REAL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    mode TEXT NOT NULL CHECK (mode IN ('simulation', 'live')),
    repo TEXT NOT NULL,
    issue_number INTEGER NOT NULL,
    issue_title TEXT NOT NULL,
    issue_url TEXT NOT NULL,
    issue_snapshot_hash TEXT,
    issue_snapshot_json TEXT,
    approval_actor TEXT,
    accepted_at REAL,
    -- PRD state dimensions
    execution TEXT NOT NULL DEFAULT 'queued',
    validation TEXT NOT NULL DEFAULT 'no_pr',
    review TEXT NOT NULL DEFAULT 'unknown',
    disposition TEXT NOT NULL DEFAULT 'active',
    devin_session_id TEXT,
    devin_session_url TEXT,
    slack_link TEXT,
    pr_number INTEGER,
    pr_url TEXT,
    base_sha TEXT,
    head_sha TEXT,
    last_error TEXT,
    cleanup_state TEXT NOT NULL DEFAULT 'pending',
    synthetic INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    UNIQUE (mode, repo, issue_number)
);

CREATE TABLE IF NOT EXISTS approval_receipts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    mode TEXT NOT NULL,
    task_id INTEGER NOT NULL REFERENCES tasks(id),
    repo TEXT NOT NULL,
    issue_number INTEGER NOT NULL,
    label_event_id TEXT NOT NULL,
    actor TEXT NOT NULL,
    observed_at REAL NOT NULL,
    issue_snapshot_hash TEXT NOT NULL,
    processing_state TEXT NOT NULL DEFAULT 'accepted',
    payload_json TEXT NOT NULL DEFAULT '{}',
    UNIQUE (mode, repo, issue_number, label_event_id)
);

CREATE TABLE IF NOT EXISTS attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL REFERENCES tasks(id),
    mode TEXT NOT NULL,
    attempt_number INTEGER NOT NULL,
    correlation_tag TEXT NOT NULL,
    base_sha TEXT,
    session_id TEXT,
    session_url TEXT,
    raw_status TEXT,
    raw_detail TEXT,
    last_seen_at REAL,
    acu_limit INTEGER,
    acu_used REAL,
    prompt_hash TEXT,
    context_hash TEXT,
    started_at REAL NOT NULL,
    finished_at REAL,
    UNIQUE (task_id, attempt_number),
    UNIQUE (mode, correlation_tag)
);

CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    mode TEXT NOT NULL,
    kind TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'queued'
        CHECK (status IN ('queued', 'claimed', 'succeeded', 'failed', 'dead')),
    dedup_key TEXT UNIQUE,
    due_at REAL NOT NULL,
    claimed_at REAL,
    lease_expires_at REAL,
    claimed_by TEXT,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 8,
    last_error TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_jobs_claim
    ON jobs (status, due_at, id);

CREATE TABLE IF NOT EXISTS evidence (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER REFERENCES tasks(id),
    attempt_id INTEGER REFERENCES attempts(id),
    mode TEXT NOT NULL,
    kind TEXT NOT NULL,
    title TEXT NOT NULL,
    body_json TEXT,
    uri TEXT,
    synthetic INTEGER NOT NULL DEFAULT 0,
    verifier TEXT,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER REFERENCES tasks(id),
    mode TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'system',
    action TEXT NOT NULL,
    dimension TEXT,
    old_value TEXT,
    new_value TEXT,
    detail TEXT,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_audit_task ON audit_events (task_id, id);

CREATE TABLE IF NOT EXISTS report_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    mode TEXT NOT NULL,
    report_type TEXT NOT NULL DEFAULT 'summary',
    period_json TEXT,
    timezone TEXT,
    schema_version INTEGER NOT NULL,
    title TEXT NOT NULL,
    content_json TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    generated_at REAL NOT NULL,
    native_session_url TEXT,
    native_state TEXT,
    slack_link TEXT,
    UNIQUE (sha256)
);

CREATE TABLE IF NOT EXISTS publication_records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_id INTEGER NOT NULL REFERENCES report_snapshots(id),
    mode TEXT NOT NULL,
    destination_repo TEXT,
    destination_issue_number INTEGER,
    status TEXT NOT NULL CHECK (status IN ('confirmed', 'unknown', 'failed')),
    observed_body_hash TEXT,
    detail TEXT,
    external_ref TEXT,
    created_at REAL NOT NULL
);

-- ---------------------------------------------------------------------------
-- Simulation fixtures: synthetic inputs consumed by the fake clients.
-- These tables only ever hold scenario data; nothing here contacts the
-- network.
CREATE TABLE IF NOT EXISTS sim_issues (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    repo TEXT NOT NULL,
    number INTEGER NOT NULL,
    title TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'open',
    labels_json TEXT NOT NULL DEFAULT '[]',
    body TEXT,
    is_pull_request INTEGER NOT NULL DEFAULT 0,
    session_script_json TEXT,
    UNIQUE (repo, number)
);

CREATE TABLE IF NOT EXISTS sim_issue_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    repo TEXT NOT NULL,
    issue_number INTEGER NOT NULL,
    event_id TEXT NOT NULL,
    event TEXT NOT NULL,
    label TEXT,
    actor TEXT,
    created_at REAL NOT NULL,
    UNIQUE (repo, issue_number, event_id)
);

CREATE TABLE IF NOT EXISTS sim_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL UNIQUE,
    correlation_tag TEXT UNIQUE,
    repo TEXT NOT NULL,
    issue_number INTEGER NOT NULL,
    script_json TEXT NOT NULL,
    cursor INTEGER NOT NULL DEFAULT 0,
    acu_used REAL NOT NULL DEFAULT 0,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS sim_check_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    repo TEXT NOT NULL,
    pr_number INTEGER NOT NULL,
    name TEXT NOT NULL,
    workflow TEXT NOT NULL DEFAULT 'pilot-validation',
    conclusion TEXT NOT NULL,
    head_sha TEXT NOT NULL,
    url TEXT,
    UNIQUE (repo, pr_number, name)
);

CREATE TABLE IF NOT EXISTS sim_call_scripts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    operation TEXT NOT NULL UNIQUE,
    remaining INTEGER NOT NULL,
    error TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sim_report_issue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    repo TEXT NOT NULL,
    issue_number INTEGER NOT NULL,
    revision INTEGER NOT NULL DEFAULT 0,
    body TEXT NOT NULL DEFAULT '',
    updated_at REAL
);

-- ---------------------------------------------------------------------------
-- Milestone B: reservation-based managed spending + operator cleanup ledger.
CREATE TABLE IF NOT EXISTS budget_reservations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL REFERENCES tasks(id),
    mode TEXT NOT NULL,
    scope TEXT NOT NULL CHECK (scope IN ('session', 'followup')),
    amount REAL NOT NULL,
    status TEXT NOT NULL DEFAULT 'held'
        CHECK (status IN ('held', 'consumed', 'released')),
    session_id TEXT,
    note TEXT,
    created_at REAL NOT NULL,
    resolved_at REAL
);

CREATE TABLE IF NOT EXISTS cleanup_records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL REFERENCES tasks(id),
    mode TEXT NOT NULL,
    action TEXT NOT NULL,       -- kept | terminated
    actor TEXT NOT NULL DEFAULT 'system',
    session_id TEXT,
    detail TEXT,
    created_at REAL NOT NULL
);
"""


def connect(sqlite_path: str) -> sqlite3.Connection:
    if sqlite_path != ":memory:":
        os.makedirs(os.path.dirname(os.path.abspath(sqlite_path)), exist_ok=True)
    conn = sqlite3.connect(
        sqlite_path,
        timeout=30,
        isolation_level=None,
        # FastAPI resolves sync generator deps and handlers on different
        # threadpool threads; CPython's sqlite3 is compiled serialized, so a
        # connection that outlives its creation thread is safe — each request
        # still gets its own connection and WAL + busy_timeout serialize
        # writers.
        check_same_thread=False,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    _migrate(conn)
    conn.execute(
        "INSERT OR IGNORE INTO control (id, paused, updated_at) VALUES (1, 0, ?)",
        (time.time(),),
    )
    conn.execute(
        "INSERT OR REPLACE INTO schema_meta (key, value) VALUES ('schema_version', ?)",
        (str(SCHEMA_VERSION),),
    )
    conn.commit()


def _migrate(conn: sqlite3.Connection) -> None:
    """Guarded ALTERs so a v1 database picks up v2 columns on first boot."""
    for table, column, ddl in (
        ("tasks", "cleanup_state",
         "ALTER TABLE tasks ADD COLUMN cleanup_state "
         "TEXT NOT NULL DEFAULT 'pending'"),
        ("sim_issues", "is_pull_request",
         "ALTER TABLE sim_issues ADD COLUMN is_pull_request "
         "INTEGER NOT NULL DEFAULT 0"),
    ):
        cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        if column not in cols:
            conn.execute(ddl)


_savepoint_counter = 0


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """BEGIN IMMEDIATE block that nests via SAVEPOINT when already inside a
    transaction, so helpers like ``jobs.enqueue`` are safe to call from within
    a handler's own transaction."""
    global _savepoint_counter
    if conn.in_transaction:
        _savepoint_counter += 1
        sp = f"sp_{_savepoint_counter}"
        conn.execute(f"SAVEPOINT {sp}")
        try:
            yield conn
        except Exception:
            conn.execute(f"ROLLBACK TO {sp}")
            conn.execute(f"RELEASE {sp}")
            raise
        else:
            conn.execute(f"RELEASE {sp}")
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except Exception:
        conn.rollback()
        raise
    else:
        conn.commit()


def now() -> float:
    return time.time()


def dumps(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True)


def loads(text: str | None, default: Any = None) -> Any:
    if text is None:
        return default
    return json.loads(text)
