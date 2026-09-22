import { useEffect } from "react";
import type { TaskDetail } from "../types";
import { fmtTime, stateChipClass } from "../util";

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

  return (
    <>
      <div className="drawer-backdrop" onClick={onClose} />
      <aside
        className="drawer"
        role="dialog"
        aria-label={`Task ${task.id} detail`}
      >
        <button className="close" onClick={onClose} aria-label="Close">
          ✕
        </button>
        <h2>
          #{task.issue_number} {task.issue_title}
          {task.synthetic && <span className="synthetic-tag">SYNTHETIC</span>}
        </h2>
        <dl>
          <dt>Execution</dt>
          <dd>
            <span className={`chip ${stateChipClass(task.execution)}`}>
              {task.execution}
            </span>
          </dd>
          <dt>Validation</dt>
          <dd>
            <span className={`chip ${stateChipClass(task.validation)}`}>
              {task.validation}
            </span>
          </dd>
          <dt>Review</dt>
          <dd>
            <span className={`chip ${stateChipClass(task.review)}`}>
              {task.review}
            </span>
          </dd>
          <dt>Disposition</dt>
          <dd>
            <span className={`chip ${stateChipClass(task.disposition)}`}>
              {task.disposition}
            </span>
          </dd>
          <dt>Issue</dt>
          <dd>
            <a href={task.issue_url}>{task.issue_url}</a>
          </dd>
          <dt>PR</dt>
          <dd>{task.pr_url ? <a href={task.pr_url}>{task.pr_url}</a> : "—"}</dd>
          <dt>Session</dt>
          <dd>
            {task.devin_session_url ? (
              <a href={task.devin_session_url}>{task.devin_session_url}</a>
            ) : (
              "—"
            )}
          </dd>
          <dt>Cleanup</dt>
          <dd>
            <span className={`chip ${stateChipClass(task.cleanup_state)}`}>
              {task.cleanup_state}
            </span>
          </dd>
          <dt>Slack</dt>
          <dd>
            {task.slack_link ? (
              <a href={task.slack_link}>{task.slack_link}</a>
            ) : (
              "—"
            )}
          </dd>
          <dt>Head SHA</dt>
          <dd className="small">{task.head_sha ?? "—"}</dd>
          <dt>Approved by</dt>
          <dd>{task.approval_actor ?? "—"}</dd>
          <dt>ACUs</dt>
          <dd>
            {task.acu_used ?? "unknown"} used
            {task.attempts[0]?.acu_limit
              ? ` / limit ${task.attempts[0].acu_limit}`
              : ""}
          </dd>
          {task.last_error && (
            <>
              <dt>Last error</dt>
              <dd>{task.last_error}</dd>
            </>
          )}
        </dl>

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
                  {a.session_id ?? "(no session)"} — {a.raw_status ?? "…"}
                  {a.acu_used != null && ` · ${a.acu_used} ACU`}
                </div>
                <div className="small">
                  tag {a.correlation_tag} · started {fmtTime(a.started_at)}
                </div>
              </div>
            ))}
          </>
        )}

        {task.evidence.length > 0 && (
          <>
            <h3 className="small">Evidence</h3>
            {task.evidence.map((e) => (
              <div className="evidence-item" key={e.id}>
                <span className="kind">
                  {e.kind}
                  {e.synthetic ? " · synthetic" : ""}
                </span>
                <div>
                  {e.uri ? <a href={e.uri}>{e.title}</a> : e.title}
                </div>
                {e.body != null && (
                  <pre>{JSON.stringify(e.body, null, 2)}</pre>
                )}
              </div>
            ))}
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
