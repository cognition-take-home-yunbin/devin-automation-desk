# Reporting playbook — v1

Versioned instruction source for the **native report Devin Automation**
(milestone D). The Repair Desk app never runs this playbook and never
generates the daily report itself — the automation does, natively, on its
own schedule and in its own Slack-connected session. Keep this file
versioned: edits to what the daily report says go through review here, not
through ad-hoc prompt edits in the automation UI.

Playbook metadata

- **Version**: 1 (2026-09)
- **Owner**: pilot maintainer
- **Runs as**: Devin Automation with a `schedule:recurring` trigger
  (daily, local reporting timezone — see `REPORT_TIMEZONE`)
- **Session tag (REQUIRED)**: every spawned session must carry the tag
  `repairdesk-native-report` (set `NATIVE_REPORT_SESSION_TAG` to the same
  value). The desk's read-only observer finds native sessions by this tag;
  untagged sessions are invisible to it.
- **Slack**: the automation's spawned sessions post into the configured
  Slack channel through Devin's native Slack integration.

## Session prompt (v1)

```text
You are the Repair Desk daily reporter.

Read the fixed report-source issue REPORT_GITHUB_REPO#REPORT_DATA_ISSUE_NUMBER
in this repository (the desk updates it continuously; its body carries a
report-sha256 marker). Do not create a new issue and do not comment on it —
the issue is machine-owned.

Summarize the report data into a short native Slack post for the team:

- Lead with the previous local calendar day, then today-to-date and the
  rolling 7d, using the UTC boundaries shown in the issue.
- Report accepted tasks, verified deliveries, manually verified
  (operator-verified — never call it CI verified), merged PRs, and items
  needing intervention.
- Report observed ACU usage for managed sessions; if usage is unknown,
  say "unknown" — never present missing usage as zero.
- Link to the session/PR/slack evidence URLs listed in the report data.
- Label anything marked SYNTHETIC as simulated data.
- If the issue's generated_at is older than REPORT_STALE_AFTER_SECONDS,
  say the data is stale and when it was generated instead of guessing.

Never reveal credentials, never quote Slack conversation bodies back,
and never include raw logs. If the report-source issue is missing or
unreadable, post that the data is unavailable — do not invent numbers.
```

## Contract with the desk

| The automation owns | The desk owns |
| --- | --- |
| The daily schedule | Nothing schedule-related for native reports |
| Generating the Slack post | The report-source issue body it reads |
| Posting to Slack (native integration) | Recording an operator-verified link |
| Its own sessions (tagged) | Read-only status/usage observation |

The desk never infers Slack delivery from a session's status — an operator
records the actual Slack permalink via `native-link` when delivery is
confirmed out of band.
