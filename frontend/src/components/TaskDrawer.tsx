import { useEffect } from "react";
import type { Evidence, TaskDetail } from "../types";
import { Icon } from "./Icons";
import {
  DIMENSION_HELP,
  describeTask,
  fmtAgo,
  fmtLocal,
  fmtNumber,
  fmtTime,
  humanize,
  stateChipClass,
  stateIcon,
} from "../util";

function Chip({ state }: { state: string }) {
  return (
    <span className={`chip ${stateChipClass(state)}`} title={state}>
      <span className="chip-dot" aria-hidden>
        {stateIcon(state)}
      </span>
      {humanize(state)}
    </span>
  );
}

function Row({ k, children }: { k: string; children: React.ReactNode }) {
  return (
    <>
      <dt>{k}</dt>
      <dd>{children}</dd>
    </>
  );
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="section">
      <h3>{title}</h3>
      {children}
    </div>
  );
}

function ExtLink({ href, icon, children }: { href: string; icon?: React.ReactNode; children: React.ReactNode }) {
  return (
    <a className="link-pill" href={href} target="_blank" rel="noreferrer" title={href}>
      {icon} {children} <Icon.External size={12} />
    </a>
  );
}

/** Group evidence by provenance: the agent's own claims stay visibly
 * separate from independently retrieved facts. */
function groupEvidence(evidence: Evidence[]) {
  const agent = evidence.filter((e) => e.verifier === "agent");
  const independent = evidence.filter((e) => e.verifier === "github-verifier");
  const manual = evidence.filter((e) => (e.verifier ?? "").startsWith("manual"));
  const other = evidence.filter(
    (e) =>
      e.verifier !== "agent" &&
      e.verifier !== "github-verifier" &&
      !(e.verifier ?? "").startsWith("manual")
  );
  return { agent, independent, manual, other };
}

function EvidenceCard({ e }: { e: Evidence }) {
  return (
    <div className={`evidence-item kind-${e.kind}`}>
      <span className="kind">
        {humanize(e.kind)}
        {e.kind === "manual_verification" && " · NOT CI verified"}
        {e.synthetic ? " · synthetic" : ""}
        {" · "}
        {fmtAgo(e.created_at)}
      </span>
      <div style={{ marginTop: 3 }}>
        {e.uri ? (
          <a href={e.uri} target="_blank" rel="noreferrer">{e.title}</a>
        ) : (
          e.title
        )}
      </div>
      {e.body != null && <pre>{JSON.stringify(e.body, null, 2)}</pre>}
    </div>
  );
}

/** Task detail drawer — the operator's evidence view for one repair. */
export default function TaskDrawer({
  task,
  onClose,
}: {
  task: TaskDetail;
  onClose: () => void;
}) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  const snapshot =
    task.issue_snapshot && typeof task.issue_snapshot === "object"
      ? (task.issue_snapshot as { body?: string })
      : null;

  // Session-reported questions: notes recorded while the session asked for
  // input — shown prominently, separate from verified facts. Excluded from
  // the provenance groups below so the question is rendered once.
  const questions = task.evidence.filter(
    (e) => e.kind === "note" && e.title.toLowerCase().includes("input")
  );
  const questionIds = new Set(questions.map((q) => q.id));
  const groups = groupEvidence(task.evidence.filter((e) => !questionIds.has(e.id)));
  const d = describeTask(task);
  const latest = task.attempts[task.attempts.length - 1];

  return (
    <>
      <div className="drawer-backdrop" onClick={onClose} />
      <aside className="drawer" role="dialog" aria-label={`Task ${task.id} detail`}>
        <button className="icon-btn close" onClick={onClose} aria-label="Close (Esc)">
          <Icon.Close size={16} />
        </button>

        <div className="drawer-title">
          <span className="eyebrow">
            task {task.id} · issue #{task.issue_number} · {task.repo}
            {task.synthetic && <> · <span className="synthetic-tag">SYNTHETIC</span></>}
          </span>
          <h2>{task.issue_title}</h2>
          <span className={`headline ${d.tone}`}>{d.headline}</span>
        </div>

        <div className="dims">
          {(["execution", "validation", "review", "disposition"] as const).map((dim) => (
            <div className="dim-card" key={dim} title={DIMENSION_HELP[dim]}>
              <div className="k">{dim}</div>
              <Chip state={task[dim]} />
            </div>
          ))}
        </div>
        <div className="card" style={{ display: "flex", justifyContent: "space-between", alignItems: "center", gap: 12 }}>
          <div>
            <div className="small" style={{ fontWeight: 600 }}>Session cleanup</div>
            <div className="small">
              {task.cleanup_state === "kept"
                ? "Session retained for the native conversation and verification update."
                : task.cleanup_state === "terminated"
                  ? "Session permanently terminated (archive=true)."
                  : "No cleanup decision recorded yet."}
            </div>
          </div>
          <Chip state={task.cleanup_state} />
        </div>

        <Section title="Links">
          <div className="links">
            <ExtLink href={task.issue_url} icon={<Icon.Doc size={13} />}>Issue #{task.issue_number}</ExtLink>
            {task.pr_url && <ExtLink href={task.pr_url} icon={<Icon.Branch size={13} />}>Pull request</ExtLink>}
            {task.devin_session_url && (
              <ExtLink href={task.devin_session_url} icon={<Icon.Session size={13} />}>Devin session</ExtLink>
            )}
            {task.slack_link ? (
              <ExtLink href={task.slack_link} icon={<Icon.Slack size={13} />}>Slack thread</ExtLink>
            ) : (
              <span className="small">No Slack thread recorded (use <code>cli slack-link</code>).</span>
            )}
          </div>
        </Section>

        <Section title="Revisions & usage">
          <div className="card">
            <dl>
              <Row k="Base SHA"><code className="sha">{task.base_sha ?? "—"}</code></Row>
              <Row k="Head SHA"><code className="sha">{task.head_sha ?? "—"}</code></Row>
              <Row k="Approved by">{task.approval_actor ?? "—"}</Row>
              <Row k="Accepted">
                {fmtLocal(task.created_at)} <span className="small">({fmtAgo(task.created_at)})</span>
              </Row>
              <Row k="ACUs">
                {task.acu_used != null ? fmtNumber(task.acu_used) : "unknown"} used
                {latest?.acu_limit ? ` · cap ${fmtNumber(latest.acu_limit)}` : ""}
              </Row>
              {task.last_error && (
                <Row k="Last error"><span style={{ color: "var(--bad)" }}>{task.last_error}</span></Row>
              )}
            </dl>
          </div>
        </Section>

        {questions.length > 0 && (
          <Section title="Questions for a human">
            {questions.map((q) => (
              <div className="evidence-item question" key={q.id}>
                <span className="kind">needs input · {fmtAgo(q.created_at)}</span>
                <div style={{ marginTop: 3 }}>{q.title}</div>
                {q.body != null && <pre>{JSON.stringify(q.body, null, 2)}</pre>}
              </div>
            ))}
          </Section>
        )}

        {snapshot?.body && (
          <Section title="Issue snapshot (frozen at intake)">
            <div className="card" style={{ whiteSpace: "pre-wrap", fontSize: 13 }}>{String(snapshot.body)}</div>
          </Section>
        )}

        {task.attempts.length > 0 && (
          <Section title={`Attempts (${task.attempts.length})`}>
            {task.attempts.map((a) => (
              <div className="evidence-item" key={a.id}>
                <span className="kind">
                  attempt {a.attempt_number} · started {fmtAgo(a.started_at)}
                  {a.finished_at ? ` · finished ${fmtAgo(a.finished_at)}` : ""}
                </span>
                <div style={{ marginTop: 4, display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
                  {a.session_url ? (
                    <a className="link-pill" href={a.session_url} target="_blank" rel="noreferrer">
                      <Icon.Session size={13} /> {a.session_id}
                    </a>
                  ) : (
                    <span className="mono">{a.session_id ?? "(no session)"}</span>
                  )}
                  {a.raw_status && <Chip state={a.raw_status} />}
                  {a.raw_detail && a.raw_detail !== a.raw_status && (
                    <span className="small">{a.raw_detail}</span>
                  )}
                  {a.acu_used != null && <span className="small">{fmtNumber(a.acu_used)} ACU</span>}
                </div>
                <div className="small context-versions" style={{ marginTop: 6 }}>
                  tag {a.correlation_tag}
                  {(a.prompt_hash || a.context_hash) && (
                    <> · prompt {a.prompt_hash?.slice(0, 10) ?? "—"} · ctx {a.context_hash?.slice(0, 10) ?? "—"}</>
                  )}
                </div>
              </div>
            ))}
          </Section>
        )}

        {task.evidence.length > 0 && (
          <Section title="Evidence">
            <p className="small" style={{ margin: "0 0 6px" }}>
              Independently retrieved facts are kept apart from what the agent reported about itself.
            </p>
            {groups.independent.length > 0 && (
              <>
                <h4 className="evidence-group">
                  Independently verified <span className="tag fact">facts</span>
                </h4>
                {groups.independent.map((e) => <EvidenceCard e={e} key={e.id} />)}
              </>
            )}
            {groups.manual.length > 0 && (
              <>
                <h4 className="evidence-group">
                  Operator records <span className="tag manual">not CI verified</span>
                </h4>
                {groups.manual.map((e) => <EvidenceCard e={e} key={e.id} />)}
              </>
            )}
            {groups.agent.length > 0 && (
              <>
                <h4 className="evidence-group">
                  Agent-reported <span className="tag claim">assertions, not facts</span>
                </h4>
                {groups.agent.map((e) => <EvidenceCard e={e} key={e.id} />)}
              </>
            )}
            {groups.other.length > 0 && (
              <>
                <h4 className="evidence-group">
                  Other <span className="tag other">system</span>
                </h4>
                {groups.other.map((e) => <EvidenceCard e={e} key={e.id} />)}
              </>
            )}
          </Section>
        )}

        <Section title={`Audit timeline (${task.audit.length})`}>
          <ul className="timeline">
            {[...task.audit].reverse().map((a) => (
              <li key={a.id}>
                <span className="t" title={fmtTime(a.created_at)}>{fmtLocal(a.created_at)}</span>{" "}
                <b>{humanize(a.action)}</b>
                {a.dimension && (
                  <span className="delta">
                    {" "}· {a.dimension}: {humanize(a.old_value)}
                    <span className="arrow">→</span>
                    {humanize(a.new_value)}
                  </span>
                )}
                {a.detail && <div className="small">{a.detail}</div>}
              </li>
            ))}
          </ul>
        </Section>
      </aside>
    </>
  );
}
