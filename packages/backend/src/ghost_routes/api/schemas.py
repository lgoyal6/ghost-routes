"""HTTP contract types. The web client's TypeScript is generated from these."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from ghost_routes.domain.model import AccessRequirement, Frame, MobilityProfile, NetworkSnapshot, ProjectSnapshot, Scenario
from ghost_routes.domain.results import AnalysisResult, ReviewQuestion
from ghost_routes.optimization.repair import Intervention, RepairResult


class ErrorBody(BaseModel):
    code: str
    message: str
    detail: dict[str, Any] = Field(default_factory=dict)


class ErrorOut(BaseModel):
    error: ErrorBody


class JobOut(BaseModel):
    id: int
    kind: str
    state: Literal["queued", "running", "succeeded", "failed", "cancelled"]
    attempts: int
    max_attempts: int
    progress: dict[str, Any]
    result: dict[str, Any] | None
    error: str | None
    created_at: str
    updated_at: str
    finished_at: str | None
    cancel_requested: bool


class JobRef(BaseModel):
    job_id: int
    created: bool
    detail: dict[str, Any] = Field(default_factory=dict)


class ModelReadiness(BaseModel):
    state: Literal["ready", "model_unavailable", "model_not_pulled"]
    model: str
    digest: str | None
    server_version: str | None
    details: dict[str, Any] | None
    message: str
    url: str


class ReadinessOut(BaseModel):
    checked_at: str
    database: dict[str, Any]
    migrations: dict[str, Any]
    worker: dict[str, Any]
    last_successful_inference: dict[str, Any] | None
    model: ModelReadiness
    ready_for_analysis: bool
    ready_for_live_inference: bool


class PlanRevisionOut(BaseModel):
    id: str
    project_id: str
    revision_label: str
    revision_date: str | None
    supersedes_id: str | None
    superseded_by: str | None
    learned_at: str
    note: str
    source: dict[str, Any]
    sheets: list[dict[str, Any]]


class ProjectRevisionSummary(BaseModel):
    id: str
    number: int
    plan_revision_id: str | None
    note: str
    created_at: str
    hypothetical: bool
    is_head: bool


class ProjectOut(BaseModel):
    id: str
    title: str
    description: str
    synthetic: bool
    hypothetical: bool
    head_revision_id: str | None
    plan_revisions: list[PlanRevisionOut]
    revisions: list[ProjectRevisionSummary]


class ObservationOut(BaseModel):
    id: str
    sheet_id: str
    extraction_run_id: str | None
    provenance: Literal["native", "model", "human", "scripted"]
    cls: str
    label: str
    geometry_page: list[list[float]]
    bbox_page: list[float]
    properties: dict[str, Any]
    phase_refs: list[str]
    supporting_text: str
    uncertainty: list[Any]
    review_state: Literal["pending", "accepted", "rejected", "edited"]
    current_value: dict[str, Any]
    revision: int
    created_at: str


class ObservationPage(BaseModel):
    items: list[ObservationOut]
    total: int
    offset: int
    limit: int


class ObservationCreate(BaseModel):
    cls: str
    label: str = ""
    geometry_page: list[list[float]] = Field(min_length=1)
    phase_refs: list[str] = Field(default_factory=list)
    properties: dict[str, Any] = Field(default_factory=dict)
    reviewer: str
    reviewer_kind: Literal["human_ui", "human_cli", "scripted"]
    rationale: str = ""


class ReviewIn(BaseModel):
    action: Literal["accept", "reject", "edit"]
    expected_revision: int
    reviewer: str = Field(min_length=1)
    reviewer_kind: Literal["human_ui", "human_cli", "scripted"]
    rationale: str = ""
    edited_value: dict[str, Any] | None = None


class ReviewOut(BaseModel):
    observation: ObservationOut
    invalidated_runs: int


class DecisionOut(BaseModel):
    id: int
    action: str
    reviewer: str
    reviewer_kind: str
    rationale: str
    edited_value: dict[str, Any] | None
    expected_revision: int
    resulting_revision: int
    created_at: str
    proposal_snapshot: dict[str, Any]


class ExtractionRunOut(BaseModel):
    id: str
    sheet_id: str
    crop_id: str
    job_id: int | None
    model: str
    model_digest: str
    prompt_version: str
    prompt_sha256: str
    settings: dict[str, Any]
    status: Literal["succeeded", "empty", "failed"]
    attempts: int
    error: str | None
    validation: dict[str, Any]
    duration_ms: int | None
    created_at: str
    observation_count: int


class ControlPoint(BaseModel):
    page: tuple[float, float]
    site: tuple[float, float]
    note: str = ""


class CalibrationIn(BaseModel):
    control_points: list[ControlPoint] = Field(min_length=2)
    frame_id: str
    reviewer: str = Field(min_length=1)
    reviewer_kind: Literal["human_ui", "human_cli", "scripted"]


class NetworkLineageOut(BaseModel):
    id: str
    title: str
    frame_id: str
    frame_kind: str
    metric: bool
    head_revision_id: str | None
    head_number: int
    synthetic: bool
    revisions: list[dict[str, Any]]


class CropOut(BaseModel):
    id: str
    rx0: int
    ry0: int
    rx1: int
    ry1: int
    sha256: str


class CalibrationOut(BaseModel):
    id: str
    frame_id: str
    method: str
    matrix: list[float]
    rms_residual_m: float | None
    control_points: list[dict[str, Any]]
    reviewer: str
    reviewer_kind: str
    created_at: str


class SheetOut(BaseModel):
    id: str
    plan_revision_id: str
    page_index: int
    printed_sheet_id: str | None
    width_pt: float
    height_pt: float
    rotation: int
    render_dpi: int
    render_width_px: int
    render_height_px: int
    vector_summary: dict[str, Any]
    word_count: int
    crops: list[CropOut]
    calibrations: list[CalibrationOut]


class GeometryOut(BaseModel):
    """Everything needed to draw a scenario revision."""

    scenario_revision_id: str
    network: NetworkSnapshot
    projects: list[ProjectSnapshot]
    frame: Frame


class ScenarioRevisionOut(BaseModel):
    id: str
    scenario_id: str
    number: int
    is_head: bool
    hypothetical: bool
    branch_of: str | None
    scenario: Scenario
    network_revision_id: str
    project_revision_ids: list[str]
    requirement_refs: list[tuple[str, int]]
    requirements: list[AccessRequirement]
    profiles: list[MobilityProfile]
    pinned_inputs_current: bool
    outdated_inputs: list[str]
    created_at: str


class ScenarioOut(BaseModel):
    id: str
    title: str
    network_lineage_id: str
    head_revision_id: str | None
    revisions: list[dict[str, Any]]


class ScenarioCreate(BaseModel):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{1,60}$")
    title: str
    network_lineage_id: str
    scenario: Scenario
    requirement_ids: list[str]


class ScenarioUpdate(BaseModel):
    expected_head: str
    scenario: Scenario
    requirement_ids: list[str] | None = None


class AnalysisRunOut(BaseModel):
    id: str
    scenario_revision_id: str
    input_hash: str
    verifier_version: str
    freshness: Literal["current", "stale", "failed"]
    stale_reason: str | None
    result: AnalysisResult | None
    timings: dict[str, Any]
    job_id: int | None
    created_at: str
    published_at: str | None


class QuestionsOut(BaseModel):
    questions: list[ReviewQuestion]
    meta: dict[str, Any]
    input_hash: str


class ResolveFactIn(BaseModel):
    fact_id: str
    value: Any
    unit: str | None = None
    reviewer: str = Field(min_length=1)
    reviewer_kind: Literal["human_ui", "human_cli", "scripted"]
    rationale: str = Field(min_length=1)


class ResolveFactOut(BaseModel):
    fact: str
    scenario_revision: str
    network_revision: str | None = None
    project_revision: str | None = None
    resolution: dict[str, Any]


class RepairIn(BaseModel):
    interventions: list[Intervention]
    restore: list[str]
    protect: list[str] = Field(default_factory=list)
    method: Literal["auto", "enumerate", "cpsat"] = "auto"
    max_candidates: int = 3
    max_evaluations: int = 500
    time_limit_s: float = 30.0


class RepairOut(BaseModel):
    id: str
    scenario_revision_id: str
    status: str | None
    freshness: str
    stale_reason: str | None
    problem: dict[str, Any]
    result: RepairResult | None
    job: dict[str, Any] | None
    candidates: list[dict[str, Any]]


class DiffOut(BaseModel):
    kind: Literal["network", "project", "analysis"]
    from_id: str
    to_id: str
    changes: list[dict[str, Any]]
    summary: dict[str, int]


class BuildNetworkIn(BaseModel):
    sheet_id: str
    calibration_id: str | None = None
    snap_tolerance: float = Field(ge=0)
    barrier_buffer: float = Field(gt=0)
    project_id: str
    phase_map: dict[str, str]
    include_pending: bool = True
    author: str = Field(min_length=1)
    author_kind: Literal["human_ui", "human_cli", "scripted"]
    expected_head: int | None = None


class ReplayOut(BaseModel):
    matches: bool
    canonical_input_hash: str
    verifier_version: str
    recorded_verifier_version: str
    outcomes: dict[str, str]
    differences: list[dict[str, Any]]
    omissions: list[dict[str, Any]]
    provenance: dict[str, Any]


class ProfileReviewIn(BaseModel):
    reviewer: str = Field(min_length=1)
    reviewer_kind: Literal["human_ui", "human_cli", "scripted"]
    rationale: str = Field(min_length=1)


