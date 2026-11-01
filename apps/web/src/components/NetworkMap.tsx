import type { NetworkSnapshot, PairResult, ProjectSnapshot, StateResult } from "../api/types";

type Props = {
  network: NetworkSnapshot;
  projects: ProjectSnapshot[];
  state?: StateResult;
  pair?: PairResult;
  /** Edges a repair keeps open or adds, drawn as the verified repair. */
  repairEdges?: string[];
  title: string;
};

/** Restriction targets active in a state: project -> active phase -> restriction edge targets. */
export function closedEdges(projects: ProjectSnapshot[], state?: StateResult): Map<string, string> {
  const out = new Map<string, string>();
  if (!state) return out;
  for (const p of projects) {
    const phase = state.active[p.project_id];
    if (!phase) continue;
    for (const r of p.restrictions ?? []) {
      if (!r.phase_ids.includes(phase) || r.state === "REJECTED") continue;
      for (const t of r.targets ?? []) out.set(t.edge_id, r.label || r.id);
    }
  }
  return out;
}

export function NetworkMap({ network, projects, state, pair, repairEdges = [], title }: Props) {
  const pts = network.edges.flatMap((e) => e.geometry).concat(network.nodes.map((n) => [n.x, n.y] as [number, number]));
  const xs = pts.map((p) => p[0]);
  const ys = pts.map((p) => p[1]);
  const pad = 14;
  const minX = Math.min(...xs) - pad, maxX = Math.max(...xs) + pad, minY = Math.min(...ys) - pad, maxY = Math.max(...ys) + pad;
  const W = 760;
  const LABEL_ROOM = 150; // space right of the network for node labels
  const k = (W - LABEL_ROOM) / (maxX - minX);
  const H = Math.max(160, (maxY - minY) * k);
  // Site frame has y up; SVG has y down.
  const X = (x: number) => (x - minX) * k;
  const Y = (y: number) => H - (y - minY) * k;
  const path = (g: number[][]) => g.map((p, i) => `${i ? "L" : "M"}${X(p[0]!).toFixed(1)},${Y(p[1]!).toFixed(1)}`).join(" ");
  const byId = new Map(network.edges.map((e) => [e.id, e]));

  const closed = closedEdges(projects, state);
  const routeIds = new Set(pair?.route?.steps.map((s) => s.edge_id) ?? []);
  const ghostIds = new Set((pair?.baseline_route?.steps ?? []).map((s) => s.edge_id).filter((id) => !routeIds.has(id)));
  const boundary = new Set(pair?.boundary?.map((b) => b.edge_id) ?? []);
  const uncertain = new Set(
    pair?.route?.steps.filter((s) => s.edge_class !== "CONSERVATIVE").map((s) => s.edge_id) ?? [],
  );
  const repair = new Set(repairEdges);
  const endpoints = new Set([pair?.origin, pair?.destination].filter(Boolean) as string[]);

  const summary = [
    `${network.edges.length} segments`,
    closed.size ? `${closed.size} closed in this state (${[...closed.keys()].join(", ")})` : "no closures in this state",
    pair?.route ? `route uses ${pair.route.steps.map((s) => s.edge_id).join(", ")}` : pair ? "no route found" : "",
    ghostIds.size ? `prior route segments now unused: ${[...ghostIds].join(", ")}` : "",
  ].filter(Boolean).join("; ");

  return (
    <figure style={{ margin: 0 }}>
      <svg className="map" viewBox={`0 0 ${W} ${H.toFixed(0)}`} role="img" aria-label={`${title}. ${summary}`}>
        {network.edges.map((e) => (
          <path key={e.id} d={path(e.geometry)} fill="none" stroke="var(--rule-strong)" strokeWidth={3}
            strokeDasharray={e.existence === "UNRESOLVED" ? "2 4" : undefined} strokeLinecap="round">
            <title>{`${e.id} ${e.label ?? ""} (${e.kind ?? "segment"}${e.existence === "UNRESOLVED" ? ", existence unreviewed" : ""})`}</title>
          </path>
        ))}
        {[...ghostIds].map((id) => byId.get(id) && (
          <path key={`g-${id}`} className="ghost-draw" d={path(byId.get(id)!.geometry)} fill="none" stroke="var(--ghost)"
            strokeWidth={9} strokeLinecap="round" opacity={0.6} />
        ))}
        {[...routeIds].map((id) => byId.get(id) && (
          <path key={`r-${id}`} d={path(byId.get(id)!.geometry)} fill="none"
            stroke={repair.has(id) ? "var(--ok)" : uncertain.has(id) ? "var(--warn)" : "var(--ink)"} strokeWidth={5}
            strokeDasharray={uncertain.has(id) ? "8 4" : undefined} strokeLinecap="round" />
        ))}
        {[...closed.keys()].map((id) => {
          const e = byId.get(id);
          if (!e) return null;
          const mid = e.geometry[Math.floor(e.geometry.length / 2)]!;
          const [cx, cy] = e.geometry.length === 2
            ? [(X(e.geometry[0]![0]) + X(e.geometry[1]![0])) / 2, (Y(e.geometry[0]![1]) + Y(e.geometry[1]![1])) / 2]
            : [X(mid[0]), Y(mid[1])];
          return (
            <g key={`c-${id}`} stroke={boundary.has(id) || ghostIds.has(id) ? "var(--bad)" : "var(--ink-2)"} strokeWidth={3}>
              <line x1={cx - 7} y1={cy - 7} x2={cx + 7} y2={cy + 7} />
              <line x1={cx - 7} y1={cy + 7} x2={cx + 7} y2={cy - 7} />
              <title>{`${id} closed: ${closed.get(id)}`}</title>
            </g>
          );
        })}
        {network.edges.map((e) => {
          const m = e.geometry[Math.floor((e.geometry.length - 1) / 2)]!;
          const n = e.geometry[Math.min(e.geometry.length - 1, Math.floor((e.geometry.length - 1) / 2) + 1)]!;
          return (
            <text key={`t-${e.id}`} x={(X(m[0]) + X(n[0])) / 2 + 4} y={(Y(m[1]) + Y(n[1])) / 2 - 5} className="mono"
              style={{ fontSize: 10, fill: "var(--ink-2)" }}>{e.id}</text>
          );
        })}
        {network.nodes.map((n) => (
          <g key={n.id}>
            <circle cx={X(n.x)} cy={Y(n.y)} r={endpoints.has(n.id) ? 7 : 4} fill={endpoints.has(n.id) ? "var(--ink)" : "var(--panel)"}
              stroke="var(--ink)" strokeWidth={1.5} />
            <text x={X(n.x) + 9} y={Y(n.y) + 4}>{n.label ? `${n.id} ${n.label}` : n.id}</text>
          </g>
        ))}
      </svg>
      <figcaption className="legend">
        <span><svg width="26" height="8" aria-hidden="true"><line x1="1" y1="4" x2="25" y2="4" stroke="var(--ink)" strokeWidth="4" /></svg>verified route</span>
        <span><svg width="26" height="8" aria-hidden="true"><line x1="1" y1="4" x2="25" y2="4" stroke="var(--warn)" strokeWidth="4" strokeDasharray="6 3" /></svg>route over unresolved evidence</span>
        <span><svg width="26" height="8" aria-hidden="true"><line x1="1" y1="4" x2="25" y2="4" stroke="var(--ghost)" strokeWidth="6" opacity="0.5" /></svg>prior route (ghost)</span>
        <span><svg width="14" height="14" aria-hidden="true"><path d="M2 2 L12 12 M2 12 L12 2" stroke="var(--bad)" strokeWidth="2.5" /></svg>closed in this state</span>
        <span><svg width="26" height="8" aria-hidden="true"><line x1="1" y1="4" x2="25" y2="4" stroke="var(--rule-strong)" strokeWidth="3" strokeDasharray="2 4" /></svg>existence unreviewed</span>
        {repair.size > 0 && <span><svg width="26" height="8" aria-hidden="true"><line x1="1" y1="4" x2="25" y2="4" stroke="var(--ok)" strokeWidth="4" /></svg>kept open by repair</span>}
      </figcaption>
    </figure>
  );
}
