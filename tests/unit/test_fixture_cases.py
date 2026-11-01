"""Exact expected outcomes for every synthetic fixture case."""

from __future__ import annotations

from datetime import timedelta

import pytest

from ghost_routes.domain.canonical import canonical_json
from ghost_routes.domain.model import Outcome, StateBasis, StateSpec
from ghost_routes.domain.results import FactKind, GapKind
from ghost_routes.optimization.repair import solve
from ghost_routes.scenarios.compiler import ScenarioError
from ghost_routes.synthetic import cases as C
from ghost_routes.synthetic.repairs import all_repair_cases
from ghost_routes.verification.verifier import verify


@pytest.mark.parametrize("case", C.all_cases(), ids=lambda c: c.name)
def test_case_outcomes(case: C.Case) -> None:
    res = verify(case.input)
    got = {r.requirement_id: r.outcome for r in res.requirements}
    assert got == case.expected
    for r in res.requirements:
        for s in r.states:
            key = (r.requirement_id, s.label)
            if key in case.expected_states:
                assert s.outcome == case.expected_states[key], key


@pytest.mark.parametrize("case", C.all_cases(), ids=lambda c: c.name)
def test_replay_is_byte_stable(case: C.Case) -> None:
    assert canonical_json(verify(case.input)) == canonical_json(verify(case.input))


def _pair(res, rid: str, label_prefix: str):  # type: ignore[no-untyped-def]
    r = next(x for x in res.requirements if x.requirement_id == rid)
    s = next(s for s in r.states if s.label.startswith(label_prefix))
    return s.pairs[0]


def test_single_closure_reports_added_distance_and_boundary() -> None:
    res = verify(C.case_single_closure().input)
    p = _pair(res, "to-C", "P / Phase 1")
    assert p.added_distance_m == pytest.approx(100.0)
    assert p.baseline_route is not None and p.baseline_route.length_m == pytest.approx(120.0)
    budget = _pair(res, "to-C-budget", "P / Phase 1")
    assert budget.budget_m == pytest.approx(170.0)
    assert budget.route is None
    assert budget.budget_exceeded_tiers == ("conservative", "possible_covered", "possible")


def test_step_free_route_avoids_stairs() -> None:
    res = verify(C.case_step_free_alternative().input)
    p = _pair(res, "stepfree", "P / Phase 1")
    assert p.route is not None
    assert [s.edge_id for s in p.route.steps] == ["e1", "e2", "ramp1"]
    assert all(c.satisfied for s in p.route.steps for c in s.checks if c.hard)


def test_unknown_width_names_the_exact_missing_fact() -> None:
    res = verify(C.case_unknown_width().input)
    p = _pair(res, "width", "P / Phase 1")
    assert [f.id for f in p.uncertainties] == ["edge:e7:attr:clear_width"]
    assert p.uncertainties[0].kind == FactKind.ATTRIBUTE_UNKNOWN


def test_conflict_is_preserved_not_tie_broken() -> None:
    res = verify(C.case_conflicting_note().input)
    p = _pair(res, "width", "P / Phase 1")
    (fact,) = p.uncertainties
    assert fact.kind == FactKind.ATTRIBUTE_CONFLICT
    assert fact.alternatives == (1.2, 1.8)
    assert set(fact.evidence) == {"fixture:note-says-1.2", "fixture:diagram-says-1.8"}


def test_missing_second_project_sheet_beats_open_baseline() -> None:
    res = verify(C.case_missing_sheet().input)
    r = res.requirements[0]
    assert r.states[0].outcome == Outcome.SUPPORTED  # baseline looks open
    p = r.states[1].pairs[0]
    assert p.outcome == Outcome.INSUFFICIENT_COVERAGE
    assert {g.project_id for g in p.coverage_gaps if g.kind == GapKind.PROJECT_COVERAGE} == {"Q"}


def test_unrelated_missing_data_does_not_invalidate_supported_route() -> None:
    # Remove coverage of a far corner that no route or component touches.
    base = C.case_preserved_route().input
    nodes = [*C.base_nodes(), C.Node(id="Y", x=500, y=500)]
    edges = [*C.base_edges(), C.edge("far", "Y", "Y2", geometry=((500, 500), (520, 500)))]
    nodes.append(C.Node(id="Y2", x=520, y=500))
    net = C.network(edges, nodes)
    res = verify(base.model_copy(update={"network": net}))
    assert res.requirements[0].outcome == Outcome.SUPPORTED


def test_endpoint_outside_coverage_gap_named() -> None:
    res = verify(C.case_endpoint_outside().input)
    p = res.requirements[0].states[0].pairs[0]
    assert any(g.kind == GapKind.ENDPOINT_OUTSIDE and g.target == "Z" for g in p.coverage_gaps)


def test_undated_projects_cannot_be_combined_implicitly() -> None:
    case = C.case_undated_phases()
    bad = case.input.scenario.model_copy(update={"states": (
        StateSpec(label="both", active={"P": "ph1", "Q": "ph1"}, basis=StateBasis.PROJECT_ISOLATED),)})
    with pytest.raises(ScenarioError, match="hypothetical concurrent"):
        verify(case.input.model_copy(update={"scenario": bad}))


def test_declared_concurrency_is_labeled_hypothetical() -> None:
    res = verify(C.case_joint_disconnection().input)
    s = next(s for s in res.requirements[0].states if s.basis == StateBasis.DECLARED_CONCURRENT)
    assert s.hypothetical
    p = s.pairs[0]
    restriction_ids = {r.restriction_id for b in p.boundary for r in b.reasons}
    assert restriction_ids == {"r-e5", "r-e4e3"}


def test_closure_during_traversal_half_open_boundary() -> None:
    res = verify(C.case_closure_during_traversal().input)
    window = res.requirements[0].states[-1]
    assert window.label == "departure window"
    gaps = window.pairs[0].departure_gaps
    conservative = [g for g in gaps if g.level == "conservative"]
    assert len(conservative) == 1
    # Departing exactly at T0+30s finishes e2 at T0+150s, the closure start: allowed.
    assert conservative[0].start == C.T0 + timedelta(seconds=30, milliseconds=1)
    assert conservative[0].end == C.T0 + timedelta(seconds=60)


def test_dated_overlap_only_fails_inside_overlap() -> None:
    res = verify(C.case_dated_overlap().input)
    bad = [s for s in res.requirements[0].states if s.outcome != Outcome.SUPPORTED]
    assert [(s.interval.start, s.interval.end) for s in bad if s.interval] == [
        (C.T0 + timedelta(minutes=60), C.T0 + timedelta(minutes=120))
    ]


def test_unreviewed_intersection_becomes_question() -> None:
    res = verify(C.case_unreviewed_intersection().input)
    p = res.requirements[0].states[1].pairs[0]
    assert [f.id for f in p.uncertainties] == ["restriction:r-e5:target:e7"]


@pytest.mark.parametrize("case", all_repair_cases(), ids=lambda c: c.name)
def test_repair_cases(case) -> None:  # type: ignore[no-untyped-def]
    r = solve(case.problem)
    assert r.status == case.expected_status, r.detail
    if case.expected_cost is not None:
        assert r.objective == case.expected_cost
    if case.acceptable_first:
        assert r.candidates[0].selection in case.acceptable_first
    for cand in r.candidates:
        assert cand.accepted and cand.input_hash
        assert all(cand.outcomes[rid] == Outcome.SUPPORTED for rid in case.problem.restore)


def test_harmful_repair_is_rejected_with_reason() -> None:
    case = next(c for c in all_repair_cases() if c.name == "repair_harms_other_requirement")
    r = solve(case.problem)
    harmful = [x for x in r.rejected if x.selection == {"enable-tc": 0, "open-e3": 0}]
    assert harmful and any("protected d-to-e regresses" in reason for reason in harmful[0].reasons)
