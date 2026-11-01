"""Bounded counterfactual repair search.

Candidates are selections of reviewer-allowed interventions. Each candidate
becomes a labeled hypothetical scenario branch and is verified from scratch by
the same pure verifier against every restored and protected requirement.
Solver statuses reflect what was actually proven.
"""

from __future__ import annotations

import itertools
import time
from enum import StrEnum
from typing import Any, Literal

from ortools.sat.python import cp_model
from pydantic import Field, model_validator

from ghost_routes.domain.model import (
    OUTCOME_SEVERITY,
    AddedRestriction,
    AnalysisInput,
    Frozen,
    Interval,
    Outcome,
    Restriction,
    RestrictionWindowOverride,
)
from ghost_routes.scenarios.compiler import ScenarioError, check_prerequisites
from ghost_routes.verification.classify import InvalidModel
from ghost_routes.verification.verifier import verify


class InterventionKind(StrEnum):
    SCHEDULE_SHIFT = "schedule_shift"
    ENABLE_TEMPORARY = "enable_temporary_connection"
    KEEP_OPEN = "keep_segment_open"


class ShiftOption(Frozen):
    seconds: int
    cost: float = Field(ge=0)


class Intervention(Frozen):
    id: str
    kind: InterventionKind
    label: str
    cost_unit: str
    cost: float = Field(default=0.0, ge=0)
    # schedule_shift
    project_id: str | None = None
    phase_id: str | None = None
    options: tuple[ShiftOption, ...] = ()
    # enable_temporary_connection
    connection_id: str | None = None
    # keep_segment_open
    restriction_id: str | None = None
    edge_ids: tuple[str, ...] = ()
    window: Interval | None = None
    # Side effects that come with this intervention (e.g. building a temporary
    # path closes another segment). Non-empty side effects make it non-monotone.
    side_effects: tuple[AddedRestriction, ...] = ()
    requires: tuple[str, ...] = ()
    excludes: tuple[str, ...] = ()
    construction_feasibility: Literal["unassessed", "reviewed_feasible", "reviewed_infeasible"] = "unassessed"

    @model_validator(mode="after")
    def _shape(self) -> Intervention:
        k = self.kind
        if k == InterventionKind.SCHEDULE_SHIFT and (not self.project_id or not self.phase_id or not self.options):
            raise ValueError("schedule shifts need project_id, phase_id and options")
        if k == InterventionKind.ENABLE_TEMPORARY and not self.connection_id:
            raise ValueError("enabling a temporary connection needs connection_id")
        if k == InterventionKind.KEEP_OPEN and (not self.restriction_id or not self.edge_ids):
            raise ValueError("keeping a segment open needs restriction_id and edge_ids")
        return self

    @property
    def monotone(self) -> bool:
        """True when selecting it can only add capability."""
        return self.kind != InterventionKind.SCHEDULE_SHIFT and not self.side_effects

    def choices(self) -> int:
        return len(self.options) if self.kind == InterventionKind.SCHEDULE_SHIFT else 1


class RepairProblem(Frozen):
    base: AnalysisInput
    interventions: tuple[Intervention, ...]
    restore: tuple[str, ...]
    protect: tuple[str, ...] = ()
    max_candidates: int = Field(default=3, ge=1)
    method: Literal["auto", "enumerate", "cpsat"] = "auto"
    enumerate_limit: int = 4096
    max_evaluations: int = Field(default=500, ge=1)
    time_limit_s: float = Field(default=30.0, gt=0)


class RepairStatus(StrEnum):
    OPTIMAL = "OPTIMAL"
    FEASIBLE = "FEASIBLE"
    INFEASIBLE = "INFEASIBLE"
    UNKNOWN = "UNKNOWN"
    INVALID_MODEL = "INVALID_MODEL"
    ERROR = "ERROR"


# Selection: intervention id -> option index (0 for binary interventions).
Selection = dict[str, int]


class CandidateEvaluation(Frozen):
    selection: dict[str, int]
    cost: float
    accepted: bool
    outcomes: dict[str, Outcome]
    baseline_outcomes: dict[str, Outcome]
    reasons: tuple[str, ...]
    input_hash: str
    scenario_revision_id: str
    construction_feasibility: dict[str, str]


class RepairResult(Frozen):
    status: RepairStatus
    method: str
    cost_unit: str | None
    objective: float | None = None
    bound: float | None = None
    candidates: tuple[CandidateEvaluation, ...] = ()
    rejected: tuple[CandidateEvaluation, ...] = ()
    evaluations: int = 0
    detail: str = ""


def _canon_cost(x: float) -> int:
    return round(x * 1000)


def selection_cost(problem: RepairProblem, sel: Selection) -> float:
    total = 0.0
    by_id = {i.id: i for i in problem.interventions}
    for iid, k in sel.items():
        iv = by_id[iid]
        total += iv.options[k].cost if iv.kind == InterventionKind.SCHEDULE_SHIFT else iv.cost
    return round(total, 9)


def apply_selection(problem: RepairProblem, sel: Selection) -> AnalysisInput:
    base = problem.base
    scn = base.scenario
    enabled = list(scn.enabled_temporary_connections)
    overrides = list(scn.disabled_restriction_windows)
    shifts = dict(scn.phase_shifts)
    added = list(scn.added_restrictions)
    by_id = {i.id: i for i in problem.interventions}
    for iid in sorted(sel):
        iv = by_id[iid]
        if iv.kind == InterventionKind.ENABLE_TEMPORARY:
            assert iv.connection_id
            enabled.append(iv.connection_id)
        elif iv.kind == InterventionKind.KEEP_OPEN:
            assert iv.restriction_id
            overrides.append(RestrictionWindowOverride(restriction_id=iv.restriction_id, edge_ids=iv.edge_ids, window=iv.window))
        else:
            key = f"{iv.project_id}/{iv.phase_id}"
            shifts[key] = shifts.get(key, 0) + iv.options[sel[iid]].seconds
        added.extend(iv.side_effects)
    tag = "+".join(f"{k}={v}" for k, v in sorted(sel.items())) or "none"
    scenario = scn.model_copy(update={
        "revision_id": f"{scn.revision_id}~repair[{tag}]",
        "label": f"{scn.label} (hypothetical repair: {tag})",
        "enabled_temporary_connections": tuple(sorted(set(enabled))),
        "disabled_restriction_windows": tuple(overrides),
        "phase_shifts": shifts,
        "added_restrictions": tuple(added),
        "hypothetical": True,
    })
    return base.model_copy(update={"scenario": scenario})


def _validate(problem: RepairProblem) -> str | None:
    units = {i.cost_unit for i in problem.interventions}
    if len(units) > 1:
        return f"interventions mix cost units {sorted(units)}; costs are not summed across units"
    ids = [i.id for i in problem.interventions]
    if len(set(ids)) != len(ids):
        return "duplicate intervention ids"
    known_reqs = {r.id for r in problem.base.requirements}
    for rid in (*problem.restore, *problem.protect):
        if rid not in known_reqs:
            return f"unknown requirement {rid}"
    if not problem.restore:
        return "nothing to restore"
    scn = problem.base.scenario
    projects = {p.project_id: p for p in problem.base.projects}
    for iv in problem.interventions:
        if iv.kind == InterventionKind.SCHEDULE_SHIFT:
            if scn.mode != "dated":
                return f"{iv.id}: schedule shifts need a dated scenario"
            p = projects.get(iv.project_id or "")
            if p is None or iv.phase_id not in {ph.id for ph in p.phases}:
                return f"{iv.id}: unknown phase {iv.project_id}/{iv.phase_id}"
        if iv.kind == InterventionKind.ENABLE_TEMPORARY:
            conns = {c.id for p in projects.values() for c in p.temporary_connections}
            if iv.connection_id not in conns:
                return f"{iv.id}: unknown temporary connection {iv.connection_id}"
        if iv.kind == InterventionKind.KEEP_OPEN:
            rs = {r.id: r for p in projects.values() for r in p.restrictions}
            r: Restriction | None = rs.get(iv.restriction_id or "")
            if r is None:
                return f"{iv.id}: unknown restriction {iv.restriction_id}"
            targets = {t.edge_id for t in r.targets}
            if not set(iv.edge_ids) <= targets:
                return f"{iv.id}: edges {sorted(set(iv.edge_ids) - targets)} are not reviewed targets of {r.id}"
        for other in (*iv.requires, *iv.excludes):
            if other not in ids:
                return f"{iv.id}: references unknown intervention {other}"
    return None


def _structurally_valid(problem: RepairProblem, sel: Selection) -> str | None:
    for iid in sel:
        iv = next(i for i in problem.interventions if i.id == iid)
        for r in iv.requires:
            if r not in sel:
                return f"{iid} requires {r}"
        for x in iv.excludes:
            if x in sel:
                return f"{iid} excludes {x}"
    return None


class _Evaluator:
    def __init__(self, problem: RepairProblem):
        self.problem = problem
        base_result = verify(problem.base)
        self.baseline = {r.requirement_id: r.outcome for r in base_result.requirements}
        self.count = 0

    def evaluate(self, sel: Selection) -> CandidateEvaluation:
        p = self.problem
        self.count += 1
        reasons: list[str] = []
        outcomes: dict[str, Outcome] = {}
        branch = apply_selection(p, sel)
        input_hash = ""
        structural = _structurally_valid(p, sel)
        if structural:
            reasons.append(structural)
        else:
            problems = [msg for proj in branch.projects for msg in check_prerequisites(proj, branch.scenario)]
            if problems:
                reasons.extend(problems)
            else:
                # Fresh, independent verifier invocation on the hypothetical branch.
                res = verify(branch)
                input_hash = res.input_hash
                outcomes = {r.requirement_id: r.outcome for r in res.requirements}
                for rid in p.restore:
                    if outcomes[rid] != Outcome.SUPPORTED:
                        reasons.append(f"{rid} remains {outcomes[rid].value}")
                for rid in p.protect:
                    if OUTCOME_SEVERITY[outcomes[rid]] > OUTCOME_SEVERITY[self.baseline[rid]]:
                        reasons.append(f"protected {rid} regresses from {self.baseline[rid].value} to {outcomes[rid].value}")
        by_id = {i.id: i for i in p.interventions}
        return CandidateEvaluation(
            selection=dict(sorted(sel.items())),
            cost=selection_cost(p, sel),
            accepted=not reasons,
            outcomes=outcomes,
            baseline_outcomes=self.baseline,
            reasons=tuple(reasons),
            input_hash=input_hash,
            scenario_revision_id=branch.scenario.revision_id,
            construction_feasibility={i: by_id[i].construction_feasibility for i in sorted(sel)},
        )


def _all_selections(problem: RepairProblem) -> list[Selection]:
    axes = []
    for iv in problem.interventions:
        axes.append([None, *range(iv.choices())])
    sels = []
    for combo in itertools.product(*axes):
        sels.append({iv.id: k for iv, k in zip(problem.interventions, combo, strict=True) if k is not None})
    sels.sort(key=lambda s: (_canon_cost(selection_cost(problem, s)), len(s), sorted(s.items())))
    return sels


def _space_size(problem: RepairProblem) -> int:
    n = 1
    for iv in problem.interventions:
        n *= iv.choices() + 1
    return n


def solve(problem: RepairProblem) -> RepairResult:
    unit = problem.interventions[0].cost_unit if problem.interventions else None
    msg = _validate(problem)
    if msg:
        return RepairResult(status=RepairStatus.INVALID_MODEL, method="none", cost_unit=unit, detail=msg)
    try:
        ev = _Evaluator(problem)
        already = [rid for rid in problem.restore if ev.baseline[rid] == Outcome.SUPPORTED]
        method = problem.method
        if method == "auto":
            method = "enumerate" if _space_size(problem) <= problem.enumerate_limit else "cpsat"
        if method == "enumerate":
            return _enumerate(problem, ev, unit, already)
        return _cpsat(problem, ev, unit, already)
    except (ScenarioError, InvalidModel) as exc:
        return RepairResult(status=RepairStatus.INVALID_MODEL, method=problem.method, cost_unit=unit, detail=str(exc))


def _enumerate(problem: RepairProblem, ev: _Evaluator, unit: str | None, already: list[str]) -> RepairResult:
    accepted: list[CandidateEvaluation] = []
    rejected: list[CandidateEvaluation] = []
    deadline = time.monotonic() + problem.time_limit_s
    sels = _all_selections(problem)
    exhausted = True
    for sel in sels:
        if len(accepted) >= problem.max_candidates:
            break
        if ev.count >= problem.max_evaluations or time.monotonic() > deadline:
            exhausted = False
            break
        res = ev.evaluate(sel)
        (accepted if res.accepted else rejected).append(res)
    detail = f"exhaustive enumeration over {len(sels)} selections in cost order"
    if already:
        detail += f"; already supported without repair: {already}"
    if accepted:
        # Enumeration is in nondecreasing cost order, so the first accepted is minimal.
        return RepairResult(status=RepairStatus.OPTIMAL, method="enumerate", cost_unit=unit,
                            objective=accepted[0].cost, bound=accepted[0].cost, candidates=tuple(accepted),
                            rejected=tuple(rejected[:20]), evaluations=ev.count, detail=detail)
    status = RepairStatus.INFEASIBLE if exhausted else RepairStatus.UNKNOWN
    if not exhausted:
        detail += f"; stopped after {ev.count} evaluations without proving infeasibility"
    lower = rejected[-1].cost if rejected and not exhausted else None
    return RepairResult(status=status, method="enumerate", cost_unit=unit, bound=lower,
                        rejected=tuple(rejected[:20]), evaluations=ev.count, detail=detail)


def _cpsat(problem: RepairProblem, ev: _Evaluator, unit: str | None, already: list[str]) -> RepairResult:
    model = cp_model.CpModel()
    # Boolean per (intervention, option); at most one option per intervention.
    lits: dict[tuple[str, int], cp_model.IntVar] = {}
    chosen: dict[str, cp_model.IntVar] = {}
    obj = []
    for iv in problem.interventions:
        opts = [model.new_bool_var(f"{iv.id}#{k}") for k in range(iv.choices())]
        for k, lit in enumerate(opts):
            lits[(iv.id, k)] = lit
            c = iv.options[k].cost if iv.kind == InterventionKind.SCHEDULE_SHIFT else iv.cost
            obj.append(_canon_cost(c) * lit)
        any_lit = model.new_bool_var(f"{iv.id}?")
        model.add(sum(opts) == any_lit)
        chosen[iv.id] = any_lit
    for iv in problem.interventions:
        for r in iv.requires:
            model.add_implication(chosen[iv.id], chosen[r])
        for x in iv.excludes:
            model.add_bool_or([chosen[iv.id].Not(), chosen[x].Not()])
    model.minimize(sum(obj))

    accepted: list[CandidateEvaluation] = []
    rejected: list[CandidateEvaluation] = []
    deadline = time.monotonic() + problem.time_limit_s
    first_proven = False
    last_status = "UNKNOWN"
    best_bound: float | None = None
    by_id = {i.id: i for i in problem.interventions}
    while len(accepted) < problem.max_candidates:
        remaining = deadline - time.monotonic()
        if remaining <= 0 or ev.count >= problem.max_evaluations:
            last_status = "LIMIT"
            break
        solver = cp_model.CpSolver()
        solver.parameters.max_time_in_seconds = remaining
        solver.parameters.num_workers = 1
        solver.parameters.random_seed = 0
        st = solver.solve(model)
        if st == cp_model.INFEASIBLE:
            last_status = "INFEASIBLE"
            break
        if st == cp_model.MODEL_INVALID:
            return RepairResult(status=RepairStatus.INVALID_MODEL, method="cpsat", cost_unit=unit,
                                detail=model.validate(), evaluations=ev.count)
        if st not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            last_status = "UNKNOWN"
            break
        proven = st == cp_model.OPTIMAL
        if best_bound is None:
            best_bound = solver.best_objective_bound / 1000
        sel = {iid: k for (iid, k), lit in lits.items() if solver.value(lit)}
        res = ev.evaluate(sel)
        # Cut: monotone interventions not selected, or any non-monotone choice changed.
        terms: list[Any] = []
        for iv in problem.interventions:
            if iv.monotone:
                if iv.id not in sel:
                    terms.append(chosen[iv.id])
            elif iv.id in sel:
                terms.append(lits[(iv.id, sel[iv.id])].Not())
            else:
                terms.append(chosen[iv.id])
        if res.accepted:
            if not accepted:
                first_proven = proven
            accepted.append(res)
            # Exclude exactly this selection to look for further distinct candidates.
            exact = [lits[(iid, k)].Not() for iid, k in sel.items()] + [
                chosen[i.id] for i in problem.interventions if i.id not in sel
            ]
            model.add_bool_or(exact or [model.new_constant(0)])
        else:
            rejected.append(res)
            model.add_bool_or(terms or [model.new_constant(0)])
    detail = "CP-SAT master with verifier-generated cuts (subset cuts for monotone interventions, no-good cuts otherwise)"
    if already:
        detail += f"; already supported without repair: {already}"
    _ = by_id
    if accepted:
        status = RepairStatus.OPTIMAL if first_proven else RepairStatus.FEASIBLE
        return RepairResult(status=status, method="cpsat", cost_unit=unit, objective=accepted[0].cost,
                            bound=accepted[0].cost if first_proven else best_bound, candidates=tuple(accepted),
                            rejected=tuple(rejected[:20]), evaluations=ev.count, detail=detail)
    if last_status == "INFEASIBLE":
        return RepairResult(status=RepairStatus.INFEASIBLE, method="cpsat", cost_unit=unit,
                            rejected=tuple(rejected[:20]), evaluations=ev.count,
                            detail=detail + "; every selection is excluded by verified cuts")
    return RepairResult(status=RepairStatus.UNKNOWN, method="cpsat", cost_unit=unit, bound=best_bound,
                        rejected=tuple(rejected[:20]), evaluations=ev.count,
                        detail=detail + f"; stopped ({last_status}) before proving optimality or infeasibility")
