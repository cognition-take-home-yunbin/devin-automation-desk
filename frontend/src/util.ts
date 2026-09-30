import type { TaskSummary } from "./types";

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

/** Local, human-friendly timestamp for tooltips ("Sep 28, 10:04"). */
export function fmtLocal(ts: number | null | undefined): string {
  if (!ts) return "—";
  return new Date(ts * 1000).toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

export function fmtNumber(v: unknown): string {
  if (v == null || v === "") return "—";
  if (typeof v === "number") {
    if (Number.isInteger(v)) return v.toLocaleString();
    return v.toLocaleString(undefined, { maximumFractionDigits: 2 });
  }
  return String(v);
}

export function fmtACU(acu: number | null | undefined): string {
  if (acu == null) return "unknown";
  return fmtNumber(acu);
}

export function fmtACUWithUSD(acu: number | null | undefined): string {
  if (acu == null) return "unknown";
  const usd = acu * 2;
  return `${fmtNumber(acu)} ACU ($${fmtNumber(usd)})`;
}

/** `agent_finished` → `Agent finished`. Raw value is kept in tooltips. */
export function humanize(state: string | null | undefined): string {
  if (!state) return "—";
  const s = state.replace(/_/g, " ");
  return s.charAt(0).toUpperCase() + s.slice(1);
}

export function greeting(d = new Date()): string {
  const h = d.getHours();
  if (h < 5) return "Working late";
  if (h < 12) return "Good morning";
  if (h < 18) return "Good afternoon";
  return "Good evening";
}

export type Tone = "ok" | "warn" | "bad" | "manual" | "dim" | "";

export function stateChipClass(state: string): Tone {
  if (["verified", "published", "delivered", "merged", "approved", "agent_finished", "working", "confirmed", "finished"].includes(state))
    return "ok";
  if (
    ["checks_failed", "failed", "dead", "blocked", "creation_unknown", "unknown", "closed_unmerged", "deleted", "terminated", "cancelled"].includes(state)
  )
    return "bad";
  if (
    ["needs_input", "approval_required", "suspended", "stale", "checks_pending", "dispatching", "stop_requested", "stopped", "changes_requested", "awaiting_review", "pr_found", "ambiguous"].includes(state)
  )
    return "warn";
  // Operator-recorded verification is a positive state but weaker than CI —
  // it gets its own color so it never reads as "CI verified".
  if (state === "manually_verified") return "manual";
  if (["queued", "no_pr", "none", "pending", "kept", "active"].includes(state)) return "dim";
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

/** Plain-language explanation of what each dimension means. */
export const DIMENSION_HELP: Record<string, string> = {
  execution: "What the Devin session is doing right now (queued → working → finished / blocked / stopped).",
  validation: "Independent check-run verification of the PR head — never taken from the agent's own claims.",
  review: "Human review state of the PR. Merging is always a human decision.",
  disposition: "Overall outcome of the task: active, delivered, blocked, failed, cancelled or deleted.",
};

/**
 * One readable sentence summarising the four dimensions, so a reader does
 * not have to decode four chips to know what is going on.
 */
export function describeTask(t: TaskSummary): { headline: string; tone: Tone } {
  if (t.disposition === "deleted") return { headline: "Removed from tracking", tone: "dim" };
  if (t.review === "merged") return { headline: "Merged by a human reviewer", tone: "ok" };
  if (t.review === "closed_unmerged") return { headline: "PR closed without merging", tone: "bad" };
  if (t.review === "approved") return { headline: "Approved by a human reviewer", tone: "ok" };
  if (t.review === "changes_requested") return { headline: "Reviewer requested changes", tone: "warn" };
  if (t.validation === "verified")
    return { headline: "PR independently verified · awaiting human review", tone: "ok" };
  if (t.validation === "manually_verified")
    return { headline: "Operator-verified PR (not CI) · awaiting review", tone: "manual" };
  if (t.validation === "checks_failed") return { headline: "PR opened but required checks failed", tone: "bad" };
  if (t.disposition === "blocked") return { headline: "Blocked — needs operator review", tone: "bad" };
  if (t.disposition === "failed") return { headline: "Attempt failed", tone: "bad" };
  if (t.disposition === "cancelled") return { headline: "Stopped by operator", tone: "warn" };
  switch (t.execution) {
    case "queued":
      return { headline: "Approved · waiting for a dispatch slot", tone: "dim" };
    case "dispatching":
      return { headline: "Creating Devin session…", tone: "warn" };
    case "creation_unknown":
      return { headline: "Session creation uncertain · reconciling by tag", tone: "bad" };
    case "working":
      return { headline: t.pr_url ? "PR opened · session still working" : "Devin is investigating and implementing", tone: "ok" };
    case "needs_input":
      return { headline: "Devin is waiting for a human answer", tone: "warn" };
    case "approval_required":
      return { headline: "Devin is waiting for an approval", tone: "warn" };
    case "suspended":
      return { headline: "Session asleep without a deliverable", tone: "warn" };
    case "agent_finished":
      if (t.validation === "checks_pending") return { headline: "Agent finished · verifying checks on head", tone: "warn" };
      if (t.validation === "pr_found") return { headline: "Agent finished · PR found, verification queued", tone: "warn" };
      if (t.validation === "unknown") return { headline: "Agent finished · verification inconclusive", tone: "bad" };
      return { headline: "Agent finished", tone: "ok" };
    case "stop_requested":
      return { headline: "Stop requested…", tone: "warn" };
    case "stopped":
      return { headline: "Session terminated", tone: "warn" };
    case "failed":
      return { headline: "Session failed", tone: "bad" };
  }
  return { headline: humanize(t.execution), tone: "" };
}
