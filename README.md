# Devin Repair Desk — milestone D

Turns maintainer-approved, reproducible Apache Superset bugs (on the
configured fork) into Devin-authored PRs — a scheduled scanner admits
approved issues, the desk creates a bounded Devin session per issue via the
**live v3 API**, tracks the repair, **independently verifies** PR checks
against a versioned policy, shows everything on a dashboard, and publishes
sanitized report facts to a fixed report-source issue.

Milestone D delivers the revised **native reporting** design: deterministic
time-windowed snapshots (today / previous local day / rolling 7d in
`REPORT_TIMEZONE`), publication to the one pre-created report-source issue
with destination validation + read-back reconcile, and read-only
observation of the external Devin Automation that owns the daily Slack
post — plus one budgeted verification update back to the repair session.

## Three automations, three owners

| | Managed repair automation (this app) | Native collaboration | Native reports (external) |
| --- | --- | --- | --- |
| What | scan → dispatch → poll → verify → publish | per-repair Slack conversation attached to the repair session | daily summary posted to Slack |
| Who runs it | this app's single worker | Devin's native Slack integration on the API session | a **native Devin Automation** you configure once |
| Budget/schedule | `*_ACU_LIMIT` reservations + `SCAN_INTERVAL_SECONDS` | operator-driven, not a managed call | the automation's own schedule + sessions |
| The app… | owns it entirely | sends one bounded update; never drives the thread | only *observes* tagged sessions read-only; never infers delivery |

Setup for the native report: **`docs/native-reporting.md`**; the versioned
prompt it runs: **`docs/reporting-playbook.md`**.

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
python -m app.cli verify-manual TASK_ID \      # CI-unavailable fallback:
    --operator NAME --head-sha SHA \          #   record operator evidence —
    --command CMD --results TEXT --evidence U #   validation=manually_verified
python -m app.cli slack-link TASK_ID URL       # record the native Slack thread
python -m app.cli publish-report-source        # publish to the fixed issue now
python -m app.cli native-link SID URL \        # operator-verified slack link on a
    --session-url U --operator NAME            #   native report session
python -m app.cli export-evidence [--output D] # JSON evidence bundle
```

The CLI never starts a worker — every action is a durable job or a read.
Simulating a scenario only *adds* synthetic state; it never resets.

Scenarios: `happy-path`, `duplicate-scan`, `needs-input`, `checks-failed`,
`creation-unknown`, `throttled`, `stale-checks`, `report-failure`,
`approval-withdrawn`, `snapshot-changed`, `native-observe-failure`.

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
- **Verification**: policy from `VERIFICATION_POLICY_PATH`. The verifier
  checks the PR targets the configured repo + base branch (out-of-scope
  targets are flagged and blocked), requires every named check to be
  `success` on the current head SHA and produced by a trusted workflow, and
  re-reads the head before the verdict — a mid-verification push re-opens
  the cycle instead of certifying a stale commit. Check-run provenance the
  policy doesn't name is flagged (`workflow_changed`), missing/skipped/
  neutral/failed/untrusted checks never verify.
- **Assertions vs facts**: evidence the session produced (`verifier=agent`)
  is stored and displayed separately from independently retrieved facts
  (`verifier=github-verifier`). The agent's claimed head SHA is recorded as
  `claimed_head_sha` inside the assertion, never as a verified field.
- **Manual verification fallback** (`verify-manual`): when CI is
  unavailable an operator can record verification — `--operator`,
  `--head-sha` (exact SHA), `--command`, `--results`, `--evidence` (URI)
  are all required. The task lands `validation=manually_verified`, a
  distinct state that is **never** presented as CI-verified, with a
  `manual_verification` evidence row labelled as such.
- **Reports (managed → native boundary)**: deterministic snapshots over
  three windows — today-to-date, previous local day, rolling 7d — computed
  in `REPORT_TIMEZONE` with exact UTC boundaries. `sha256` covers canonical
  content *excluding* generation time, so identical data is identical:
  publication is skipped unless the hash materially changed or
  `REPORT_PUBLISH_INTERVAL_SECONDS` elapsed. The body embeds a
  `report-sha256` marker; ambiguous writes reconcile by **reading the
  issue back**, and the destination is re-validated each cycle (open issue
  in `REPORT_GITHUB_REPO`, never a PR). Credential-shaped strings are
  scrubbed; snapshots carry evidence links, explicit unknowns, and
  managed-vs-native coverage — never secrets, Slack bodies, or raw logs.
  Snapshots persist locally; history is never rewritten.
- **Native observation**: sessions tagged `NATIVE_REPORT_SESSION_TAG` (the
  external reporting automation) are listed read-only into
  `native_sessions` on the scan cadence — separate from managed attempts.
  Usage the API can't return stays `unknown`, observation failure surfaces
  as `native_observe_error` + a dashboard badge, and a session's status
  never implies Slack delivery — only an operator-recorded `native-link`
  permalink does.
- **Verification update**: after `validation=verified`, one budgeted,
  deduplicated `verification_update` job posts a factual update to the
  repair session asking it to summarize the evidence in its connected
  native conversation. State/cap/capacity checks run first; a blocked or
  failed send is recorded as evidence without corrupting the outcome.
- **Reports**: publication failure never changes the remediation outcome.

## Layout

```
app/
  main.py            ASGI app + lifespan (starts the one worker)
  config.py          §10.4 contract, fail-closed loading, doctor
  db.py              SQLite schema v3 + WAL + transaction helpers
  states.py          4 state dimensions + transition map
  transitions.py     audited state transitions + evidence writes
  cli.py             operator CLI (never starts a worker)
  routes/dashboard.py  read API + simulation-only scenario trigger
  clients/{base,fakes,github,devin,factory}.py
  services/{scanner,dispatch,monitor,verification,reporting,
            report_source,jobs,worker,simulator,context,policy,
            operator,budget,native}.py
frontend/            React + TS + Vite dashboard (built into the image)
config/verification.yaml   versioned verification policy
tests/               pytest: states, jobs, config, all 11 scenarios,
                     milestone-B integrity/budget/operator paths,
                     milestone-C verifier/manual-verify/flags,
                     milestone-D windows/publication/native/update
docs/architecture.md         component view + mermaid diagram
docs/native-slack-sync.md    Slack-on-API-session feasibility note
docs/native-reporting.md     native report automation setup guide
docs/reporting-playbook.md   versioned prompt for the report automation
docs/recovery.md             restart/failure recovery runbook
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
- The daily report's schedule, Slack delivery and generation live in the
  external Devin Automation by design — the app intentionally does not
  implement them (`docs/native-reporting.md`). Simulation fakes the
  observation surface; it does not purport to test the real Slack
  integration or real schedule.
- `list_sessions_by_tag` requires API permission to list org sessions;
  without it native sessions show as unavailable (error surfaced), never
  silently empty.
- Actual delivery of the verification update into the session's native
  conversation is confirmed by inspecting the session — the desk records
  the API send, never assumes the post landed.
- The dashboard is read-only; operator actions go through the CLI.
- `manually_verified` tasks were operator-verified, not CI-verified — the
  UI and exports keep that distinction.
