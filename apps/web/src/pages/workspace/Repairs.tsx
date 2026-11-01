import { useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { api, ApiError, unwrap } from "../../api/client";
import type { Geometry, Intervention, Repair, ScenarioRevision } from "../../api/types";
import { ErrorBox, JobStatus, OutcomeBadge } from "../../components/common";
import { useJob, useLoad } from "../../lib/hooks";

type Kind = Intervention["kind"];
const KIND_TEXT: Record<Kind, string> = {
  keep_segment_open: "Keep a segment open during a closure",
  enable_temporary_connection: "Build a temporary connection",
  schedule_shift: "Shift a phase later",
};

/** A complete Intervention with the server's defaults made explicit. */
function intervention(p: Pick<Intervention, "id" | "kind" | "label" | "cost_unit"> & Partial<Intervention>): Intervention {
  return { cost: 0, edge_ids: [], options: [], side_effects: [], requires: [], excludes: [], construction_feasibility: "unassessed", ...p };
}

function describe(iv: Intervention): string {
  if (iv.kind === "schedule_shift") return `${iv.project_id}/${iv.phase_id}: ${(iv.options ?? []).map((o) => `+${o.seconds / 60} min for ${o.cost}`).join(" or ")}`;
  const side = (iv.side_effects ?? []).map((s) => `side effect closes ${(s.restriction.targets ?? []).map((t) => t.edge_id).join(", ")}`).join("; ");
  const base = iv.kind === "keep_segment_open" ? `${iv.restriction_id} keeps ${(iv.edge_ids ?? []).join(", ")} open` : `enable ${iv.connection_id}`;
  return `${base}, cost ${iv.cost}${side ? `; ${side}` : ""}`;
}

function InterventionForm({ geometry, unit, onAdd, taken }: { geometry: Geometry; unit: string; onAdd: (iv: Intervention) => void; taken: Set<string> }) {
  const [kind, setKind] = useState<Kind>("keep_segment_open");
  const restrictions = geometry.projects.flatMap((p) => (p.restrictions ?? []).map((r) => ({ p, r })));
  const temps = geometry.projects.flatMap((p) => (p.temporary_connections ?? []).map((t) => ({ p, t })));
  const phases = geometry.projects.flatMap((p) => p.phases.map((ph) => ({ p, ph })));
  const [ref, setRef] = useState("");
  const [edges, setEdges] = useState<string[]>([]);
  const [cost, setCost] = useState("1");
  const [options, setOptions] = useState("1800:3, 3600:6");
  const [sideEdges, setSideEdges] = useState("");
  const [label, setLabel] = useState("");
  const [err, setErr] = useState("");

  const chosenR = restrictions.find(({ p, r }) => `${p.project_id}/${r.id}` === ref);
  const chosenT = temps.find(({ p, t }) => `${p.project_id}/${t.id}` === ref);
  const chosenP = phases.find(({ p, ph }) => `${p.project_id}/${ph.id}` === ref);

  const add = (e: React.FormEvent) => {
    e.preventDefault();
    setErr("");
    const id = (label || `${kind}-${ref}`).toLowerCase().replace(/[^a-z0-9-]+/g, "-").replace(/^-|-$/g, "").slice(0, 60);
    if (taken.has(id)) return setErr(`An intervention with id ${id} already exists.`);
    const base = { id, kind, label: label || KIND_TEXT[kind], cost_unit: unit } as const;
    if (kind === "keep_segment_open") {
      if (!chosenR || !edges.length) return setErr("Choose a restriction and at least one of its segments.");
      onAdd(intervention({ ...base, restriction_id: chosenR.r.id, edge_ids: edges, cost: Number(cost) }));
    } else if (kind === "enable_temporary_connection") {
      if (!chosenT) return setErr("Choose a temporary connection.");
      const se = sideEdges.split(",").map((s) => s.trim()).filter(Boolean);
      const phase = chosenT.t.phase_ids[0]!;
      onAdd(intervention({
        ...base, connection_id: chosenT.t.id, cost: Number(cost),
        side_effects: se.length ? [{ project_id: chosenT.p.project_id, restriction: {
          id: `${id}-side-effect`, kind: "closure", state: "ACCEPTED", evidence: [`ui:${id}`], phase_ids: [phase],
          targets: se.map((edge_id) => ({ edge_id, evidence: [], state: "ACCEPTED" as const })),
          label: `building ${chosenT.t.id} closes ${se.join(", ")}` } }] : [],
      }));
    } else {
      if (!chosenP) return setErr("Choose a phase.");
      const opts = options.split(",").map((s) => s.trim().split(":")).map(([sec, c]) => ({ seconds: Number(sec), cost: Number(c) }));
      if (opts.some((o) => !Number.isFinite(o.seconds) || !Number.isFinite(o.cost) || o.seconds <= 0)) return setErr("Options are seconds:cost pairs, for example 1800:3.");
      onAdd(intervention({ ...base, project_id: chosenP.p.project_id, phase_id: chosenP.ph.id, options: opts }));
    }
    setRef(""); setEdges([]); setLabel(""); setSideEdges("");
  };

  return (
    <form className="form" onSubmit={add} aria-label="Add an allowed intervention">
      <div className="row">
        <label>Kind
          <select value={kind} onChange={(e) => { setKind(e.target.value as Kind); setRef(""); setEdges([]); }}>
            {(Object.keys(KIND_TEXT) as Kind[]).map((k) => <option key={k} value={k}>{KIND_TEXT[k]}</option>)}
          </select>
        </label>
        <label>Applies to
          <select value={ref} onChange={(e) => { setRef(e.target.value); setEdges([]); }} required>
            <option value="">Choose...</option>
            {kind === "keep_segment_open" && restrictions.map(({ p, r }) => <option key={`${p.project_id}/${r.id}`} value={`${p.project_id}/${r.id}`}>{p.project_id}: {r.label || r.id}</option>)}
            {kind === "enable_temporary_connection" && temps.map(({ p, t }) => <option key={`${p.project_id}/${t.id}`} value={`${p.project_id}/${t.id}`}>{p.project_id}: {t.edge.label || t.id}</option>)}
            {kind === "schedule_shift" && phases.map(({ p, ph }) => <option key={`${p.project_id}/${ph.id}`} value={`${p.project_id}/${ph.id}`}>{p.project_id}: {ph.label}</option>)}
          </select>
        </label>
        <label>Label<input value={label} onChange={(e) => setLabel(e.target.value)} placeholder="Short description" /></label>
      </div>
      {kind === "keep_segment_open" && chosenR && (
        <fieldset><legend>Segments to keep open</legend>
          {(chosenR.r.targets ?? []).map((t) => (
            <label key={t.edge_id} className="row" style={{ display: "inline-flex", marginRight: "1rem" }}>
              <input type="checkbox" checked={edges.includes(t.edge_id)}
                onChange={(e) => setEdges(e.target.checked ? [...edges, t.edge_id] : edges.filter((x) => x !== t.edge_id))} />
              <span className="mono">{t.edge_id}</span>
            </label>
          ))}
        </fieldset>
      )}
      <div className="row">
        {kind !== "schedule_shift" && <label>Cost ({unit})<input type="number" min="0" step="any" value={cost} onChange={(e) => setCost(e.target.value)} required /></label>}
        {kind === "schedule_shift" && <label>Shift options (seconds:cost)<input value={options} onChange={(e) => setOptions(e.target.value)} /></label>}
        {kind === "enable_temporary_connection" && <label>Side effect: segments closed while building (comma-separated)<input value={sideEdges} onChange={(e) => setSideEdges(e.target.value)} placeholder="e6" /></label>}
      </div>
      {err && <p className="notice error" role="alert">{err}</p>}
      <div><button type="submit">Add intervention</button></div>
    </form>
  );
}

function RepairResult({ repair }: { repair: Repair }) {
  const r = repair.result;
  const branches = new Map(repair.candidates.map((c) => [JSON.stringify(c.selection), c.branch_scenario_revision_id as string | null]));
  if (!r) return <p className="muted">No result yet ({String(repair.job?.state ?? "queued")}).</p>;
  const reqs = Object.keys((r.candidates[0] ?? r.rejected[0])?.outcomes ?? {});
  const rows = [...r.candidates, ...r.rejected];
  return (
    <div>
      <div className="row" style={{ marginBottom: "0.5rem" }}>
        <span className={`tag ${r.status === "OPTIMAL" ? "ok" : r.status === "FEASIBLE" ? "warn" : "bad"}`}>{r.status}</span>
        <span className="small">method {r.method}, {r.evaluations} independent verifier runs{r.objective != null ? `, best cost ${r.objective} ${r.cost_unit ?? ""}` : ""}{r.bound != null ? `, bound ${r.bound}` : ""}</span>
        {repair.freshness !== "current" && <span className="tag bad">stale: {repair.stale_reason}</span>}
      </div>
      {r.detail && <p className="small muted">{r.detail}</p>}
      {rows.length === 0 ? <p>No candidate restores the selected requirements within the allowed interventions.</p> : (
        <table aria-label="Repair candidates">
          <thead><tr><th>Interventions</th><th>Cost</th><th>Recheck</th>{reqs.map((q) => <th key={q}>{q}</th>)}<th>Verified branch</th></tr></thead>
          <tbody>
            {rows.map((c, i) => (
              <tr key={i}>
                <td className="mono">{Object.entries(c.selection).map(([k, v]) => (v ? `${k} (option ${v})` : k)).join(" + ") || "(none)"}</td>
                <td>{c.cost}</td>
                <td>{c.accepted ? <span className="tag ok">{"✓"} accepted</span> : <span className="tag bad">{"✕"} rejected</span>}
                  {c.reasons.map((x, j) => <div key={j} className="small">{x}</div>)}</td>
                {reqs.map((q) => (
                  <td key={q}><OutcomeBadge outcome={c.outcomes[q]!} />{c.baseline_outcomes[q] !== c.outcomes[q] && <div className="small muted">was {c.baseline_outcomes[q]}</div>}</td>
                ))}
                <td>{branches.get(JSON.stringify(c.selection)) ? <Link to={`/scenario-revisions/${encodeURIComponent(branches.get(JSON.stringify(c.selection))!)}`}>Open branch</Link> : <span className="muted">none</span>}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}

export function Repairs({ revision, geometry }: { revision: ScenarioRevision; geometry: Geometry }) {
  const past = useLoad(() => unwrap(api.GET("/api/scenario-revisions/{revision_id}/repairs", { params: { path: { revision_id: revision.id } } })), [revision.id]);
  const [unit, setUnit] = useState("crew-days");
  const [ivs, setIvs] = useState<Intervention[]>([]);
  const reqIds = revision.requirements.map((r) => r.id);
  const [restore, setRestore] = useState<string[]>([]);
  const [protect, setProtect] = useState<string[]>([]);
  const [method, setMethod] = useState<"auto" | "enumerate" | "cpsat">("auto");
  const [jobId, setJobId] = useState<number>();
  const [runId, setRunId] = useState<string>();
  const [error, setError] = useState<ApiError>();
  const job = useJob(jobId, () => past.reload());
  const taken = useMemo(() => new Set(ivs.map((i) => i.id)), [ivs]);
  const latest = runId ? past.data?.find((r) => r.id === runId) : past.data?.[0];

  const run = async () => {
    setError(undefined);
    try {
      const ref = await unwrap(api.POST("/api/scenario-revisions/{revision_id}/repairs", {
        params: { path: { revision_id: revision.id } },
        body: { interventions: ivs, restore, protect, method, max_candidates: 3, max_evaluations: 500, time_limit_s: 30 },
      }));
      setRunId(ref.detail?.repair_run_id as string);
      setJobId(ref.job_id);
    } catch (e) {
      setError(e as ApiError);
    }
  };

  return (
    <div>
      <p className="small muted">The search only considers interventions you allow here. Every candidate is re-verified from scratch, and a candidate that makes a protected requirement worse is rejected.</p>
      <label style={{ maxWidth: 220 }}>Cost unit<input value={unit} onChange={(e) => setUnit(e.target.value)} /></label>
      <InterventionForm geometry={geometry} unit={unit} taken={taken} onAdd={(iv) => setIvs([...ivs, iv])} />
      {ivs.length > 0 && (
        <ul aria-label="Allowed interventions">
          {ivs.map((iv) => (
            <li key={iv.id} className="row"><span><strong>{iv.label}</strong> <span className="mono small">{iv.id}</span>: {describe(iv)}</span>
              <button className="small" onClick={() => setIvs(ivs.filter((x) => x.id !== iv.id))} aria-label={`Remove ${iv.label}`}>Remove</button></li>
          ))}
        </ul>
      )}
      <div className="row" style={{ alignItems: "flex-start", marginTop: "0.5rem" }}>
        <fieldset><legend>Restore</legend>
          {reqIds.map((id) => (
            <label key={id} className="row" style={{ display: "flex" }}><input type="checkbox" checked={restore.includes(id)}
              onChange={(e) => { setRestore(e.target.checked ? [...restore, id] : restore.filter((x) => x !== id)); setProtect(protect.filter((x) => x !== id)); }} />{id}</label>
          ))}
        </fieldset>
        <fieldset><legend>Protect (must not get worse)</legend>
          {reqIds.filter((id) => !restore.includes(id)).map((id) => (
            <label key={id} className="row" style={{ display: "flex" }}><input type="checkbox" checked={protect.includes(id)}
              onChange={(e) => setProtect(e.target.checked ? [...protect, id] : protect.filter((x) => x !== id))} />{id}</label>
          ))}
        </fieldset>
        <label>Method
          <select value={method} onChange={(e) => setMethod(e.target.value as typeof method)}>
            <option value="auto">auto</option><option value="enumerate">enumerate</option><option value="cpsat">CP-SAT</option>
          </select>
        </label>
      </div>
      <button className="primary" onClick={run} disabled={!ivs.length || !restore.length || (job && job.state === "running")}>Search for repairs</button>
      <ErrorBox error={error} title="Repair request failed" />
      <JobStatus job={job} label="Repair search" />
      {latest && <div style={{ marginTop: "0.75rem" }}><h3>{runId ? "Result" : "Most recent repair search"}</h3><RepairResult repair={latest} /></div>}
    </div>
  );
}
