import { useState } from "react";
import { Link } from "react-router-dom";
import { api, ApiError, unwrap } from "../api/client";
import type { Project } from "../api/types";
import { ErrorBox, Loading, Panel } from "../components/common";
import { when } from "../lib/format";
import { useLoad } from "../lib/hooks";

type Source = { sha256: string; filename: string; source_url: string | null; reuse_status: string; synthetic: boolean; size_bytes: number };
type SheetRef = { id: string; printed_sheet_id: string | null; page_index: number };

function Upload({ onDone }: { onDone: () => void }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<ApiError>();
  const [ok, setOk] = useState<string>();
  const submit = async (e: React.FormEvent<HTMLFormElement>) => {
    e.preventDefault();
    const form = new FormData(e.currentTarget);
    setBusy(true); setError(undefined); setOk(undefined);
    try {
      const res = await fetch("/api/sources", { method: "POST", body: form });
      const body = await res.json();
      if (!res.ok) throw new ApiError(res.status, body.error?.code ?? "http_error", body.error?.message ?? res.statusText, body.error?.detail);
      setOk(`Ingested ${body.sheets?.length ?? 0} sheet(s) into plan revision ${body.plan_revision_id}.`);
      e.currentTarget?.reset();
      onDone();
    } catch (err) {
      setError(err instanceof ApiError ? err : new ApiError(0, "network", (err as Error).message));
    } finally {
      setBusy(false);
    }
  };
  return (
    <form className="form" onSubmit={submit} aria-label="Import a plan PDF">
      <div className="row">
        <label>PDF file<input type="file" name="file" accept="application/pdf" required /></label>
        <label>Project id<input name="project_id" pattern="[A-Za-z0-9][A-Za-z0-9\-]{1,60}" required placeholder="e.g. iowa-sgrand" /></label>
        <label>Project title<input name="project_title" required /></label>
        <label>Revision label<input name="revision_label" required size={6} placeholder="A" /></label>
      </div>
      <div className="row">
        <label>Source URL (optional)<input name="source_url" type="url" size={40} /></label>
        <label>Reuse status
          <select name="reuse_status" defaultValue="reference_only">
            <option value="reference_only">reference only (not redistributed)</option>
            <option value="redistributable">redistributable</option>
            <option value="synthetic_original">synthetic original</option>
            <option value="unknown">unknown</option>
          </select>
        </label>
      </div>
      <p className="small muted">The original bytes are stored once by hash and never modified. A new revision label for an existing project supersedes the previous plan revision and marks dependent results stale.</p>
      <ErrorBox error={error} title="Import failed" />
      {ok && <p className="notice ok" role="status">{ok}</p>}
      <div><button className="primary" type="submit" disabled={busy}>{busy ? "Importing..." : "Import"}</button></div>
    </form>
  );
}

function ProjectCard({ p }: { p: Project }) {
  return (
    <Panel id={`p-${p.id}`} title={<>{p.title} {p.synthetic && <span className="tag">synthetic</span>} {p.hypothetical && <span className="tag warn">hypothetical</span>}</>}
      actions={<span className="mono small muted">{p.id}</span>}>
      {p.description && <p className="small">{p.description}</p>}
      <h3>Plan revisions</h3>
      {p.plan_revisions.length === 0 ? <p className="small muted">No source documents.</p> : (
        <table aria-label={`Plan revisions of ${p.title}`}>
          <thead><tr><th>Revision</th><th>Source</th><th>Sheets</th><th>Lineage</th></tr></thead>
          <tbody>
            {p.plan_revisions.map((x) => {
              const pr = x as unknown as { id: string; revision_label: string; learned_at: string; supersedes_id: string | null; superseded_by: string | null; source: Source; sheets: SheetRef[] };
              return (
                <tr key={pr.id}>
                  <td><strong>{pr.revision_label}</strong><div className="small muted">learned {when(pr.learned_at)}</div></td>
                  <td>
                    <div>{pr.source.filename}</div>
                    <div className="mono small muted" title={pr.source.sha256}>sha256 {pr.source.sha256.slice(0, 12)}</div>
                    <span className="tag">{pr.source.reuse_status.replace(/_/g, " ")}</span>
                    {pr.source.source_url && <div className="small"><a href={pr.source.source_url} rel="noreferrer noopener" target="_blank">original URL</a></div>}
                  </td>
                  <td>{pr.sheets.map((s) => <div key={s.id}><Link to={`/sheets/${encodeURIComponent(s.id)}`}>{s.printed_sheet_id ? `Sheet ${s.printed_sheet_id}` : `Page ${s.page_index + 1}`}</Link></div>)}</td>
                  <td className="small">
                    {pr.superseded_by ? <span className="tag warn">superseded by a later revision</span> : <span className="tag ok">current</span>}
                    {pr.supersedes_id && <div className="muted">supersedes an earlier revision</div>}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      )}
      <h3 style={{ marginTop: "0.8rem" }}>Modeled project revisions</h3>
      {p.revisions.length === 0 ? <p className="small muted">Not modeled yet: review the sheet's observations, then build the project.</p> : (
        <ul className="small">
          {p.revisions.map((x) => {
            const r = x as { id: string; number: number; is_head: boolean; note: string; plan_revision_id: string | null };
            return <li key={r.id}><span className="mono">{r.id}</span> {r.is_head && <span className="tag ok">head</span>} {r.note}</li>;
          })}
        </ul>
      )}
    </Panel>
  );
}

export function Library() {
  const projects = useLoad(() => unwrap(api.GET("/api/projects")), []);
  return (
    <div>
      <h1>Sources and projects</h1>
      <Panel id="upload-h" title="Import a plan"><Upload onDone={projects.reload} /></Panel>
      <ErrorBox error={projects.error} />
      {!projects.data ? <Loading what="projects" /> : projects.data.map((p) => <ProjectCard key={p.id} p={p} />)}
    </div>
  );
}
