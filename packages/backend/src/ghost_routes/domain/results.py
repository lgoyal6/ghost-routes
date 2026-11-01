"""Typed verifier outputs. Every list is emitted in a stable order."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import Field

from ghost_routes.domain.model import (
    EdgeKind,
    EvidenceState,
    Frozen,
    Interval,
    Outcome,
    Scalar,
    StateBasis,
)


class EdgeClass(StrEnum):
    CONSERVATIVE = "CONSERVATIVE"
    POSSIBLE_COVERED = "POSSIBLE_COVERED"
    POSSIBLE_UNCOVERED = "POSSIBLE_UNCOVERED"
    BLOCKED = "BLOCKED"


class FactKind(StrEnum):
    EDGE_EXISTENCE = "edge_existence"
    ATTRIBUTE_UNKNOWN = "attribute_unknown"
    ATTRIBUTE_UNRESOLVED = "attribute_unresolved"
    ATTRIBUTE_CONFLICT = "attribute_conflict"
    RESTRICTION_UNRESOLVED = "restriction_unresolved"
    RESTRICTION_TARGET = "restriction_target"
    UNREVIEWED_INTERSECTION = "unreviewed_intersection"
    TEMPORARY_CONNECTION = "temporary_connection"
    UNCALIBRATED_GEOMETRY = "uncalibrated_geometry"
    BASELINE_UNAVAILABLE = "baseline_unavailable"
    SOFT_UNKNOWN = "soft_unknown"
    TEMPORAL_SEARCH_BOUND = "temporal_search_bound"


class Fact(Frozen):
    """An unresolved fact that a route or result depends on.

    ``id`` is the key used for review prioritization and for explicit
    ``assumptions`` overrides.
    """

    id: str
    kind: FactKind
    target: str
    description: str
    alternatives: tuple[Scalar | None, ...] = ()
    evidence: tuple[str, ...] = ()
    # Value that makes the target usable, used by sensitivity analysis.
    favorable: Scalar | None = None
    unfavorable: Scalar | None = None


class GapKind(StrEnum):
    NETWORK_COVERAGE = "network_coverage"
    PROJECT_COVERAGE = "project_coverage"
    ENDPOINT_OUTSIDE = "endpoint_outside"
    OPEN_BOUNDARY = "open_boundary"


class CoverageGap(Frozen):
    kind: GapKind
    target: str
    project_id: str | None = None
    description: str


class BlockKind(StrEnum):
    RESTRICTION = "restriction"
    CONSTRAINT = "constraint"
    EXISTENCE = "existence"
    DIRECTION = "direction"
    TEMPORARY_INACTIVE = "temporary_inactive"


class BlockReason(Frozen):
    kind: BlockKind
    description: str
    restriction_id: str | None = None
    project_id: str | None = None
    phase_id: str | None = None
    attribute: str | None = None
    value: Scalar | None = None
    threshold: Scalar | None = None
    evidence: tuple[str, ...] = ()


class ConstraintCheck(Frozen):
    attribute: str
    comparator: str
    threshold: Scalar
    unit: str | None
    hard: bool
    value: Scalar | None
    state: EvidenceState | None
    satisfied: bool | None  # None means unknown
    evidence: tuple[str, ...] = ()


class EdgeAssessment(Frozen):
    edge_id: str
    edge_class: EdgeClass
    facts: tuple[Fact, ...] = ()
    gaps: tuple[CoverageGap, ...] = ()
    blocks: tuple[BlockReason, ...] = ()
    checks: tuple[ConstraintCheck, ...] = ()
    soft_penalty: float = 0.0
    evidence: tuple[str, ...] = ()


class RouteStep(Frozen):
    edge_id: str
    from_node: str
    to_node: str
    kind: EdgeKind
    length_m: float | None
    edge_class: EdgeClass
    checks: tuple[ConstraintCheck, ...]
    evidence: tuple[str, ...]
    temporary: bool = False


class Route(Frozen):
    nodes: tuple[str, ...]
    steps: tuple[RouteStep, ...]
    length_m: float | None
    cost: float
    crossings: int


class Blocker(Frozen):
    edge_id: str
    from_node: str
    to_node: str
    reasons: tuple[BlockReason, ...]


class RejectedAlternative(Frozen):
    route_edges: tuple[str, ...]
    length_m: float | None
    failing: tuple[EdgeAssessment, ...]


class DepartureGap(Frozen):
    start: datetime
    end: datetime
    level: str  # which graph tier failed for these departures


class PairResult(Frozen):
    origin: str
    destination: str
    outcome: Outcome
    route: Route | None = None
    baseline_route: Route | None = None
    added_distance_m: float | None = None
    added_crossings: int | None = None
    budget_m: float | None = None
    budget_exceeded_tiers: tuple[str, ...] = ()
    alternatives_found: int | None = None
    uncertainties: tuple[Fact, ...] = ()
    coverage_gaps: tuple[CoverageGap, ...] = ()
    boundary: tuple[Blocker, ...] = ()
    rejected_alternatives: tuple[RejectedAlternative, ...] = ()
    departure_gaps: tuple[DepartureGap, ...] = ()
    notes: tuple[str, ...] = ()


class StateResult(Frozen):
    label: str
    basis: StateBasis
    active: dict[str, str]
    interval: Interval | None = None
    hypothetical: bool = False
    outcome: Outcome
    pairs: tuple[PairResult, ...]

    def __hash__(self) -> int:
        return hash((self.label, self.outcome))


class RequirementResult(Frozen):
    requirement_id: str
    requirement_version: int
    profile_id: str
    profile_version: int
    outcome: Outcome
    states: tuple[StateResult, ...]
    affected_pairs: tuple[tuple[str, str], ...]
    assumptions: tuple[str, ...] = ()


class AnalysisResult(Frozen):
    input_hash: str
    verifier_version: str
    scenario_id: str
    scenario_revision_id: str
    network_revision_id: str
    project_revision_ids: tuple[str, ...]
    requirements: tuple[RequirementResult, ...]
    assumptions_applied: dict[str, Scalar | None] = Field(default_factory=dict)
    warnings: tuple[str, ...] = ()


class ReviewQuestion(Frozen):
    fact: Fact
    affected: tuple[str, ...]  # requirement ids whose outcome can change
    outcome_if_favorable: dict[str, Outcome]
    outcome_if_unfavorable: dict[str, Outcome]
    review_cost: float
    score: float
