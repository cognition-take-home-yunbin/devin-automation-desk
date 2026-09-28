import { useCallback, useEffect, useMemo, useState } from "react";
import { api } from "./api";
import type {
  NativeSession,
  Overview,
  Report,
  TaskDetail,
  TaskSummary,
} from "./types";
import TaskDrawer from "./components/TaskDrawer";
import { Icon } from "./components/Icons";
import {
  DIMENSION_HELP,
  describeTask,
  fmtAgo,
  fmtLocal,
  fmtNumber,
  fmtTime,
  greeting,
  humanize,
  stateChipClass,
  stateIcon,
} from "./util";

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

// Mirrors _UNDELETABLE_EXECUTIONS on the API — a session/dispatch may
// still be live, so the button is disabled rather than guessing.
const UNDELETABLE = new Set([
  "queued",
  "dispatching",
  "creation_unknown",
  "working",
  "needs_input",
  "approval_required",
  "suspended",
  "stop_requested",
]);

const EXECUTION_ORDER = [
  "queued",
  "dispatching",
  "creation_unknown",
  "working",
  "needs_input",
  "approval_required",
  "suspended",
  "agent_finished",
  "stop_requested",
  "stopped",
  "failed",
];

type Quick = "all" | "active" | "attention" | "delivered" | "ended";
const QUICK_FILTERS: { id: Quick; label: string; match: (t: TaskSummary) => boolean }[] = [
  { id: "all", label: "All", match: () => true },
  { id: "active", label: "In progress", match: (t) => t.disposition === "active" },
  {
    id: "attention",
    label: "Needs attention",
    match: (t) =>
      t.execution === "needs_input" ||
      t.execution === "approval_required" ||
      t.disposition === "blocked",
  },
  { id: "delivered", label: "Delivered", match: (t) => t.disposition === "delivered" },
  {
    id: "ended",
    label: "Ended",
    match: (t) => ["failed", "cancelled", "deleted"].includes(t.disposition),
  },
];

const NAV = [
  { id: "overview", label: "Overview" },
  { id: "tasks", label: "Tasks" },
  { id: "reports", label: "Reports" },
  { id: "native", label: "Native sessions" },
];

export default function App() {
  const [overview, setOverview] = useState<Overview | null>(null);
  const [tasks, setTasks] = useState<TaskSummary[]>([]);
  const [reports, setReports] = useState<Report[]>([]);
  const [nativeSessions, setNativeSessions] = useState<NativeSession[]>([]);
  const [selected, setSelected] = useState<TaskDetail | null>(null);
  const [stale, setStale] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [toast, setToast] = useState<{ text: string; tone?: "bad" } | null>(null);
  const [filter, setFilter] = useState("");
  const [quick, setQuick] = useState<Quick>("all");
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [scanState, setScanState] = useState<"idle" | "sending" | "queued" | "pending">("idle");
  const [deletingId, setDeletingId] = useState<number | null>(null);
  const [launching, setLaunching] = useState<string | null>(null);
  const [activeNav, setActiveNav] = useState("overview");

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

  useEffect(() => {
    if (!toast) return;
    const t = setTimeout(() => setToast(null), 4200);
    return () => clearTimeout(t);
  }, [toast]);

  // Highlight the nav pill for the section currently in view.
  useEffect(() => {
    const els = NAV.map((n) => document.getElementById(n.id)).filter(Boolean) as HTMLElement[];
    if (els.length === 0 || typeof IntersectionObserver === "undefined") return;
    const io = new IntersectionObserver(
      (entries) => {
        const visible = entries
          .filter((e) => e.isIntersecting)
          .sort((a, b) => a.boundingClientRect.top - b.boundingClientRect.top);
        if (visible[0]) setActiveNav(visible[0].target.id);
      },
      { rootMargin: "-20% 0px -60% 0px" }
    );
    els.forEach((el) => io.observe(el));
    return () => io.disconnect();
  }, [overview?.mode]);

  const manualRefresh = async () => {
    setRefreshing(true);
    await refresh();
    setTimeout(() => setRefreshing(false), 500);
  };
  const openTask = async (id: number) => setSelected(await api.task(id));
  const runScenario = async (name: string) => {
    setLaunching(name);
    try {
      await api.startScenario(name);
      setToast({ text: `Scenario “${name}” seeded — a scan is queued.` });
    } catch (e) {
      setToast({ text: e instanceof Error ? e.message : String(e), tone: "bad" });
    } finally {
      setLaunching(null);
    }
    await refresh();
  };
  const deleteTask = async (id: number, label: string) => {
    if (!window.confirm(`Delete task ${label}? It leaves tracking permanently; the record stays for audit.`))
      return;
    setDeletingId(id);
    try {
      await api.deleteTask(id);
      if (selected?.id === id) setSelected(null);
      setToast({ text: `Task ${label} removed from tracking (record kept for audit).` });
    } catch (e) {
      setToast({ text: e instanceof Error ? e.message : String(e), tone: "bad" });
    } finally {
      setDeletingId(null);
    }
    await refresh();
  };
  const scanNow = async () => {
    if (scanState === "sending") return;
    setScanState("sending");
    try {
      const res = await api.scanNow();
      setScanState(res.queued ? "queued" : "pending");
      setToast({
        text: res.queued
          ? "Scan queued — approved issues will be picked up on the next worker tick."
          : "A scan is already pending; nothing new was queued.",
      });
      setTimeout(() => setScanState("idle"), 4000);
    } catch (e) {
      setToast({ text: e instanceof Error ? e.message : String(e), tone: "bad" });
      setScanState("idle");
    }
    await refresh();
  };

  const quickDef = QUICK_FILTERS.find((q) => q.id === quick) ?? QUICK_FILTERS[0];
  const filtered = tasks.filter(
    (t) =>
      quickDef.match(t) &&
      (!filter ||
        t.execution === filter ||
        t.validation === filter ||
        t.review === filter ||
        t.disposition === filter)
  );

  const m = overview?.metrics ?? {};
  const limits = overview?.limits ?? {};
  const sim = overview?.mode === "simulation";

  return (
    <>
      <div className="backdrop" aria-hidden />
      <div className={`shell ${stale ? "content-stale" : ""}`}>
        <header className="topbar">
          <div className="brand">
            <span className="mark">
              <Icon.Logo size={22} />
            </span>
            <div>
              <h1>Devin Repair Desk</h1>
              <span className="brand-sub">managed repair automation</span>
            </div>
          </div>

          <nav className="nav glass" aria-label="sections">
            {NAV.map((n) => (
              <a
                key={n.id}
                href={`#${n.id}`}
                className={activeNav === n.id ? "active" : ""}
                onClick={() => setActiveNav(n.id)}
              >
                {n.label}
              </a>
            ))}
          </nav>

          <div className="top-actions">
            {overview && (
              <span
                className={`mode-pill glass badge ${overview.mode}`}
                title={
                  sim
                    ? "Simulation: fake GitHub/Devin clients, synthetic data, no network calls."
                    : "Live: real GitHub and Devin API clients."
                }
              >
                <span className="dot" aria-hidden />
                {overview.mode.toUpperCase()}
              </span>
            )}
            <button
              className={`icon-btn ${refreshing ? "spin" : ""}`}
              onClick={manualRefresh}
              title="Refresh now (the desk also refreshes every 3 seconds)"
              aria-label="Refresh"
            >
              <Icon.Refresh size={17} />
            </button>
            <button
              className="primary scan-now"
              onClick={scanNow}
              disabled={scanState === "sending"}
              title="Enqueue a scan for approved issues right now (human override). Dispatch still respects pause, budgets, and approval checks."
            >
              <Icon.Radar size={16} />
              {scanState === "sending"
                ? "Queueing…"
                : scanState === "queued"
                  ? "Scan queued"
                  : scanState === "pending"
                    ? "Scan already pending"
                    : "Scan now"}
            </button>
          </div>
        </header>

        <section className="hero" id="overview">
          <div>
            <h2>{greeting()}, operator.</h2>
            <p>
              {overview ? (
                <>
                  Watching <span className="repo" title="Configured repository">{overview.repo}</span>
                  {" · "}
                  {tasks.length === 0
                    ? "no tracked tasks yet"
                    : `${tasks.length} tracked ${tasks.length === 1 ? "task" : "tasks"}`}
                  {" · "}scanning every {fmtNumber(limits.scan_interval_seconds)}s
                </>
              ) : (
                "Connecting to the desk…"
              )}
            </p>
          </div>
          <div className="status-strip" aria-live="polite">
            {overview?.paused && (
              <span className="badge paused" title="Dispatch of new sessions is paused by an operator. Polling, verification and reporting continue.">
                <Icon.Pause size={12} /> PAUSED
              </span>
            )}
            {overview && !overview.scan_fresh && (
              <span
                className="badge stale-scan"
                title="No successful scan within 3× the scan interval — the scanner may be down or throttled."
              >
                SCAN STALE
                {overview.scan_age_seconds != null &&
                  ` ${Math.round(overview.scan_age_seconds)}s`}
              </span>
            )}
            {overview && !overview.publish_fresh && (
              <span className="badge stale-scan" title="No confirmed report publication within the staleness window.">
                REPORT STALE
              </span>
            )}
            {overview?.native_observe_error && (
              <span className="badge stale-scan" title={overview.native_observe_error}>
                NATIVE OBS UNAVAILABLE
              </span>
            )}
            <span
              className={`refresh ${stale ? "stale" : ""}`}
              role={stale ? "alert" : undefined}
              title={stale ? error ?? undefined : "Data refreshes every 3 seconds"}
            >
              <span className="pulse" aria-hidden />
              {stale
                ? `Stale — last good data retained (${error})`
                : overview
                  ? `Live · refreshed ${fmtAgo(overview.generated_at)}`
                  : "connecting…"}
            </span>
          </div>
        </section>

        {loading && !overview && (
          <div className="glass empty" role="status">
            <span className="ico"><Icon.Refresh /></span>
            <b>Loading dashboard…</b>
          </div>
        )}
        {stale && !overview && (
          <div className="glass error" role="alert">
            Could not reach the API — {error ?? "unknown error"}
          </div>
        )}

        {overview && (
          <div className="grid">
            <div className="col-main">
              <section className="metrics" aria-label="metrics">
                <Metric
                  icon={<Icon.Bolt />}
                  label="Active tasks"
                  value={m.active}
                  sub={`${fmtNumber(m.tasks_total)} tracked in total`}
                  trend={
                    Number(m.blocked) > 0
                      ? { text: `${fmtNumber(m.blocked)} blocked`, tone: "bad" }
                      : undefined
                  }
                />
                <Metric
                  icon={<Icon.Alert />}
                  iconTone="warn"
                  label="Needs intervention"
                  value={m.needs_intervention}
                  sub="waiting for input, approval, or blocked"
                  trend={
                    Number(m.needs_intervention) === 0
                      ? { text: "all clear", tone: "ok" }
                      : { text: "action required", tone: "warn" }
                  }
                />
                <Metric
                  icon={<Icon.Check />}
                  iconTone="ok"
                  label="Verified PRs"
                  value={m.verified_prs}
                  sub={`${fmtNumber(m.manually_verified)} operator-verified · ${fmtNumber(m.merged_prs)} merged`}
                  trend={
                    Number(m.manually_verified) > 0
                      ? { text: "manual ≠ CI", tone: "warn" }
                      : undefined
                  }
                />
                <Metric
                  icon={<Icon.Gauge />}
                  label="Observed ACUs"
                  value={m.observed_acus}
                  unit="ACU"
                  sub={`${String(m.acus_scope ?? "")} · ${fmtNumber(m.sessions_seen)} sessions`}
                />
              </section>

              {sim && (
                <section className="glass panel" aria-label="simulation scenarios">
                  <div className="panel-head">
                    <div>
                      <h2>
                        <Icon.Flask size={16} style={{ verticalAlign: "-3px", marginRight: 6 }} />
                        Simulation lab
                      </h2>
                      <p>
                        Seed a deterministic scenario with synthetic data. Seeding is additive and
                        idempotent — re-running a scenario changes nothing.
                      </p>
                    </div>
                  </div>
                  <div className="panel-body padded">
                    <div className="scenarios">
                      {SCENARIOS.map((s) => (
                        <button
                          key={s}
                          onClick={() => runScenario(s)}
                          disabled={launching === s}
                          title={`Seed the “${s}” scenario`}
                        >
                          {launching === s ? "seeding…" : s}
                        </button>
                      ))}
                    </div>
                  </div>
                </section>
              )}

              <section className="glass panel" id="tasks">
                <div className="panel-head">
                  <div>
                    <h2>
                      Repair tasks
                      <span className="count">{filtered.length}{filtered.length !== tasks.length ? ` of ${tasks.length}` : ""}</span>
                    </h2>
                    <p>
                      One row per approved issue. Each task tracks four independent
                      dimensions — hover a column header to see what it means.
                    </p>
                  </div>
                </div>
                <FilterBar
                  quick={quick}
                  onQuick={setQuick}
                  value={filter}
                  onChange={setFilter}
                  tasks={tasks}
                />
                <div className="panel-body">
                  {filtered.length === 0 ? (
                    <div className="empty">
                      <span className="ico"><Icon.Inbox /></span>
                      {filter || quick !== "all" ? (
                        <>
                          <b>No tasks match this filter.</b>
                          <span>Try “All” or clear the state filter.</span>
                        </>
                      ) : sim ? (
                        <>
                          <b>No tasks yet.</b>
                          <span>Launch a scenario above to exercise the pipeline with synthetic data.</span>
                        </>
                      ) : (
                        <>
                          <b>No approved issues discovered yet.</b>
                          <span>Apply the approval label on the fork and the next scan will pick it up.</span>
                        </>
                      )}
                    </div>
                  ) : (
                    <div className="table-wrap">
                      <table aria-label="repair tasks">
                        <thead>
                          <tr>
                            <th>Issue</th>
                            <th title={DIMENSION_HELP.execution}>Execution</th>
                            <th title={DIMENSION_HELP.validation}>Validation</th>
                            <th title={DIMENSION_HELP.review}>Review</th>
                            <th title={DIMENSION_HELP.disposition}>Disposition</th>
                            <th title="Time since the task was accepted.">Age</th>
                            <th title="Latest cumulative ACU usage reported by the session.">ACUs</th>
                            <th>Links</th>
                          </tr>
                        </thead>
                        <tbody>
                          {filtered.map((t) => {
                            const d = describeTask(t);
                            return (
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
                                    <span className="title">
                                      <span className="num">#{t.issue_number}</span>
                                      {t.issue_title}
                                      {t.synthetic && <span className="synthetic-tag">SYNTHETIC</span>}
                                    </span>
                                    <span className={`headline ${d.tone}`}>{d.headline}</span>
                                    <span
                                      className="actor small"
                                      title="Who applied the approval label (must be an allowed approver)."
                                    >
                                      <span className="avatar" aria-hidden>
                                        {(t.approval_actor ?? "?").slice(0, 1).toUpperCase()}
                                      </span>
                                      {t.approval_actor ? (
                                        <>approved by <b>{t.approval_actor}</b></>
                                      ) : (
                                        "approver unknown"
                                      )}
                                    </span>
                                  </div>
                                </td>
                                <td><StateChip state={t.execution} /></td>
                                <td><StateChip state={t.validation} /></td>
                                <td><StateChip state={t.review} /></td>
                                <td><StateChip state={t.disposition} /></td>
                                <td className="num-cell" title={fmtLocal(t.created_at)}>{fmtAgo(t.created_at)}</td>
                                <td className="num-cell">
                                  {t.acu_used != null ? fmtNumber(t.acu_used) : <span className="dim-text">unknown</span>}
                                </td>
                                <td onClick={(e) => e.stopPropagation()}>
                                  <div className="links">
                                    {t.pr_url && (
                                      <a className="link-pill" href={t.pr_url} target="_blank" rel="noreferrer">
                                        <Icon.Branch size={13} /> PR
                                      </a>
                                    )}
                                    {t.devin_session_url && (
                                      <a className="link-pill" href={t.devin_session_url} target="_blank" rel="noreferrer">
                                        <Icon.Session size={13} /> session
                                      </a>
                                    )}
                                    {t.slack_link && (
                                      <a className="link-pill" href={t.slack_link} target="_blank" rel="noreferrer">
                                        <Icon.Slack size={13} /> slack
                                      </a>
                                    )}
                                    <button
                                      className="delete-task"
                                      disabled={deletingId === t.id || UNDELETABLE.has(t.execution)}
                                      aria-label={`Delete task #${t.issue_number}`}
                                      title={
                                        UNDELETABLE.has(t.execution)
                                          ? "Stop the task (cli stop) before deleting — a session may still be live"
                                          : "Remove this task from tracking permanently (record kept for audit)"
                                      }
                                      onClick={() => deleteTask(t.id, `#${t.issue_number}`)}
                                    >
                                      <Icon.Trash size={15} />
                                    </button>
                                  </div>
                                </td>
                              </tr>
                            );
                          })}
                        </tbody>
                      </table>
                    </div>
                  )}
                </div>
              </section>

              <section className="glass panel" id="reports">
                <div className="panel-head">
                  <div>
                    <h2>
                      Report snapshots
                      <span className="count">{reports.length}</span>
                    </h2>
                    <p>
                      Sanitized facts published to the fixed report-source issue. The hash covers
                      content only, so identical data is never re-published.
                    </p>
                  </div>
                </div>
                <div className="panel-body">
                  {reports.length === 0 ? (
                    <div className="empty">
                      <span className="ico"><Icon.Doc /></span>
                      <b>No facts snapshots published yet.</b>
                      <span>A snapshot is generated after the first verified delivery or on the publish interval.</span>
                    </div>
                  ) : (
                    <div className="table-wrap">
                      <table aria-label="report snapshots">
                        <thead>
                          <tr>
                            <th>Snapshot</th>
                            <th title="Content hash, excluding generation time.">sha256</th>
                            <th>Generated</th>
                            <th title="Where and whether the snapshot landed.">Publications</th>
                            <th title="Read-only observation of the external reporting automation. A session link never implies Slack delivery.">Native evidence</th>
                          </tr>
                        </thead>
                        <tbody>
                          {reports.map((r) => (
                            <tr key={r.id} className="static" tabIndex={-1}>
                              <td>
                                <div className="stack">
                                  <span style={{ fontWeight: 600 }}>{r.title}</span>
                                  <span className="small">
                                    #{r.id} · {humanize(r.report_type)} · schema v{r.schema_version}
                                  </span>
                                </div>
                              </td>
                              <td><span className="mono" title={r.sha256}>{r.sha256.slice(0, 12)}…</span></td>
                              <td title={fmtTime(r.generated_at)}>
                                <div className="stack">
                                  <span>{fmtAgo(r.generated_at)}</span>
                                  <span className="small">{fmtLocal(r.generated_at)}</span>
                                </div>
                              </td>
                              <td>
                                {r.publications.length === 0 ? (
                                  <span className="dim-text">not published</span>
                                ) : (
                                  <div className="stack">
                                    {r.publications.map((p, i) => (
                                      <span key={i} className="links">
                                        <StateChip state={p.status} />
                                        <span className="small mono">
                                          {p.destination_repo}#{p.destination_issue_number}
                                        </span>
                                      </span>
                                    ))}
                                  </div>
                                )}
                              </td>
                              <td>
                                <div className="links">
                                  {r.native_session_url && (
                                    <a className="link-pill" href={r.native_session_url} target="_blank" rel="noreferrer">
                                      <Icon.Session size={13} /> session · {humanize(r.native_state ?? "unknown")}
                                    </a>
                                  )}
                                  {r.slack_link && (
                                    <a className="link-pill" href={r.slack_link} target="_blank" rel="noreferrer">
                                      <Icon.Slack size={13} /> slack
                                    </a>
                                  )}
                                  {!r.native_session_url && !r.slack_link && <span className="dim-text">—</span>}
                                </div>
                              </td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                  )}
                </div>
              </section>

              <section className="glass panel" id="native">
                <div className="panel-head">
                  <div>
                    <h2>
                      Native report sessions
                      <span className="count">{nativeSessions.length}</span>
                    </h2>
                    <p>
                      Read-only observations of the external reporting automation, separate from
                      managed repairs. A session&apos;s status says nothing about Slack delivery —
                      only an operator-recorded link does.
                    </p>
                  </div>
                  {overview.last_native_observe_at != null && (
                    <span className="small">last checked {fmtAgo(overview.last_native_observe_at)}</span>
                  )}
                </div>
                <div className="panel-body">
                  {nativeSessions.length === 0 ? (
                    <div className="empty">
                      <span className="ico"><Icon.Radar /></span>
                      <b>No native report sessions observed{overview.last_native_observe_at == null && !overview.native_observe_error ? " yet" : ""}.</b>
                      {overview.native_observe_error ? (
                        <span>Observation unavailable: {overview.native_observe_error}</span>
                      ) : overview.last_native_observe_at != null ? (
                        <span>Last checked {fmtAgo(overview.last_native_observe_at)}.</span>
                      ) : (
                        <span>The desk lists sessions tagged for the reporting automation on the scan cadence.</span>
                      )}
                    </div>
                  ) : (
                    <div className="table-wrap">
                      <table aria-label="native report sessions">
                        <thead>
                          <tr>
                            <th>Session</th>
                            <th>Status</th>
                            <th>Detail</th>
                            <th>ACUs</th>
                            <th title="Only shown when an operator recorded the permalink.">Slack</th>
                            <th>Last seen</th>
                          </tr>
                        </thead>
                        <tbody>
                          {nativeSessions.map((n) => (
                            <tr key={n.id} className="static" tabIndex={-1}>
                              <td>
                                {n.url ? (
                                  <a className="link-pill" href={n.url} target="_blank" rel="noreferrer">
                                    <Icon.Session size={13} /> {n.session_id}
                                  </a>
                                ) : (
                                  <span className="mono">{n.session_id}</span>
                                )}
                              </td>
                              <td><StateChip state={n.status} /></td>
                              <td className="small">{n.status_detail || "—"}</td>
                              <td className="num-cell">
                                {n.acu_used != null ? fmtNumber(n.acu_used) : <span className="dim-text">unknown</span>}
                              </td>
                              <td>
                                {n.slack_link ? (
                                  <a className="link-pill" href={n.slack_link} target="_blank" rel="noreferrer">
                                    <Icon.Slack size={13} /> slack
                                  </a>
                                ) : (
                                  <span className="dim-text">not recorded</span>
                                )}
                              </td>
                              <td title={fmtLocal(n.last_seen_at)}>{fmtAgo(n.last_seen_at)}</td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                  )}
                </div>
              </section>
            </div>

            <aside className="col-side" aria-label="desk status">
              <HealthCard overview={overview} />
              <BudgetCard overview={overview} />
              <PipelineCard tasks={tasks} />
            </aside>
          </div>
        )}
      </div>

      {selected && <TaskDrawer task={selected} onClose={() => setSelected(null)} />}
      {toast && (
        <div className={`toast glass ${toast.tone ?? ""}`} role="status">
          {toast.text}
        </div>
      )}
    </>
  );
}

/* ---------------------------------------------------------------------- */

export function StateChip({ state }: { state: string }) {
  return (
    <span className={`chip ${stateChipClass(state)}`} title={state}>
      <span className="chip-dot" aria-hidden>
        {stateIcon(state)}
      </span>
      {humanize(state)}
    </span>
  );
}

function Metric({
  icon,
  iconTone,
  label,
  value,
  unit,
  sub,
  trend,
}: {
  icon: React.ReactNode;
  iconTone?: "ok" | "warn";
  label: string;
  value: unknown;
  unit?: string;
  sub?: string;
  trend?: { text: string; tone: "ok" | "bad" | "warn" | "dim" };
}) {
  return (
    <div className="glass metric">
      <span className={`ico ${iconTone ?? ""}`}>{icon}</span>
      {trend && <span className={`trend ${trend.tone}`}>{trend.text}</span>}
      <div>
        <div className="value">
          {fmtNumber(value)}
          {unit && value != null && <small>{unit}</small>}
        </div>
        <div className="label">{label}</div>
        {sub && <div className="sub">{sub}</div>}
      </div>
    </div>
  );
}

function HealthCard({ overview }: { overview: Overview }) {
  const l = overview.limits;
  const signals: { k: string; sub: string; v: string; tone: "ok" | "warn" | "bad" | "dim" }[] = [
    {
      k: "Scanner",
      sub: `every ${fmtNumber(l.scan_interval_seconds)}s`,
      v: overview.last_scan_at ? fmtAgo(overview.last_scan_at) : "not yet run",
      tone: overview.last_scan_at == null ? "dim" : overview.scan_fresh ? "ok" : "bad",
    },
    {
      k: "Dispatch",
      sub: overview.paused ? "paused by operator" : `up to ${fmtNumber(l.max_active_sessions)} concurrent`,
      v: overview.paused ? "paused" : "accepting",
      tone: overview.paused ? "warn" : "ok",
    },
    {
      k: "Report publication",
      sub: "fixed report-source issue",
      v: overview.last_publish_at ? fmtAgo(overview.last_publish_at) : "none yet",
      tone: overview.last_publish_at == null ? "dim" : overview.publish_fresh ? "ok" : "warn",
    },
    {
      k: "Native observation",
      sub: "reporting automation sessions",
      v: overview.native_observe_error
        ? "unavailable"
        : overview.last_native_observe_at
          ? fmtAgo(overview.last_native_observe_at)
          : "not yet run",
      tone: overview.native_observe_error ? "bad" : overview.last_native_observe_at ? "ok" : "dim",
    },
  ];
  const healthy = signals.filter((s) => s.tone === "ok").length;
  const pct = Math.round((healthy / signals.length) * 100);
  return (
    <section className="glass side-card" aria-label="desk health">
      <div className="side-head">
        <div>
          <h3>Desk health</h3>
          <div className="sub">
            {healthy === signals.length
              ? "All systems reporting"
              : `${healthy} of ${signals.length} signals healthy`}
          </div>
        </div>
        <span className="corner" aria-hidden><Icon.Sparkle size={15} /></span>
      </div>
      <div className="progress" role="progressbar" aria-valuenow={pct} aria-valuemin={0} aria-valuemax={100}>
        <span style={{ width: `${pct}%` }} />
      </div>
      <div className="progress-label">
        <span>Freshness</span>
        <span>{pct}%</span>
      </div>
      <ul className="health-list">
        {signals.map((s) => (
          <li key={s.k}>
            <span className={`dot ${s.tone}`} aria-hidden />
            <span className="k">
              {s.k}
              <small>{s.sub}</small>
            </span>
            <span className="v">{s.v}</span>
          </li>
        ))}
      </ul>
    </section>
  );
}

function BudgetCard({ overview }: { overview: Overview }) {
  const m = overview.metrics;
  const l = overview.limits;
  const dailyLimit = Number(l.daily_admission_acu_limit) || 0;
  const dailyLeft = Number(m.daily_admission_remaining);
  const ratio = dailyLimit > 0 ? Math.max(0, Math.min(1, dailyLeft / dailyLimit)) : 0;
  const pct = Math.round(ratio * 100);
  const tone = ratio > 0.5 ? "ok" : ratio > 0.2 ? "warn" : "bad";

  // Semi-circular gauge, drawn as 24 ticks like a speedometer.
  const ticks = 24;
  const lit = Math.round(ratio * ticks);
  const cx = 110, cy = 100, rOuter = 92, rInner = 70;
  return (
    <section className="glass side-card" aria-label="budget">
      <div className="side-head">
        <div>
          <h3>Daily admission budget</h3>
          <div className="sub">ACU remaining for new dispatches today</div>
        </div>
        <span className="corner" aria-hidden><Icon.Wallet size={15} /></span>
      </div>
      <div className="gauge-wrap">
        <svg width="220" height="120" viewBox="0 0 220 120" role="img" aria-label={`${pct}% of daily admission budget remaining`}>
          {Array.from({ length: ticks }).map((_, i) => {
            const a = Math.PI - (i / (ticks - 1)) * Math.PI;
            const x1 = cx + rInner * Math.cos(a), y1 = cy - rInner * Math.sin(a);
            const x2 = cx + rOuter * Math.cos(a), y2 = cy - rOuter * Math.sin(a);
            const on = i < lit;
            return (
              <line
                key={i}
                x1={x1} y1={y1} x2={x2} y2={y2}
                stroke={on ? (tone === "ok" ? "#7d6cf2" : tone === "warn" ? "#efab3c" : "#e05252") : "rgba(125,108,242,0.16)"}
                strokeWidth={7}
                strokeLinecap="round"
                style={{ filter: on ? "drop-shadow(0 2px 4px rgba(125,108,242,0.25))" : undefined }}
              />
            );
          })}
        </svg>
        <div className="gauge-center">
          <div className="big">{pct}%</div>
          <div className="small">
            {fmtNumber(dailyLeft)} of {fmtNumber(dailyLimit)} ACU left
          </div>
        </div>
      </div>
      <div className="mini-stats">
        <div className="mini-stat">
          <div className="k">ACU held</div>
          <div className="v">{fmtNumber(m.budget_held_acu)}<small>reserved</small></div>
        </div>
        <div className="mini-stat">
          <div className="k">Project left</div>
          <div className="v">{fmtNumber(m.project_admission_remaining)}<small>/ {fmtNumber(l.project_admission_acu_limit)}</small></div>
        </div>
      </div>
      <p className="small" style={{ margin: "12px 0 0" }}>
        Each repair reserves up to {fmtNumber(l.repair_acu_limit)} ACU before the session is
        created. Budgets govern managed dispatch only — native Slack conversations are not metered here.
      </p>
    </section>
  );
}

function PipelineCard({ tasks }: { tasks: TaskSummary[] }) {
  const counts = useMemo(() => {
    const c = new Map<string, number>();
    for (const t of tasks) {
      if (t.disposition === "deleted") continue;
      c.set(t.execution, (c.get(t.execution) ?? 0) + 1);
    }
    return EXECUTION_ORDER.filter((s) => c.has(s)).map((s) => ({ state: s, n: c.get(s)! }));
  }, [tasks]);
  const max = Math.max(1, ...counts.map((c) => c.n));
  const live = tasks.filter((t) => t.disposition !== "deleted").length;
  return (
    <section className="glass side-card" aria-label="pipeline distribution">
      <div className="side-head">
        <div>
          <h3>Where tasks are</h3>
          <div className="sub">Execution state of {live} tracked {live === 1 ? "task" : "tasks"}</div>
        </div>
        <span className="corner" aria-hidden><Icon.Bolt size={15} /></span>
      </div>
      {counts.length === 0 ? (
        <p className="small" style={{ margin: "14px 0 0" }}>
          Nothing in flight. Distribution appears here as soon as a task is accepted.
        </p>
      ) : (
        <div className="bars" role="img" aria-label={counts.map((c) => `${humanize(c.state)}: ${c.n}`).join(", ")}>
          {counts.map((c) => {
            const tone = stateChipClass(c.state);
            const cls = tone === "ok" ? "hot" : tone === "bad" ? "bad" : tone === "warn" ? "warn" : "";
            return (
              <div className="bar" key={c.state} title={`${humanize(c.state)}: ${c.n}`}>
                <span className={cls} style={{ height: `${Math.max(8, (c.n / max) * 100)}%` }}>
                  <span className="n">{c.n}</span>
                </span>
                <label>{humanize(c.state)}</label>
              </div>
            );
          })}
        </div>
      )}
    </section>
  );
}

function FilterBar({
  quick,
  onQuick,
  value,
  onChange,
  tasks,
}: {
  quick: Quick;
  onQuick: (q: Quick) => void;
  value: string;
  onChange: (v: string) => void;
  tasks: TaskSummary[];
}) {
  const states = Array.from(
    new Set(tasks.flatMap((t) => [t.execution, t.validation, t.review, t.disposition]))
  ).sort();
  // Keep the active selection visible even when no row currently matches —
  // otherwise the select renders "all" while the dead filter still applies.
  if (value && !states.includes(value)) states.push(value);
  const matches = (s: string) =>
    tasks.some(
      (t) => t.execution === s || t.validation === s || t.review === s || t.disposition === s
    );
  return (
    <div className="filterbar">
      <div className="segmented" role="tablist" aria-label="quick filters">
        {QUICK_FILTERS.map((q) => {
          const n = tasks.filter(q.match).length;
          return (
            <button
              key={q.id}
              role="tab"
              aria-selected={quick === q.id}
              className={quick === q.id ? "active" : ""}
              onClick={() => onQuick(q.id)}
            >
              {q.label}
              <span className="n">{n}</span>
            </button>
          );
        })}
      </div>
      <label className="small">
        <span className="sr-only">Filter by exact state</span>
        <select value={value} onChange={(e) => onChange(e.target.value)} aria-label="Filter by state">
          <option value="">Any state</option>
          {states.map((s) => (
            <option key={s} value={s}>
              {humanize(s)}
              {s === value && !matches(s) ? " (no matches)" : ""}
            </option>
          ))}
        </select>
      </label>
      <span className="hint">Rows are keyboard focusable — Enter or Space opens details</span>
    </div>
  );
}
