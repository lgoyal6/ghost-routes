import { useState } from "react";
import { api, ApiError, unwrap } from "../api/client";
import type { Profile, Requirement } from "../api/types";
import { ErrorBox, Loading, Panel } from "../components/common";
import { useLoad, useReviewer } from "../lib/hooks";

function ProfileCard({ p, onChanged }: { p: Profile; onChanged: () => void }) {
  const [reviewer] = useReviewer();
  const [error, setError] = useState<ApiError>();
  const [rationale, setRationale] = useState("");
  const review = async () => {
    setError(undefined);
    try {
      await unwrap(api.POST("/api/profiles/{profile_id}/review", { params: { path: { profile_id: p.id } }, body: { reviewer, reviewer_kind: "human_ui", rationale } }));
      onChanged();
    } catch (e) {
      setError(e as ApiError);
    }
  };
  return (
    <Panel id={`prof-${p.id}`} title={<>{p.label} <span className="mono small muted">{p.id} v{p.version}</span></>}
      actions={<span className={`tag ${p.review_state === "reviewed" ? "ok" : "warn"}`}>{p.review_state ?? "unreviewed"}</span>}>
      {p.illustrative && <p className="notice warn small">Illustrative thresholds for the demo, not an accessibility standard.</p>}
      {p.description && <p className="small">{p.description}</p>}
      <table aria-label={`Constraints of ${p.label}`}>
        <thead><tr><th>Attribute</th><th>Rule</th><th>Type</th><th>Source</th></tr></thead>
        <tbody>
          {p.constraints.map((c, i) => (
            <tr key={i}><td className="mono">{c.attribute}</td><td>{c.comparator} {String(c.threshold)} {c.unit ?? ""}</td>
              <td>{c.hard === false ? `soft (weight ${c.weight ?? 1})` : "hard limit"}</td><td className="small">{c.source || c.rationale || ""}</td></tr>
          ))}
        </tbody>
      </table>
      {p.review_state !== "reviewed" && (
        <div className="row" style={{ marginTop: "0.5rem" }}>
          <label>Review rationale<input value={rationale} onChange={(e) => setRationale(e.target.value)} /></label>
          <button onClick={review} disabled={!reviewer || !rationale.trim()}>Mark reviewed (new version)</button>
        </div>
      )}
      {p.reviewer && <p className="small muted">Reviewed by {p.reviewer}</p>}
      <ErrorBox error={error} />
    </Panel>
  );
}

function RequirementForm({ profiles, onSaved, initial }: { profiles: Profile[]; onSaved: () => void; initial?: Requirement }) {
  const [id, setId] = useState(initial?.id ?? "");
  const [label, setLabel] = useState(initial?.label ?? "");
  const [profile, setProfile] = useState(initial?.profile_id ?? profiles[0]?.id ?? "");
  const [origins, setOrigins] = useState(initial?.origins.join(", ") ?? "");
  const [dests, setDests] = useState(initial?.destinations.join(", ") ?? "");
  const [maxAdded, setMaxAdded] = useState(initial?.max_added_distance_m != null ? String(initial.max_added_distance_m) : "");
  const [error, setError] = useState<ApiError>();
  const [ok, setOk] = useState<string>();
  const split = (s: string) => s.split(",").map((x) => x.trim()).filter(Boolean);
  const save = async (e: React.FormEvent) => {
    e.preventDefault();
    setError(undefined); setOk(undefined);
    try {
      const out = await unwrap(api.POST("/api/requirements", {
        body: { ...(initial ?? {}), id, version: initial?.version ?? 1, label, profile_id: profile, origins: split(origins), destinations: split(dests),
          max_added_distance_m: maxAdded ? Number(maxAdded) : null } as Requirement,
      }));
      setOk(`Saved ${out.id} as version ${out.version}. Scenarios pinned to the earlier version now show it as outdated.`);
      onSaved();
    } catch (err) {
      setError(err as ApiError);
    }
  };
  return (
    <form className="form" onSubmit={save} aria-label={initial ? `Edit ${initial.id}` : "New requirement"}>
      <div className="row">
        <label>Id<input value={id} onChange={(e) => setId(e.target.value)} required disabled={Boolean(initial)} /></label>
        <label>Label<input value={label} onChange={(e) => setLabel(e.target.value)} required size={36} /></label>
        <label>Profile<select value={profile} onChange={(e) => setProfile(e.target.value)}>{profiles.map((p) => <option key={p.id} value={p.id}>{p.label}</option>)}</select></label>
      </div>
      <div className="row">
        <label>Origin nodes<input value={origins} onChange={(e) => setOrigins(e.target.value)} required placeholder="A" /></label>
        <label>Destination nodes<input value={dests} onChange={(e) => setDests(e.target.value)} required placeholder="F" /></label>
        <label>Max added distance (m, optional)<input type="number" min="0" step="any" value={maxAdded} onChange={(e) => setMaxAdded(e.target.value)} /></label>
      </div>
      <ErrorBox error={error} title="Not saved" />
      {ok && <p className="notice ok" role="status">{ok}</p>}
      <div><button className="primary" type="submit">{initial ? "Save new version" : "Create requirement"}</button></div>
    </form>
  );
}

export function Requirements() {
  const profiles = useLoad(() => unwrap(api.GET("/api/profiles")), []);
  const reqs = useLoad(() => unwrap(api.GET("/api/requirements")), []);
  const [editing, setEditing] = useState<string>();
  return (
    <div>
      <h1>Profiles and requirements</h1>
      <p className="muted">A requirement says who must get from where to where. A profile says what a usable segment means for them. Unknown required attributes never count as satisfied.</p>
      <ErrorBox error={profiles.error ?? reqs.error} />
      <Panel id="reqs-h" title="Requirements">
        {!reqs.data ? <Loading what="requirements" /> : (
          <table aria-label="Requirements">
            <thead><tr><th>Requirement</th><th>Journey</th><th>Profile</th><th></th></tr></thead>
            <tbody>
              {reqs.data.map((r) => (
                <tr key={r.id}>
                  <td><strong>{r.label}</strong><div className="mono small muted">{r.id} v{r.version}</div></td>
                  <td>{r.origins.join(", ")} to {r.destinations.join(", ")}{r.max_added_distance_m != null && <div className="small">detour budget {r.max_added_distance_m} m</div>}</td>
                  <td className="mono">{r.profile_id}</td>
                  <td><button className="small" onClick={() => setEditing(editing === r.id ? undefined : r.id)} aria-expanded={editing === r.id}>Edit</button></td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
        {editing && profiles.data && reqs.data && (
          <div style={{ marginTop: "0.75rem" }}><RequirementForm key={editing} profiles={profiles.data} initial={reqs.data.find((r) => r.id === editing)} onSaved={reqs.reload} /></div>
        )}
        <details style={{ marginTop: "0.75rem" }}>
          <summary>New requirement</summary>
          {profiles.data && <RequirementForm profiles={profiles.data} onSaved={reqs.reload} />}
        </details>
      </Panel>
      <h2>Mobility profiles</h2>
      {!profiles.data ? <Loading what="profiles" /> : profiles.data.map((p) => <ProfileCard key={p.id} p={p} onChanged={profiles.reload} />)}
    </div>
  );
}
