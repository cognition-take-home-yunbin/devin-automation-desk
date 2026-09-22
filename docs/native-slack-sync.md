# Native Slack sync on API-created sessions — feasibility note

## Constraint

The PRD forbids a custom Slack surface: no Slack app, no Slack SDK client,
no command endpoint, no webhook gateway, no tunnel. Any Slack-side
conversation about a repair must ride **Devin's own Slack integration**.

The desk creates repair sessions through the **Devin v3 API**
(`POST /v3/organizations/{org}/sessions`), not through Slack. The open
question is whether a session born that way can still carry a *native*
Slack conversation — i.e., whether an operator can talk to that session in
Slack and have Devin's own integration route the replies, so the task's
session keeps a human channel without the desk ever touching Slack.

## The feasibility test (to run once against the live org)

1. Create a session via the v3 API exactly as `dispatch` does today
   (prompt + playbook + correlation tag).
2. In the org's Slack workspace, open a thread and hand the thread to the
   session through Devin's native surface (the Devin app for Slack /
   `attach_thread`-style binding Devin's own integration exposes).
3. Post an operator message in the thread; verify the API-created session
   receives it and its reply lands back in the same thread — all through
   Devin's integration, nothing custom on our side.
4. Repeat for the verification-update case: when the desk's independent
   verification lands, the operator follow-up (`message`) must still reach
   the same session, and any native thread stays attached.

**Outcome needed:** both directions work with zero custom Slack code.

**Status:** written for milestone B; requires a live org + the Devin Slack
integration installed to execute. Not yet exercised — simulation covers
the API-side half (`send_message`, `stop_task`, polling) and the Slack
half is documented manually via `slack-link`.

## What the desk does today

- **Operator CLI `slack-link TASK_ID URL`** records the thread URL an
  operator created natively. It's displayed in the task drawer and the
  task table. Recording is manual bookkeeping — the desk never writes to
  Slack.
- **Budgets govern managed dispatch only.** A native Slack conversation
  an operator has with the session is not a managed API call and is not
  charged against `DAILY_ADMISSION_ACU_LIMIT` /
  `PROJECT_ADMISSION_ACU_LIMIT`. The API-side follow-ups the desk *does*
  make (`message TASK_ID`) are capacity-checked before submission.
- **Handoff policy** keeps a verified/finished task's session available
  (cleanup_state `pending` → `kept`) precisely so the planned native
  conversation and the verification update still have somewhere to land.
  Only `stop` terminates (`archive=true`), and that is recorded in
  `cleanup_records` separately from the task outcome.
