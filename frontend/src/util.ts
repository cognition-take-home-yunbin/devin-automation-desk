export function fmtAgo(ts: number | null | undefined): string {
  if (!ts) return "—";
  const s = Math.max(0, Date.now() / 1000 - ts);
  if (s < 5) return "just now";
  if (s < 60) return `${Math.floor(s)}s ago`;
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
}

export function fmtTime(ts: number | null | undefined): string {
  if (!ts) return "—";
  return new Date(ts * 1000).toISOString().replace("T", " ").slice(0, 19) + "Z";
}

export function stateChipClass(state: string): string {
  if (["verified", "published", "delivered", "merged", "approved", "agent_finished"].includes(state))
    return "ok";
  if (
    ["checks_failed", "failed", "dead", "blocked", "creation_unknown", "unknown", "closed_unmerged"].includes(state)
  )
    return "bad";
  if (
    ["needs_input", "approval_required", "suspended", "stale", "checks_pending", "dispatching", "stop_requested"].includes(state)
  )
    return "warn";
  if (["queued", "no_pr", "none"].includes(state)) return "dim";
  return "";
}
