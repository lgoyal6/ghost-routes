import { api, unwrap } from "../api/client";
import { useLoad } from "../lib/hooks";

/** Compact system status for the sidebar: database, migrations, worker, and the live model. */
export function Readiness() {
  const r = useLoad(() => unwrap(api.GET("/api/readiness")), []);
  if (r.error) return <p className="small" role="alert">API unreachable. Run <code>make dev</code>.</p>;
  if (!r.data) return <p className="small muted">Checking services...</p>;
  const d = r.data;
  const db = d.database as { ok?: boolean; postgis?: string };
  const mig = d.migrations as { ok?: boolean; current?: string | null };
  const worker = d.worker as { recently_active?: boolean; queued?: number; running?: number };
  const model = d.model;
  const inf = d.last_successful_inference as { at?: string } | null;
  const rows: [string, boolean, string][] = [
    ["Database", Boolean(db.ok), db.ok ? `up (PostGIS ${db.postgis ?? "?"})` : "down"],
    ["Migrations", Boolean(mig.ok), mig.ok ? `at ${mig.current}` : "not current"],
    ["Worker", Boolean(worker.recently_active), `${worker.recently_active ? "active" : "idle or stopped"}, ${worker.queued ?? 0} queued`],
    ["Model", model.state === "ready", model.state === "ready" ? model.model : model.state.replace(/_/g, " ")],
  ];
  return (
    <section aria-label="Service status" className="small">
      <ul style={{ listStyle: "none", padding: 0, margin: 0 }}>
        {rows.map(([name, ok, text]) => (
          <li key={name} className="row" style={{ gap: "0.4rem" }}>
            <span aria-hidden="true" style={{ color: ok ? "var(--ok)" : "var(--bad)" }}>{ok ? "●" : "○"}</span>
            <span>{name}:</span> <span className="muted">{text}</span>
          </li>
        ))}
      </ul>
      {inf?.at && <p className="muted" style={{ margin: "0.3rem 0 0" }}>Last live inference {new Date(inf.at).toLocaleString()}</p>}
      {model.digest && <p className="mono muted" title={model.digest} style={{ margin: "0.3rem 0 0" }}>digest {model.digest.slice(0, 12)}</p>}
      <button className="small" style={{ marginTop: "0.4rem" }} onClick={r.reload}>Recheck</button>
    </section>
  );
}
