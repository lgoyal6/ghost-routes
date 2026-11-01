"""Typed, immutable values shared by every layer.

Nothing here performs I/O. Missing information is represented explicitly
(``None`` values with an evidence state) and is never replaced by a default.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

VERIFIER_VERSION = "gr-verifier/1.0.0"


class Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class EvidenceState(StrEnum):
    ACCEPTED = "ACCEPTED"
    UNRESOLVED = "UNRESOLVED"
    CONFLICTING = "CONFLICTING"
    REJECTED = "REJECTED"


class Outcome(StrEnum):
    SUPPORTED = "SUPPORTED"
    NEEDS_VERIFICATION = "NEEDS_VERIFICATION"
    INSUFFICIENT_COVERAGE = "INSUFFICIENT_COVERAGE"
    DISCONNECTED_WITHIN_COVERAGE = "DISCONNECTED_WITHIN_COVERAGE"


# Higher is worse; used to aggregate pair outcomes into a requirement outcome.
OUTCOME_SEVERITY: dict[Outcome, int] = {
    Outcome.SUPPORTED: 0,
    Outcome.NEEDS_VERIFICATION: 1,
    Outcome.INSUFFICIENT_COVERAGE: 2,
    Outcome.DISCONNECTED_WITHIN_COVERAGE: 3,
}

Point = tuple[float, float]
Scalar = float | int | str | bool


class Frame(Frozen):
    """Coordinate frame of a network. Metric claims need ``metric=True``."""

    id: str
    kind: Literal["sheet_points", "site_metric"]
    metric: bool
    description: str = ""

    @model_validator(mode="after")
    def _metric_matches_kind(self) -> Frame:
        if self.kind == "sheet_points" and self.metric:
            raise ValueError("a sheet_points frame cannot be metric without calibration")
        return self


class AttributeValue(Frozen):
    """One attribute claim. ``value=None`` means unknown.

    For CONFLICTING claims ``alternatives`` holds every competing value and
    ``value`` must be ``None`` until a review decision resolves it.
    """

    value: Scalar | None = None
    unit: str | None = None
    state: EvidenceState = EvidenceState.UNRESOLVED
    alternatives: tuple[Scalar, ...] = ()
    evidence: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _consistent(self) -> AttributeValue:
        if self.state == EvidenceState.CONFLICTING:
            if self.value is not None or len(self.alternatives) < 2:
                raise ValueError("conflicting attribute needs >=2 alternatives and no chosen value")
        if self.state == EvidenceState.ACCEPTED and self.value is None:
            raise ValueError("an accepted attribute must have a value")
        if isinstance(self.value, float) and not _finite(self.value):
            raise ValueError("attribute values must be finite")
        return self

    def candidates(self) -> tuple[Scalar | None, ...]:
        """Values this attribute could take under some resolution."""
        if self.state == EvidenceState.CONFLICTING:
            return self.alternatives
        if self.value is None:
            return (None,)
        return (self.value,)


def _finite(x: float) -> bool:
    return x == x and x not in (float("inf"), float("-inf"))


class NodeKind(StrEnum):
    JUNCTION = "junction"
    ENTRANCE = "entrance"
    CROSSING_END = "crossing_end"
    CURB = "curb"
    ENDPOINT = "endpoint"


class Node(Frozen):
    id: str
    x: float
    y: float
    kind: NodeKind = NodeKind.JUNCTION
    level: int = 0
    label: str = ""
    # The modeled network continues past this node outside coverage.
    open_boundary: bool = False
    wait_allowed: bool = False


class EdgeKind(StrEnum):
    SIDEWALK = "sidewalk"
    CROSSING = "crossing"
    PATH = "path"
    RAMP = "ramp"
    STAIRS = "stairs"
    ENTRANCE_LINK = "entrance_link"
    TEMPORARY = "temporary"


class Edge(Frozen):
    id: str
    u: str
    v: str
    kind: EdgeKind = EdgeKind.SIDEWALK
    directed: bool = False
    geometry: tuple[Point, ...]
    # None when the frame is not metric; never estimated from pixels.
    length_m: float | None = None
    existence: EvidenceState = EvidenceState.ACCEPTED
    existence_evidence: tuple[str, ...] = ()
    attributes: dict[str, AttributeValue] = Field(default_factory=dict)
    label: str = ""

    @field_validator("geometry")
    @classmethod
    def _geometry(cls, g: tuple[Point, ...]) -> tuple[Point, ...]:
        if len(g) < 2:
            raise ValueError("edge geometry needs at least two points")
        for x, y in g:
            if not (_finite(x) and _finite(y)):
                raise ValueError("geometry coordinates must be finite")
        return g

    @field_validator("length_m")
    @classmethod
    def _length(cls, v: float | None) -> float | None:
        if v is not None and (not _finite(v) or v < 0):
            raise ValueError("length must be finite and non-negative")
        return v

    def __hash__(self) -> int:  # dict field prevents the default frozen hash
        return hash(self.id)


class CoverageAssertion(Frozen):
    """A reviewed claim that a region is completely modeled.

    ``scope='network'`` covers baseline pedestrian geometry. ``scope=<project id>``
    says that project's restrictions within the region were reviewed.
    """

    id: str
    scope: str
    polygon: tuple[Point, ...]
    attributes_complete: tuple[str, ...] = ()
    sheets: tuple[str, ...] = ()
    reviewer: str
    assumptions: str = ""


class NetworkSnapshot(Frozen):
    revision_id: str
    frame: Frame
    nodes: tuple[Node, ...]
    edges: tuple[Edge, ...]
    coverage: tuple[CoverageAssertion, ...] = ()
    snap_tolerance: float = 0.0

    @model_validator(mode="after")
    def _referential(self) -> NetworkSnapshot:
        ids = {n.id for n in self.nodes}
        if len(ids) != len(self.nodes):
            raise ValueError("duplicate node id")
        eids = {e.id for e in self.edges}
        if len(eids) != len(self.edges):
            raise ValueError("duplicate edge id")
        for e in self.edges:
            if e.u not in ids or e.v not in ids:
                raise ValueError(f"edge {e.id} references an unknown node")
            if e.u == e.v:
                raise ValueError(f"edge {e.id} is a self loop")
        for c in self.coverage:
            if c.scope != "network":
                raise ValueError("network snapshots only hold network-scope coverage")
        return self


class Interval(Frozen):
    """Half-open ``[start, end)``; ``end=None`` is unbounded."""

    start: datetime
    end: datetime | None = None

    @model_validator(mode="after")
    def _ordered(self) -> Interval:
        if self.start.tzinfo is None or (self.end is not None and self.end.tzinfo is None):
            raise ValueError("interval instants must be timezone-aware")
        if self.end is not None and self.end <= self.start:
            raise ValueError("interval end must be after start")
        return self

    def contains(self, t: datetime) -> bool:
        return self.start <= t and (self.end is None or t < self.end)


class Phase(Frozen):
    id: str
    label: str
    order: int
    prerequisites: tuple[str, ...] = ()
    effective: Interval | None = None


class RestrictionKind(StrEnum):
    CLOSURE = "closure"
    ATTRIBUTE_OVERRIDE = "attribute_override"


class TargetRelation(Frozen):
    """A reviewed or proposed link between a restriction and an edge."""

    edge_id: str
    state: EvidenceState = EvidenceState.ACCEPTED
    evidence: tuple[str, ...] = ()


class Restriction(Frozen):
    id: str
    phase_ids: tuple[str, ...]
    kind: RestrictionKind = RestrictionKind.CLOSURE
    targets: tuple[TargetRelation, ...] = ()
    polygon: tuple[Point, ...] | None = None
    overrides: dict[str, AttributeValue] = Field(default_factory=dict)
    effective: Interval | None = None
    state: EvidenceState = EvidenceState.ACCEPTED
    evidence: tuple[str, ...] = ()
    label: str = ""

    def __hash__(self) -> int:
        return hash(self.id)


class TemporaryConnection(Frozen):
    id: str
    phase_ids: tuple[str, ...]
    edge: Edge
    effective: Interval | None = None
    state: EvidenceState = EvidenceState.ACCEPTED
    # Inactive candidates are only enabled by a repair intervention.
    active_by_default: bool = True
    evidence: tuple[str, ...] = ()


class ProjectSnapshot(Frozen):
    project_id: str
    revision_id: str
    title: str
    phases: tuple[Phase, ...]
    restrictions: tuple[Restriction, ...] = ()
    temporary_connections: tuple[TemporaryConnection, ...] = ()
    # Declared work extent; any part not covered by project coverage is a gap.
    extent: tuple[Point, ...] | None = None
    coverage: tuple[CoverageAssertion, ...] = ()
    hypothetical: bool = False

    @model_validator(mode="after")
    def _referential(self) -> ProjectSnapshot:
        pids = {p.id for p in self.phases}
        if len(pids) != len(self.phases):
            raise ValueError("duplicate phase id")
        for p in self.phases:
            for q in p.prerequisites:
                if q not in pids:
                    raise ValueError(f"phase {p.id} prerequisite {q} unknown")
        for r in self.restrictions:
            for ph in r.phase_ids:
                if ph not in pids:
                    raise ValueError(f"restriction {r.id} references unknown phase {ph}")
        for t in self.temporary_connections:
            for ph in t.phase_ids:
                if ph not in pids:
                    raise ValueError(f"temporary connection {t.id} references unknown phase {ph}")
        for c in self.coverage:
            if c.scope != self.project_id:
                raise ValueError("project coverage scope must equal the project id")
        return self

    def phase(self, phase_id: str) -> Phase:
        for p in self.phases:
            if p.id == phase_id:
                return p
        raise KeyError(phase_id)


class Comparator(StrEnum):
    GE = ">="
    LE = "<="
    EQ = "=="
    NE = "!="


class Constraint(Frozen):
    attribute: str
    comparator: Comparator
    threshold: Scalar
    unit: str | None = None
    hard: bool = True
    # Soft constraints add ``weight`` cost units per metre (or per edge when
    # lengths are not metric) when violated or unknown. Hard ones filter only.
    weight: float = 0.0
    source: str = ""
    rationale: str = ""

    @model_validator(mode="after")
    def _weights(self) -> Constraint:
        if self.hard and self.weight:
            raise ValueError("hard constraints cannot carry preference weight")
        if not self.hard and self.weight < 0:
            raise ValueError("preference weight must be non-negative")
        return self


class MobilityProfile(Frozen):
    id: str
    version: int
    label: str
    constraints: tuple[Constraint, ...]
    review_state: Literal["draft", "scripted_review", "reviewed"] = "draft"
    reviewer: str = ""
    illustrative: bool = False
    description: str = ""

    def hard(self) -> tuple[Constraint, ...]:
        return tuple(c for c in self.constraints if c.hard)

    def soft(self) -> tuple[Constraint, ...]:
        return tuple(c for c in self.constraints if not c.hard)


class AccessRequirement(Frozen):
    id: str
    version: int
    label: str
    profile_id: str
    origins: tuple[str, ...]
    destinations: tuple[str, ...]
    max_added_distance_m: float | None = None
    max_detour_ratio: float | None = None
    required_alternatives: int = 1
    # Dated mode only: departures in this window must all succeed.
    departure_window: Interval | None = None
    walking_speed_mps: float | None = None
    description: str = ""

    @model_validator(mode="after")
    def _shape(self) -> AccessRequirement:
        if not self.origins or not self.destinations:
            raise ValueError("requirements need origins and destinations")
        if self.required_alternatives < 1:
            raise ValueError("required_alternatives must be >=1")
        if self.departure_window is not None and not self.walking_speed_mps:
            raise ValueError("a departure window needs an explicit walking speed assumption")
        if self.walking_speed_mps is not None and self.walking_speed_mps <= 0:
            raise ValueError("walking speed must be positive")
        return self


class StateBasis(StrEnum):
    BASELINE = "baseline"
    PROJECT_ISOLATED = "project_isolated"
    DECLARED_CONCURRENT = "declared_concurrent"
    SCHEDULE = "schedule"


class StateSpec(Frozen):
    """A selection of active phases (undated mode)."""

    label: str
    active: dict[str, str]  # project id -> phase id
    basis: StateBasis

    def __hash__(self) -> int:
        return hash((self.label, tuple(sorted(self.active.items())), self.basis))


class Scenario(Frozen):
    id: str
    revision_id: str
    label: str
    mode: Literal["phases", "dated"]
    project_ids: tuple[str, ...]
    # Undated mode: explicit states to evaluate. Empty means each phase of
    # each project in isolation plus the baseline.
    states: tuple[StateSpec, ...] = ()
    # Dated mode: analysis horizon.
    horizon: Interval | None = None
    display_timezone: str = "UTC"
    # Hypothetical overlays (repairs, what-ifs) are labeled, never official.
    enabled_temporary_connections: tuple[str, ...] = ()
    disabled_restriction_windows: tuple[RestrictionWindowOverride, ...] = ()
    phase_shifts: dict[str, int] = Field(default_factory=dict)  # "proj/phase" -> seconds
    added_restrictions: tuple[AddedRestriction, ...] = ()
    hypothetical: bool = False

    @model_validator(mode="after")
    def _shape(self) -> Scenario:
        if self.mode == "dated" and self.horizon is None:
            raise ValueError("dated scenarios need a horizon")
        if self.mode == "phases" and self.phase_shifts:
            raise ValueError("schedule shifts only apply to dated scenarios")
        return self


class RestrictionWindowOverride(Frozen):
    """Keep a restriction's target edges open during an interval."""

    restriction_id: str
    edge_ids: tuple[str, ...]
    window: Interval | None = None  # None means for the whole restriction


class AddedRestriction(Frozen):
    """A hypothetical restriction overlay attached to a project's phase."""

    project_id: str
    restriction: Restriction


Scenario.model_rebuild()


class AnalysisInput(Frozen):
    network: NetworkSnapshot
    projects: tuple[ProjectSnapshot, ...]
    scenario: Scenario
    profiles: tuple[MobilityProfile, ...]
    requirements: tuple[AccessRequirement, ...]
    # Explicit resolutions of unresolved facts (sensitivity/what-if only).
    assumptions: dict[str, Scalar | None] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _referential(self) -> AnalysisInput:
        node_ids = {n.id for n in self.network.nodes}
        prof = {p.id for p in self.profiles}
        for r in self.requirements:
            if r.profile_id not in prof:
                raise ValueError(f"requirement {r.id} references unknown profile {r.profile_id}")
            for n in (*r.origins, *r.destinations):
                if n not in node_ids:
                    raise ValueError(f"requirement {r.id} references unknown node {n}")
        known = {p.project_id for p in self.projects}
        for pid in self.scenario.project_ids:
            if pid not in known:
                raise ValueError(f"scenario references unknown project {pid}")
        return self

    def profile(self, profile_id: str) -> MobilityProfile:
        for p in self.profiles:
            if p.id == profile_id:
                return p
        raise KeyError(profile_id)

    def project(self, project_id: str) -> ProjectSnapshot:
        for p in self.projects:
            if p.project_id == project_id:
                return p
        raise KeyError(project_id)


PositiveFloat = Annotated[float, Field(gt=0)]
