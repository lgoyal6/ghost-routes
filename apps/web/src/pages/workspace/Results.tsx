import { useState } from "react";
import type { AnalysisResult, Geometry, PairResult, StateResult } from "../../api/types";
import { OutcomeBadge } from "../../components/common";
import { NetworkMap } from "../../components/NetworkMap";
import { EDGE_CLASS_TEXT, metres } from "../../lib/format";

const SEVERITY = { SUPPORTED: 0, NEEDS_VERIFICATION: 1, INSUFFICIENT_COVERAGE: 2, DISCONNECTED_WITHIN_COVERAGE: 3 } as const;

function fmt(iso: string, tz: string): string {
  return new Date(iso).toLocaleString(undefined, { timeZone: tz, month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
}

/** A readable state name: the time window for schedule states, otherwise the active phases. */
function stateName(s: StateResult, tz: string): string {
  if (s.interval) return `${fmt(s.interval.start, tz)} to ${s.interval.end ? fmt(s.interval.end, tz) : "end"}`;
  if (s.basis === "baseline") return "Baseline (no works)";
  return activeText(s);
}

function activeText(s: StateResult): string {
  const parts = Object.entries(s.active).map(([p, ph]) => `${p}: ${ph}`);
  return parts.length ? parts.join(", ") : "no project active";
}

function PairDetail({ pair }: { pair: PairResult }) {
  return (
    <div>
      <div className="row" style={{ marginBottom: "0.5rem" }}>
        <strong>{pair.origin} to {pair.destination}</strong>
        <OutcomeBadge outcome={pair.outcome} />
        {pair.added_distance_m != null && <span className="small">detour {metres(pair.added_distance_m)} over baseline</span>}
        {pair.budget_exceeded_tiers && pair.budget_exceeded_tiers.length > 0 && (
          <span className="tag bad">over distance budget ({pair.budget_exceeded_tiers.join(", ")})</span>
        )}
      </div>
      {pair.route ? (
        <table aria-label="Route steps">
          <thead><tr><th>Segment</th><th>From</th><th>To</th><th>Kind</th><th>Evidence status</th><th>Length</th></tr></thead>
          <tbody>
            {pair.route.steps.map((s) => (
              <tr key={s.edge_id}>
                <td className="mono">{s.edge_id}{s.temporary && <span className="tag" style={{ marginLeft: 4 }}>temporary</span>}</td>
                <td>{s.from_node}</td><td>{s.to_node}</td><td>{s.kind}</td>
                <td>{s.edge_class === "CONSERVATIVE" ? EDGE_CLASS_TEXT[s.edge_class] : <span className="tag warn">{EDGE_CLASS_TEXT[s.edge_class]}</span>}</td>
                <td>{metres(s.length_m)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : <p className="muted">No route within the modeled network for this state.</p>}

      {(pair.boundary ?? []).length > 0 && (
        <section aria-label="Blockers">
          <h3 style={{ marginTop: "0.8rem" }}>Blockers that separate the endpoints</h3>
          <ul>
            {pair.boundary!.map((b) => (
              <li key={b.edge_id}>
                <span className="mono">{b.edge_id}</span> ({b.from_node} to {b.to_node}):{" "}
                {b.reasons.map((r, i) => (
                  <span key={i}>{r.description}{r.evidence && r.evidence.length > 0 && <span className="small muted"> [{r.evidence.join(", ")}]</span>}{i < b.reasons.length - 1 ? "; " : ""}</span>
                ))}
              </li>
            ))}
          </ul>
        </section>
      )}
      {(pair.uncertainties ?? []).length > 0 && (
        <section aria-label="Missing evidence">
          <h3 style={{ marginTop: "0.8rem" }}>Missing or unresolved evidence</h3>
          <ul>
            {pair.uncertainties!.map((f) => (
              <li key={f.id}><span className="mono">{f.id}</span>: {f.description}</li>
            ))}
          </ul>
        </section>
      )}
      {(pair.coverage_gaps ?? []).length > 0 && (
        <section aria-label="Coverage gaps">
          <h3 style={{ marginTop: "0.8rem" }}>Coverage gaps</h3>
          <ul>{pair.coverage_gaps!.map((g, i) => <li key={i}>{g.description}</li>)}</ul>
        </section>
      )}
      {(pair.departure_gaps ?? []).length > 0 && (
        <section aria-label="Departure gaps">
          <h3 style={{ marginTop: "0.8rem" }}>Departures with no route</h3>
          <ul>{pair.departure_gaps!.map((g, i) => <li key={i}>{new Date(g.start).toLocaleString()} to {g.end ? new Date(g.end).toLocaleString() : "the horizon end"} ({g.level})</li>)}</ul>
        </section>
      )}
      {(pair.rejected_alternatives ?? []).length > 0 && (
        <details style={{ marginTop: "0.6rem" }}>
          <summary>Rejected alternatives ({pair.rejected_alternatives!.length})</summary>
          <ul>
            {pair.rejected_alternatives!.map((a, i) => (
              <li key={i}><span className="mono">{a.route_edges.join(" > ")}</span>: fails on {a.failing.map((f) => `${f.edge_id} (${EDGE_CLASS_TEXT[f.edge_class]})`).join(", ")}</li>
            ))}
          </ul>
        </details>
      )}
      {(pair.notes ?? []).length > 0 && <ul className="small muted">{pair.notes!.map((n, i) => <li key={i}>{n}</li>)}</ul>}
    </div>
  );
}

export function Results({ result, geometry, repairEdges, timezone = "UTC" }: { result: AnalysisResult; geometry: Geometry; repairEdges?: string[]; timezone?: string }) {
  // Open on the requirement in the worst state; ties keep the server's order.
  const [reqIdx, setReqIdx] = useState(() => result.requirements.reduce((best, r, i, all) => (SEVERITY[r.outcome] > SEVERITY[all[best]!.outcome] ? i : best), 0));
  const req = result.requirements[Math.min(reqIdx, result.requirements.length - 1)];
  const worst = req?.states.findIndex((s) => s.outcome === req.outcome) ?? 0;
  const [stateIdx, setStateIdx] = useState<number | null>(null);
  const [pairIdx, setPairIdx] = useState(0);
  if (!req) return <p className="muted">This analysis has no requirements.</p>;
  const state = req.states[stateIdx ?? Math.max(worst, 0)];
  const pair = state?.pairs[Math.min(pairIdx, (state?.pairs.length ?? 1) - 1)];

  return (
    <div>
      <div role="group" aria-label="Requirement" className="row" style={{ marginBottom: "0.75rem" }}>
        {result.requirements.map((r, i) => (
          <button key={r.requirement_id} aria-pressed={i === reqIdx} onClick={() => { setReqIdx(i); setStateIdx(null); setPairIdx(0); }}
            className={i === reqIdx ? "primary" : undefined}>
            {r.requirement_id} <span className={`o-${r.outcome}`} aria-hidden="true">{"●"}</span>
            <span className="sr-only"> {r.outcome}</span>
          </button>
        ))}
      </div>
      <div className="row" style={{ marginBottom: "0.5rem" }}>
        <span>Overall for <strong>{req.requirement_id}</strong> (profile {req.profile_id} v{req.profile_version}):</span>
        <OutcomeBadge outcome={req.outcome} />
      </div>
      <table aria-label="Outcome by state">
        <caption className="small muted" style={{ textAlign: "left", captionSide: "bottom", paddingTop: "0.3rem" }}>Times in {timezone}.</caption>
        <thead><tr><th>State</th><th>Basis</th><th>Active phases</th><th>Outcome</th></tr></thead>
        <tbody>
          {req.states.map((s, i) => {
            const sel = s === state;
            return (
              <tr key={s.label} aria-selected={sel}>
                <td>
                  <button className="small" onClick={() => { setStateIdx(i); setPairIdx(0); }} aria-pressed={sel} title={s.label}>
                    {stateName(s, timezone)}
                  </button>
                  {s.hypothetical && <span className="tag warn" style={{ marginLeft: 4 }}>hypothetical</span>}
                </td>
                <td>{s.basis.replace(/_/g, " ")}</td>
                <td>{activeText(s)}</td>
                <td><OutcomeBadge outcome={s.outcome} /></td>
              </tr>
            );
          })}
        </tbody>
      </table>
      {state && (
        <div style={{ marginTop: "1rem" }}>
          {state.pairs.length > 1 && (
            <label style={{ maxWidth: 320, marginBottom: "0.5rem" }}>Journey
              <select value={pairIdx} onChange={(e) => setPairIdx(Number(e.target.value))}>
                {state.pairs.map((p, i) => <option key={i} value={i}>{p.origin} to {p.destination}: {p.outcome}</option>)}
              </select>
            </label>
          )}
          <NetworkMap network={geometry.network} projects={geometry.projects} state={state} pair={pair}
            repairEdges={repairEdges} title={`Network in state ${state.label}`} />
          {pair && <div style={{ marginTop: "0.75rem" }}><PairDetail pair={pair} /></div>}
        </div>
      )}
    </div>
  );
}
