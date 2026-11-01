"""The pure access verifier.

Inputs are a fully materialized :class:`AnalysisInput`; outputs are typed
results. No persistence, model calls or network access happen here.
"""

from __future__ import annotations

import heapq
import math
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from itertools import islice, pairwise

import networkx as nx
from shapely.geometry import LineString

from ghost_routes.domain.canonical import content_hash
from ghost_routes.domain.model import (
    OUTCOME_SEVERITY,
    VERIFIER_VERSION,
    AccessRequirement,
    AnalysisInput,
    Edge,
    EdgeKind,
    Interval,
    MobilityProfile,
    NetworkSnapshot,
    Outcome,
    StateBasis,
)
from ghost_routes.domain.results import (
    AnalysisResult,
    Blocker,
    CoverageGap,
    DepartureGap,
    EdgeAssessment,
    EdgeClass,
    Fact,
    FactKind,
    GapKind,
    PairResult,
    RejectedAlternative,
    RequirementResult,
    Route,
    RouteStep,
    StateResult,
)
from ghost_routes.scenarios.compiler import (
    baseline_state,
    compile_states,
)
from ghost_routes.verification.classify import (
    ClassifiedState,
    GeometryIndex,
    classify_state,
)
from ghost_routes.verification.intervals import IntervalSet, traversal_departures

TIERS: tuple[tuple[str, frozenset[EdgeClass], Outcome], ...] = (
    ("conservative", frozenset({EdgeClass.CONSERVATIVE}), Outcome.SUPPORTED),
    (
        "possible_covered",
        frozenset({EdgeClass.CONSERVATIVE, EdgeClass.POSSIBLE_COVERED}),
        Outcome.NEEDS_VERIFICATION,
    ),
    (
        "possible",
        frozenset({EdgeClass.CONSERVATIVE, EdgeClass.POSSIBLE_COVERED, EdgeClass.POSSIBLE_UNCOVERED}),
        Outcome.INSUFFICIENT_COVERAGE,
    ),
)
POSSIBLE = TIERS[2][1]
TEMPORAL_LABEL_CAP = 200_000


def edge_length(edge: Edge, network: NetworkSnapshot) -> float | None:
    if edge.length_m is not None:
        return edge.length_m
    if network.frame.metric:
        return LineString(edge.geometry).length
    return None


def _frame_length(edge: Edge) -> float:
    return LineString(edge.geometry).length


@dataclass(frozen=True)
class Arc:
    edge_id: str
    src: str
    dst: str


def arcs_of(edge: Edge) -> list[Arc]:
    arcs = [Arc(edge.id, edge.u, edge.v)]
    if not edge.directed:
        arcs.append(Arc(edge.id, edge.v, edge.u))
    return arcs


class Graph:
    """Deterministic adjacency over a subset of classified edges."""

    def __init__(self, cs: ClassifiedState, network: NetworkSnapshot, classes: frozenset[EdgeClass]):
        self.cs = cs
        self.network = network
        self.adj: dict[str, list[Arc]] = {}
        for eid in sorted(cs.edges):
            if cs.assessments[eid].edge_class not in classes:
                continue
            for arc in arcs_of(cs.edges[eid]):
                self.adj.setdefault(arc.src, []).append(arc)
        for lst in self.adj.values():
            lst.sort(key=lambda a: (a.dst, a.edge_id))

    def weight(self, eid: str, *, by_length: bool) -> float:
        e = self.cs.edges[eid]
        base = edge_length(e, self.network)
        if base is None:
            base = _frame_length(e)
        if by_length:
            return base
        return base * (1.0 + self.cs.assessments[eid].soft_penalty)

    def shortest(self, sources: Iterable[str], target: str, *, by_length: bool = False) -> list[Arc] | None:
        """Dijkstra with deterministic tie-breaking by predecessor edge id."""
        dist: dict[str, float] = {}
        pred: dict[str, Arc | None] = {}
        heap: list[tuple[float, str]] = []
        for s in sorted(set(sources)):
            dist[s] = 0.0
            pred[s] = None
            heap.append((0.0, s))
        heapq.heapify(heap)
        done: set[str] = set()
        while heap:
            d, n = heapq.heappop(heap)
            if n in done:
                continue
            done.add(n)
            if n == target:
                break
            for arc in self.adj.get(n, ()):
                nd = d + self.weight(arc.edge_id, by_length=by_length)
                cur = dist.get(arc.dst)
                if cur is None or nd < cur - 1e-12 or (
                    abs(nd - cur) <= 1e-12 and (p := pred.get(arc.dst)) is not None and arc.edge_id < p.edge_id
                ):
                    dist[arc.dst] = nd
                    pred[arc.dst] = arc
                    heapq.heappush(heap, (nd, arc.dst))
        if target not in done:
            return None
        path: list[Arc] = []
        n = target
        while (step := pred[n]) is not None:
            path.append(step)
            n = step.src
        return list(reversed(path))

    def reachable(self, start: str) -> set[str]:
        seen = {start}
        stack = [start]
        while stack:
            n = stack.pop()
            for arc in self.adj.get(n, ()):
                if arc.dst not in seen:
                    seen.add(arc.dst)
                    stack.append(arc.dst)
        return seen

    def reverse_reachable(self, start: str) -> set[str]:
        radj: dict[str, list[str]] = {}
        for lst in self.adj.values():
            for a in lst:
                radj.setdefault(a.dst, []).append(a.src)
        seen = {start}
        stack = [start]
        while stack:
            n = stack.pop()
            for m in radj.get(n, ()):
                if m not in seen:
                    seen.add(m)
                    stack.append(m)
        return seen

    def disjoint_paths(self, s: str, t: str) -> int:
        g: nx.DiGraph[str] = nx.DiGraph()
        for lst in self.adj.values():
            for a in lst:
                # Parallel edges add capacity between the same nodes.
                if g.has_edge(a.src, a.dst):
                    g[a.src][a.dst]["capacity"] += 1
                else:
                    g.add_edge(a.src, a.dst, capacity=1)
        if s not in g or t not in g:
            return 0
        return int(nx.maximum_flow_value(g, s, t))


def build_route(path: list[Arc], cs: ClassifiedState, network: NetworkSnapshot, graph: Graph) -> Route:
    steps = []
    total: float | None = 0.0
    cost = 0.0
    crossings = 0
    for arc in path:
        e = cs.edges[arc.edge_id]
        a = cs.assessments[arc.edge_id]
        ln = edge_length(e, network)
        total = None if (total is None or ln is None) else total + ln
        cost += graph.weight(arc.edge_id, by_length=False)
        crossings += e.kind == EdgeKind.CROSSING
        steps.append(
            RouteStep(
                edge_id=e.id,
                from_node=arc.src,
                to_node=arc.dst,
                kind=e.kind,
                length_m=ln,
                edge_class=a.edge_class,
                checks=a.checks,
                evidence=a.evidence,
                temporary=e.id in cs.temporary,
            )
        )
    nodes = tuple([path[0].src] + [a.dst for a in path]) if path else ()
    return Route(nodes=nodes, steps=tuple(steps), length_m=total, cost=round(cost, 9), crossings=crossings)


def _unique_facts(assessments: Iterable[EdgeAssessment]) -> tuple[Fact, ...]:
    out: dict[str, Fact] = {}
    for a in assessments:
        for f in a.facts:
            out.setdefault(f.id, f)
    return tuple(sorted(out.values(), key=lambda f: f.id))


def _unique_gaps(gaps: Iterable[CoverageGap]) -> tuple[CoverageGap, ...]:
    out = {(g.kind, g.target, g.project_id): g for g in gaps}
    return tuple(out[k] for k in sorted(out, key=lambda k: (k[0].value, k[1], k[2] or "")))


@dataclass
class Context:
    inp: AnalysisInput
    geo: GeometryIndex
    profile: MobilityProfile
    requirement: AccessRequirement
    baseline: ClassifiedState


def budget_cap(req: AccessRequirement, baseline_len: float | None) -> float | None:
    if baseline_len is None:
        return None
    caps = []
    if req.max_added_distance_m is not None:
        caps.append(baseline_len + req.max_added_distance_m)
    if req.max_detour_ratio is not None:
        caps.append(baseline_len * req.max_detour_ratio)
    return min(caps) if caps else None


def evaluate_pair(ctx: Context, cs: ClassifiedState, origin: str, dest: str) -> PairResult:
    network = ctx.inp.network
    req = ctx.requirement
    nodes = {n.id: n for n in network.nodes}
    notes: list[str] = []

    # Baseline comparison route (no project overlays, same profile, conservative).
    base_graph = Graph(ctx.baseline, network, TIERS[0][1])
    base_path = base_graph.shortest([origin], dest, by_length=True) if origin != dest else []
    baseline_route = build_route(base_path, ctx.baseline, network, base_graph) if base_path else None
    baseline_len = baseline_route.length_m if baseline_route else (0.0 if origin == dest else None)

    wants_budget = req.max_added_distance_m is not None or req.max_detour_ratio is not None
    budget_facts: list[Fact] = []
    cap: float | None = None
    if wants_budget:
        if not network.frame.metric:
            budget_facts.append(Fact(id="network:uncalibrated", kind=FactKind.UNCALIBRATED_GEOMETRY, target=network.revision_id,
                                     description="detour budget needs calibrated metric geometry"))
        elif baseline_len is None:
            budget_facts.append(Fact(id=f"baseline:{origin}->{dest}", kind=FactKind.BASELINE_UNAVAILABLE, target=f"{origin}->{dest}",
                                     description="no supported baseline route to compare the detour against"))
        else:
            cap = budget_cap(req, baseline_len)

    endpoint_gaps = [
        CoverageGap(kind=GapKind.ENDPOINT_OUTSIDE, target=n, description=f"{n} lies outside reviewed network coverage")
        for n in (origin, dest)
        if not ctx.geo.node_covered(nodes[n].x, nodes[n].y)
    ]

    exceeded: list[str] = []
    for tier_name, classes, tier_outcome in TIERS:
        g = Graph(cs, network, classes)
        if origin == dest:
            path: list[Arc] | None = []
        else:
            path = g.shortest([origin], dest)
        if path is None:
            continue
        route = build_route(path, cs, network, g)
        if cap is not None and route.length_m is not None and route.length_m > cap + 1e-9:
            alt = g.shortest([origin], dest, by_length=True)
            alt_route = build_route(alt, cs, network, g) if alt else None
            if alt_route is None or alt_route.length_m is None or alt_route.length_m > cap + 1e-9:
                exceeded.append(tier_name)
                continue
            route = alt_route
            notes.append(f"{tier_name}: lowest-cost route exceeded the detour budget; reporting the shortest route")
        alts = None
        if req.required_alternatives > 1:
            alts = g.disjoint_paths(origin, dest)
            if alts < req.required_alternatives:
                notes.append(f"{tier_name}: {alts} edge-disjoint routes < required {req.required_alternatives}")
                continue
        outcome = tier_outcome
        step_assess = [cs.assessments[s.edge_id] for s in route.steps]
        facts = list(_unique_facts(step_assess))
        gaps = [gp for a in step_assess for gp in a.gaps]
        if endpoint_gaps:
            outcome = Outcome.INSUFFICIENT_COVERAGE
            gaps += endpoint_gaps
        if outcome == Outcome.SUPPORTED and budget_facts:
            outcome = Outcome.NEEDS_VERIFICATION
        facts += budget_facts
        added = (
            route.length_m - baseline_len
            if route.length_m is not None and baseline_len is not None
            else None
        )
        return PairResult(
            origin=origin,
            destination=dest,
            outcome=outcome,
            route=route,
            baseline_route=baseline_route,
            added_distance_m=round(added, 6) if added is not None else None,
            added_crossings=(route.crossings - baseline_route.crossings) if baseline_route else None,
            budget_m=cap,
            budget_exceeded_tiers=tuple(exceeded),
            alternatives_found=alts,
            uncertainties=tuple(sorted({f.id: f for f in facts}.values(), key=lambda f: f.id)),
            coverage_gaps=_unique_gaps(gaps),
            rejected_alternatives=rejected_alternatives(cs, network, origin, dest) if outcome != Outcome.SUPPORTED else (),
            notes=tuple(notes),
        )

    # No satisfying route even in the permissive graph.
    possible = Graph(cs, network, POSSIBLE)
    bounded, gaps = disconnection_bounded(ctx, cs, possible, origin, dest)
    gaps = endpoint_gaps + gaps
    outcome = Outcome.DISCONNECTED_WITHIN_COVERAGE if bounded and not endpoint_gaps else Outcome.INSUFFICIENT_COVERAGE
    if exceeded:
        notes.append("routes exist but every candidate exceeds the detour budget in tiers: " + ", ".join(exceeded))
    return PairResult(
        origin=origin,
        destination=dest,
        outcome=outcome,
        baseline_route=baseline_route,
        budget_m=cap,
        budget_exceeded_tiers=tuple(exceeded),
        uncertainties=tuple(budget_facts),
        coverage_gaps=_unique_gaps(gaps),
        boundary=separating_boundary(cs, possible, origin),
        rejected_alternatives=rejected_alternatives(cs, network, origin, dest),
        notes=tuple(notes),
    )


def disconnection_bounded(
    ctx: Context, cs: ClassifiedState, possible: Graph, origin: str, dest: str
) -> tuple[bool, list[CoverageGap]]:
    """A disconnection is bounded if either side's component is closed and reviewed."""
    nodes = {n.id: n for n in ctx.inp.network.nodes}
    all_gaps: list[CoverageGap] = []

    def closed(component: set[str]) -> bool:
        ok = True
        for nid in sorted(component):
            n = nodes[nid]
            if n.open_boundary:
                all_gaps.append(CoverageGap(kind=GapKind.OPEN_BOUNDARY, target=nid,
                                            description=f"the modeled network continues past {nid}"))
                ok = False
            if not ctx.geo.node_covered(n.x, n.y):
                all_gaps.append(CoverageGap(kind=GapKind.NETWORK_COVERAGE, target=nid,
                                            description=f"{nid} lies outside reviewed network coverage"))
                ok = False
            for pid in cs.state.active_projects:
                if ctx.geo.point_project_gap(pid, n.x, n.y):
                    all_gaps.append(CoverageGap(kind=GapKind.PROJECT_COVERAGE, target=nid, project_id=pid,
                                                description=f"{nid} is inside {pid}'s unreviewed work extent"))
                    ok = False
        for eid, a in cs.assessments.items():
            e = cs.edges[eid]
            if a.edge_class == EdgeClass.POSSIBLE_UNCOVERED and (e.u in component or e.v in component):
                all_gaps.extend(a.gaps)
                ok = False
        return ok

    fwd = closed(possible.reachable(origin))
    if fwd:
        return True, []
    back = closed(possible.reverse_reachable(dest))
    return back, ([] if back else all_gaps)


def separating_boundary(cs: ClassifiedState, possible: Graph, origin: str) -> tuple[Blocker, ...]:
    comp = possible.reachable(origin)
    out = []
    for eid in sorted(cs.edges):
        a = cs.assessments[eid]
        if a.edge_class != EdgeClass.BLOCKED:
            continue
        e = cs.edges[eid]
        for arc in arcs_of(e):
            if arc.src in comp and arc.dst not in comp:
                out.append(Blocker(edge_id=eid, from_node=arc.src, to_node=arc.dst, reasons=a.blocks))
                break
    return tuple(out)


def rejected_alternatives(
    cs: ClassifiedState, network: NetworkSnapshot, origin: str, dest: str, k: int = 3
) -> tuple[RejectedAlternative, ...]:
    """Shortest geometric routes ignoring every block, with the facts that fail them."""
    if origin == dest:
        return ()
    g: nx.DiGraph[str] = nx.DiGraph()
    best: dict[tuple[str, str], tuple[float, str]] = {}
    for eid in sorted(cs.edges):
        e = cs.edges[eid]
        w = _frame_length(e)
        for arc in arcs_of(e):
            key = (arc.src, arc.dst)
            if key not in best or (w, eid) < best[key]:
                best[key] = (w, eid)
    for (s, t), (w, eid) in sorted(best.items()):
        g.add_edge(s, t, weight=w, eid=eid)
    if origin not in g or dest not in g:
        return ()
    out = []
    try:
        paths = list(islice(nx.shortest_simple_paths(g, origin, dest, weight="weight"), k))
    except nx.NetworkXNoPath:
        return ()
    for p in paths:
        eids = tuple(g[a][b]["eid"] for a, b in pairwise(p))
        failing = tuple(cs.assessments[e] for e in eids if cs.assessments[e].edge_class != EdgeClass.CONSERVATIVE)
        if not failing:
            continue
        lengths = [edge_length(cs.edges[e], network) for e in eids]
        total = None if any(x is None for x in lengths) else sum(x for x in lengths if x is not None)
        out.append(RejectedAlternative(route_edges=eids, length_m=total, failing=failing))
    return tuple(out)


def worst(outcomes: Iterable[Outcome]) -> Outcome:
    outs = list(outcomes)
    if not outs:
        return Outcome.SUPPORTED
    return max(outs, key=lambda o: OUTCOME_SEVERITY[o])


def _state_result(ctx: Context, cs: ClassifiedState) -> StateResult:
    req = ctx.requirement
    pairs = tuple(
        evaluate_pair(ctx, cs, o, d)
        for o in sorted(req.origins)
        for d in sorted(req.destinations)
    )
    st = cs.state
    return StateResult(
        label=st.label,
        basis=st.basis,
        active=st.active,
        interval=st.interval,
        hypothetical=st.hypothetical,
        outcome=worst(p.outcome for p in pairs),
        pairs=pairs,
    )


# ---------------------------------------------------------------- dated mode


def _ms(t: datetime) -> int:
    return int(t.astimezone(UTC).timestamp() * 1000)


def _from_ms(ms: int) -> datetime:
    return datetime(1970, 1, 1, tzinfo=UTC) + timedelta(milliseconds=ms)


class TemporalBoundExceeded(RuntimeError):
    pass


def feasible_departures(
    states: list[ClassifiedState],
    network: NetworkSnapshot,
    classes: frozenset[EdgeClass],
    origin: str,
    dest: str,
    window: IntervalSet,
    speed: float,
    max_offset: int,
    horizon_end: int,
    permissive_beyond_horizon: bool,
) -> IntervalSet:
    """Exact set of departure instants (ms) in ``window`` with a no-wait route.

    Each traversed edge must be in ``classes`` for its whole traversal
    ``[enter, exit)``. Journeys longer than ``max_offset`` are not considered.
    """
    if origin == dest:
        return window
    avail: dict[str, list[tuple[int, int]]] = {}
    edges: dict[str, Edge] = {}
    for cs in states:
        assert cs.state.interval is not None and cs.state.interval.end is not None
        a, b = _ms(cs.state.interval.start), _ms(cs.state.interval.end)
        for eid, asm in cs.assessments.items():
            edges.setdefault(eid, cs.edges[eid])
            if asm.edge_class in classes:
                avail.setdefault(eid, []).append((a, b))
    if permissive_beyond_horizon:
        for eid in edges:
            avail.setdefault(eid, []).append((horizon_end, horizon_end + max_offset + 1))
    avail_sets = {eid: IntervalSet.of(sp) for eid, sp in avail.items()}
    dur: dict[str, int] = {}
    adj: dict[str, list[Arc]] = {}
    for eid in sorted(avail_sets):
        e = edges[eid]
        ln = edge_length(e, network)
        if ln is None:
            raise ValueError("temporal analysis needs metric edge lengths")
        dur[eid] = max(1, math.ceil(ln / speed * 1000))
        for arc in arcs_of(e):
            adj.setdefault(arc.src, []).append(arc)
    for lst in adj.values():
        lst.sort(key=lambda a: (a.dst, a.edge_id))

    labels: dict[tuple[str, int], IntervalSet] = {(origin, 0): window}
    heap: list[tuple[int, str]] = [(0, origin)]
    done = IntervalSet()
    expanded = 0
    while heap:
        off, node = heapq.heappop(heap)
        s = labels.pop((node, off), None)
        if s is None:
            continue
        s = s.subtract(done)
        if not s:
            continue
        if node == dest:
            done = done.union(s)
            if done.covers(window):
                break
            continue
        expanded += 1
        if expanded > TEMPORAL_LABEL_CAP:
            raise TemporalBoundExceeded(f"more than {TEMPORAL_LABEL_CAP} temporal labels")
        for arc in adj.get(node, ()):
            d = dur[arc.edge_id]
            noff = off + d
            if noff > max_offset:
                continue
            ok = s.intersect(traversal_departures(avail_sets[arc.edge_id], off, d))
            if not ok:
                continue
            key = (arc.dst, noff)
            if key in labels:
                labels[key] = labels[key].union(ok)
            else:
                labels[key] = ok
                heapq.heappush(heap, (noff, arc.dst))
    return done.intersect(window)


def evaluate_window(ctx: Context, classified: list[ClassifiedState], origin: str, dest: str) -> PairResult:
    req = ctx.requirement
    network = ctx.inp.network
    assert req.departure_window is not None and req.walking_speed_mps is not None
    horizon = ctx.inp.scenario.horizon
    assert horizon is not None and horizon.end is not None
    w = req.departure_window
    w_end = w.end or horizon.end
    window = IntervalSet.of([(_ms(w.start), _ms(w_end))])

    anchor = next(
        (cs for cs in classified if cs.state.interval and cs.state.interval.contains(w.start)), classified[0]
    )
    static = evaluate_pair(ctx, anchor, origin, dest)
    if not network.frame.metric:
        return static.model_copy(update={
            "outcome": max(static.outcome, Outcome.NEEDS_VERIFICATION, key=lambda o: OUTCOME_SEVERITY[o]),
            "uncertainties": static.uncertainties + (Fact(id="network:uncalibrated", kind=FactKind.UNCALIBRATED_GEOMETRY,
                                                          target=network.revision_id,
                                                          description="traversal timing needs calibrated metric geometry"),),
            "notes": static.notes + ("departure window not evaluated: geometry is not metric",),
        })

    total_len = sum((edge_length(e, network) or 0.0) for e in network.edges) + sum(
        (edge_length(c.connection.edge, network) or 0.0) for cs in classified for c in cs.state.connections
    )
    cap = static.budget_m
    max_len = cap if cap is not None else total_len
    max_offset = max(1, math.ceil(max_len / req.walking_speed_mps * 1000))

    sets: list[IntervalSet] = []
    try:
        for i, (_, classes, _) in enumerate(TIERS):
            sets.append(
                feasible_departures(
                    classified, network, classes, origin, dest, window, req.walking_speed_mps,
                    max_offset, _ms(horizon.end), permissive_beyond_horizon=(i == 2),
                )
            )
    except TemporalBoundExceeded as exc:
        return static.model_copy(update={
            "outcome": Outcome.NEEDS_VERIFICATION if static.outcome == Outcome.SUPPORTED else static.outcome,
            "uncertainties": static.uncertainties + (Fact(id="temporal:bound", kind=FactKind.TEMPORAL_SEARCH_BOUND,
                                                          target=f"{origin}->{dest}", description=str(exc)),),
        })

    gaps: list[DepartureGap] = []
    names = ["conservative", "possible_covered", "possible"]
    for name, fs in zip(names, sets, strict=True):
        for a, b in window.subtract(fs).spans:
            gaps.append(DepartureGap(start=_from_ms(a), end=_from_ms(b), level=name))

    notes = list(static.notes) + [
        f"departure window [{w.start.isoformat()}, {w_end.isoformat()}) checked exactly at "
        f"{req.walking_speed_mps} m/s (assumed); waiting is not modeled",
    ]
    if sets[0].covers(window):
        outcome = Outcome.SUPPORTED
    elif sets[1].covers(window):
        outcome = Outcome.NEEDS_VERIFICATION
    elif sets[2].covers(window):
        outcome = Outcome.INSUFFICIENT_COVERAGE
    else:
        first_gap = window.subtract(sets[2]).spans[0][0]
        at = next(
            (cs for cs in classified if cs.state.interval and cs.state.interval.contains(_from_ms(first_gap))),
            anchor,
        )
        possible = Graph(at, network, POSSIBLE)
        bounded, _ = disconnection_bounded(ctx, at, possible, origin, dest)
        outcome = Outcome.DISCONNECTED_WITHIN_COVERAGE if bounded else Outcome.INSUFFICIENT_COVERAGE

    # Collect uncertainties and gaps from every state a failing departure could touch.
    facts: dict[str, Fact] = {f.id: f for f in static.uncertainties}
    cgaps = list(static.coverage_gaps)
    if outcome != Outcome.SUPPORTED:
        for cs in classified:
            pr = evaluate_pair(ctx, cs, origin, dest)
            for f in pr.uncertainties:
                facts.setdefault(f.id, f)
            if outcome == Outcome.INSUFFICIENT_COVERAGE:
                cgaps.extend(pr.coverage_gaps)
    return static.model_copy(update={
        "outcome": outcome,
        "route": static.route if outcome == Outcome.SUPPORTED or static.outcome != Outcome.SUPPORTED else None,
        "uncertainties": tuple(sorted(facts.values(), key=lambda f: f.id)),
        "coverage_gaps": _unique_gaps(cgaps),
        "departure_gaps": tuple(gaps),
        "notes": tuple(notes),
    })


# ---------------------------------------------------------------- entry point


def verify(inp: AnalysisInput) -> AnalysisResult:
    states = compile_states(inp)
    geo = GeometryIndex(inp.network, tuple(inp.project(p) for p in inp.scenario.project_ids))
    assumptions = dict(inp.assumptions)
    results: list[RequirementResult] = []
    warnings: list[str] = []
    if any(s.hypothetical for s in states):
        warnings.append("some states include hypothetical overlays; they are not official plan content")
    for req in sorted(inp.requirements, key=lambda r: r.id):
        profile = inp.profile(req.profile_id)
        base = classify_state(inp.network, baseline_state(), profile, geo, assumptions)
        ctx = Context(inp, geo, profile, req, base)
        classified = [classify_state(inp.network, s, profile, geo, assumptions) for s in states]
        state_results = [_state_result(ctx, base)] + [_state_result(ctx, cs) for cs in classified]
        if inp.scenario.mode == "dated" and req.departure_window is not None:
            relevant = [cs for cs in classified if cs.state.interval is not None]
            wpairs = tuple(
                evaluate_window(ctx, relevant, o, d)
                for o in sorted(req.origins)
                for d in sorted(req.destinations)
            )
            state_results.append(
                StateResult(
                    label="departure window",
                    basis=StateBasis.SCHEDULE,
                    active={},
                    interval=req.departure_window,
                    hypothetical=any(cs.state.hypothetical for cs in relevant),
                    outcome=worst(p.outcome for p in wpairs),
                    pairs=wpairs,
                )
            )
            overall = state_results[-1].outcome
            scored = [state_results[-1]]
        else:
            scored = state_results[1:] if len(state_results) > 1 else state_results
            overall = worst(s.outcome for s in scored)
        affected = sorted({
            (p.origin, p.destination)
            for s in scored
            for p in s.pairs
            if p.outcome != Outcome.SUPPORTED
        })
        results.append(
            RequirementResult(
                requirement_id=req.id,
                requirement_version=req.version,
                profile_id=profile.id,
                profile_version=profile.version,
                outcome=overall,
                states=tuple(state_results),
                affected_pairs=tuple(affected),
                assumptions=tuple(sorted(assumptions)),
            )
        )
    return AnalysisResult(
        input_hash=analysis_input_hash(inp),
        verifier_version=VERIFIER_VERSION,
        scenario_id=inp.scenario.id,
        scenario_revision_id=inp.scenario.revision_id,
        network_revision_id=inp.network.revision_id,
        project_revision_ids=tuple(sorted(inp.project(p).revision_id for p in inp.scenario.project_ids)),
        requirements=tuple(results),
        assumptions_applied=assumptions,
        warnings=tuple(warnings),
    )


def analysis_input_hash(inp: AnalysisInput) -> str:
    return content_hash({"verifier": VERIFIER_VERSION, "input": inp})


def _interval_label(iv: Interval | None) -> str:
    if iv is None:
        return ""
    return f"[{iv.start.isoformat()}, {iv.end.isoformat() if iv.end else '...'})"
