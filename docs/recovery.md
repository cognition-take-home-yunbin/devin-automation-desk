# Recovery runbook

Everything durable lives in one SQLite database (WAL mode) at
`DATABASE_PATH` — in the compose stack that's `/data/repairdesk.sqlite`
on the `app-data` volume. There is no in-memory-only state: tasks,
receipts, attempts, jobs, evidence, audits, snapshots, publications,
budget reservations, native observations and the control row all survive
a restart or a container rebuild.

## Restart behavior (automatic)

On boot the worker's `recover()` reattaches in-flight work **without
recreating it**:

- Tasks holding a session get a `poll_session` job re-enqueued (dedup
  key `poll:{session_id}` — safe when one is already queued).
- `creation_unknown` tasks get `reconcile_creation` jobs that look the
  session up by correlation tag rather than retrying creation.
- Claimed-but-unfinished jobs whose lease expired are claimable again —
  nothing is lost between processes.
- Unique task constraints and job dedup keys make repeated scans/retries
  idempotent after any crash window.

Nothing else is required: `docker compose up -d` resumes where it left
off, including across image rebuilds.

## Evidence export

```bash
# inside the compose stack — lands on the durable volume
docker compose exec app python -m app.cli export-evidence --output /data/evidence
# locally
python -m app.cli export-evidence --output ./exports
```

The JSON bundle contains every task with its approval receipts, attempts,
evidence and audit events, plus report snapshots, publication records,
native session observations, budget reservations, the control row and the
job ledger — enough to reconstruct *what the desk believed and did* at
export time. Copies off the box are your backup tier; the bundle is
append-only data, safe to ship or archive.

## Specific failure recoveries

| Symptom | Recovery |
| --- | --- |
| `job_dead` audit / failed rows in `/api/jobs` | The job exhausted bounded retries — read `last_error`, fix the cause (usually credentials/network), then re-trigger the action (`scan now`, `publish-report-source`, `verify-manual`). Nothing auto-retries forever. |
| `creation_unknown` task | `python -m app.cli reconcile TASK_ID` — finds the session by the persisted correlation tag. Never re-dispatch blindly (unique constraint blocks a second task anyway). |
| `publication_records.status = unknown` | A publish may or may not have landed. The next `publish_report` read-back reconciles via the `report-sha256` marker in the issue body — no manual rewrite needed. If the marker shows an older sha, the job simply republishes. |
| Native feed shows `native_observe_error` / "NATIVE OBS UNAVAILABLE" | Observation failed (usually API permissions on `list_sessions_by_tag`). The error is visible in overview + `reports`; repair work is unaffected. Once the permission is fixed the next observe cycle clears the error. Rows stay read-only — repair attempts are never blocked on this. |
| Report destination rejected (`destination_rejected`) | `REPORT_DATA_ISSUE_NUMBER` points at a missing/closed issue or a pull request. Fix the config to a real open issue — the publisher validates every cycle, so the next run publishes. |
| Verification update `outcome: not_sent`/`failed` | Budget/capacity blocked it or the session couldn't be messaged. The verified outcome is untouched; the evidence row records the inability. Check the repair session and capacity, then `message TASK_ID` manually if needed. |
| SCAN STALE badge | Worker or scan job is down — the badge is downtime surfacing by design. Check the app container; `scan now` kick-starts a cycle. |

## Full reset (simulation only)

```bash
docker compose down -v   # destroys the app-data volume — simulation only
```

Never do this on the live volume. To wipe only simulation state while
keeping live, use the separate `COMPOSE_PROJECT_NAME` volumes —
`repairdesk-sim` and `repairdesk-live` never share `/data`.

## Backup

Back up the `app-data` volume (or just `/data/repairdesk.sqlite*` —
database + WAL sidecars) plus periodic `export-evidence` bundles off-box.
Restoring = place the DB file back on the volume and start the app;
recovery runs automatically and dedup keys make any replayed schedule a
no-op.
