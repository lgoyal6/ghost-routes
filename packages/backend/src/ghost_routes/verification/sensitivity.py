"""Review prioritization by outcome sensitivity.

A question is worth asking when some resolution of the fact changes a
requirement's outcome. Ranking uses the count of affected requirements over a
declared review-cost assumption; it is not a calibrated information gain.
"""

from __future__ import annotations

from ghost_routes.domain.model import AnalysisInput, Comparator, Outcome, Scalar
from ghost_routes.domain.results import AnalysisResult, Fact, FactKind, ReviewQuestion
from ghost_routes.domain.units import normalize
from ghost_routes.scenarios.compiler import baseline_state, compile_states
from ghost_routes.verification.classify import GeometryIndex, classify_state
from ghost_routes.verification.verifier import verify

# Declared assumptions about relative reviewer effort per fact kind.
DEFAULT_REVIEW_COST: dict[FactKind, float] = {
    FactKind.EDGE_EXISTENCE: 1.0,
    FactKind.ATTRIBUTE_UNKNOWN: 2.0,  # usually needs a site measurement
    FactKind.ATTRIBUTE_UNRESOLVED: 1.0,
    FactKind.ATTRIBUTE_CONFLICT: 1.0,
    FactKind.RESTRICTION_UNRESOLVED: 1.0,
    FactKind.RESTRICTION_TARGET: 1.0,
    FactKind.UNREVIEWED_INTERSECTION: 1.0,
    FactKind.TEMPORARY_CONNECTION: 1.0,
}

_SKIP = {FactKind.UNCALIBRATED_GEOMETRY, FactKind.BASELINE_UNAVAILABLE, FactKind.TEMPORAL_SEARCH_BOUND, FactKind.SOFT_UNKNOWN}


def collect_facts(inp: AnalysisInput) -> dict[str, Fact]:
    """Every unresolved fact on a non-blocked edge in any evaluated state."""
    geo = GeometryIndex(inp.network, tuple(inp.project(p) for p in inp.scenario.project_ids))
    states = [baseline_state(), *compile_states(inp)]
    facts: dict[str, Fact] = {}
    for req in inp.requirements:
        prof = inp.profile(req.profile_id)
        for st in states:
            cs = classify_state(inp.network, st, prof, geo, dict(inp.assumptions))
            for a in cs.assessments.values():
                for f in a.facts:
                    if f.kind not in _SKIP:
                        facts.setdefault(f.id, f)
    return facts


def _attribute_resolutions(inp: AnalysisInput, fact: Fact) -> list[Scalar | None]:
    attr = fact.id.split(":attr:", 1)[1]
    if fact.kind == FactKind.ATTRIBUTE_CONFLICT and fact.alternatives:
        return list(fact.alternatives)
    good: list[Scalar] = []
    bad: list[Scalar] = []
    for prof in inp.profiles:
        for c in prof.hard():
            if c.attribute != attr:
                continue
            thr, _ = normalize(c.threshold, c.unit)
            if c.comparator == Comparator.EQ:
                good.append(thr)
                if isinstance(thr, bool):
                    bad.append(not thr)
            elif c.comparator == Comparator.GE and isinstance(thr, (int, float)):
                good.append(float(thr))
                bad.append(float(thr) / 2 if thr > 0 else float(thr) - 1)
            elif c.comparator == Comparator.LE and isinstance(thr, (int, float)):
                good.append(float(thr))
                bad.append(float(thr) * 2 + 1)
    res: list[Scalar | None] = []
    if good:
        nums = [g for g in good if isinstance(g, (int, float)) and not isinstance(g, bool)]
        res.append(max(nums) if nums and all(
            c.comparator == Comparator.GE for p in inp.profiles for c in p.hard() if c.attribute == attr
        ) else good[0])
    if bad:
        res.append(bad[0])
    return list(dict.fromkeys(res))


def resolutions(inp: AnalysisInput, fact: Fact) -> list[Scalar | None]:
    if ":attr:" in fact.id:
        return _attribute_resolutions(inp, fact)
    return [True, False]


def outcomes(result: AnalysisResult) -> dict[str, Outcome]:
    return {r.requirement_id: r.outcome for r in result.requirements}


def review_priorities(
    inp: AnalysisInput,
    *,
    review_cost: dict[FactKind, float] | None = None,
    max_facts: int = 200,
) -> tuple[list[ReviewQuestion], dict[str, object]]:
    costs = {**DEFAULT_REVIEW_COST, **(review_cost or {})}
    base = outcomes(verify(inp))
    facts = collect_facts(inp)
    ordered = sorted(facts.values(), key=lambda f: f.id)
    truncated = len(ordered) > max_facts
    questions: list[ReviewQuestion] = []
    for fact in ordered[:max_facts]:
        options = resolutions(inp, fact)
        if len(options) < 2:
            continue
        per_option: list[dict[str, Outcome]] = []
        for value in options:
            alt = inp.model_copy(update={"assumptions": {**inp.assumptions, fact.id: value}})
            per_option.append(outcomes(verify(alt)))
        affected = sorted(
            rid for rid in base if len({o[rid] for o in per_option}) > 1
        )
        if not affected:
            continue
        cost = costs.get(fact.kind, 1.0)
        questions.append(
            ReviewQuestion(
                fact=fact,
                affected=tuple(affected),
                outcome_if_favorable=per_option[0],
                outcome_if_unfavorable=per_option[-1],
                review_cost=cost,
                score=round(len(affected) / cost, 6),
            )
        )
    questions.sort(key=lambda q: (-q.score, -len(q.affected), q.fact.id))
    meta: dict[str, object] = {
        "facts_considered": min(len(ordered), max_facts),
        "facts_total": len(ordered),
        "truncated": truncated,
        "baseline_outcomes": {k: v.value for k, v in base.items()},
        "cost_assumptions": {k.value: v for k, v in costs.items()},
        "method": "outcome sensitivity over explicit alternative resolutions; not calibrated information gain",
    }
    return questions, meta
