---
name: testing-repair-desk
description: How to run and end-to-end test the Devin Repair Desk dashboard in simulation mode (compose stack, scenario seeding, lifecycle timing, pause/stale checks).
---

# Testing the Devin Repair Desk dashboard

## Stack

- Simulation mode needs no credentials: `.env` with `APP_MODE=simulation` + `COMPOSE_PROJECT_NAME=repairdesk-sim`.
- `docker compose up --build -d`; UI + API served by the one container at `http://127.0.0.1:8000`. Frontend is a prebuilt SPA baked into the image (`STATIC_DIR=/app/frontend/dist`).
- Health: `curl http://127.0.0.1:8000/healthz` → `{"status":"ok","mode":"simulation"}`.
- CLI inside the container: `docker compose exec -T app python -m app.cli doctor|tasks|reports|pause|unpause|simulate <scenario>|export-evidence`.

## Fresh-lifecycle demo requires a DB reset

- Each UI scenario button POSTs `/api/simulation/scenarios` and seeds a **fixed** issue number (happy-path=101, needs-input=102, checks-failed=103, creation-unknown=104, throttled=105, stale-checks=106, report-failure=107, duplicate-scan=108). Seeding is `INSERT OR IGNORE` — re-running a scenario **dedupes silently**; no new task, no visible change.
- To watch a scenario progress live, reset first: `docker compose down -v && docker compose up -d` (image persists; SQLite volume is recreated empty; schema auto-inits on boot).
- Nothing auto-seeds at startup — tasks exist only after someone launches a scenario (UI button or `cli simulate`).

## Timing

- Worker idle tick ~1s (`WORKER_IDLE_SECONDS`), session poll `POLL_INTERVAL_SECONDS` (1s in sim), periodic scan `SCAN_INTERVAL_SECONDS` (15s). UI auto-refreshes every 3s.
- happy-path completes end-to-end in ~20s: queued → dispatching → working → agent_finished; validation no_pr → pr_found → checks_pending → verified; review → awaiting_review; disposition → delivered; a report snapshot publishes automatically and shows `confirmed → <report-repo>#<issue>`.

## Serial admission & pause semantics (expected, not bugs)

- `MAX_ACTIVE_SESSIONS=1`: only one task can be in dispatching/working/needs_input/etc. A needs_input task parks there forever (no UI to answer the question), so every later task stays `queued`/`active`. Order clicks deliberately: launch happy-path first if you want it to finish.
- `cli pause` → header shows `PAUSED` badge and new tasks stick at `queued` (dispatch job requeues before transitioning); `cli unpause` resumes. Scans still run while paused.
- Stale indicator: stop the container (`docker compose stop app`) and the loaded page shows `stale — last good refresh retained (...)`; data is kept, not zeroed. `docker compose start app` → recovers.

## Dashboard under test

- Header: mode badge, repo, PAUSED badge, refresh/stale text. Metrics row: active tasks, needs intervention, verified PRs, merged PRs, observed ACUs.
- Task rows: issue #+title+SYNTHETIC tag, approver, 4 state chips (execution/validation/review/disposition), elapsed, ACUs, PR/session links. Click a row → detail drawer (issue snapshot, attempts with correlation tags, evidence, audit timeline). Escape or ✕ or backdrop closes.
- "Filter by state" dropdown filters rows across all 4 dimensions.
- needs_input evidence carries the scripted question (e.g. "Which baseline SHA should I use?").

## Known cosmetic quirk

- Task #101's audit timeline can contain `job_retry — verify_task: InvalidTransition: validation: pr_found -> verified is not allowed`: the verify job raced the pr_found→checks_pending transition and retried harmlessly. End state is correct; worth a look if strict audit cleanliness matters.

## Devin Secrets Needed

- None for simulation mode. Live mode (`APP_MODE=live`) is fail-closed and requires the §10.4 vars in `.env.example` — do not test live without them.
