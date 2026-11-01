import { Link } from "react-router-dom";
import { api, unwrap } from "../api/client";
import { ErrorBox, Loading, Panel } from "../components/common";
import { when } from "../lib/format";
import { useLoad } from "../lib/hooks";

export function Scenarios() {
  const list = useLoad(() => unwrap(api.GET("/api/scenarios")), []);
  return (
    <div>
      <h1>Scenarios</h1>
      <p className="muted">A scenario pins a network revision, project revisions and requirements. Every change creates a new revision; old results stay attached to the inputs they used.</p>
      <ErrorBox error={list.error} />
      {!list.data ? <Loading what="scenarios" /> : list.data.length === 0 ? (
        <p>No scenarios yet. Run <code>make seed</code> to load the fictional Ashgrove demo.</p>
      ) : list.data.map((s) => (
        <Panel key={s.id} id={`sc-${s.id}`} title={<Link to={`/scenario-revisions/${encodeURIComponent(s.head_revision_id ?? "")}`}>{s.title}</Link>}
          actions={<span className="mono small muted">{s.id}</span>}>
          <table aria-label={`Revisions of ${s.title}`}>
            <thead><tr><th>Revision</th><th>Created</th><th>Notes</th></tr></thead>
            <tbody>
              {[...s.revisions].reverse().map((x) => {
                const rv = x as { id: string; number: number; hypothetical: boolean; branch_of: string | null; created_at: string };
                return (
                  <tr key={rv.id}>
                    <td><Link to={`/scenario-revisions/${encodeURIComponent(rv.id)}`}>{rv.id}</Link>{rv.id === s.head_revision_id && <span className="tag ok" style={{ marginLeft: 6 }}>head</span>}</td>
                    <td>{when(rv.created_at)}</td>
                    <td>{rv.branch_of ? `repair branch of ${rv.branch_of}` : rv.hypothetical ? "hypothetical" : ""}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </Panel>
      ))}
    </div>
  );
}
