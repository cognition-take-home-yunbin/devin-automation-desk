# Devin Repair Desk — milestone A

Turns maintainer-approved, reproducible Apache Superset bugs (on the
configured fork) into Devin-authored PRs — a scheduled scanner admits
approved issues, the desk creates a bounded Devin session per issue, tracks
the repair, **independently verifies** PR checks against a versioned policy,
shows everything on a dashboard, and publishes sanitized report facts to a
fixed report-source issue.

Milestone A is the **credential-free, simulation-capable foundation**: the
full orchestration (durable jobs, state dimensions, dispatch rules,
verification, reporting) runs against fake GitHub/Devin clients. Real
remediation lands in later milestones.

## Quick start (simulation — no secrets needed)

```bash
cp .env.example .env        # APP_MODE=simulation already set
docker compose up --build -d
docker compose exec app python -m app.cli doctor
docker compose exec app python -m app.cli simulate happy-path
docker compose exec app python -m app.cli tasks
# open http://127.0.0.1:8000 — SIMULATION badge + synthetic task
```

`docker compose run --rm test` runs backend tests, the frontend typecheck,
and a production frontend build against an isolated database.

## CLI

```
python -m app.cli doctor                       # config preflight (read-only)
python -m app.cli simulate SCENARIO            # seed a scenario + queue a scan
python -m app.cli tasks                        # task states (all 4 dimensions)
python -m app.cli reports                      # snapshots + publication status
python -m app.cli pause / unpause              # pause new dispatch only
python -m app.cli export-evidence [--output D] # JSON evidence bundle
```

The CLI never starts a worker — the app process owns the single background
worker. Simulating a scenario only *adds* synthetic state; it never resets.

Scenarios: `happy-path`, `duplicate-scan`, `needs-input`, `checks-failed`,
`creation-unknown`, `throttled`, `stale-checks`, `report-failure`.

## Modes & isolation

`APP_MODE` is fail-closed: unset/unknown → startup refuses; `live` requires
every §10.4 variable (see `.env.example`) to be present and non-placeholder.
Simulation needs **no** credentials and never touches the network — fake
clients read `sim_*` fixture tables. `COMPOSE_PROJECT_NAME` separates
simulation/live volumes (`repairdesk-sim` vs `repairdesk-live`); each mode
uses its own `DATABASE_PATH`, and every persisted row carries `mode` +
`synthetic` flags.

## Layout

```
app/
  main.py            ASGI app + lifespan (starts the one worker)
  config.py          §10.4 contract, fail-closed loading, doctor
  db.py              SQLite schema + WAL + transaction helpers
  states.py          4 state dimensions + transition map
  transitions.py     audited state transitions + evidence writes
  cli.py             operator CLI (never starts a worker)
  routes/dashboard.py  read API + simulation-only scenario trigger
  clients/{base,fakes,github,devin,factory}.py
  services/{scanner,dispatch,monitor,verification,reporting,
            report_source,jobs,worker,simulator,context,policy}.py
frontend/            React + TS + Vite dashboard (built into the image)
config/verification.yaml   versioned verification policy
tests/               pytest: states, jobs, config, all 8 scenarios
scripts/test.sh      backend tests + typecheck + frontend build
docs/architecture.md component view + mermaid diagram
```

## Semantics worth knowing

- **Approval**: both labels *and* the latest `devin-approved` label event must
  come from `GITHUB_ALLOWED_APPROVERS`; labels alone are not authorization.
- **Dispatch**: creation intent + correlation tag persist before the Devin
  call; ambiguous creation reconciles by tag, never blind-retries.
- **Verification**: policy from `VERIFICATION_POLICY_PATH`; missing/failed/
  untrusted/stale checks never verify; head SHA is re-fetched at verify time.
- **Pause**: blocks new dispatch only — polling, verification, and reporting
  keep flowing.
- **Reports**: deterministic facts snapshots (schema version + sha256)
  published to one fixed report-source issue; publication failure never
  changes the remediation outcome.

## Milestone A limitations

- Live GitHub/Devin HTTP calls are stubs: reads raise
  `LiveReadsNotImplemented`, writes raise `ExternalWriteDisabled` — there are
  no external writes at all.
- Review states (`merged`, `approved`, …) are modelled but nothing watches
  GitHub for review events yet; `merged_prs` stays 0.
- Scheduled scanning exists in the worker loop; live scan specifics
  (ETag/conditional requests) are future work.
- `message`/`stop`/`retry`/`record-verification`/`publish-report-source` CLI
  commands are later milestones, as are budget reservations and Slack.
