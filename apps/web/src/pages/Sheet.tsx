import { useCallback, useEffect, useMemo, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { api, ApiError, unwrap } from "../api/client";
import type { Observation, Sheet as SheetT } from "../api/types";
import { ErrorBox, JobStatus, Loading, Panel } from "../components/common";
import { when } from "../lib/format";
import { useJob, useLoad, useReviewer } from "../lib/hooks";

const FLAG_TEXT: Record<string, string> = {
  anchor_inferred_from_bbox: "The model cited no text lines; the anchor is the native text inside its box.",
  no_native_text_anchor: "No native PDF text backs this proposal.",
  label_differs_from_anchor_text: "The model's label does not match the text it sits on.",
  phase_ref_unsupported: "A phase the model named does not appear in the anchored text, so it was dropped.",
  no_line_geometry: "A line feature was proposed without line geometry.",
  dimension_not_in_native_text: "No dimension appears in the anchored text.",
  dimension_label_disagrees_with_anchor: "The model's dimension disagrees with the printed one, so no value was recorded.",
  anchor_outside_bbox: "Cited text lies outside the proposal's box.",
  unknown_line_id: "The model cited a text line that does not exist.",
  no_phase_callout_nearby: "No phase callout points at or sits near this line; its phase is unresolved.",
  phase_from_nearest_callout: "No leader points at this line; the phase comes from the nearest leaderless phase callout and needs review.",
  phase_ambiguous: "Callouts for different phases point at or sit near this line, so no phase was assigned.",
};

function flagText(u: string): string {
  if (u.startsWith("model: ")) return `Model note: ${u.slice(7)}`;
  const key = u.split(":")[0]!;
  if (key.startsWith("bbox_covers_")) return "The box covers too many text lines to anchor to a specific note.";
  return FLAG_TEXT[key] ? `${FLAG_TEXT[key]}${u.includes(":") ? ` (${u.split(":").slice(1).join(":")})` : ""}` : u;
}

const STATE_COLOR: Record<string, string> = { pending: "var(--warn)", accepted: "var(--ok)", edited: "var(--ok)", rejected: "var(--ghost)" };

function Overlay({ sheet, items, selected, onSelect }: { sheet: SheetT; items: Observation[]; selected?: string; onSelect: (id: string) => void }) {
  return (
    <svg viewBox={`0 0 ${sheet.width_pt} ${sheet.height_pt}`} preserveAspectRatio="none" aria-hidden="true">
      {items.map((o) => {
        const [x0 = 0, y0 = 0, x1 = 0, y1 = 0] = o.bbox_page;
        const x = Math.min(x0, x1), y = Math.min(y0, y1), w = Math.abs(x1 - x0), h = Math.abs(y1 - y0);
        const sel = o.id === selected;
        return (
          <g key={o.id} onClick={() => onSelect(o.id)} style={{ cursor: "pointer" }}>
            <rect x={x} y={y} width={Math.max(w, 2)} height={Math.max(h, 2)} fill={sel ? "rgba(29,78,216,0.12)" : "transparent"}
              stroke={sel ? "var(--focus)" : STATE_COLOR[o.review_state]} strokeWidth={sel ? 2 : 1}
              strokeDasharray={o.provenance === "native" ? "3 2" : undefined} vectorEffect="non-scaling-stroke" />
            {o.geometry_page.length > 1 && (
              <polyline points={o.geometry_page.map((p) => p.join(",")).join(" ")} fill="none" stroke={sel ? "var(--focus)" : "var(--bad)"}
                strokeWidth={2} vectorEffect="non-scaling-stroke" />
            )}
          </g>
        );
      })}
    </svg>
  );
}

function Detail({ o, onChanged }: { o: Observation; onChanged: (o: Observation) => void }) {
  const [reviewer] = useReviewer();
  const decisions = useLoad(() => unwrap(api.GET("/api/observations/{observation_id}/decisions", { params: { path: { observation_id: o.id } } })), [o.id, o.revision]);
  const [editing, setEditing] = useState(false);
  const cv = o.current_value as { cls?: string; label?: string; phase_refs?: string[] };
  const [label, setLabel] = useState(cv.label ?? o.label);
  const [phases, setPhases] = useState((cv.phase_refs ?? o.phase_refs).join(", "));
  const [rationale, setRationale] = useState("");
  const [error, setError] = useState<ApiError>();
  useEffect(() => { setEditing(false); setLabel(cv.label ?? o.label); setPhases((cv.phase_refs ?? o.phase_refs).join(", ")); setError(undefined); }, [o.id, o.revision]); // eslint-disable-line react-hooks/exhaustive-deps

  const act = useCallback(async (action: "accept" | "reject" | "edit") => {
    setError(undefined);
    try {
      const res = await unwrap(api.POST("/api/observations/{observation_id}/review", {
        params: { path: { observation_id: o.id } },
        body: { action, expected_revision: o.revision, reviewer, reviewer_kind: "human_ui", rationale,
          edited_value: action === "edit" ? { label, phase_refs: phases.split(",").map((s) => s.trim().toUpperCase()).filter(Boolean) } : null },
      }));
      onChanged(res.observation);
      setRationale("");
    } catch (e) {
      setError(e as ApiError);
    }
  }, [o.id, o.revision, reviewer, rationale, label, phases, onChanged]);

  useEffect(() => {
    const key = (e: KeyboardEvent) => {
      const t = e.target as HTMLElement;
      if (["INPUT", "TEXTAREA", "SELECT"].includes(t.tagName) || e.metaKey || e.ctrlKey || !reviewer) return;
      if (e.key === "a") { e.preventDefault(); void act("accept"); }
      if (e.key === "r") { e.preventDefault(); void act("reject"); }
    };
    window.addEventListener("keydown", key);
    return () => window.removeEventListener("keydown", key);
  }, [act, reviewer]);

  const props = o.properties as { anchor_source?: string; crop_id?: string; value_m?: number; source?: string; binding?: string };
  const found = props.source === "vector" && props.binding
    ? props.binding.startsWith("legend:")
      ? `PDF vector strokes in the style the legend shows for "${props.binding.slice(7)}"`
      : "PDF vector strokes interleaved with X symbols (a drawn fence)"
    : undefined;
  return (
    <section aria-label="Selected observation">
      <div className="spread">
        <h3 style={{ margin: 0 }}>{o.cls.replace(/_/g, " ")}: {cv.label ?? o.label}</h3>
        <span className="tag">{o.review_state}</span>
      </div>
      <dl className="kv" style={{ marginTop: "0.5rem" }}>
        <dt>Provenance</dt><dd>{o.provenance === "model" ? "vision model proposal" : o.provenance}{o.extraction_run_id && <span className="mono small"> ({o.extraction_run_id})</span>}</dd>
        {found && <><dt>Found in</dt><dd>{found}</dd></>}
        {props.anchor_source && <><dt>Text anchor</dt><dd>{props.anchor_source.replace(/_/g, " ")}</dd></>}
        <dt>Anchored text</dt><dd>{o.supporting_text ? <q>{o.supporting_text}</q> : <span className="muted">none</span>}</dd>
        <dt>Phases</dt><dd>{(cv.phase_refs ?? o.phase_refs).join(", ") || <span className="muted">none supported</span>}</dd>
        {props.value_m != null && <><dt>Value</dt><dd>{props.value_m} m</dd></>}
        <dt>Revision</dt><dd>{o.revision}</dd>
      </dl>
      {o.uncertainty.length > 0 && (
        <ul className="small" aria-label="Validation flags">
          {(o.uncertainty as string[]).map((u, i) => <li key={i}>{flagText(u)}</li>)}
        </ul>
      )}
      {!reviewer && <p className="notice warn small">Set "Reviewing as" in the sidebar to record decisions.</p>}
      <label>Rationale (recorded with the decision)<input value={rationale} onChange={(e) => setRationale(e.target.value)} /></label>
      <div className="row" style={{ marginTop: "0.5rem" }}>
        <button onClick={() => act("accept")} disabled={!reviewer}>Accept <span className="kbd">a</span></button>
        <button className="danger" onClick={() => act("reject")} disabled={!reviewer}>Reject <span className="kbd">r</span></button>
        <button onClick={() => setEditing(!editing)} aria-expanded={editing} disabled={!reviewer}>Edit</button>
      </div>
      {editing && (
        <form className="form" style={{ marginTop: "0.5rem" }} onSubmit={(e) => { e.preventDefault(); void act("edit"); }} aria-label="Edit observation">
          <label>Label<input value={label} onChange={(e) => setLabel(e.target.value)} required /></label>
          <label>Phase scope (comma-separated, e.g. PHASE 1)<input value={phases} onChange={(e) => setPhases(e.target.value)} /></label>
          <div><button className="primary" type="submit">Save edit</button></div>
        </form>
      )}
      <ErrorBox error={error} title="Review not recorded" />
      <h3 style={{ marginTop: "0.8rem" }}>Decision history</h3>
      {decisions.data?.length ? (
        <ol className="small">
          {decisions.data.map((d) => (
            <li key={d.id}><strong>{d.action}</strong> by {d.reviewer} <span className="tag">{d.reviewer_kind.replace("_", " ")}</span> {when(d.created_at)}{d.rationale && `: ${d.rationale}`}</li>
          ))}
        </ol>
      ) : <p className="small muted">No decisions yet. The original proposal is preserved either way.</p>}
    </section>
  );
}

function Extraction({ sheetId }: { sheetId: string }) {
  const runs = useLoad(() => unwrap(api.GET("/api/sheets/{sheet_id}/extraction-runs", { params: { path: { sheet_id: sheetId } } })), [sheetId]);
  const [jobId, setJobId] = useState<number>();
  const [error, setError] = useState<ApiError>();
  const job = useJob(jobId, () => runs.reload());
  const start = async () => {
    setError(undefined);
    try {
      const ref = await unwrap(api.POST("/api/sheets/{sheet_id}/extractions", { params: { path: { sheet_id: sheetId } }, body: null }));
      setJobId(ref.job_id);
    } catch (e) {
      setError(e as ApiError);
    }
  };
  return (
    <div>
      <p className="small muted">Runs the local vision model on every crop, one at a time. Finished crops are reused, so rerunning only processes what is missing.</p>
      <button onClick={start} disabled={job?.state === "running" || job?.state === "queued"}>Run live extraction</button>
      <ErrorBox error={error} title="Extraction not started" />
      <JobStatus job={job} label="Extraction" />
      {runs.data && runs.data.length > 0 && (
        <table aria-label="Extraction runs" style={{ marginTop: "0.5rem" }}>
          <thead><tr><th>Crop</th><th>Status</th><th>Model</th><th>Validator</th><th>Proposals</th><th>Time</th></tr></thead>
          <tbody>
            {runs.data.map((r) => (
              <tr key={r.id}>
                <td className="mono small">{r.crop_id.slice(5, 13)}</td>
                <td>{r.status === "succeeded" ? <span className="tag ok">{r.status}</span> : <span className="tag bad" title={r.error ?? ""}>{r.status}</span>}</td>
                <td className="small"><span className="mono" title={r.model_digest}>{r.model}</span><div className="muted">{r.prompt_version}</div></td>
                <td className="small mono">{(r.validation as { validator?: string }).validator ?? "gr-validate/1"}</td>
                <td>{r.observation_count}</td>
                <td className="small">{r.duration_ms ? `${(r.duration_ms / 1000).toFixed(0)} s` : ""}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}

function Calibration({ sheet, onDone }: { sheet: SheetT; onDone: () => void }) {
  const [reviewer] = useReviewer();
  const [rows, setRows] = useState([{ px: "", py: "", sx: "", sy: "", note: "" }, { px: "", py: "", sx: "", sy: "", note: "" }]);
  const [frame, setFrame] = useState("site");
  const [error, setError] = useState<ApiError>();
  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError(undefined);
    try {
      await unwrap(api.POST("/api/sheets/{sheet_id}/calibrations", {
        params: { path: { sheet_id: sheet.id } },
        body: { frame_id: frame, reviewer, reviewer_kind: "human_ui",
          control_points: rows.map((r) => ({ page: [Number(r.px), Number(r.py)] as [number, number], site: [Number(r.sx), Number(r.sy)] as [number, number], note: r.note })) },
      }));
      onDone();
    } catch (err) {
      setError(err as ApiError);
    }
  };
  const set = (i: number, k: keyof (typeof rows)[0], v: string) => setRows(rows.map((r, j) => (j === i ? { ...r, [k]: v } : r)));
  return (
    <div>
      {sheet.calibrations.length > 0 && (
        <ul className="small">
          {sheet.calibrations.map((c) => <li key={c.id}>{c.method} to frame <span className="mono">{c.frame_id}</span>, RMS residual {c.rms_residual_m?.toFixed(3)} m, by {c.reviewer} ({c.reviewer_kind}) {when(c.created_at)}</li>)}
        </ul>
      )}
      <form className="form" onSubmit={submit} aria-label="Add calibration">
        <p className="small muted">Pair points on the sheet (page points, origin top-left) with site coordinates in metres. Distances along the printed graphic scale bar are the usual source; a printed "1 inch = N feet" label can disagree with the bar.</p>
        <table>
          <thead><tr><th>Page x</th><th>Page y</th><th>Site X (m)</th><th>Site Y (m)</th><th>Note</th></tr></thead>
          <tbody>
            {rows.map((r, i) => (
              <tr key={i}>
                {(["px", "py", "sx", "sy"] as const).map((k) => <td key={k}><input aria-label={`Point ${i + 1} ${k}`} type="number" step="any" value={r[k]} onChange={(e) => set(i, k, e.target.value)} required size={7} /></td>)}
                <td><input aria-label={`Point ${i + 1} note`} value={r.note} onChange={(e) => set(i, "note", e.target.value)} /></td>
              </tr>
            ))}
          </tbody>
        </table>
        <div className="row">
          <button type="button" onClick={() => setRows([...rows, { px: "", py: "", sx: "", sy: "", note: "" }])}>Add point</button>
          <label>Frame id<input value={frame} onChange={(e) => setFrame(e.target.value)} required size={10} /></label>
          <button className="primary" type="submit" disabled={!reviewer}>Save calibration</button>
        </div>
        <ErrorBox error={error} title="Calibration failed" />
      </form>
    </div>
  );
}

export function SheetPage() {
  const { sheetId = "" } = useParams();
  const sheet = useLoad(() => unwrap(api.GET("/api/sheets/{sheet_id}", { params: { path: { sheet_id: sheetId } } })), [sheetId]);
  const [prov, setProv] = useState("");
  const [state, setState] = useState("");
  const [cls, setCls] = useState("");
  const obs = useLoad(() => unwrap(api.GET("/api/sheets/{sheet_id}/observations", {
    params: { path: { sheet_id: sheetId }, query: { provenance: prov || null, review_state: state || null, cls: cls || null, limit: 500 } },
  })), [sheetId, prov, state, cls]);
  const [items, setItems] = useState<Observation[]>([]);
  const [selected, setSelected] = useState<string>();
  const [zoom, setZoom] = useState(1);
  useEffect(() => { if (obs.data) setItems(obs.data.items); }, [obs.data]);
  const idx = useMemo(() => items.findIndex((o) => o.id === selected), [items, selected]);
  const current = idx >= 0 ? items[idx] : undefined;

  useEffect(() => {
    const key = (e: KeyboardEvent) => {
      const t = e.target as HTMLElement;
      if (["INPUT", "TEXTAREA", "SELECT"].includes(t.tagName) || e.metaKey || e.ctrlKey) return;
      if (e.key === "j" || e.key === "k") {
        e.preventDefault();
        const n = items.length;
        if (!n) return;
        const next = idx < 0 ? 0 : (idx + (e.key === "j" ? 1 : n - 1)) % n;
        setSelected(items[next]!.id);
        document.getElementById(`obs-${items[next]!.id}`)?.scrollIntoView({ block: "nearest" });
      }
    };
    window.addEventListener("keydown", key);
    return () => window.removeEventListener("keydown", key);
  }, [items, idx]);

  const changed = useCallback((o: Observation) => setItems((xs) => xs.map((x) => (x.id === o.id ? o : x))), []);
  if (sheet.error) return <ErrorBox error={sheet.error} title="Sheet not found" />;
  if (!sheet.data) return <Loading what="sheet" />;
  const s = sheet.data;
  const counts = items.reduce<Record<string, number>>((m, o) => ({ ...m, [o.review_state]: (m[o.review_state] ?? 0) + 1 }), {});

  return (
    <div>
      <p className="small muted" style={{ margin: 0 }}><Link to="/">Sources</Link> / {s.plan_revision_id}</p>
      <h1>{s.printed_sheet_id ? `Sheet ${s.printed_sheet_id}` : `Page ${s.page_index + 1}`}</h1>
      <p className="small muted">{s.width_pt} x {s.height_pt} pt, rotation {s.rotation}, rendered at {s.render_dpi} dpi, {s.crops.length} crops, {s.word_count} native words.</p>
      <div className="grid2">
        <Panel id="sheet-h" title="Source" actions={
          <div className="row" role="group" aria-label="Zoom">
            {[1, 2, 3].map((z) => <button key={z} className="small" aria-pressed={zoom === z} onClick={() => setZoom(z)}>{z * 100}%</button>)}
          </div>}>
          <div className="sheet-view">
            <div style={{ width: `${zoom * 100}%`, position: "relative" }}>
              <img src={`/api/sheets/${encodeURIComponent(s.id)}/render`} alt={`Rendered sheet ${s.printed_sheet_id ?? s.id}; observations are listed beside it`} />
              <Overlay sheet={s} items={items} selected={selected} onSelect={setSelected} />
            </div>
          </div>
          <p className="legend">
            <span><span aria-hidden="true" style={{ color: "var(--warn)" }}>{"□"}</span> pending</span>
            <span><span aria-hidden="true" style={{ color: "var(--ok)" }}>{"□"}</span> accepted or edited</span>
            <span><span aria-hidden="true" style={{ color: "var(--ghost)" }}>{"□"}</span> rejected</span>
            <span>dashed boxes: native PDF text or vector strokes; solid boxes: model or human proposal</span>
          </p>
        </Panel>
        <div>
          <Panel id="obs-h" title={`Observations (${items.length})`} actions={<span className="small muted">{Object.entries(counts).map(([k, v]) => `${v} ${k}`).join(", ")}</span>}>
            <div className="row" style={{ marginBottom: "0.5rem" }}>
              <label>Provenance<select value={prov} onChange={(e) => setProv(e.target.value)}><option value="">all</option><option value="model">model</option><option value="native">native text and vectors</option><option value="human">human</option></select></label>
              <label>Review<select value={state} onChange={(e) => setState(e.target.value)}><option value="">all</option><option>pending</option><option>accepted</option><option>edited</option><option>rejected</option></select></label>
              <label>Class<select value={cls} onChange={(e) => setCls(e.target.value)}><option value="">all</option>{["path", "barrier", "crossing", "entrance", "note", "phase_label", "legend_entry", "dimension", "place_label"].map((c) => <option key={c}>{c}</option>)}</select></label>
            </div>
            <p className="small muted">Keys: <span className="kbd">j</span>/<span className="kbd">k</span> next/previous, <span className="kbd">a</span> accept, <span className="kbd">r</span> reject.</p>
            <ErrorBox error={obs.error} />
            <div className="obs-list">
              <table aria-label="Observations">
                <thead><tr><th>Class</th><th>Label</th><th>Status</th></tr></thead>
                <tbody>
                  {items.map((o) => (
                    <tr key={o.id} id={`obs-${o.id}`} aria-selected={o.id === selected}>
                      <td>{o.cls.replace(/_/g, " ")}<div className="small muted">{o.provenance}</div></td>
                      <td><button className="small" style={{ textAlign: "left", border: 0, background: "transparent", padding: 0 }} onClick={() => setSelected(o.id)} aria-pressed={o.id === selected}>
                        {((o.current_value as { label?: string }).label ?? o.label).slice(0, 70) || "(no label)"}</button>
                        {(o.uncertainty as string[]).some((u) => !u.startsWith("model:")) && <span className="tag warn" style={{ marginLeft: 4 }}>{(o.uncertainty as string[]).filter((u) => !u.startsWith("model:")).length} flags</span>}
                      </td>
                      <td><span className="tag">{o.review_state}</span></td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </Panel>
          {current && <Panel id="detail-h" title="Review"><Detail o={current} onChanged={changed} /></Panel>}
        </div>
      </div>
      <div className="grid2">
        <Panel id="extract-h" title="Live extraction"><Extraction sheetId={s.id} /></Panel>
        <Panel id="cal-h" title="Calibration"><Calibration sheet={s} onDone={sheet.reload} /></Panel>
      </div>
    </div>
  );
}
