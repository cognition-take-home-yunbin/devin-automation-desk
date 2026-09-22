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
  // Operator-recorded verification is a positive state but weaker than CI —
  // it gets its own color so it never reads as "CI verified".
  if (state === "manually_verified") return "manual";
  if (["queued", "no_pr", "none", "pending", "kept"].includes(state)) return "dim";
  return "";
}

/** A short status glyph shown before the chip label. */
export function stateIcon(state: string): string {
  const cls = stateChipClass(state);
  if (cls === "ok") return "\u25CF";        // ●
  if (cls === "bad") return "\u25CF";       // ●
  if (cls === "warn") return "\u25CF";      // ●
  if (cls === "manual") return "\u25CB";    // ○ hollow — weaker than CI
  return "\u00B7";                          // ·
}
