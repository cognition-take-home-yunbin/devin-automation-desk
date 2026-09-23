# Native reporting — setup guide (milestone D)

The daily team report is produced by a **native Devin Automation**, not by
this app. The desk's only jobs are (a) to keep the machine-readable
report-source issue fresh and (b) to *observe* the automation's tagged
sessions read-only. It does not implement the schedule, Slack commands,
Slack delivery, or the report generation.

```text
┌─────────────── managed by this app ───────────────┐
│ deterministic snapshots → REPORT_DATA_ISSUE_NUMBER │
│ publish ≤ 1/interval unless material change        │
│ read-only observation of tagged native sessions    │
└──────────────────────┬────────────────────────────┘
                       │ reads (human/machine boundary)
┌────────────── native Devin Automation ────────────┐
│ schedule trigger → playbook session → Slack post   │
└────────────────────────────────────────────────────┘
```

## 1. Report-source issue (the desk → automation boundary)

1. In the automation repository, create one ordinary issue — e.g.
   `Devin Repair Desk — report data`. This is the single fixed
   destination; the publisher updates its body in place, never comments,
   never opens new issues.
2. Configure it (already in `.env.example`):
   - `REPORT_GITHUB_REPO` — e.g. `YOUR_ORG/devin-automation-desk`
   - `REPORT_DATA_ISSUE_NUMBER` — the issue number from step 1
   - `REPORT_GITHUB_TOKEN` — a token scoped to **only** that repository's
     issues (contents:write suffices for body edits). This is the only
     live write credential in the app.
   - `REPORT_TIMEZONE` — local timezone for report windows
     (e.g. `Asia/Singapore`); period boundaries are emitted in UTC.
   - `REPORT_PUBLISH_INTERVAL_SECONDS` / `REPORT_STALE_AFTER_SECONDS`.

The publisher validates the destination every cycle (must be an open
issue, never a pull request), embeds a `report-sha256` marker, reconciles
ambiguous writes by reading the issue back, and scrubs credential-shaped
strings before writing.

Manual trigger (interval/material-change gate still applies):

```bash
python -m app.cli publish-report-source
python -m app.cli reports      # snapshots, publication status, native sessions
```

## 2. The native Devin Automation

Create it once in the Devin webapp (**Automations → New automation**):

| Field | Value |
| --- | --- |
| Trigger | `schedule:recurring` — daily at your chosen hour in the `REPORT_TIMEZONE` timezone |
| Action | `start_session` with the prompt from `docs/reporting-playbook.md` (versioned) |
| Session tags | `repairdesk-native-report` (must match `NATIVE_REPORT_SESSION_TAG`) |
| Slack | grant the channel the daily post should land in (native integration) |
| Run as | `organization` (survives creator churn) |

The tag is the whole contract: the desk's `observe_native` job lists
sessions by `list_sessions_by_tag(NATIVE_REPORT_SESSION_TAG)` on the scan
cadence and records them in `native_sessions` — separate from managed
repair attempts. If the configured API permissions can't list them, the
dashboard shows the native feed as unavailable (`native_observe_error`)
rather than silently empty.

## 3. Operator-recorded evidence links

A session finishing does **not** prove the Slack post landed — the desk
never infers delivery. When you've confirmed the post out of band, record
it:

```bash
python -m app.cli native-link <session-id> <slack-permalink> \
    --session-url https://app.devin.ai/sessions/<session-id> \
    --operator <you>
```

The link then appears on the dashboard's native panel and inside
snapshot coverage blocks.

## 4. Verification updates on repair completion

After independent verification succeeds, the desk enqueues one budgeted,
deduplicated `verification_update` job: a factual update to the **repair**
session asking it to summarize the evidence in its own connected native
conversation. State, cap and capacity checks run first; a blocked or
failed send is recorded as evidence (`verification_update` kind,
`outcome: not_sent|failed|sent`) without corrupting the verified outcome.

Verify delivery during live testing by checking the repair session's
message history in the Devin webapp — the desk records *that* it sent the
API call; native-conversation delivery is confirmed by looking at the
session, never assumed.

## 5. What simulation covers — and what it doesn't

`sim_native_sessions` + scripted `devin.list_sessions_by_tag` errors
exercise the observation, failure and accounting paths end-to-end. They
do **not** purport to test the real Slack integration or the real
automation schedule — those are verified in the live deployment only.
