"""The flagship fictional scenario (Ashgrove campus, dated schedules).

Each project alone preserves the Halden Lab to Merrow Library journey; their
overlap leaves only an unreviewed courtyard path. Rejecting that path makes the
overlap a bounded disconnection, which the repair engine then addresses.
"""

from __future__ import annotations

from datetime import timedelta

from ghost_routes.domain.model import (
    AddedRestriction,
    AnalysisInput,
    Edge,
    EdgeKind,
    EvidenceState,
    Interval,
    TemporaryConnection,
)
from ghost_routes.optimization.repair import Intervention, InterventionKind, ShiftOption
from ghost_routes.synthetic.cases import (
    PROFILES,
    T0,
    accepted,
    base_edges,
    closure,
    dated_phase,
    edge,
    inp,
    network,
    project,
    rect,
    req,
    scenario,
    std_attrs,
)

SITE = rect(-10, -10, 140, 75)
UNIT = "crew-days (illustrative)"


def flagship_input() -> AnalysisInput:
    courtyard = edge("e8", "D", "F", kind=EdgeKind.PATH, geometry=((0, 50), (60, 66), (120, 50)),
                     attributes={"step_free": accepted(True, None, "fixture:model-proposal")},
                     label="courtyard path (model proposal, unreviewed)")
    courtyard = courtyard.model_copy(update={"existence": EvidenceState.UNRESOLVED,
                                             "existence_evidence": ("fixture:model-proposal",)})
    net = network([*base_edges(), courtyard], coverage=SITE, revision="ashgrove-net")
    ramp = Edge(id="t-ramp", u="C", v="F", kind=EdgeKind.TEMPORARY, geometry=((120, 0), (132, 25), (120, 50)),
                attributes=std_attrs(width=1.6), existence_evidence=("fixture:temp-ramp",), label="temporary ramp C-F")
    p = project("P", [dated_phase("ph1", 0, 120)], [closure("r-stairs", "ph1", "e5", label="stair repair closes C-F stairs")],
                extent=SITE, coverage=SITE,
                temps=[TemporaryConnection(id="tc-ramp", phase_ids=("ph1",), edge=ramp, active_by_default=False,
                                           evidence=("fixture:temp-ramp",))])
    q = project("Q", [dated_phase("ph1", 60, 180)],
                [closure("r-trench", "ph1", "e4", "e7", label="trench closes B-E crossing and E-F walk")],
                extent=SITE, coverage=SITE)
    scn = scenario(("P", "Q"), mode="dated", horizon=Interval(start=T0, end=T0 + timedelta(hours=4)),
                   label="Ashgrove spring works (fictional)")
    reqs = [
        req("lab-to-library", "width-1.5", label="Halden Lab to Merrow Library, clear width >= 1.5 m"),
        req("lab-to-library-stepfree", "step-free", label="Halden Lab to Merrow Library, step-free"),
        req("d-to-e", "any-walker", o="D", d="E", label="West lawn D to E (protected)"),
    ]
    return inp(net, [p, q], scn, reqs, PROFILES)


def flagship_interventions(project_prefix: str = "") -> list[Intervention]:
    pp, qq = f"{project_prefix}P", f"{project_prefix}Q"
    ramp_side_effect = AddedRestriction(project_id=pp, restriction=closure(
        "ramp-staging", "ph1", "e6", label="temporary ramp staging closes D-E"))
    return [
        Intervention(id="enable-ramp", kind=InterventionKind.ENABLE_TEMPORARY, label="Build the temporary ramp C-F",
                     connection_id="tc-ramp", cost=3, cost_unit=UNIT, side_effects=(ramp_side_effect,)),
        Intervention(id="keep-e7", kind=InterventionKind.KEEP_OPEN, label="Keep the E-F walk open during the trench",
                     restriction_id="r-trench", edge_ids=("e7",), cost=4, cost_unit=UNIT),
        Intervention(id="delay-q", kind=InterventionKind.SCHEDULE_SHIFT, label="Delay the trench phase",
                     project_id=qq, phase_id="ph1", cost_unit=UNIT,
                     options=(ShiftOption(seconds=1800, cost=3), ShiftOption(seconds=3600, cost=6))),
    ]

