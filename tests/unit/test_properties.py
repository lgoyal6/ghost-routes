"""Property-based invariants checked against independent exhaustive oracles.

The oracles below share no code with the verifier: they enumerate every
resolution of every unresolved fact and run plain reachability.
"""

from __future__ import annotations

import itertools
import math
from datetime import UTC, datetime, timedelta

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from ghost_routes.domain.model import (
    OUTCOME_SEVERITY,
    AccessRequirement,
    AnalysisInput,
    AttributeValue,
    CoverageAssertion,
    Edge,
    EvidenceState,
    Interval,
    NetworkSnapshot,
    Node,
    Outcome,
    Phase,
    ProjectSnapshot,
    Restriction,
    Scenario,
    StateBasis,
    StateSpec,
    TargetRelation,
)
from ghost_routes.optimization.repair import solve
from ghost_routes.synthetic import cases as C
from ghost_routes.synthetic.repairs import base_input, interventions
from ghost_routes.verification.intervals import IntervalSet, traversal_departures
from ghost_routes.verification.verifier import verify

SETTINGS = settings(max_examples=120, deadline=None, suppress_health_check=[HealthCheck.too_slow])

# ------------------------------------------------------------------ generator

WIDTH_STATES = ["ok", "narrow", "unknown", "proposed_ok", "conflict"]
EXIST_STATES = ["accepted", "unresolved", "rejected"]


@st.composite
def small_graph(draw):  # type: ignore[no-untyped-def]
    n = draw(st.integers(3, 6))
    pairs = list(itertools.combinations(range(n), 2))
    chosen = draw(st.lists(st.sampled_from(pairs), min_size=1, max_size=min(8, len(pairs)), unique=True))
    edges = []
    for i, (u, v) in enumerate(chosen):
        edges.append({
            "id": f"e{i}", "u": u, "v": v,
            "exist": draw(st.sampled_from(EXIST_STATES)),
            "width": draw(st.sampled_from(WIDTH_STATES)),
            "closed": draw(st.sampled_from(["no", "no", "accepted", "unresolved"])),
        })
    return n, edges


def width_attr(kind: str) -> dict[str, AttributeValue]:
    if kind == "ok":
        return {"clear_width": AttributeValue(value=2.0, unit="m", state=EvidenceState.ACCEPTED)}
    if kind == "narrow":
        return {"clear_width": AttributeValue(value=1.0, unit="m", state=EvidenceState.ACCEPTED)}
    if kind == "proposed_ok":
        return {"clear_width": AttributeValue(value=2.0, unit="m", state=EvidenceState.UNRESOLVED)}
    if kind == "conflict":
        return {"clear_width": AttributeValue(unit="m", state=EvidenceState.CONFLICTING, alternatives=(1.0, 2.0))}
    return {}


def build(n: int, edges: list[dict], *, covered: bool = True) -> AnalysisInput:  # type: ignore[type-arg]
    nodes = tuple(Node(id=f"n{i}", x=math.cos(i) * 40, y=math.sin(i) * 40) for i in range(n))
    pos = {f"n{i}": (nd.x, nd.y) for i, nd in enumerate(nodes)}
    es = []
    for e in edges:
        u, v = f"n{e['u']}", f"n{e['v']}"
        es.append(Edge(id=e["id"], u=u, v=v, geometry=(pos[u], pos[v]),
                       existence={"accepted": EvidenceState.ACCEPTED, "unresolved": EvidenceState.UNRESOLVED,
                                  "rejected": EvidenceState.REJECTED}[e["exist"]],
                       attributes=width_attr(e["width"])))
    cov = (CoverageAssertion(id="c", scope="network", polygon=((-60, -60), (60, -60), (60, 60), (-60, 60)),
                             reviewer="property-test"),) if covered else ()
    net = NetworkSnapshot(revision_id="g", frame=C.FRAME, nodes=nodes, edges=tuple(es), coverage=cov)
    restrictions = []
    for e in edges:
        if e["closed"] != "no":
            restrictions.append(Restriction(id=f"r-{e['id']}", phase_ids=("ph",),
                                            targets=(TargetRelation(edge_id=e["id"]),),
                                            state=EvidenceState.ACCEPTED if e["closed"] == "accepted" else EvidenceState.UNRESOLVED))
    proj = ProjectSnapshot(project_id="P", revision_id="p", title="P", phases=(Phase(id="ph", label="ph", order=1),),
                           restrictions=tuple(restrictions), extent=None,
                           coverage=(CoverageAssertion(id="pc", scope="P", polygon=((-60, -60), (60, -60), (60, 60), (-60, 60)),
                                                       reviewer="property-test"),))
    scn = Scenario(id="s", revision_id="s", label="s", mode="phases", project_ids=("P",),
                   states=(StateSpec(label="ph", active={"P": "ph"}, basis=StateBasis.PROJECT_ISOLATED),))
    req = AccessRequirement(id="r", version=1, label="r", profile_id="width-1.5", origins=("n0",), destinations=(f"n{n-1}",))
    return AnalysisInput(network=net, projects=(proj,), scenario=scn, profiles=C.PROFILES, requirements=(req,))


# ------------------------------------------------------------------ oracle


def oracle_reachability(n: int, edges: list[dict]) -> tuple[bool, bool]:  # type: ignore[type-arg]
    """(reachable in every resolution, reachable in some resolution)."""
    choices = []
    for e in edges:
        exist = [True] if e["exist"] == "accepted" else [False] if e["exist"] == "rejected" else [True, False]
        width = {"ok": [True], "narrow": [False], "unknown": [True, False], "proposed_ok": [True, False],
                 "conflict": [True, False]}[e["width"]]
        closed = {"no": [False], "accepted": [True], "unresolved": [True, False]}[e["closed"]]
        choices.append([(a and b and not c) for a in exist for b in width for c in closed])
    results = []
    for combo in itertools.product(*choices):
        adj: dict[int, set[int]] = {i: set() for i in range(n)}
        for e, usable in zip(edges, combo, strict=True):
            if usable:
                adj[e["u"]].add(e["v"])
                adj[e["v"]].add(e["u"])
        seen, stack = {0}, [0]
        while stack:
            x = stack.pop()
            for y in adj[x]:
                if y not in seen:
                    seen.add(y)
                    stack.append(y)
        results.append(n - 1 in seen)
    return all(results), any(results)


@SETTINGS
@given(small_graph())
def test_outcome_matches_exhaustive_resolution_oracle(g) -> None:  # type: ignore[no-untyped-def]
    n, edges = g
    res = verify(build(n, edges))
    outcome = res.requirements[0].states[1].outcome
    always, sometimes = oracle_reachability(n, edges)
    if always:
        assert outcome == Outcome.SUPPORTED
    elif sometimes:
        assert outcome == Outcome.NEEDS_VERIFICATION
    else:
        assert outcome == Outcome.DISCONNECTED_WITHIN_COVERAGE


@SETTINGS
@given(small_graph())
def test_supported_routes_never_use_unknown_or_failing_hard_attributes(g) -> None:  # type: ignore[no-untyped-def]
    n, edges = g
    for s in verify(build(n, edges)).requirements[0].states:
        for p in s.pairs:
            if p.outcome == Outcome.SUPPORTED and p.route:
                for step in p.route.steps:
                    assert all(c.satisfied is True for c in step.checks if c.hard)
                    assert step.edge_class.value == "CONSERVATIVE"


@SETTINGS
@given(small_graph())
def test_incomplete_coverage_never_supports_or_disconnects(g) -> None:  # type: ignore[no-untyped-def]
    n, edges = g
    for s in verify(build(n, edges, covered=False)).requirements[0].states:
        assert s.outcome in (Outcome.INSUFFICIENT_COVERAGE, Outcome.NEEDS_VERIFICATION) or (
            s.outcome == Outcome.INSUFFICIENT_COVERAGE
        )
        assert s.outcome not in (Outcome.SUPPORTED, Outcome.DISCONNECTED_WITHIN_COVERAGE)


@SETTINGS
@given(small_graph(), st.data())
def test_adding_a_closure_never_improves_outcome(g, data) -> None:  # type: ignore[no-untyped-def]
    n, edges = g
    before = verify(build(n, edges)).requirements[0].states[1].outcome
    idx = data.draw(st.integers(0, len(edges) - 1))
    edges2 = [dict(e) for e in edges]
    edges2[idx]["closed"] = "accepted"
    after = verify(build(n, edges2)).requirements[0].states[1].outcome
    assert OUTCOME_SEVERITY[after] >= OUTCOME_SEVERITY[before]


@SETTINGS
@given(small_graph(), st.data())
def test_concurrent_projects_combine_by_union(g, data) -> None:  # type: ignore[no-untyped-def]
    """A second project's open edge cannot cancel the first project's closure."""
    n, edges = g
    base = build(n, edges)
    p = base.projects[0]
    victim = data.draw(st.sampled_from([e["id"] for e in edges]))
    q = ProjectSnapshot(project_id="Q", revision_id="q", title="Q", phases=(Phase(id="ph", label="ph", order=1),),
                        restrictions=(Restriction(id="rq", phase_ids=("ph",), targets=(TargetRelation(edge_id=victim),)),),
                        extent=None, coverage=(CoverageAssertion(id="qc", scope="Q", polygon=((-60, -60), (60, -60), (60, 60), (-60, 60)),
                                                                 reviewer="property-test"),))
    states = (
        StateSpec(label="P", active={"P": "ph"}, basis=StateBasis.PROJECT_ISOLATED),
        StateSpec(label="Q", active={"Q": "ph"}, basis=StateBasis.PROJECT_ISOLATED),
        StateSpec(label="PQ", active={"P": "ph", "Q": "ph"}, basis=StateBasis.DECLARED_CONCURRENT),
    )
    scn = base.scenario.model_copy(update={"project_ids": ("P", "Q"), "states": states})
    res = verify(base.model_copy(update={"projects": (p, q), "scenario": scn}))
    by = {s.label: s.outcome for s in res.requirements[0].states}
    assert OUTCOME_SEVERITY[by["PQ"]] >= max(OUTCOME_SEVERITY[by["P"]], OUTCOME_SEVERITY[by["Q"]])


@SETTINGS
@given(small_graph())
def test_geometric_crossings_without_nodes_do_not_connect(g) -> None:  # type: ignore[no-untyped-def]
    n, edges = g
    base = build(n, edges)
    before = verify(base).requirements[0].states[1].outcome
    # A long isolated edge crossing the whole site, connected to nothing reachable.
    extra_nodes = (Node(id="q1", x=-55, y=-55), Node(id="q2", x=55, y=55))
    extra = Edge(id="cross", u="q1", v="q2", geometry=((-55, -55), (55, 55)),
                 attributes={"clear_width": AttributeValue(value=3.0, unit="m", state=EvidenceState.ACCEPTED)})
    net = base.network.model_copy(update={"nodes": base.network.nodes + extra_nodes, "edges": base.network.edges + (extra,)})
    after = verify(base.model_copy(update={"network": net})).requirements[0].states[1].outcome
    assert after == before


@settings(max_examples=40, deadline=None)
@given(st.floats(0, 2 * math.pi), st.floats(0.2, 5.0), st.floats(-500, 500), st.floats(-500, 500))
def test_similarity_transform_invariance(theta, scale, dx, dy) -> None:  # type: ignore[no-untyped-def]
    """Rotation/scale/translation of every geometry keeps outcomes and routes (lengths are explicit)."""
    def tf(p):  # type: ignore[no-untyped-def]
        x, y = p
        return (scale * (x * math.cos(theta) - y * math.sin(theta)) + dx,
                scale * (x * math.sin(theta) + y * math.cos(theta)) + dy)

    for case in (C.case_joint_disconnection(), C.case_missing_sheet(), C.case_unreviewed_intersection()):
        inp = case.input
        net = inp.network
        nodes = tuple(nd.model_copy(update={"x": tf((nd.x, nd.y))[0], "y": tf((nd.x, nd.y))[1]}) for nd in net.nodes)
        edges = tuple(e.model_copy(update={"geometry": tuple(tf(p) for p in e.geometry),
                                           "length_m": math.dist(*e.geometry[:2]) if len(e.geometry) == 2 else e.length_m})
                      for e in net.edges)
        cov = tuple(c.model_copy(update={"polygon": tuple(tf(p) for p in c.polygon)}) for c in net.coverage)
        projects = tuple(p.model_copy(update={
            "extent": tuple(tf(q) for q in p.extent) if p.extent else None,
            "coverage": tuple(c.model_copy(update={"polygon": tuple(tf(q) for q in c.polygon)}) for c in p.coverage),
            "restrictions": tuple(r.model_copy(update={"polygon": tuple(tf(q) for q in r.polygon) if r.polygon else None})
                                  for r in p.restrictions),
        }) for p in inp.projects)
        moved = inp.model_copy(update={"network": net.model_copy(update={"nodes": nodes, "edges": edges, "coverage": cov}),
                                       "projects": projects})
        fixed = inp.model_copy(update={"network": net.model_copy(update={"edges": tuple(
            e.model_copy(update={"length_m": math.dist(*e.geometry[:2]) if len(e.geometry) == 2 else e.length_m})
            for e in net.edges)})})
        a, b = verify(fixed), verify(moved)
        for ra, rb in zip(a.requirements, b.requirements, strict=True):
            assert [s.outcome for s in ra.states] == [s.outcome for s in rb.states]
            for sa, sb in zip(ra.states, rb.states, strict=True):
                for pa, pb in zip(sa.pairs, sb.pairs, strict=True):
                    ea = [s.edge_id for s in pa.route.steps] if pa.route else None
                    eb = [s.edge_id for s in pb.route.steps] if pb.route else None
                    assert ea == eb


# ------------------------------------------------------------------ intervals


@settings(max_examples=300, deadline=None)
@given(st.lists(st.tuples(st.integers(0, 40), st.integers(1, 10)), max_size=5), st.integers(0, 15), st.integers(1, 8))
def test_traversal_departures_match_brute_force(spans, offset, duration) -> None:  # type: ignore[no-untyped-def]
    avail = IntervalSet.of([(a, a + w) for a, w in spans])
    got = traversal_departures(avail, offset, duration)
    for t in range(-30, 60):
        brute = all(avail.contains(x) for x in range(t + offset, t + offset + duration))
        assert got.contains(t) == brute, (t, avail, offset, duration)


@settings(max_examples=200, deadline=None)
@given(st.lists(st.tuples(st.integers(-20, 20), st.integers(1, 15)), max_size=4),
       st.lists(st.tuples(st.integers(-20, 20), st.integers(1, 15)), max_size=4))
def test_interval_set_algebra(a_spans, b_spans) -> None:  # type: ignore[no-untyped-def]
    a = IntervalSet.of([(x, x + w) for x, w in a_spans])
    b = IntervalSet.of([(x, x + w) for x, w in b_spans])
    for t in range(-25, 40):
        assert a.union(b).contains(t) == (a.contains(t) or b.contains(t))
        assert a.intersect(b).contains(t) == (a.contains(t) and b.contains(t))
        assert a.subtract(b).contains(t) == (a.contains(t) and not b.contains(t))


T0 = datetime(2027, 1, 1, tzinfo=UTC)


@settings(max_examples=60, deadline=None)
@given(st.integers(1, 30), st.integers(1, 30), st.integers(0, 60), st.integers(1, 40))
def test_window_engine_matches_per_departure_simulation(l1, l2, close_at, close_len) -> None:  # type: ignore[no-untyped-def]
    """Two parallel routes; one segment closes for a while. Compare to per-second simulation."""
    nodes = (Node(id="a", x=0, y=0), Node(id="b", x=10, y=0), Node(id="c", x=20, y=0), Node(id="m", x=10, y=10))
    w = {"clear_width": AttributeValue(value=2.0, unit="m", state=EvidenceState.ACCEPTED)}
    edges = (
        Edge(id="ab", u="a", v="b", geometry=((0, 0), (10, 0)), length_m=float(l1), attributes=w),
        Edge(id="bc", u="b", v="c", geometry=((10, 0), (20, 0)), length_m=float(l1), attributes=w),
        Edge(id="am", u="a", v="m", geometry=((0, 0), (10, 10)), length_m=float(l2), attributes=w),
        Edge(id="mc", u="m", v="c", geometry=((10, 10), (20, 0)), length_m=float(l2 * 2), attributes=w),
    )
    cov = (CoverageAssertion(id="c", scope="network", polygon=((-5, -5), (25, -5), (25, 15), (-5, 15)), reviewer="t"),)
    net = NetworkSnapshot(revision_id="t", frame=C.FRAME, nodes=nodes, edges=edges, coverage=cov)
    ph = Phase(id="ph", label="ph", order=1,
               effective=Interval(start=T0 + timedelta(seconds=close_at), end=T0 + timedelta(seconds=close_at + close_len)))
    proj = ProjectSnapshot(project_id="P", revision_id="p", title="P", phases=(ph,),
                           restrictions=(Restriction(id="r", phase_ids=("ph",), targets=(TargetRelation(edge_id="bc"),)),),
                           extent=None, coverage=(CoverageAssertion(id="pc", scope="P", polygon=cov[0].polygon, reviewer="t"),))
    scn = Scenario(id="s", revision_id="s", label="s", mode="dated", project_ids=("P",),
                   horizon=Interval(start=T0, end=T0 + timedelta(seconds=400)))
    req = AccessRequirement(id="r", version=1, label="r", profile_id="any-walker", origins=("a",), destinations=("c",),
                            departure_window=Interval(start=T0, end=T0 + timedelta(seconds=60)), walking_speed_mps=1.0)
    res = verify(AnalysisInput(network=net, projects=(proj,), scenario=scn, profiles=C.PROFILES, requirements=(req,)))
    window = res.requirements[0].states[-1]
    gaps = [g for g in window.pairs[0].departure_gaps if g.level == "conservative"]

    closed = (close_at * 1000, (close_at + close_len) * 1000)
    horizon_end = 400_000

    def ok(t_ms: int) -> bool:
        # Route 1: a-b-c; bc must be open for its whole traversal and within the horizon.
        enter, leave = t_ms + l1 * 1000, t_ms + 2 * l1 * 1000
        r1 = leave <= horizon_end and (leave <= closed[0] or enter >= closed[1])
        r2 = t_ms + 3 * l2 * 1000 <= horizon_end
        return r1 or r2

    def in_gap(t_ms: int) -> bool:
        return any(T0 + timedelta(milliseconds=t_ms) >= g.start and T0 + timedelta(milliseconds=t_ms) < g.end for g in gaps)

    for t in range(0, 60_000, 250):
        assert ok(t) != in_gap(t), t


# ------------------------------------------------------------------ repair


def test_repair_objective_matches_brute_force() -> None:
    problem_input = base_input()
    ivs = interventions()
    from ghost_routes.optimization.repair import (
        RepairProblem,
        apply_selection,
        selection_cost,
    )

    problem = RepairProblem(base=problem_input, interventions=ivs, restore=("lab-to-library",))
    best = None
    axes = [[None, *range(i.choices())] for i in ivs]
    for combo in itertools.product(*axes):
        sel = {i.id: k for i, k in zip(ivs, combo, strict=True) if k is not None}
        branch = apply_selection(problem, sel)
        try:
            out = verify(branch).requirements[0].outcome
        except Exception:
            continue
        if out == Outcome.SUPPORTED:
            c = selection_cost(problem, sel)
            best = c if best is None else min(best, c)
    for method in ("enumerate", "cpsat"):
        r = solve(problem.model_copy(update={"method": method}))
        assert r.objective == best, method
