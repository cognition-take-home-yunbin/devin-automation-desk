import { useCallback, useEffect, useState } from "react";
import { api } from "./api";
import type { Overview, Report, TaskDetail, TaskSummary } from "./types";
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
];

export default function App() {
  const [overview, setOverview] = useState<Overview | null>(null);
  const [tasks, setTasks] = useState<TaskSummary[]>([]);
  const [reports, setReports] = useState<Report[]>([]);
  const [selected, setSelected] = useState<TaskDetail | null>(null);
  const [stale, setStale] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [filter, setFilter] = useState("");
  const [loading, setLoading] = useState(true);

  const refresh = useCallback(async () => {
    try {
      const [o, t, r] = await Promise.all([
        api.overview(),
        api.tasks(),
        api.reports(),
      ]);
      setOverview(o);
      setTasks(t);
      setReports(r);
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
        <span className="repo" title="Configured repository">
          {overview?.repo ?? "…"}
        </span>
        <span className="spacer" />
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
              {overview?.mode === "simulation"
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
  return (
    <div className="filterbar">
      <label className="small">
        Filter by state:{" "}
        <select value={value} onChange={(e) => onChange(e.target.value)}>
          <option value="">all</option>
          {states.map((s) => (
            <option key={s} value={s}>
              {s}
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
