import { useEffect } from "react";
import type { Evidence, TaskDetail } from "../types";
import { fmtTime, stateChipClass, stateIcon } from "../util";

function Chip({ state }: { state: string }) {
  return (
    <span className={`chip ${stateChipClass(state)}`}>
      <span className="chip-dot" aria-hidden>
        {stateIcon(state)}
      </span>
      {state}
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
        {e.kind}
        {e.kind === "manual_verification" && " · NOT CI verified"}
        {e.synthetic ? " · synthetic" : ""}
      </span>
      <div>{e.uri ? <a href={e.uri}>{e.title}</a> : e.title}</div>
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
  const groups = groupEvidence(
    task.evidence.filter((e) => !questionIds.has(e.id))
  );

  return (
    <>
      <div className="drawer-backdrop" onClick={onClose} />
      <aside
        className="drawer"
        role="dialog"
        aria-label={`Task ${task.id} detail`}
      >
        <button className="close" onClick={onClose} aria-label="Close (Esc)">
          ✕
        </button>
        <h2>
          #{task.issue_number} {task.issue_title}
          {task.synthetic && <span className="synthetic-tag">SYNTHETIC</span>}
        </h2>

        <dl>
          <Row k="Execution"><Chip state={task.execution} /></Row>
          <Row k="Validation"><Chip state={task.validation} /></Row>
          <Row k="Review"><Chip state={task.review} /></Row>
          <Row k="Disposition"><Chip state={task.disposition} /></Row>
          <Row k="Cleanup"><Chip state={task.cleanup_state} /></Row>
        </dl>

        <h3 className="small">Links</h3>
        <dl>
          <Row k="Issue"><a href={task.issue_url}>{task.issue_url}</a></Row>
          <Row k="PR">
            {task.pr_url ? <a href={task.pr_url}>{task.pr_url}</a> : "—"}
          </Row>
          <Row k="Session">
            {task.devin_session_url ? (
              <a href={task.devin_session_url}>{task.devin_session_url}</a>
            ) : (
              "—"
            )}
          </Row>
          <Row k="Slack">
            {task.slack_link ? (
              <a href={task.slack_link}>{task.slack_link}</a>
            ) : (
              "—"
            )}
          </Row>
        </dl>

        <h3 className="small">Revisions</h3>
        <dl>
          <Row k="Base SHA">
            <code className="sha">{task.base_sha ?? "—"}</code>
          </Row>
          <Row k="Head SHA">
            <code className="sha">{task.head_sha ?? "—"}</code>
          </Row>
          <Row k="Approved by">{task.approval_actor ?? "—"}</Row>
          <Row k="ACUs">
            {task.acu_used ?? "unknown"} used
            {task.attempts[0]?.acu_limit
              ? ` / limit ${task.attempts[0].acu_limit}`
              : ""}
          </Row>
          {task.last_error && <Row k="Last error">{task.last_error}</Row>}
        </dl>

        {questions.length > 0 && (
          <>
            <h3 className="small">Questions for a human</h3>
            {questions.map((q) => (
              <div className="evidence-item question" key={q.id}>
                <span className="kind">needs input</span>
                <div>{q.title}</div>
                {q.body != null && (
                  <pre>{JSON.stringify(q.body, null, 2)}</pre>
                )}
              </div>
            ))}
          </>
        )}

        {snapshot?.body && (
          <>
            <h3 className="small">Issue snapshot (frozen at intake)</h3>
            <div className="evidence-item">{String(snapshot.body)}</div>
          </>
        )}

        {task.attempts.length > 0 && (
          <>
            <h3 className="small">Attempts</h3>
            {task.attempts.map((a) => (
              <div className="evidence-item" key={a.id}>
                <span className="kind">attempt {a.attempt_number}</span>
                <div>
                  {a.session_url ? (
                    <a href={a.session_url}>{a.session_id}</a>
                  ) : (
                    (a.session_id ?? "(no session)")
                  )}{" "}
                  — {a.raw_status ?? "…"}
                  {a.raw_detail && a.raw_detail !== a.raw_status
                    ? ` · ${a.raw_detail}`
                    : ""}
                  {a.acu_used != null && ` · ${a.acu_used} ACU`}
                </div>
                <div className="small">
                  tag {a.correlation_tag} · started {fmtTime(a.started_at)}
                </div>
                {(a.prompt_hash || a.context_hash) && (
                  <div className="small context-versions">
                    context prompt:{a.prompt_hash?.slice(0, 10) ?? "—"} ctx:
                    {a.context_hash?.slice(0, 10) ?? "—"}
                  </div>
                )}
              </div>
            ))}
          </>
        )}

        {task.evidence.length > 0 && (
          <>
            <h3 className="small">Evidence</h3>
            {groups.independent.length > 0 && (
              <>
                <h4 className="evidence-group">Independently verified</h4>
                {groups.independent.map((e) => (
                  <EvidenceCard e={e} key={e.id} />
                ))}
              </>
            )}
            {groups.manual.length > 0 && (
              <>
                <h4 className="evidence-group">
                  Operator records (not CI verified)
                </h4>
                {groups.manual.map((e) => (
                  <EvidenceCard e={e} key={e.id} />
                ))}
              </>
            )}
            {groups.agent.length > 0 && (
              <>
                <h4 className="evidence-group">
                  Agent-reported (assertions, not facts)
                </h4>
                {groups.agent.map((e) => (
                  <EvidenceCard e={e} key={e.id} />
                ))}
              </>
            )}
            {groups.other.length > 0 && (
              <>
                <h4 className="evidence-group">Other</h4>
                {groups.other.map((e) => (
                  <EvidenceCard e={e} key={e.id} />
                ))}
              </>
            )}
          </>
        )}

        <h3 className="small">Audit timeline</h3>
        <ul className="timeline">
          {task.audit.map((a) => (
            <li key={a.id}>
              <span className="t">{fmtTime(a.created_at)}</span>{" "}
              <b>{a.action}</b>
              {a.dimension && (
                <>
                  {" "}
                  {a.dimension}: {a.old_value} → {a.new_value}
                </>
              )}
              {a.detail && <div className="small">{a.detail}</div>}
            </li>
          ))}
        </ul>
      </aside>
    </>
  );
}
