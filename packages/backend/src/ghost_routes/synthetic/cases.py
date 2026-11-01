"""Original synthetic fixture cases with exact expected outcomes.

Every case is fictional ("Ashgrove campus") and labeled synthetic. Criteria are
illustrative and carry a scripted review, never a professional approval.

Base network (metric local frame, metres)::

    A(0,0) ---e1--- B(60,0) ---e2--- C(120,0)
      |               |                |
      e3              e4               e5 (stairs)
      |               |                |
    D(0,50) --e6--- E(60,50) --e7--- F(120,50)

``A`` is the Halden Lab north exit, ``F`` the Merrow Library entrance.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from ghost_routes.domain.model import (
    AccessRequirement,
    AnalysisInput,
    AttributeValue,
    Comparator,
    Constraint,
    CoverageAssertion,
    Edge,
    EdgeKind,
    EvidenceState,
    Frame,
    Interval,
    MobilityProfile,
    NetworkSnapshot,
    Node,
    NodeKind,
    Outcome,
    Phase,
    Point,
    ProjectSnapshot,
    Restriction,
    Scenario,
    StateBasis,
    StateSpec,
    TargetRelation,
    TemporaryConnection,
)

SYNTHETIC_NOTICE = "Synthetic fixture (fictional Ashgrove campus); not derived from any real plan."
FRAME = Frame(id="ashgrove-site", kind="site_metric", metric=True, description="Ashgrove local site frame, metres")
T0 = datetime(2027, 3, 1, 8, 0, tzinfo=UTC)

COORDS: dict[str, Point] = {
    "A": (0, 0), "B": (60, 0), "C": (120, 0),
    "D": (0, 50), "E": (60, 50), "F": (120, 50),
}
LABELS = {"A": "Halden Lab north exit", "F": "Merrow Library entrance"}


def accepted(value: float | bool | str, unit: str | None = None, ev: str = "") -> AttributeValue:
    return AttributeValue(value=value, unit=unit, state=EvidenceState.ACCEPTED, evidence=(ev,) if ev else ())


def std_attrs(width: float = 2.4, step_free: bool = True) -> dict[str, AttributeValue]:
    return {
        "clear_width": accepted(width, "m", "fixture:survey"),
        "step_free": accepted(step_free, None, "fixture:survey"),
    }


def edge(eid: str, u: str, v: str, kind: EdgeKind = EdgeKind.SIDEWALK, **kw) -> Edge:  # type: ignore[no-untyped-def]
    attrs = kw.pop("attributes", None)
    return Edge(
        id=eid,
        u=u,
        v=v,
        kind=kind,
        geometry=kw.pop("geometry", None) or (COORDS[u], COORDS[v]),
        attributes=std_attrs() if attrs is None else attrs,
        existence_evidence=("fixture:survey",),
        **kw,
    )


def base_edges() -> list[Edge]:
    return [
        edge("e1", "A", "B"),
        edge("e2", "B", "C"),
        edge("e3", "A", "D"),
        edge("e4", "B", "E", kind=EdgeKind.CROSSING),
        edge("e5", "C", "F", kind=EdgeKind.STAIRS, attributes=std_attrs(step_free=False)),
        edge("e6", "D", "E"),
        edge("e7", "E", "F"),
    ]


def base_nodes(open_boundary: frozenset[str] = frozenset()) -> list[Node]:
    out = []
    for nid, (x, y) in COORDS.items():
        kind = NodeKind.ENTRANCE if nid in ("A", "F") else NodeKind.JUNCTION
        out.append(Node(id=nid, x=x, y=y, kind=kind, label=LABELS.get(nid, ""), open_boundary=nid in open_boundary))
    return out


def rect(x0: float, y0: float, x1: float, y1: float) -> tuple[Point, ...]:
    return ((x0, y0), (x1, y0), (x1, y1), (x0, y1))


SITE = rect(-10, -10, 130, 60)


def network(
    edges: list[Edge] | None = None,
    nodes: list[Node] | None = None,
    coverage: tuple[Point, ...] | None = SITE,
    revision: str = "net-r1",
    frame: Frame = FRAME,
) -> NetworkSnapshot:
    cov: tuple[CoverageAssertion, ...] = ()
    if coverage is not None:
        cov = (CoverageAssertion(id=f"{revision}-cov", scope="network", polygon=coverage,
                                 attributes_complete=("clear_width", "step_free"), sheets=("ASH-1",),
                                 reviewer="scripted-fixture-review", assumptions=SYNTHETIC_NOTICE),)
    return NetworkSnapshot(revision_id=revision, frame=frame, nodes=tuple(nodes or base_nodes()),
                           edges=tuple(edges or base_edges()), coverage=cov)


STEP_FREE = MobilityProfile(
    id="step-free", version=1, label="Step-free (illustrative)",
    constraints=(
        Constraint(attribute="step_free", comparator=Comparator.EQ, threshold=True, source="illustrative fixture criterion",
                   rationale="Journey must avoid steps; illustrative, not a legal standard."),
    ),
    review_state="scripted_review", reviewer="scripted-fixture-review", illustrative=True,
)
WIDTH_15 = MobilityProfile(
    id="width-1.5", version=1, label="Clear width >= 1.5 m (illustrative)",
    constraints=(
        Constraint(attribute="clear_width", comparator=Comparator.GE, threshold=1.5, unit="m",
                   source="illustrative fixture criterion", rationale="Illustrative passing-width threshold."),
    ),
    review_state="scripted_review", reviewer="scripted-fixture-review", illustrative=True,
)
ANY = MobilityProfile(id="any-walker", version=1, label="No hard constraints (illustrative)", constraints=(),
                      review_state="scripted_review", reviewer="scripted-fixture-review", illustrative=True)
PROFILES = (STEP_FREE, WIDTH_15, ANY)


def req(rid: str, profile: str, o: str = "A", d: str = "F", **kw) -> AccessRequirement:  # type: ignore[no-untyped-def]
    return AccessRequirement(id=rid, version=1, label=kw.pop("label", f"{o} to {d} ({profile})"),
                             profile_id=profile, origins=(o,), destinations=(d,), **kw)


def closure(rid: str, phase: str, *edges: str, state: EvidenceState = EvidenceState.ACCEPTED,
            polygon: tuple[Point, ...] | None = None, effective: Interval | None = None, label: str = "") -> Restriction:
    return Restriction(id=rid, phase_ids=(phase,), targets=tuple(TargetRelation(edge_id=e) for e in edges),
                       polygon=polygon, state=state, effective=effective, evidence=(f"fixture:{rid}",),
                       label=label or f"closure {rid}")


def project(pid: str, phases: list[Phase], restrictions: list[Restriction], extent: tuple[Point, ...] = SITE,
            coverage: tuple[Point, ...] | None = SITE, temps: list[TemporaryConnection] | None = None,
            hypothetical: bool = False, revision: str | None = None) -> ProjectSnapshot:
    cov: tuple[CoverageAssertion, ...] = ()
    if coverage is not None:
        cov = (CoverageAssertion(id=f"{pid}-cov", scope=pid, polygon=coverage, sheets=(f"{pid}-1",),
                                 reviewer="scripted-fixture-review", assumptions=SYNTHETIC_NOTICE),)
    return ProjectSnapshot(project_id=pid, revision_id=revision or f"{pid}-r1", title=f"Synthetic project {pid}",
                           phases=tuple(phases), restrictions=tuple(restrictions), extent=extent, coverage=cov,
                           temporary_connections=tuple(temps or ()), hypothetical=hypothetical)


def scenario(projects: tuple[str, ...], states: tuple[StateSpec, ...] = (), rev: str = "scn-r1", **kw) -> Scenario:  # type: ignore[no-untyped-def]
    return Scenario(id=kw.pop("sid", "scn"), revision_id=rev, label=kw.pop("label", "synthetic scenario"),
                    mode=kw.pop("mode", "phases"), project_ids=projects, states=states, **kw)


def inp(net: NetworkSnapshot, projects: list[ProjectSnapshot], scn: Scenario, reqs: list[AccessRequirement],
        profiles: tuple[MobilityProfile, ...] = PROFILES) -> AnalysisInput:
    return AnalysisInput(network=net, projects=tuple(projects), scenario=scn, profiles=profiles, requirements=tuple(reqs))


@dataclass
class Case:
    name: str
    description: str
    input: AnalysisInput
    expected: dict[str, Outcome]
    # Optional per-state expectations: (requirement id, state label) -> outcome.
    expected_states: dict[tuple[str, str], Outcome] = field(default_factory=dict)
    synthetic: bool = True


P1 = Phase(id="ph1", label="Phase 1", order=1)
P2 = Phase(id="ph2", label="Phase 2", order=2, prerequisites=("ph1",))


def case_preserved_route() -> Case:
    p = project("P", [P1], [closure("r-e6", "ph1", "e6")])
    i = inp(network(), [p], scenario(("P",)), [req("width", "width-1.5")])
    return Case("preserved_route", "Closure away from the route leaves A to F supported.", i,
                {"width": Outcome.SUPPORTED})


def case_single_closure() -> Case:
    p = project("P", [P1], [closure("r-e2", "ph1", "e2")])
    i = inp(network(), [p], scenario(("P",)), [
        req("to-C", "any-walker", d="C"),
        req("to-C-budget", "any-walker", d="C", max_added_distance_m=50),
    ])
    return Case("single_closure", "Closing e2 adds an 100 m detour to C; a 50 m budget turns it into a bounded failure.", i,
                {"to-C": Outcome.SUPPORTED, "to-C-budget": Outcome.DISCONNECTED_WITHIN_COVERAGE})


def case_step_free_alternative() -> Case:
    edges = base_edges() + [edge("ramp1", "C", "F", kind=EdgeKind.RAMP, geometry=((120, 0), (135, 25), (120, 50)))]
    net = network(edges, coverage=rect(-10, -10, 140, 60))
    p = project("P", [P1], [closure("r-e7", "ph1", "e7")], extent=rect(-10, -10, 140, 60), coverage=rect(-10, -10, 140, 60))
    i = inp(net, [p], scenario(("P",)), [req("stepfree", "step-free")])
    return Case("step_free_alternative", "With e7 closed, the step-free route uses the ramp, never the stairs.", i,
                {"stepfree": Outcome.SUPPORTED})


def case_unknown_width() -> Case:
    edges = [e if e.id != "e7" else e.model_copy(update={"attributes": {"step_free": accepted(True)}}) for e in base_edges()]
    p = project("P", [P1], [closure("r-e5", "ph1", "e5")])
    i = inp(network(edges), [p], scenario(("P",)), [req("width", "width-1.5")])
    return Case("unknown_width", "The only open route crosses e7 whose clear width is unknown.", i,
                {"width": Outcome.NEEDS_VERIFICATION})


def case_conflicting_note() -> Case:
    conflict = AttributeValue(state=EvidenceState.CONFLICTING, unit="m", alternatives=(1.2, 1.8),
                              evidence=("fixture:note-says-1.2", "fixture:diagram-says-1.8"))
    edges = [e if e.id != "e7" else e.model_copy(update={"attributes": {**std_attrs(), "clear_width": conflict}})
             for e in base_edges()]
    p = project("P", [P1], [closure("r-e5", "ph1", "e5")])
    i = inp(network(edges), [p], scenario(("P",)), [req("width", "width-1.5")])
    return Case("conflicting_note_diagram", "A note (1.2 m) and the diagram (1.8 m) disagree on e7; the conflict stays open.", i,
                {"width": Outcome.NEEDS_VERIFICATION})


def case_crossing_lines() -> Case:
    # Two paths cross geometrically at (60,25) without a shared junction.
    nodes = [Node(id="A", x=0, y=0, label="west entrance"), Node(id="F", x=120, y=50, label="east entrance"),
             Node(id="G", x=0, y=50), Node(id="H", x=120, y=0)]
    edges = [edge("x1", "A", "F", geometry=((0, 0), (120, 50))), edge("x2", "G", "H", geometry=((0, 50), (120, 0)))]
    net = network(edges, nodes)
    i = inp(net, [], scenario(()), [req("G-to-H", "any-walker", o="G", d="F")])
    return Case("crossing_lines_without_junction", "x1 and x2 intersect visually but share no node, so G cannot reach F.", i,
                {"G-to-H": Outcome.DISCONNECTED_WITHIN_COVERAGE})


def case_missing_sheet() -> Case:
    # Route A-B-C-F looks open, but project Q's extent covers e2 and its sheet was never reviewed.
    p = project("P", [P1], [closure("r-e3", "ph1", "e3")])
    q = project("Q", [P1], [], extent=rect(70, -10, 130, 60), coverage=None)
    states = (StateSpec(label="P and Q concurrent (declared)", active={"P": "ph1", "Q": "ph1"},
                        basis=StateBasis.DECLARED_CONCURRENT),)
    i = inp(network(), [p, q], scenario(("P", "Q"), states), [req("any", "any-walker", d="C")])
    return Case("missing_sheet_partial_coverage",
                "An apparently open route crosses project Q's extent whose sheet is missing.", i,
                {"any": Outcome.INSUFFICIENT_COVERAGE})


def case_endpoint_outside() -> Case:
    nodes = base_nodes() + [Node(id="Z", x=200, y=0, label="Corran Hall (outside coverage)")]
    edges = base_edges() + [edge("e8", "C", "Z", geometry=((120, 0), (200, 0)))]
    i = inp(network(edges, nodes), [], scenario(()), [req("to-Z", "any-walker", d="Z")])
    return Case("endpoint_outside_coverage", "Z is outside reviewed coverage, so no conclusion is supported.", i,
                {"to-Z": Outcome.INSUFFICIENT_COVERAGE})


def case_undated_phases() -> Case:
    p = project("P", [P1], [closure("r-e5", "ph1", "e5")])
    q = project("Q", [P1], [closure("r-e4", "ph1", "e4", "e3")])
    i = inp(network(), [p, q], scenario(("P", "Q")), [req("any", "any-walker")])
    return Case("undated_phases", "Undated projects are evaluated in isolation; no overlap is inferred.", i,
                {"any": Outcome.SUPPORTED})


def case_joint_disconnection() -> Case:
    p = project("P", [P1], [closure("r-e5", "ph1", "e5", label="P fencing at C-F stairs")])
    q = project("Q", [P1], [closure("r-e4e3", "ph1", "e4", "e3", label="Q fence line across B-E and A-D")])
    states = (
        StateSpec(label="P alone", active={"P": "ph1"}, basis=StateBasis.PROJECT_ISOLATED),
        StateSpec(label="Q alone", active={"Q": "ph1"}, basis=StateBasis.PROJECT_ISOLATED),
        StateSpec(label="P and Q concurrent (declared)", active={"P": "ph1", "Q": "ph1"}, basis=StateBasis.DECLARED_CONCURRENT),
    )
    i = inp(network(), [p, q], scenario(("P", "Q"), states), [req("any", "any-walker")])
    return Case("joint_disconnection", "Each project alone preserves A to F; together they sever it.", i,
                {"any": Outcome.DISCONNECTED_WITHIN_COVERAGE},
                {("any", "P alone"): Outcome.SUPPORTED, ("any", "Q alone"): Outcome.SUPPORTED,
                 ("any", "P and Q concurrent (declared)"): Outcome.DISCONNECTED_WITHIN_COVERAGE})


def dated_phase(pid: str, start_min: int, end_min: int, order: int = 1, prereq: tuple[str, ...] = ()) -> Phase:
    return Phase(id=pid, label=f"Phase {order}", order=order, prerequisites=prereq,
                 effective=Interval(start=T0 + timedelta(minutes=start_min), end=T0 + timedelta(minutes=end_min)))


def case_closure_during_traversal() -> Case:
    # Single path A-B-C (120 m) at 1 m/s. e2 closes at T0+150s for 10 minutes, so
    # departures up to exactly T0+30s clear e2 (half-open) and later ones do not.
    nodes = [n for n in base_nodes() if n.id in "ABC"]
    edges = [e for e in base_edges() if e.id in ("e1", "e2")]
    net = network(edges, nodes, coverage=rect(-10, -10, 130, 10))
    ph = Phase(id="ph1", label="Phase 1", order=1,
               effective=Interval(start=T0 + timedelta(seconds=150), end=T0 + timedelta(seconds=750)))
    p = project("P", [ph], [closure("r-e2", "ph1", "e2")], extent=rect(-10, -10, 130, 10), coverage=rect(-10, -10, 130, 10))
    scn = scenario(("P",), mode="dated", horizon=Interval(start=T0, end=T0 + timedelta(hours=1)))
    r = req("window", "any-walker", d="C", departure_window=Interval(start=T0, end=T0 + timedelta(seconds=60)),
            walking_speed_mps=1.0)
    return Case("closure_during_traversal",
                "Departures after T0+30s reach e2 before it reopens; e2 must stay usable for the whole traversal.",
                inp(net, [p], scn, [r]), {"window": Outcome.DISCONNECTED_WITHIN_COVERAGE})


def case_dated_overlap() -> Case:
    p = project("P", [dated_phase("ph1", 0, 120)], [closure("r-e5", "ph1", "e5")])
    q = project("Q", [dated_phase("ph1", 60, 180)], [closure("r-e4e3", "ph1", "e4", "e3")])
    scn = scenario(("P", "Q"), mode="dated", horizon=Interval(start=T0, end=T0 + timedelta(hours=4)))
    return Case("dated_overlap", "Dated schedules overlap from 09:00 to 10:00, disconnecting A to F only then.",
                inp(network(), [p, q], scn, [req("any", "any-walker")]), {"any": Outcome.DISCONNECTED_WITHIN_COVERAGE})


def case_unreviewed_intersection() -> Case:
    # The restriction's fence polygon crosses e7 but only e5 is a reviewed target.
    p = project("P", [P1], [closure("r-e5", "ph1", "e5", polygon=rect(80, 40, 125, 60))])
    i = inp(network(), [p], scenario(("P",)), [req("any", "any-walker")])
    return Case("unreviewed_intersection", "A closure polygon touches e7 without a reviewed relation.", i,
                {"any": Outcome.NEEDS_VERIFICATION})


def case_open_boundary() -> Case:
    # Closures split {A, B, D} from {C, E, F}; the network continues past D and C.
    nodes = base_nodes(open_boundary=frozenset({"D", "C"}))
    p = project("P", [P1], [closure("r", "ph1", "e2", "e4", "e6")])
    i = inp(network(nodes=nodes), [p], scenario(("P",)), [req("any", "any-walker")])
    return Case("open_boundary_not_disconnection",
                "No modeled route, but the network continues past D and C, so disconnection is not bounded.", i,
                {"any": Outcome.INSUFFICIENT_COVERAGE})


def repair_base() -> tuple[NetworkSnapshot, list[ProjectSnapshot], Scenario]:
    """Dated joint disconnection with repair options (used by optimization tests)."""
    tc_edge = Edge(id="t-ef", u="D", v="F", kind=EdgeKind.TEMPORARY, geometry=((0, 50), (60, 58), (120, 50)),
                   attributes=std_attrs(width=1.8), existence_evidence=("fixture:temp-path",), label="temporary path D-F")
    p = project("P", [dated_phase("ph1", 0, 120)], [closure("r-e5", "ph1", "e5")])
    q = project("Q", [dated_phase("ph1", 60, 180)], [closure("r-e4e3", "ph1", "e4", "e3"), closure("r-e7", "ph1", "e7")],
                temps=[TemporaryConnection(id="tc-df", phase_ids=("ph1",), edge=tc_edge, active_by_default=False,
                                           evidence=("fixture:temp-path",))])
    scn = scenario(("P", "Q"), mode="dated", horizon=Interval(start=T0, end=T0 + timedelta(hours=4)))
    return network(), [p, q], scn


def all_cases() -> list[Case]:
    return [
        case_preserved_route(), case_single_closure(), case_step_free_alternative(), case_unknown_width(),
        case_conflicting_note(), case_crossing_lines(), case_missing_sheet(), case_endpoint_outside(),
        case_undated_phases(), case_joint_disconnection(), case_closure_during_traversal(), case_dated_overlap(),
        case_unreviewed_intersection(), case_open_boundary(),
    ]
