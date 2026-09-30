---
name: testing-repair-desk
description: How to run and end-to-end test the Devin Repair Desk dashboard in simulation mode (compose stack, scenario seeding, lifecycle timing, pause/stale checks).
---

# Testing the Devin Repair Desk dashboard

## Stack

- Simulation mode needs no credentials, but `.env` needs more than `APP_MODE=simulation` for scenarios to produce tasks:
  - `GITHUB_REPO=<any value>` — REQUIRED. Fallbacks differ: `simulator.seed_scenario` seeds issues under `acme/superset-demo` while `scanner.handle_scan` lists `example/superset-demo`; unset = `candidates=0` forever.
  - `GITHUB_ALLOWED_APPROVERS=<any login>` — REQUIRED. The scanner gates on `approval_event.actor in allowed_approvers`; the seeder uses the first entry as the approval actor (fallback `ops-lead`). Empty = `approval_insufficient` audits, no tasks.
  - `COMPOSE_PROJECT_NAME=repairdesk-sim`, `APP_MODE=simulation`.
  - For lively demos: `SCAN_INTERVAL_SECONDS=15`, `POLL_INTERVAL_SECONDS=1`.
- `docker compose up --build -d`; UI + API served by the one container at `http://127.0.0.1:8000`. Frontend is a prebuilt SPA baked into the image (`STATIC_DIR=/app/frontend/dist`).
- Health: `curl http://127.0.0.1:8000/healthz` → `{"status":"ok","mode":"simulation"}`.
- CLI inside the container: `docker compose exec -T app python -m app.cli doctor|tasks|reports|pause|unpause|simulate <scenario>|export-evidence`.

## Native fallback when Docker Hub rate-limits (429)

`docker compose up --build` can fail on `registry-1.docker.io ... 429 Too Many Requests`. The same app runs natively — identical FastAPI + embedded worker, no container:

- `cd frontend && npm ci && npm run build` (dist is gitignored — always rebuild when the PR touches `frontend/src`).
- From repo root: `APP_MODE=simulation GITHUB_REPO=acme/superset-demo GITHUB_BASE_BRANCH=master GITHUB_ALLOWED_APPROVERS=ops-lead DATABASE_PATH=/tmp/repairdesk-sim.sqlite VERIFICATION_POLICY_PATH=config/verification.yaml STATIC_DIR=frontend/dist POLL_INTERVAL_SECONDS=1 SCAN_INTERVAL_SECONDS=15 WORKER_IDLE_SECONDS=1 python3 -m uvicorn app.main:create_app --factory --host 127.0.0.1 --port 8000`
- Use `python3 -m uvicorn` (pyenv), NOT a stray `uvicorn` binary — `~/.local/bin/uvicorn` may belong to a different Python lacking fastapi.
- Fresh lifecycle = delete the sqlite file + restart (no volume needed).
- Kill by port, not `pkill -f "uvicorn app.main"` — the pattern matches your own shell's command line and kills it. `ss -tlnp | grep :8000` → kill the PID.
- Worker runs as an in-process asyncio task; jobs land in the same sqlite — query `DATABASE_PATH` directly to inspect jobs/audits.

## Scan cadence drives the PR watcher — "Scan now" does NOT

- `watch_prs` is enqueued beside `scan_issues` only when `_maybe_schedule_scan` finds `control.last_scan_at` older than `SCAN_INTERVAL_SECONDS`; `handle_scan` re-stamps `last_scan_at` on completion.
- The "Scan now" button (`POST /api/scan`) enqueues `scan_issues` ONLY — it can never trigger the watcher, and its completion re-anchors the cadence (it can *delay* the next watch tick, not hurry it). To observe post-delivery facts just wait ~1 scan interval after `delivered`. Same for `cli scan now`.

## Fresh-lifecycle demo requires a DB reset

- Each UI scenario button POSTs `/api/simulation/scenarios` and seeds a **fixed** issue number (happy-path=101, needs-input=102, checks-failed=103, creation-unknown=104, throttled=105, stale-checks=106, report-failure=107, duplicate-scan=108, approval-withdrawn=109, snapshot-changed=110, native-observe-failure=111, merged=112, closed-unmerged=113, review-decisions=114). Seeding is `INSERT OR IGNORE` — re-running a scenario **dedupes silently**; no new task, no visible change.
- Post-delivery review states land via the `watch_prs` job on the scan cadence (~15s in sim): `merged`/`closed-unmerged` flip the PR closed on the 3rd PR read (`merged_after_reads`/`closed_after_reads`=2; verify reads the PR twice, so the watcher's first read after delivery is #3); `review-decisions` seeds APPROVED(ops-lead)+CHANGES_REQUESTED(maintainer) reviews — latest-substantive-per-author wins (changes_requested). Each landed outcome republishes the report.
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

- Top bar: brand, section nav pills (Overview/Tasks/Reports/Native sessions), mode pill (`SIMULATION`/`LIVE`), Refresh, `Scan now`. Greeting row shows repo + tracked-task count; its status strip carries the PAUSED / `SCAN STALE {age}s` / REPORT STALE / NATIVE OBS UNAVAILABLE badges and the `Live · refreshed …` / `Stale — last good data retained (...)` text.
- Hero metrics (4 cards): active tasks (sub: total tracked, `N blocked` pill), needs intervention, verified PRs (sub: operator-verified · merged), observed ACUs (sub: scope · sessions). Right sidebar: **Desk health** (scanner / dispatch / report publication / native observation signals), **Daily admission budget** gauge (% remaining, ACU held, project left), **Where tasks are** (execution-state bar chart).
- Task rows: issue #+title+SYNTHETIC tag, a plain-language headline (e.g. "PR independently verified · awaiting human review"), `approved by <actor>`, 4 state chips (execution/validation/review/disposition, humanized — raw value in the chip `title`), age, ACUs, PR/session/slack link pills, delete icon button. Click a row → detail drawer (issue snapshot, attempts with correlation tags, evidence grouped by provenance, audit timeline newest-first). Escape or ✕ or backdrop closes.
- Quick filter tabs (All / In progress / Needs attention / Delivered / Ended) plus an "Any state" select filter rows across all 4 dimensions; both compose.
- needs_input evidence carries the scripted question (e.g. "Which baseline SHA should I use?").
- Row Links column shows a `slack` anchor when `slack_link` is set; drawer has a `Cleanup` chip (pending/kept/terminated) + `Slack` field.
- Headlines for post-delivery states (util.ts `describeTask`): merged → "Merged by a human reviewer" (ok); closed_unmerged → "PR closed without merging" (bad); approved → "Approved by a human reviewer" (ok); changes_requested → "Reviewer requested changes" (warn).
- Scenario chip clicks: the browser window needs focus or the first click only focuses it — click once, then again / press Enter. Chips are `<button>`; read `getBoundingClientRect` via the app tab's console if coordinates seem off.

## Operator CLI flows (milestone B)

- All under `docker compose exec -T app python -m app.cli …`; they enqueue jobs the worker executes (~1s).
- `scan now` → `scan_requested` audit. `message TASK_ID TEXT` → needs a live session + execution not stopped/failed; audits `message_sent` + note evidence. `slack-link TASK_ID URL` → `slack_link_recorded` audit.
- `stop TASK_ID` → stopped/cancelled/Cleanup=terminated + termination evidence; on agent_finished/delivered it's `stop_skipped` ("nothing to terminate") and the outcome is preserved. Stopping a task that holds the session slot frees it for queued dispatches. Stop consumes the held 20-ACU reservation at FULL amount (not observed spend) — daily-admission-left drops by 20.
- `retry TASK_ID --reason R` → only from failed/stopped/agent_finished with no pr_number and no live session; requeues, disposition→active, `operator_retry`+`retry_queued` audits, fresh attempt+session. Deterministic verified-retry demo: stop a QUEUED task → retry it (stays queued behind the slot) → stop the needs_input task → slot frees → retried task dispatches → delivered.
- `tasks.cleanup_state` is first-writer-wins: the monitor only writes 'kept'/'terminated' while it's 'pending', so a task stopped-then-retried keeps 'terminated' on its summary chip even though the new session's cleanup_record is 'kept'.

## Review-blocked scenarios & task-ID mapping

- `approval-withdrawn` (#109) and `snapshot-changed` (#110) seed a task with an ACCEPTED approval receipt, then flip live issue state; dispatch re-verifies and lands execution=queued/disposition=blocked with a `dispatch_review_blocked` audit, NO session (devin_session_id NULL), receipt→rejected.
- CLI takes DB task ids, not issue numbers. Get them from `curl -s http://127.0.0.1:8000/api/tasks` (task 1≈issue 101, created in launch order) or the drawer aria-label "Task N detail".

## Forcing SCAN STALE deterministically

- Badge shows when `scan_age > 3×SCAN_INTERVAL_SECONDS` (check `.env` — it may differ from `.env.example`'s 60s; e.g. 15s → 45s threshold). `cli pause` does NOT gate scans, and container stop kills the API (stale-data instead). Backdating `control.last_scan_at` alone self-heals on the next successful scan.
- Working lever: inject a sustained error via the app's own fault table — `INSERT INTO sim_call_scripts (operation, remaining, error) VALUES ('github.list_candidate_issues', 9999, 'rate_limited')` inside the container (sqlite at `/data/repairdesk.sqlite`) + backdate `last_scan_at` → scans requeue forever while the API serves → badge persists with counting age. `DELETE` the row + `cli scan now` → badge clears.

## Known cosmetic quirk

- Older builds could show `job_retry — verify_task: InvalidTransition: pr_found -> verified` in a task's audit (verify job raced the pr_found→checks_pending transition). Fixed on the milestone-B branch — a clean timeline should now show pr_found → checks_pending → verified with no retry. If it reappears, that fix regressed.

## Devin Secrets Needed

- None for simulation mode. Live mode (`APP_MODE=live`) is fail-closed and requires the §10.4 vars in `.env.example` — do not test live without them.
