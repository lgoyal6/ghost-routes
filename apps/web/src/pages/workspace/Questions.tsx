import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { api, ApiError, unwrap } from "../../api/client";
import type { Question } from "../../api/types";
import { ErrorBox, OutcomeBadge } from "../../components/common";
import { useReviewer } from "../../lib/hooks";

/** Facts whose resolution can be a yes/no: existence, restriction scope, temporary connections. */
function isBoolean(q: Question): boolean {
  return ["edge_existence", "restriction_unresolved", "restriction_target", "temporary_connection"].includes(q.fact.kind);
}

function ResolveForm({ q, revisionId, onCancel }: { q: Question; revisionId: string; onCancel: () => void }) {
  const [reviewer] = useReviewer();
  const nav = useNavigate();
  const boolean = isBoolean(q);
  const choices = (q.fact.alternatives ?? []).filter((a) => a !== null);
  const [value, setValue] = useState<string>(boolean ? "false" : String(choices[0] ?? ""));
  const [unit, setUnit] = useState("m");
  const [rationale, setRationale] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<ApiError>();

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(undefined);
    const parsed = boolean ? value === "true" : value.trim() !== "" && !Number.isNaN(Number(value)) ? Number(value) : value;
    try {
      const res = await unwrap(api.POST("/api/scenario-revisions/{revision_id}/resolve-fact", {
        params: { path: { revision_id: revisionId } },
        body: { fact_id: q.fact.id, value: parsed, unit: boolean ? null : unit || null, reviewer, reviewer_kind: "human_ui", rationale },
      }));
      nav(`/scenario-revisions/${encodeURIComponent(res.scenario_revision)}?resolved=${encodeURIComponent(q.fact.id)}`);
    } catch (err) {
      setError(err as ApiError);
      setBusy(false);
    }
  };

  return (
    <form className="form" onSubmit={submit} aria-label={`Resolve ${q.fact.id}`} style={{ marginTop: "0.5rem" }}>
      {!reviewer && <p className="notice warn">Set "Reviewing as" in the sidebar first. UI decisions are attributed to that label.</p>}
      <div className="row">
        {boolean ? (
          <label>Finding
            <select value={value} onChange={(e) => setValue(e.target.value)}>
              <option value="true">{q.fact.kind === "edge_existence" ? "Exists and is walkable" : "Confirmed (applies)"}</option>
              <option value="false">{q.fact.kind === "edge_existence" ? "Does not exist / not walkable" : "Rejected (does not apply)"}</option>
            </select>
          </label>
        ) : (
          <>
            <label>Reviewed value
              <input value={value} onChange={(e) => setValue(e.target.value)} list={`alts-${q.fact.id}`} required />
              <datalist id={`alts-${q.fact.id}`}>{choices.map((c) => <option key={String(c)} value={String(c)} />)}</datalist>
            </label>
            <label>Unit<input value={unit} onChange={(e) => setUnit(e.target.value)} size={4} /></label>
          </>
        )}
      </div>
      <label>Rationale and source (required)
        <textarea value={rationale} onChange={(e) => setRationale(e.target.value)} required rows={2}
          placeholder="What you checked, for example a site visit or sheet note" />
      </label>
      <ErrorBox error={error} title="Could not record the resolution" />
      <div className="row">
        <button className="primary" type="submit" disabled={busy || !reviewer || !rationale.trim()}>Record and create new revision</button>
        <button type="button" onClick={onCancel}>Cancel</button>
      </div>
    </form>
  );
}

export function Questions({ questions, revisionId, editable }: { questions: Question[]; revisionId: string; editable: boolean }) {
  const [open, setOpen] = useState<string>();
  if (!questions.length) return <p className="muted">No unresolved fact could change any requirement's outcome.</p>;
  return (
    <ol style={{ paddingLeft: "1.2rem", margin: 0 }}>
      {questions.map((q) => (
        <li key={q.fact.id} style={{ marginBottom: "0.8rem" }}>
          <div className="spread">
            <span><span className="mono">{q.fact.id}</span> <span className="tag">{q.fact.kind.replace(/_/g, " ")}</span></span>
            <span className="small muted">priority {q.score.toFixed(2)} (review cost {q.review_cost})</span>
          </div>
          <p style={{ margin: "0.2rem 0" }}>{q.fact.description}</p>
          <div className="small">
            Affects {q.affected.join(", ")}.{" "}
            {q.affected.map((r) => (
              <span key={r} className="row" style={{ gap: "0.3rem", display: "inline-flex", marginRight: "0.8rem" }}>
                {r}: if favorable <OutcomeBadge outcome={q.outcome_if_favorable[r]!} /> if unfavorable <OutcomeBadge outcome={q.outcome_if_unfavorable[r]!} />
              </span>
            ))}
          </div>
          {(q.fact.evidence ?? []).length > 0 && <p className="small muted">Evidence: {q.fact.evidence!.join(", ")}</p>}
          {editable && (open === q.fact.id
            ? <ResolveForm q={q} revisionId={revisionId} onCancel={() => setOpen(undefined)} />
            : <button onClick={() => setOpen(q.fact.id)}>Resolve this fact</button>)}
        </li>
      ))}
    </ol>
  );
}
