import { useState } from "react";
import { useSearchParams } from "react-router-dom";
import { api, ApiError, unwrap } from "../api/client";
import type { Diff, Replay } from "../api/types";
import { ErrorBox, Loading, OutcomeBadge, Panel } from "../components/common";
import { useLoad } from "../lib/hooks";

type Kind = "scenario" | "analysis" | "network" | "project";

function DiffTable({ diff, title }: { diff: Diff; title: string }) {
  return (
    <section aria-label={title} style={{ marginBottom: "1rem" }}>
      <h3>{title} <span className="small muted">{Object.entries(diff.summary).map(([k, v]) => `${v} ${k}`).join(", ") || "no changes"}</span></h3>
      {diff.changes.length > 0 && (
        <table>
          <thead><tr><th>Entity</th><th>Change</th><th>From</th><th>To</th></tr></thead>
          <tbody>
            {diff.changes.map((c, i) => {
              const ch = c as Record<string, unknown>;
              const show = (v: unknown) => typeof v === "string" && /^[A-Z_]+$/.test(v) && v in { SUPPORTED: 1, NEEDS_VERIFICATION: 1, INSUFFICIENT_COVERAGE: 1, DISCONNECTED_WITHIN_COVERAGE: 1 }
                ? <OutcomeBadge outcome={v as never} /> : <span className="mono small">{v === undefined || v === null ? "" : typeof v === "string" ? v : JSON.stringify(v)}</span>;
              return (
                <tr key={i}>
                  <td><span className="tag">{String(ch.entity)}</span> <span className="mono small">{String(ch.id ?? "")}</span></td>
                  <td>{String(ch.change ?? "")}{ch.field ? `: ${String(ch.field)}` : ""}</td>
                  <td>{show(ch.from)}</td><td>{show(ch.to)}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      )}
    </section>
  );
}

async function diff(kind: "analysis" | "network" | "project", a: string, b: string): Promise<Diff> {
  return unwrap(api.GET("/api/diff/{kind}", { params: { path: { kind }, query: { from_id: a, to_id: b } } }));
}

/** Two scenario revisions: their latest analyses, network revisions and shared projects. */
async function scenarioDiffs(a: string, b: string): Promise<{ title: string; diff: Diff }[]> {
  const get = (id: string) => unwrap(api.GET("/api/scenario-revisions/{revision_id}", { params: { path: { revision_id: id } } }));
  const run = (id: string) => unwrap(api.GET("/api/scenario-revisions/{revision_id}/analysis", { params: { path: { revision_id: id } } }));
  const [ra, rb, aa, ab] = await Promise.all([get(a), get(b), run(a), run(b)]);
  const out: { title: string; diff: Diff }[] = [];
  if (aa && ab) out.push({ title: "Analysis outcomes", diff: await diff("analysis", aa.id, ab.id) });
  if (ra.network_revision_id !== rb.network_revision_id) out.push({ title: "Network", diff: await diff("network", ra.network_revision_id, rb.network_revision_id) });
  const byProject = (ids: string[]) => new Map(ids.map((x) => [x.split("@")[0]!, x]));
  const pa = byProject(ra.project_revision_ids), pb = byProject(rb.project_revision_ids);
  for (const [pid, x] of pa) {
    const y = pb.get(pid);
    if (y && y !== x) out.push({ title: `Project ${pid}`, diff: await diff("project", x, y) });
  }
  if (!aa || !ab) out.push({ title: "Analysis outcomes", diff: { kind: "analysis", from_id: a, to_id: b, changes: [], summary: { "unavailable: run the analysis on both revisions": 1 } } });
  return out;
}

function Replayer() {
  const [result, setResult] = useState<Replay>();
  const [error, setError] = useState<ApiError>();
  const [busy, setBusy] = useState(false);
  const submit = async (e: React.FormEvent<HTMLFormElement>) => {
    e.preventDefault();
    setBusy(true); setError(undefined); setResult(undefined);
    try {
      const res = await fetch("/api/bundles/replay", { method: "POST", body: new FormData(e.currentTarget) });
      const body = await res.json();
      if (!res.ok) throw new ApiError(res.status, body.error?.code ?? "http_error", body.error?.message ?? res.statusText, body.error?.detail);
      setResult(body as Replay);
    } catch (err) {
      setError(err instanceof ApiError ? err : new ApiError(0, "network", (err as Error).message));
    } finally {
      setBusy(false);
    }
  };
  return (
    <div>
      <form className="row" onSubmit={submit} aria-label="Replay a bundle">
        <label>Bundle (.zip)<input type="file" name="file" accept=".zip,application/zip" required /></label>
        <button className="primary" type="submit" disabled={busy}>{busy ? "Replaying..." : "Replay"}</button>
      </form>
      <p className="small muted">Replay validates hashes and paths, then reruns the pure verifier on the bundled inputs. It needs no database records or model. It checks the recorded model, not the site.</p>
      <ErrorBox error={error} title="Bundle rejected" />
      {result && (
        <div className={`notice ${result.matches ? "ok" : "error"}`} role="status">
          <strong>{result.matches ? "✓ Replay matches the recorded result" : "✕ Replay differs from the recorded result"}</strong>
          <dl className="kv" style={{ marginTop: "0.4rem" }}>
            <dt>Input hash</dt><dd className="mono">{result.canonical_input_hash}</dd>
            <dt>Verifier</dt><dd className="mono">{result.verifier_version} (recorded {result.recorded_verifier_version})</dd>
            <dt>Outcomes</dt><dd>{Object.entries(result.outcomes).map(([k, v]) => <span key={k} style={{ marginRight: "0.8rem" }}>{k} <OutcomeBadge outcome={v as never} /></span>)}</dd>
            {result.differences.length > 0 && <><dt>Differences</dt><dd className="mono small">{JSON.stringify(result.differences)}</dd></>}
            {result.omissions.length > 0 && <><dt>Omitted</dt><dd className="small">{result.omissions.map((o, i) => <div key={i}>{String((o as { reason?: string }).reason ?? JSON.stringify(o))}</div>)}</dd></>}
          </dl>
        </div>
      )}
    </div>
  );
}

export function Compare() {
  const [params, setParams] = useSearchParams();
  const kind = (params.get("kind") as Kind) ?? "scenario";
  const a = params.get("a") ?? "";
  const b = params.get("b") ?? "";
  const [fa, setFa] = useState(a), [fb, setFb] = useState(b), [fk, setFk] = useState<Kind>(kind);
  const result = useLoad(async () => {
    if (!a || !b) return [];
    return kind === "scenario" ? scenarioDiffs(a, b) : [{ title: `${kind} diff`, diff: await diff(kind, a, b) }];
  }, [kind, a, b]);
  return (
    <div>
      <h1>Compare and replay</h1>
      <Panel id="cmp-h" title="Compare revisions">
        <form className="row" onSubmit={(e) => { e.preventDefault(); setParams({ kind: fk, a: fa, b: fb }); }} aria-label="Choose revisions to compare">
          <label>Kind<select value={fk} onChange={(e) => setFk(e.target.value as Kind)}>
            <option value="scenario">scenario revisions</option><option value="analysis">analysis runs</option>
            <option value="network">network revisions</option><option value="project">project revisions</option></select></label>
          <label>From<input value={fa} onChange={(e) => setFa(e.target.value)} required className="mono" /></label>
          <label>To<input value={fb} onChange={(e) => setFb(e.target.value)} required className="mono" /></label>
          <button type="submit">Compare</button>
        </form>
        <div style={{ marginTop: "0.75rem" }}>
          <ErrorBox error={result.error} />
          {a && b && result.loading && <Loading what="differences" />}
          {result.data?.map((d) => <DiffTable key={d.title} title={d.title} diff={d.diff} />)}
        </div>
      </Panel>
      <Panel id="replay-h" title="Replay a bundle"><Replayer /></Panel>
    </div>
  );
}
