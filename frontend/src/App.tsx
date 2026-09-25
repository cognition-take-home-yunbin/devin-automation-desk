import { useCallback, useEffect, useState } from "react";
import { api } from "./api";
import type {
  NativeSession,
  Overview,
  Report,
  TaskDetail,
  TaskSummary,
} from "./types";
import TaskDrawer from "./components/TaskDrawer";
import { fmtAgo, fmtTime, stateChipClass, stateIcon } from "./util";

const SCENARIOS = [
  "happy-path",
  "duplicate-scan",
  "needs-input",
  "checks-failed",
  "creation-unknown",
  "throttled",
  "stale-checks",
  "report-failure",
  "approval-withdrawn",
  "snapshot-changed",
  "native-observe-failure",
];

export default function App() {
  const [overview, setOverview] = useState<Overview | null>(null);
  const [tasks, setTasks] = useState<TaskSummary[]>([]);
  const [reports, setReports] = useState<Report[]>([]);
  const [nativeSessions, setNativeSessions] = useState<NativeSession[]>([]);
  const [selected, setSelected] = useState<TaskDetail | null>(null);
  const [stale, setStale] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [filter, setFilter] = useState("");
  const [loading, setLoading] = useState(true);
  const [scanState, setScanState] = useState<"idle" | "sending" | "queued" | "pending">("idle");

  const refresh = useCallback(async () => {
    try {
      const [o, t, r, n] = await Promise.all([
        api.overview(),
        api.tasks(),
        api.reports(),
        api.nativeSessions(),
      ]);
      setOverview(o);
      setTasks(t);
      setReports(r);
      setNativeSessions(n);
      setStale(false);
      setError(null);
      if (selected) {
        const fresh = await api.task(selected.id);
        setSelected(fresh);
      }
    } catch (e) {
      // Keep prior data with a stale indicator rather than zeroing out.
      setStale(true);
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, [selected]);

  useEffect(() => {
    refresh();
    const t = setInterval(refresh, 3000);
    return () => clearInterval(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const openTask = async (id: number) => setSelected(await api.task(id));
  const runScenario = async (name: string) => {
    await api.startScenario(name);
    await refresh();
  };
  const scanNow = async () => {
    if (scanState === "sending") return;
    setScanState("sending");
    try {
      const res = await api.scanNow();
      setScanState(res.queued ? "queued" : "pending");
      setTimeout(() => setScanState("idle"), 4000);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      setScanState("idle");
    }
    await refresh();
  };

  const filtered = tasks.filter(
    (t) =>
      !filter ||
      t.execution === filter ||
      t.validation === filter ||
      t.review === filter ||
      t.disposition === filter
  );

  return (
    <>
      <header className="topbar">
        <h1>Devin Repair Desk</h1>
        {overview && (
          <span className={`badge ${overview.mode}`}>
            {overview.mode.toUpperCase()}
          </span>
        )}
        {overview?.paused && <span className="badge paused">PAUSED</span>}
        {overview && !overview.scan_fresh && (
          <span className="badge stale-scan">
            SCAN STALE
            {overview.scan_age_seconds != null &&
              ` ${Math.round(overview.scan_age_seconds)}s`}
          </span>
        )}
        {overview && !overview.publish_fresh && (
          <span className="badge stale-scan" title="no confirmed report publication within the staleness window">
            REPORT STALE
          </span>
        )}
        {overview?.native_observe_error && (
          <span
            className="badge stale-scan"
            title={overview.native_observe_error}
          >
            NATIVE OBS UNAVAILABLE
          </span>
        )}
        <span className="repo" title="Configured repository">
          {overview?.repo ?? "…"}
        </span>
        <span className="spacer" />
        <button
          className="scan-now"
          onClick={scanNow}
          disabled={scanState === "sending"}
          title="Enqueue a scan for approved issues right now (human override). Dispatch still respects pause, budgets, and approval checks."
        >
          {scanState === "sending"
            ? "queueing…"
            : scanState === "queued"
              ? "scan queued"
              : scanState === "pending"
                ? "scan already pending"
                : "Scan now"}
        </button>
        <span
          className={`refresh ${stale ? "stale" : ""}`}
          role={stale ? "alert" : undefined}
        >
          {stale
            ? `stale — last good refresh retained (${error})`
            : overview
            ? `refreshed ${fmtAgo(overview.generated_at)}`
            : "connecting…"}
        </span>
      </header>
      <main className={stale ? "content-stale" : ""}>
        {loading && !overview && (
          <div className="empty" role="status">
            Loading dashboard…
          </div>
        )}
        {stale && !overview && (
          <div className="error" role="alert">
            Could not reach the API — {error ?? "unknown error"}
          </div>
        )}
        {overview && (
          <section className="metrics" aria-label="metrics">
            <Metric label="active tasks" value={overview.metrics.active} />
            <Metric label="blocked" value={overview.metrics.blocked} />
            <Metric
              label="needs intervention"
              value={overview.metrics.needs_intervention}
            />
            <Metric label="verified PRs" value={overview.metrics.verified_prs} />
            <Metric
              label="manual verifies"
              value={overview.metrics.manually_verified}
              sub="operator evidence — not CI"
            />
            <Metric label="merged PRs" value={overview.metrics.merged_prs} />
            <Metric
              label="observed ACUs"
              value={overview.metrics.observed_acus}
              sub={String(overview.metrics.acus_scope)}
            />
            <Metric
              label="ACU held"
              value={overview.metrics.budget_held_acu}
              sub="active reservations"
            />
            <Metric
              label="daily admission left"
              value={overview.metrics.daily_admission_remaining}
              sub={`project left ${overview.metrics.project_admission_remaining}`}
            />
          </section>
        )}

        {overview?.mode === "simulation" && (
          <section className="scenarios" aria-label="simulation scenarios">
            <span className="small">Launch scenario:</span>
            {SCENARIOS.map((s) => (
              <button key={s} onClick={() => runScenario(s)}>
                {s}
              </button>
            ))}
          </section>
        )}

        <section className="panel">
          <h2>Tasks</h2>
          <FilterBar value={filter} onChange={setFilter} tasks={tasks} />
          {filtered.length === 0 ? (
            <div className="empty">
              {filter
                ? `No tasks match “${filter}” right now.`
                : overview?.mode === "simulation"
                  ? "No tasks yet — launch a scenario above to exercise the pipeline with synthetic data."
                  : "No approved issues discovered yet."}
            </div>
          ) : (
            <table aria-label="repair tasks">
              <thead>
                <tr>
                  <th>Issue</th>
                  <th>Approver</th>
                  <th>Execution</th>
                  <th>Validation</th>
                  <th>Review</th>
                  <th>Disposition</th>
                  <th>Elapsed</th>
                  <th>ACUs</th>
                  <th>Links</th>
                </tr>
              </thead>
              <tbody>
                {filtered.map((t) => (
                  <tr
                    key={t.id}
                    onClick={() => openTask(t.id)}
                    onKeyDown={(e) => {
                      if (e.key === "Enter" || e.key === " ") {
                        e.preventDefault();
                        openTask(t.id);
                      }
                    }}
                    tabIndex={0}
                    aria-label={`Task ${t.id}: issue ${t.issue_number} — ${t.issue_title}`}
                  >
                    <td>
                      <div className="issue">
                        <span className="num">#{t.issue_number}</span>
                        {t.issue_title}
                        {t.synthetic && (
                          <span className="synthetic-tag">SYNTHETIC</span>
                        )}
                      </div>
                    </td>
                    <td>{t.approval_actor ?? "—"}</td>
                    <td>
                      <StateChip state={t.execution} />
                    </td>
                    <td>
                      <StateChip state={t.validation} />
                    </td>
                    <td>
                      <StateChip state={t.review} />
                    </td>
                    <td>
                      <StateChip state={t.disposition} />
                    </td>
                    <td>{fmtAgo(t.created_at)}</td>
                    <td>{t.acu_used ?? "unknown"}</td>
                    <td onClick={(e) => e.stopPropagation()}>
                      {t.pr_url && <a href={t.pr_url}>PR</a>}{" "}
                      {t.devin_session_url && (
                        <a href={t.devin_session_url}>session</a>
                      )}
                      {t.slack_link && <a href={t.slack_link}>slack</a>}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </section>

        <section className="panel">
          <h2>Report snapshots</h2>
          {reports.length === 0 ? (
            <div className="empty">No facts snapshots published yet.</div>
          ) : (
            <table aria-label="report snapshots">
              <thead>
                <tr>
                  <th>Snapshot</th>
                  <th>sha256</th>
                  <th>Generated</th>
                  <th>Publications</th>
                  <th>Native evidence</th>
                </tr>
              </thead>
              <tbody>
                {reports.map((r) => (
                  <tr key={r.id}>
                    <td>
                      #{r.id} {r.title}
                    </td>
                    <td className="small">{r.sha256.slice(0, 16)}</td>
                    <td>{fmtTime(r.generated_at)}</td>
                    <td>
                      {r.publications.length === 0
                        ? "—"
                        : r.publications.map((p, i) => (
                            <div key={i} className="small">
                              {p.status} → {p.destination_repo}#
                              {p.destination_issue_number}
                            </div>
                          ))}
                    </td>
                    <td className="small">
                      {r.native_session_url && (
                        <a href={r.native_session_url}>
                          session ({r.native_state ?? "unknown"})
                        </a>
                      )}{" "}
                      {r.slack_link && <a href={r.slack_link}>slack</a>}
                      {!r.native_session_url && !r.slack_link && "—"}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </section>

        <section className="panel">
          <h2>Native report sessions</h2>
          <p className="small">
            Read-only observations of the external reporting automation —
            separate from managed repair attempts. A session&apos;s status
            says nothing about Slack delivery; only an operator-recorded
            link does.
          </p>
          {nativeSessions.length === 0 ? (
            <div className="empty">
              No native report sessions observed
              {overview?.native_observe_error
                ? ` — observation unavailable: ${overview.native_observe_error}`
                : overview?.last_native_observe_at == null
                  ? " yet."
                  : ` (last checked ${fmtAgo(overview.last_native_observe_at)}).`}
            </div>
          ) : (
            <table aria-label="native report sessions">
              <thead>
                <tr>
                  <th>Session</th>
                  <th>Status</th>
                  <th>Detail</th>
                  <th>ACUs</th>
                  <th>Slack</th>
                  <th>Last seen</th>
                </tr>
              </thead>
              <tbody>
                {nativeSessions.map((n) => (
                  <tr key={n.id}>
                    <td>
                      {n.url ? (
                        <a href={n.url}>{n.session_id}</a>
                      ) : (
                        n.session_id
                      )}
                    </td>
                    <td>
                      <StateChip state={n.status} />
                    </td>
                    <td className="small">{n.status_detail || "—"}</td>
                    <td>{n.acu_used ?? "unknown"}</td>
                    <td>
                      {n.slack_link ? (
                        <a href={n.slack_link}>slack</a>
                      ) : (
                        "—"
                      )}
                    </td>
                    <td>{fmtAgo(n.last_seen_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </section>
      </main>
      {selected && (
        <TaskDrawer task={selected} onClose={() => setSelected(null)} />
      )}
    </>
  );
}

function StateChip({ state }: { state: string }) {
  return (
    <span className={`chip ${stateChipClass(state)}`}>
      <span className="chip-dot" aria-hidden>
        {stateIcon(state)}
      </span>
      {state}
    </span>
  );
}

function Metric({
  label,
  value,
  sub,
}: {
  label: string;
  value: unknown;
  sub?: string;
}) {
  return (
    <div className="metric">
      <div className="label">{label}</div>
      <div className="value">{String(value ?? "—")}</div>
      {sub && <div className="sub">{sub}</div>}
    </div>
  );
}

function FilterBar({
  value,
  onChange,
  tasks,
}: {
  value: string;
  onChange: (v: string) => void;
  tasks: TaskSummary[];
}) {
  const states = Array.from(
    new Set(
      tasks.flatMap((t) => [
        t.execution,
        t.validation,
        t.review,
        t.disposition,
      ])
    )
  ).sort();
  // Keep the active selection visible even when no row currently matches —
  // otherwise the select renders "all" while the dead filter still applies.
  if (value && !states.includes(value)) states.push(value);
  return (
    <div className="filterbar">
      <label className="small">
        Filter by state:{" "}
        <select value={value} onChange={(e) => onChange(e.target.value)}>
          <option value="">all</option>
          {states.map((s) => (
            <option key={s} value={s}>
              {s === value && !tasks.some(
                (t) =>
                  t.execution === s ||
                  t.validation === s ||
                  t.review === s ||
                  t.disposition === s
              )
                ? `${s} (no matches)`
                : s}
            </option>
          ))}
        </select>
      </label>
      <span className="small hint">
        rows are keyboard focusable — Enter/Space opens the drawer
      </span>
    </div>
  );
}
