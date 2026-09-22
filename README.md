# Devin Repair Desk — milestone B

Turns maintainer-approved, reproducible Apache Superset bugs (on the
configured fork) into Devin-authored PRs — a scheduled scanner admits
approved issues, the desk creates a bounded Devin session per issue via the
**live v3 API**, tracks the repair, **independently verifies** PR checks
against a versioned policy, shows everything on a dashboard, and publishes
sanitized report facts to a fixed report-source issue.

Milestone B adds the **live path**: real GitHub/Devin clients, the
approval-integrity re-check before dispatch, reservation-based ACU budgets,
the full operator CLI, and the handoff/cleanup ledger — while simulation
mode still runs the entire pipeline credential-free on fake providers.

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

## Quick start (live)

Fill every live variable in `.env` (see `.env.example` — `APP_MODE=live`,
`GITHUB_REPO`, `GITHUB_TOKEN`, `GITHUB_ALLOWED_APPROVERS`, `DEVIN_API_KEY`,
`DEVIN_ORG_ID`, `DEVIN_REPO_REF`, `DEVIN_REMEDIATION_PLAYBOOK_ID`,
`DEVIN_KNOWLEDGE_IDS`, `REPORT_*`). Incomplete live config refuses startup —
the app never starts half-configured. `DISPATCH_PAUSED_ON_FIRST_START=true`
keeps a brand-new deployment paused until an operator unpauses it.

## CLI

```
python -m app.cli doctor                       # config preflight (read-only)
python -m app.cli simulate SCENARIO            # simulation only: seed + scan
python -m app.cli scan now                     # enqueue a scan ahead of schedule
python -m app.cli tasks                        # task states (all 4 dimensions)
python -m app.cli reports                      # snapshots + publication status
python -m app.cli pause / unpause              # pause new dispatch only
python -m app.cli message TASK_ID TEXT         # budgeted follow-up to the session
python -m app.cli stop TASK_ID [--reason R]    # permanent terminate (archive=true)
python -m app.cli retry TASK_ID --reason R     # intentional re-dispatch
python -m app.cli reconcile TASK_ID            # find session by correlation tag
python -m app.cli slack-link TASK_ID URL       # record the native Slack thread
python -m app.cli export-evidence [--output D] # JSON evidence bundle
```

The CLI never starts a worker — every action is a durable job or a read.
Simulating a scenario only *adds* synthetic state; it never resets.

Scenarios: `happy-path`, `duplicate-scan`, `needs-input`, `checks-failed`,
`creation-unknown`, `throttled`, `stale-checks`, `report-failure`,
`approval-withdrawn`, `snapshot-changed`.

## Modes & isolation

`APP_MODE` is fail-closed: unset/unknown → startup refuses; `live` requires
every §10.4 variable to be present and non-placeholder. Simulation needs
**no** credentials and never touches the network — fake clients read
`sim_*` fixture tables. `COMPOSE_PROJECT_NAME` separates
simulation/live volumes (`repairdesk-sim` vs `repairdesk-live`); each mode
uses its own `DATABASE_PATH`, and every persisted row carries `mode` +
`synthetic` flags. Job and state-transition logic is identical in both
modes — the fake providers exercise the same failure surface (rate limits,
ambiguous writes) the live clients surface.

## Semantics worth knowing

- **Approval**: both labels *and* the latest `devin-approved` label event
  must come from `GITHUB_ALLOWED_APPROVERS`; labels alone are not
  authorization, and PR objects are never issues.
- **Intake freeze**: issue snapshot + approval receipt persist before
  dispatch is enqueued. Before a session is created, dispatch **re-verifies**
  — the issue still open, approval label present, latest approval event an
  allowed actor's `labeled`, frozen hash unchanged. Any drift stops the
  task for review (`disposition=blocked`, receipt rejected) — never spend.
- **Issue text is evidence, not instructions**: the frozen snapshot goes to
  Devin explicitly marked UNTRUSTED EVIDENCE; behavior is pinned by the
  configured playbook/knowledge + acceptance criteria, not by issue text.
- **Dispatch**: creation intent + correlation tag persist before the Devin
  call; ambiguous creation → `creation_unknown` → reconcile by tag, never
  blind retry. Live reads honor `Retry-After`/`X-RateLimit-Reset`; writes
  that may have landed are `AmbiguousCreation` — preserved, not retried.
- **Budgets**: dispatch *reserves* `REPAIR_ACU_LIMIT` first
  (`budget_reservations` `held` → `consumed` at terminal with observed ACU,
  or `released` when nothing was spent). `DAILY_ADMISSION_ACU_LIMIT` and
  `PROJECT_ADMISSION_ACU_LIMIT` gate admission; `message`/`retry` verify
  remaining capacity before the API call. Budgets govern managed dispatch —
  a native Slack conversation an operator drives is not a managed call.
- **Stop vs keep**: `stop` permanently terminates the session
  (`archive=true`) and preserves the task outcome; `cleanup_state` +
  `cleanup_records` track the ledger separately — `kept` (retained for the
  native conversation + verification update), `terminated` (operator or
  remote/provider), `pending`. Sessions are never auto-resumed.
- **Retry** is operator-only: requires `--reason`, refuses while a session
  is live or a PR already exists, and mints a new attempt + new correlation
  tag + new session.
- **Scan freshness**: the worker enqueues `scan_issues` every
  `SCAN_INTERVAL_SECONDS` and `last_scan_at` feeds a SCAN STALE badge —
  downtime is visible; the periodic trigger makes no webhook-style
  immediacy promise.
- **Slack**: no custom Slack app/SDK/gateway — native replies ride Devin's
  own integration. See `docs/native-slack-sync.md` for the feasibility test;
  `slack-link` records the operator-created thread per task.
- **Serial pilot**: `MAX_ACTIVE_SESSIONS=1` — one managed session at a
  time; further dispatch requeues until the slot frees.
- **Verification**: policy from `VERIFICATION_POLICY_PATH`; missing/failed/
  untrusted/stale checks never verify; head SHA is re-fetched at verify time.
- **Reports**: deterministic facts snapshots (schema version + sha256)
  published via `REPORT_GITHUB_TOKEN` to one fixed report-source issue;
  publication failure never changes the remediation outcome.

## Layout

```
app/
  main.py            ASGI app + lifespan (starts the one worker)
  config.py          §10.4 contract, fail-closed loading, doctor
  db.py              SQLite schema v2 + WAL + transaction helpers
  states.py          4 state dimensions + transition map
  transitions.py     audited state transitions + evidence writes
  cli.py             operator CLI (never starts a worker)
  routes/dashboard.py  read API + simulation-only scenario trigger
  clients/{base,fakes,github,devin,factory}.py
  services/{scanner,dispatch,monitor,verification,reporting,
            report_source,jobs,worker,simulator,context,policy,
            operator,budget}.py
frontend/            React + TS + Vite dashboard (built into the image)
config/verification.yaml   versioned verification policy
tests/               pytest: states, jobs, config, all 10 scenarios,
                     milestone-B integrity/budget/operator paths
scripts/test.sh      backend tests + typecheck + frontend build
docs/architecture.md        component view + mermaid diagram
docs/native-slack-sync.md   Slack-on-API-session feasibility note
```

## Current limitations

- Review states (`merged`, `approved`, …) are modelled but nothing watches
  GitHub for review events yet; `merged_prs` stays 0.
- The native-Slack-on-API-session feasibility test
  (`docs/native-slack-sync.md`) is documented but not yet executed against
  a live org; `slack-link` is the manual bridge until it is confirmed.
- `daily_acu_usage` (enterprise consumption API) degrades to `None` when
  the service user lacks the scope — budget accounting then relies on the
  local reservation ledger only.
- The dashboard is read-only; operator actions go through the CLI.
