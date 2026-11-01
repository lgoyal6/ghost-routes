import type { EdgeClass, Outcome } from "../api/types";

export const OUTCOME_TEXT: Record<Outcome, string> = {
  SUPPORTED: "Supported",
  NEEDS_VERIFICATION: "Needs verification",
  INSUFFICIENT_COVERAGE: "Insufficient coverage",
  DISCONNECTED_WITHIN_COVERAGE: "Disconnected within coverage",
};

/** A glyph per outcome so status never depends on color alone. */
export const OUTCOME_GLYPH: Record<Outcome, string> = {
  SUPPORTED: "✓",
  NEEDS_VERIFICATION: "?",
  INSUFFICIENT_COVERAGE: "◌",
  DISCONNECTED_WITHIN_COVERAGE: "✕",
};

export const EDGE_CLASS_TEXT: Record<EdgeClass, string> = {
  CONSERVATIVE: "usable (reviewed)",
  POSSIBLE_COVERED: "possibly usable, unresolved",
  POSSIBLE_UNCOVERED: "possibly usable, outside coverage",
  BLOCKED: "blocked",
};

export function metres(m: number | null | undefined): string {
  return m === null || m === undefined ? "unknown" : `${m.toFixed(m < 10 ? 1 : 0)} m`;
}

export function when(iso: string | null | undefined): string {
  if (!iso) return "never";
  const d = new Date(iso);
  return d.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
}

export function plural(n: number, word: string): string {
  return `${n} ${word}${n === 1 ? "" : "s"}`;
}
