import { useEffect, useState } from "react";
import { Link, useNavigate, useParams, useSearchParams } from "react-router-dom";
import { api, ApiError, unwrap } from "../../api/client";
import type { Project } from "../../api/types";
import { ErrorBox, JobStatus, Loading, Panel } from "../../components/common";
import { when } from "../../lib/format";
import { useJob, useLoad } from "../../lib/hooks";
import { Questions } from "./Questions";
import { Repairs } from "./Repairs";
import { Results } from "./Results";

function ProjectToggles({ scenarioId, revisionId, current, expectedHead, editable }: {
  scenarioId: string; revisionId: string; current: string[]; expectedHead: string; editable: boolean;
}) {
  const projects = useLoad(() => unwrap(api.GET("/api/projects")), []);
  const rev = useLoad(() => unwrap(api.GET("/api/scenario-revisions/{revision_id}", { params: { path: { revision_id: revisionId } } })), [revisionId]);
  const [sel, setSel] = useState<string[]>(current);
  const [error, setError] = useState<ApiError>();
  const nav = useNavigate();
  useEffect(() => setSel(current), [current]);
  const modeled = (projects.data ?? []).filter((p: Project) => p.head_revision_id);
  const changed = [...sel].sort().join() !== [...current].sort().join();

  const save = async () => {
    if (!rev.data) return;
    setError(undefined);
    try {
      const out = await unwrap(api.POST("/api/scenarios/{scenario_id}/revisions", {
        params: { path: { scenario_id: scenarioId } },
        body: { expected_head: expectedHead, scenario: { ...rev.data.scenario, project_ids: [...sel].sort() } },
      }));
      nav(`/scenario-revisions/${encodeURIComponent(out.id)}`);
    } catch (e) {
      setError(e as ApiError);
    }
  };

  return (
    <div>
      <fieldset disabled={!editable}>
        <legend>Projects in this scenario</legend>
        {modeled.map((p) => (
          <label key={p.id} className="row" style={{ display: "flex" }}>
            <input type="checkbox" checked={sel.includes(p.id)} onChange={(e) => setSel(e.target.checked ? [...sel, p.id] : sel.filter((x) => x !== p.id))} />
            <span>{p.title} <span className="mono small muted">{p.id}</span>{p.synthetic && <span className="tag" style={{ marginLeft: 4 }}>synthetic</span>}</span>
          </label>
        ))}
      </fieldset>
      {!editable && <p className="small muted">Only the head revision can be changed. Open the head to toggle projects.</p>}
      {changed && <button className="primary" onClick={save}>Save as new revision</button>}
      <ErrorBox error={error} title="Could not save the scenario" />
    </div>
  );
}

export function Workspace() {
  const { revisionId = "" } = useParams();
  const [search] = useSearchParams();
  const nav = useNavigate();
  const rev = useLoad(() => unwrap(api.GET("/api/scenario-revisions/{revision_id}", { params: { path: { revision_id: revisionId } } })), [revisionId]);
  const geo = useLoad(() => unwrap(api.GET("/api/scenario-revisions/{revision_id}/geometry", { params: { path: { revision_id: revisionId } } })), [revisionId]);
  const run = useLoad(() => unwrap(api.GET("/api/scenario-revisions/{revision_id}/analysis", { params: { path: { revision_id: revisionId } } })), [revisionId]);
  const qs = useLoad(() => unwrap(api.GET("/api/scenario-revisions/{revision_id}/questions", { params: { path: { revision_id: revisionId } } })), [revisionId]);
  const scen = useLoad(() => unwrap(api.GET("/api/scenarios")), [revisionId]);
  const [jobId, setJobId] = useState<number>();
  const [error, setError] = useState<ApiError>();
  const job = useJob(jobId, () => { run.reload(); qs.reload(); });

  if (rev.error) return <ErrorBox error={rev.error} title="Scenario revision not found" />;
  if (!rev.data || !geo.data) return <Loading what="scenario" />;
  const r = rev.data;
  const sc = scen.data?.find((s) => s.id === r.scenario_id);
  const resolved = search.get("resolved");

  const analyze = async () => {
    setError(undefined);
    try {
      const ref = await unwrap(api.POST("/api/scenario-revisions/{revision_id}/analyze", { params: { path: { revision_id: r.id } } }));
      setJobId(ref.job_id);
    } catch (e) {
      setError(e as ApiError);
    }
  };
  const rebase = async () => {
    setError(undefined);
    try {
      const out = await unwrap(api.POST("/api/scenarios/{scenario_id}/rebase", { params: { path: { scenario_id: r.scenario_id } } }));
      nav(`/scenario-revisions/${encodeURIComponent(out.id)}`);
    } catch (e) {
      setError(e as ApiError);
    }
  };

  const a = run.data;
  return (
    <div>
      <div className="spread">
        <div>
          <p className="small muted" style={{ margin: 0 }}><Link to="/scenarios">Scenarios</Link> / {r.scenario_id}</p>
          <h1>{r.scenario.label} <span className="muted">rev {r.number}</span></h1>
        </div>
        <div className="row">
          {r.is_head ? <span className="tag ok">head revision</span> : <span className="tag warn">not the head</span>}
          {r.hypothetical && <span className="tag warn">hypothetical branch</span>}
          {r.scenario.hypothetical && <span className="tag warn">labeled hypothetical</span>}
        </div>
      </div>
      {resolved && <div className="notice ok" role="status">Recorded the resolution of <span className="mono">{resolved}</span>. This is the new revision; run the analysis to recompute.</div>}
      {r.branch_of && <p className="small">Branch of <Link to={`/scenario-revisions/${encodeURIComponent(r.branch_of)}`}>{r.branch_of}</Link> (a repair candidate checked by a fresh verifier run).</p>}
      {!r.pinned_inputs_current && (
        <div className="notice warn" role="status">
          <strong>Inputs changed since this revision was pinned:</strong> {r.outdated_inputs.join("; ")}
          {r.is_head && <div style={{ marginTop: "0.4rem" }}><button onClick={rebase}>Rebase onto current inputs</button></div>}
        </div>
      )}
      <ErrorBox error={error} />

      <div className="grid2">
        <div>
          <Panel id="analysis-h" title="Analysis" actions={<button className="primary" onClick={analyze} disabled={job?.state === "running" || job?.state === "queued"}>Run analysis</button>}>
            <JobStatus job={job} label="Analysis" />
            {run.loading && !a && <Loading what="latest analysis" />}
            {!run.loading && !a && <p className="muted">No analysis has been run for this revision yet.</p>}
            {a && (
              <>
                <div className="row small" style={{ marginBottom: "0.75rem" }}>
                  {a.freshness === "current"
                    ? <span className="tag ok">{"✓"} current</span>
                    : <span className="tag bad" title={a.stale_reason ?? ""}>{"⚠"} {a.freshness}</span>}
                  <span>published {when(a.published_at)}</span>
                  <span className="mono" title={a.input_hash}>input {a.input_hash.slice(0, 12)}</span>
                  <span className="mono">{a.verifier_version}</span>
                  <a className="button small" href={`/api/analysis-runs/${encodeURIComponent(a.id)}/bundle`} download>Export bundle</a>
                </div>
                {a.freshness !== "current" && <p className="notice warn">Stale: {a.stale_reason}. The result below describes the inputs it was computed from, not the current ones.</p>}
                {a.result && <Results key={a.id} result={a.result} geometry={geo.data} timezone={r.scenario.display_timezone ?? "UTC"} />}
              </>
            )}
          </Panel>
        </div>
        <div>
          <Panel id="questions-h" title="What to review next">
            <p className="small muted">Ranked by whether resolving the fact could change a requirement's outcome, against its review cost.</p>
            {qs.error && <ErrorBox error={qs.error} />}
            {qs.data ? <Questions questions={qs.data.questions} revisionId={r.id} editable={r.is_head} /> : <Loading what="questions" />}
          </Panel>
          <Panel id="projects-h" title="Projects and requirements">
            <ProjectToggles scenarioId={r.scenario_id} revisionId={r.id} current={r.scenario.project_ids} expectedHead={sc?.head_revision_id ?? r.id} editable={r.is_head} />
            <h3 style={{ marginTop: "0.8rem" }}>Requirements</h3>
            <ul className="small">
              {r.requirements.map((q) => (
                <li key={q.id}><strong>{q.label}</strong> <span className="mono muted">{q.id} v{q.version}</span>: {q.origins.join(", ")} to {q.destinations.join(", ")}, profile {q.profile_id}</li>
              ))}
            </ul>
            <p className="small">Mode: {r.scenario.mode === "dated" ? "dated schedule" : "phases"}{r.scenario.horizon ? `, horizon ${when(r.scenario.horizon.start)} to ${when(r.scenario.horizon.end)}` : ""}.</p>
            <dl className="kv small">
              <dt>Network</dt><dd className="mono">{r.network_revision_id}</dd>
              <dt>Projects</dt><dd className="mono">{r.project_revision_ids.join(", ") || "none"}</dd>
            </dl>
          </Panel>
          {sc && sc.revisions.length > 1 && (
            <Panel id="lineage-h" title="Revision lineage">
              <ol className="small">
                {sc.revisions.map((x) => {
                  const rv = x as { id: string; number: number; hypothetical: boolean; branch_of: string | null };
                  return <li key={rv.id}>{rv.id === r.id ? <strong>{rv.id}</strong> : <Link to={`/scenario-revisions/${encodeURIComponent(rv.id)}`}>{rv.id}</Link>}
                    {rv.branch_of && <span className="muted"> branch of {rv.branch_of}</span>}
                    {rv.id !== r.id && <> (<Link to={`/compare?kind=scenario&a=${encodeURIComponent(rv.id)}&b=${encodeURIComponent(r.id)}`}>compare</Link>)</>}</li>;
                })}
              </ol>
            </Panel>
          )}
        </div>
      </div>
      <Panel id="repairs-h" title="Repairs">
        <Repairs revision={r} geometry={geo.data} />
      </Panel>
    </div>
  );
}
