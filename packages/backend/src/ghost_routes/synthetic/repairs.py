"""Synthetic repair problems with exactly known answers."""

from __future__ import annotations

from dataclasses import dataclass

from ghost_routes.domain.model import AddedRestriction, AnalysisInput
from ghost_routes.optimization.repair import (
    Intervention,
    InterventionKind,
    RepairProblem,
    RepairStatus,
    ShiftOption,
)
from ghost_routes.synthetic.cases import closure, inp, repair_base, req

UNIT = "crew-days (illustrative)"


def base_input(extra_reqs: bool = False) -> AnalysisInput:
    net, projects, scn = repair_base()
    reqs = [req("lab-to-library", "any-walker", label="Halden Lab to Merrow Library")]
    if extra_reqs:
        reqs.append(req("d-to-e", "any-walker", o="D", d="E", label="D to E (protected)"))
    return inp(net, projects, scn, reqs)


def interventions(side_effect: bool = False) -> tuple[Intervention, ...]:
    tc_side: tuple[AddedRestriction, ...] = ()
    if side_effect:
        tc_side = (AddedRestriction(project_id="Q", restriction=closure("tc-build-closes-e6", "ph1", "e6",
                                                                          label="building the temporary path closes e6")),)
    return (
        Intervention(id="shift-q", kind=InterventionKind.SCHEDULE_SHIFT, label="Delay project Q phase 1",
                     project_id="Q", phase_id="ph1", cost_unit=UNIT,
                     options=(ShiftOption(seconds=1800, cost=3), ShiftOption(seconds=3600, cost=6))),
        Intervention(id="enable-tc", kind=InterventionKind.ENABLE_TEMPORARY, label="Build temporary path D-F",
                     connection_id="tc-df", cost=3, cost_unit=UNIT, side_effects=tc_side),
        Intervention(id="open-e3", kind=InterventionKind.KEEP_OPEN, label="Keep A-D (e3) open during Q",
                     restriction_id="r-e4e3", edge_ids=("e3",), cost=2, cost_unit=UNIT),
        Intervention(id="open-e4", kind=InterventionKind.KEEP_OPEN, label="Keep B-E crossing (e4) open during Q",
                     restriction_id="r-e4e3", edge_ids=("e4",), cost=2, cost_unit=UNIT),
        Intervention(id="open-e7", kind=InterventionKind.KEEP_OPEN, label="Keep E-F (e7) open during Q",
                     restriction_id="r-e7", edge_ids=("e7",), cost=4, cost_unit=UNIT),
    )


@dataclass
class RepairCase:
    name: str
    description: str
    problem: RepairProblem
    expected_status: RepairStatus
    expected_cost: float | None = None
    # Any of these selections is an acceptable first (minimum-cost) candidate.
    acceptable_first: tuple[dict[str, int], ...] = ()


# Two selections tie at cost 5: A-D-F via e3, or A-B-E-D-F via e4 and e6.
TIED_OPTIMA = ({"enable-tc": 0, "open-e3": 0}, {"enable-tc": 0, "open-e4": 0})


def all_repair_cases() -> list[RepairCase]:
    ok = RepairProblem(base=base_input(), interventions=interventions(), restore=("lab-to-library",))
    return [
        RepairCase("successful_repair",
                   "Keeping e3 (or e4) open plus the temporary path restores the journey at cost 5.",
                   ok, RepairStatus.OPTIMAL, 5.0, TIED_OPTIMA),
        RepairCase("successful_repair_cpsat", "Same problem solved by the CP-SAT master with verifier cuts.",
                   ok.model_copy(update={"method": "cpsat"}), RepairStatus.OPTIMAL, 5.0, TIED_OPTIMA),
        RepairCase("repair_harms_other_requirement",
                   "The temporary path's construction closes e6 and would cut protected D to E; the next option wins.",
                   RepairProblem(base=base_input(extra_reqs=True), interventions=interventions(side_effect=True),
                                 restore=("lab-to-library",), protect=("d-to-e",)),
                   RepairStatus.OPTIMAL, 6.0),
        RepairCase("optimizer_infeasible", "Only the temporary path is allowed, and A cannot reach it.",
                   RepairProblem(base=base_input(), interventions=(interventions()[1],), restore=("lab-to-library",)),
                   RepairStatus.INFEASIBLE),
        RepairCase("optimizer_infeasible_cpsat", "Infeasibility proven by CP-SAT cuts.",
                   RepairProblem(base=base_input(), interventions=(interventions()[1],), restore=("lab-to-library",),
                                 method="cpsat"),
                   RepairStatus.INFEASIBLE),
        RepairCase("optimizer_unknown", "A one-evaluation budget stops the search before anything is proven.",
                   ok.model_copy(update={"max_evaluations": 1}), RepairStatus.UNKNOWN),
        RepairCase("optimizer_unknown_cpsat", "CP-SAT search stopped by the evaluation budget.",
                   ok.model_copy(update={"max_evaluations": 1, "method": "cpsat"}), RepairStatus.UNKNOWN),
    ]
