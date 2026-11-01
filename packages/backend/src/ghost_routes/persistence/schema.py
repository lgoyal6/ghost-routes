"""Relational schema. Geometry is stored in local frames (SRID 0) with an explicit frame id."""

from __future__ import annotations

from datetime import datetime
from typing import Any, ClassVar

from geoalchemy2 import Geometry
from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, TSTZRANGE
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    type_annotation_map: ClassVar[dict[Any, Any]] = {dict[str, Any]: JSONB, list[Any]: JSONB}


def _now() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


EVIDENCE = "('ACCEPTED','UNRESOLVED','CONFLICTING','REJECTED')"


class Project(Base):
    __tablename__ = "projects"
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    title: Mapped[str] = mapped_column(Text)
    description: Mapped[str] = mapped_column(Text, default="")
    synthetic: Mapped[bool] = mapped_column(Boolean, default=False)
    hypothetical: Mapped[bool] = mapped_column(Boolean, default=False)
    head_revision_id: Mapped[str | None] = mapped_column(String(80))
    created_at: Mapped[datetime] = _now()


class SourceArtifact(Base):
    """Immutable original bytes, identified by content hash."""

    __tablename__ = "source_artifacts"
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    sha256: Mapped[str] = mapped_column(String(64), unique=True)
    mime: Mapped[str] = mapped_column(String(80))
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    original_filename: Mapped[str] = mapped_column(Text)
    source_url: Mapped[str | None] = mapped_column(Text)
    retrieved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    storage_path: Mapped[str] = mapped_column(Text)
    reuse_status: Mapped[str] = mapped_column(String(40))
    page_count: Mapped[int] = mapped_column(Integer)
    synthetic: Mapped[bool] = mapped_column(Boolean, default=False)
    metadata_: Mapped[dict[str, Any]] = mapped_column("metadata", JSONB, default=dict)
    created_at: Mapped[datetime] = _now()
    __table_args__ = (
        CheckConstraint("reuse_status in ('redistributable','reference_only','unknown','synthetic_original')"),
        CheckConstraint("size_bytes > 0"),
    )


class PlanRevision(Base):
    __tablename__ = "plan_revisions"
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"))
    source_artifact_id: Mapped[str] = mapped_column(ForeignKey("source_artifacts.id"))
    revision_label: Mapped[str] = mapped_column(String(80))
    revision_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    supersedes_id: Mapped[str | None] = mapped_column(ForeignKey("plan_revisions.id"))
    learned_at: Mapped[datetime] = _now()
    note: Mapped[str] = mapped_column(Text, default="")
    __table_args__ = (UniqueConstraint("project_id", "revision_label"),)


class Sheet(Base):
    __tablename__ = "sheets"
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    plan_revision_id: Mapped[str] = mapped_column(ForeignKey("plan_revisions.id"))
    page_index: Mapped[int] = mapped_column(Integer)
    printed_sheet_id: Mapped[str | None] = mapped_column(String(40))
    width_pt: Mapped[float] = mapped_column(Float)
    height_pt: Mapped[float] = mapped_column(Float)
    rotation: Mapped[int] = mapped_column(Integer)
    render_dpi: Mapped[int] = mapped_column(Integer)
    render_width_px: Mapped[int] = mapped_column(Integer)
    render_height_px: Mapped[int] = mapped_column(Integer)
    render_path: Mapped[str] = mapped_column(Text)
    native_words: Mapped[list[Any]] = mapped_column(JSONB, default=list)
    vector_summary: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    __table_args__ = (
        UniqueConstraint("plan_revision_id", "page_index"),
        CheckConstraint("rotation in (0, 90, 180, 270)"),
    )


class SheetCalibration(Base):
    __tablename__ = "sheet_calibrations"
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    sheet_id: Mapped[str] = mapped_column(ForeignKey("sheets.id"))
    frame_id: Mapped[str] = mapped_column(String(80))
    method: Mapped[str] = mapped_column(String(40))
    control_points: Mapped[list[Any]] = mapped_column(JSONB)
    matrix: Mapped[list[Any]] = mapped_column(JSONB)
    rms_residual: Mapped[float] = mapped_column(Float)
    reviewer: Mapped[str] = mapped_column(String(120))
    reviewer_kind: Mapped[str] = mapped_column(String(20))
    created_at: Mapped[datetime] = _now()


class Crop(Base):
    __tablename__ = "crops"
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    sheet_id: Mapped[str] = mapped_column(ForeignKey("sheets.id"))
    sha256: Mapped[str] = mapped_column(String(64))
    # Rendered-pixel rectangle (render space) and the crop's own pixel size.
    rx0: Mapped[int] = mapped_column(Integer)
    ry0: Mapped[int] = mapped_column(Integer)
    rx1: Mapped[int] = mapped_column(Integer)
    ry1: Mapped[int] = mapped_column(Integer)
    width_px: Mapped[int] = mapped_column(Integer)
    height_px: Mapped[int] = mapped_column(Integer)
    storage_path: Mapped[str] = mapped_column(Text)
    purpose: Mapped[str] = mapped_column(String(40))
    __table_args__ = (UniqueConstraint("sheet_id", "sha256"),)


class ExtractionRun(Base):
    __tablename__ = "extraction_runs"
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    identity_hash: Mapped[str] = mapped_column(String(80), unique=True)
    sheet_id: Mapped[str] = mapped_column(ForeignKey("sheets.id"))
    crop_id: Mapped[str] = mapped_column(ForeignKey("crops.id"))
    job_id: Mapped[int | None] = mapped_column(BigInteger)
    pipeline: Mapped[str] = mapped_column(String(80))
    model: Mapped[str] = mapped_column(String(120))
    model_digest: Mapped[str] = mapped_column(String(80))
    prompt_version: Mapped[str] = mapped_column(String(40))
    prompt_sha256: Mapped[str] = mapped_column(String(64))
    settings: Mapped[dict[str, Any]] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(String(20))
    attempts: Mapped[int] = mapped_column(Integer, default=1)
    raw_response_path: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)
    validation: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = _now()
    __table_args__ = (CheckConstraint("status in ('succeeded','empty','failed')"),)


class Observation(Base):
    """A proposed fact with an exact evidence anchor. Review state is separate from job state."""

    __tablename__ = "observations"
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    idempotency_key: Mapped[str] = mapped_column(String(120), unique=True)
    sheet_id: Mapped[str] = mapped_column(ForeignKey("sheets.id"))
    extraction_run_id: Mapped[str | None] = mapped_column(ForeignKey("extraction_runs.id"))
    provenance: Mapped[str] = mapped_column(String(20))
    cls: Mapped[str] = mapped_column(String(40))
    label: Mapped[str] = mapped_column(Text, default="")
    # Page-point coordinates (unrotated PDF user space).
    geom: Mapped[Any] = mapped_column(Geometry(srid=0, spatial_index=False))
    bbox: Mapped[Any] = mapped_column(Geometry("POLYGON", srid=0, spatial_index=False))
    properties: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    phase_refs: Mapped[list[str]] = mapped_column(ARRAY(String), default=list)
    supporting_text: Mapped[str] = mapped_column(Text, default="")
    uncertainty: Mapped[list[Any]] = mapped_column(JSONB, default=list)
    review_state: Mapped[str] = mapped_column(String(20), default="pending")
    current_value: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    revision: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = _now()
    __table_args__ = (
        CheckConstraint("provenance in ('native','model','human','scripted')"),
        CheckConstraint("review_state in ('pending','accepted','rejected','edited')"),
        Index("ix_observations_geom", "geom", postgresql_using="gist"),
        Index("ix_observations_bbox", "bbox", postgresql_using="gist"),
    )


class ReviewDecision(Base):
    """Append-only; a trigger forbids UPDATE and DELETE."""

    __tablename__ = "review_decisions"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    observation_id: Mapped[str] = mapped_column(ForeignKey("observations.id"))
    action: Mapped[str] = mapped_column(String(20))
    proposal_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB)
    edited_value: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    reviewer: Mapped[str] = mapped_column(String(120))
    reviewer_kind: Mapped[str] = mapped_column(String(20))
    rationale: Mapped[str] = mapped_column(Text, default="")
    expected_revision: Mapped[int] = mapped_column(Integer)
    resulting_revision: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = _now()
    __table_args__ = (
        CheckConstraint("action in ('accept','reject','edit')"),
        CheckConstraint("reviewer_kind in ('human_ui','human_cli','scripted')"),
    )


class NetworkLineage(Base):
    __tablename__ = "network_lineages"
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    title: Mapped[str] = mapped_column(Text)
    frame_id: Mapped[str] = mapped_column(String(80))
    frame_kind: Mapped[str] = mapped_column(String(20))
    metric: Mapped[bool] = mapped_column(Boolean)
    frame_description: Mapped[str] = mapped_column(Text, default="")
    head_revision_id: Mapped[str | None] = mapped_column(String(80))
    head_number: Mapped[int] = mapped_column(Integer, default=0)
    synthetic: Mapped[bool] = mapped_column(Boolean, default=False)
    __table_args__ = (CheckConstraint("frame_kind in ('sheet_points','site_metric')"),
                      CheckConstraint("NOT (frame_kind = 'sheet_points' AND metric)"))


class NetworkRevision(Base):
    __tablename__ = "network_revisions"
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    lineage_id: Mapped[str] = mapped_column(ForeignKey("network_lineages.id"))
    number: Mapped[int] = mapped_column(Integer)
    parent_id: Mapped[str | None] = mapped_column(ForeignKey("network_revisions.id"))
    author: Mapped[str] = mapped_column(String(120))
    author_kind: Mapped[str] = mapped_column(String(20))
    note: Mapped[str] = mapped_column(Text, default="")
    change: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    content_hash: Mapped[str] = mapped_column(String(80))
    snap_tolerance: Mapped[float] = mapped_column(Float, default=0.0)
    created_at: Mapped[datetime] = _now()
    __table_args__ = (UniqueConstraint("lineage_id", "number"),)


class NetworkNode(Base):
    __tablename__ = "network_nodes"
    revision_id: Mapped[str] = mapped_column(ForeignKey("network_revisions.id"), primary_key=True)
    node_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    geom: Mapped[Any] = mapped_column(Geometry("POINT", srid=0, spatial_index=False))
    kind: Mapped[str] = mapped_column(String(20))
    level: Mapped[int] = mapped_column(Integer, default=0)
    label: Mapped[str] = mapped_column(Text, default="")
    open_boundary: Mapped[bool] = mapped_column(Boolean, default=False)
    wait_allowed: Mapped[bool] = mapped_column(Boolean, default=False)
    __table_args__ = (Index("ix_network_nodes_geom", "geom", postgresql_using="gist"),)


class NetworkEdge(Base):
    __tablename__ = "network_edges"
    revision_id: Mapped[str] = mapped_column(ForeignKey("network_revisions.id"), primary_key=True)
    edge_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    u: Mapped[str] = mapped_column(String(80))
    v: Mapped[str] = mapped_column(String(80))
    kind: Mapped[str] = mapped_column(String(20))
    directed: Mapped[bool] = mapped_column(Boolean, default=False)
    geom: Mapped[Any] = mapped_column(Geometry("LINESTRING", srid=0, spatial_index=False))
    length_m: Mapped[float | None] = mapped_column(Float)
    existence: Mapped[str] = mapped_column(String(20))
    existence_evidence: Mapped[list[str]] = mapped_column(ARRAY(String), default=list)
    attributes: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    label: Mapped[str] = mapped_column(Text, default="")
    __table_args__ = (
        ForeignKeyConstraint(["revision_id", "u"], ["network_nodes.revision_id", "network_nodes.node_id"]),
        ForeignKeyConstraint(["revision_id", "v"], ["network_nodes.revision_id", "network_nodes.node_id"]),
        CheckConstraint(f"existence in {EVIDENCE}"),
        CheckConstraint("u <> v"),
        CheckConstraint("length_m IS NULL OR length_m >= 0"),
        Index("ix_network_edges_geom", "geom", postgresql_using="gist"),
    )


class ProjectRevision(Base):
    __tablename__ = "project_revisions"
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"))
    number: Mapped[int] = mapped_column(Integer)
    parent_id: Mapped[str | None] = mapped_column(ForeignKey("project_revisions.id"))
    plan_revision_id: Mapped[str | None] = mapped_column(ForeignKey("plan_revisions.id"))
    frame_id: Mapped[str] = mapped_column(String(80))
    extent: Mapped[Any | None] = mapped_column(Geometry("POLYGON", srid=0, spatial_index=False))
    hypothetical: Mapped[bool] = mapped_column(Boolean, default=False)
    content_hash: Mapped[str] = mapped_column(String(80))
    note: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = _now()
    __table_args__ = (UniqueConstraint("project_id", "number"),)


class CoverageRow(Base):
    __tablename__ = "coverage_assertions"
    id: Mapped[str] = mapped_column(String(120), primary_key=True)
    network_revision_id: Mapped[str | None] = mapped_column(ForeignKey("network_revisions.id"))
    project_revision_id: Mapped[str | None] = mapped_column(ForeignKey("project_revisions.id"))
    scope: Mapped[str] = mapped_column(String(80))
    polygon: Mapped[Any] = mapped_column(Geometry("POLYGON", srid=0, spatial_index=False))
    attributes_complete: Mapped[list[str]] = mapped_column(ARRAY(String), default=list)
    sheets: Mapped[list[str]] = mapped_column(ARRAY(String), default=list)
    reviewer: Mapped[str] = mapped_column(String(120))
    assumptions: Mapped[str] = mapped_column(Text, default="")
    __table_args__ = (
        CheckConstraint("(network_revision_id IS NULL) <> (project_revision_id IS NULL)"),
        Index("ix_coverage_polygon", "polygon", postgresql_using="gist"),
    )


class PhaseRow(Base):
    __tablename__ = "phases"
    project_revision_id: Mapped[str] = mapped_column(ForeignKey("project_revisions.id"), primary_key=True)
    phase_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    label: Mapped[str] = mapped_column(Text)
    ord: Mapped[int] = mapped_column(Integer)
    prerequisites: Mapped[list[str]] = mapped_column(ARRAY(String), default=list)
    effective: Mapped[Any | None] = mapped_column(TSTZRANGE)
    __table_args__ = (Index("ix_phases_effective", "effective", postgresql_using="gist"),)


class RestrictionRow(Base):
    __tablename__ = "restrictions"
    project_revision_id: Mapped[str] = mapped_column(ForeignKey("project_revisions.id"), primary_key=True)
    restriction_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    phase_ids: Mapped[list[str]] = mapped_column(ARRAY(String))
    kind: Mapped[str] = mapped_column(String(30))
    targets: Mapped[list[Any]] = mapped_column(JSONB, default=list)
    polygon: Mapped[Any | None] = mapped_column(Geometry("POLYGON", srid=0, spatial_index=False))
    overrides: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    effective: Mapped[Any | None] = mapped_column(TSTZRANGE)
    state: Mapped[str] = mapped_column(String(20))
    evidence: Mapped[list[str]] = mapped_column(ARRAY(String), default=list)
    label: Mapped[str] = mapped_column(Text, default="")
    __table_args__ = (
        CheckConstraint(f"state in {EVIDENCE}"),
        Index("ix_restrictions_polygon", "polygon", postgresql_using="gist"),
        Index("ix_restrictions_effective", "effective", postgresql_using="gist"),
    )


class TemporaryConnectionRow(Base):
    __tablename__ = "temporary_connections"
    project_revision_id: Mapped[str] = mapped_column(ForeignKey("project_revisions.id"), primary_key=True)
    connection_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    phase_ids: Mapped[list[str]] = mapped_column(ARRAY(String))
    edge: Mapped[dict[str, Any]] = mapped_column(JSONB)
    geom: Mapped[Any] = mapped_column(Geometry("LINESTRING", srid=0, spatial_index=False))
    effective: Mapped[Any | None] = mapped_column(TSTZRANGE)
    state: Mapped[str] = mapped_column(String(20))
    active_by_default: Mapped[bool] = mapped_column(Boolean, default=True)
    evidence: Mapped[list[str]] = mapped_column(ARRAY(String), default=list)
    __table_args__ = (CheckConstraint(f"state in {EVIDENCE}"),)


class ProfileRow(Base):
    __tablename__ = "mobility_profiles"
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    version: Mapped[int] = mapped_column(Integer, primary_key=True)
    body: Mapped[dict[str, Any]] = mapped_column(JSONB)
    review_state: Mapped[str] = mapped_column(String(20))
    created_at: Mapped[datetime] = _now()


class RequirementRow(Base):
    __tablename__ = "access_requirements"
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    version: Mapped[int] = mapped_column(Integer, primary_key=True)
    profile_id: Mapped[str] = mapped_column(String(80))
    body: Mapped[dict[str, Any]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = _now()


class ScenarioRow(Base):
    __tablename__ = "scenarios"
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    title: Mapped[str] = mapped_column(Text)
    network_lineage_id: Mapped[str] = mapped_column(ForeignKey("network_lineages.id"))
    head_revision_id: Mapped[str | None] = mapped_column(String(120))
    created_at: Mapped[datetime] = _now()


class ScenarioRevisionRow(Base):
    __tablename__ = "scenario_revisions"
    id: Mapped[str] = mapped_column(String(120), primary_key=True)
    scenario_id: Mapped[str] = mapped_column(ForeignKey("scenarios.id"))
    number: Mapped[int] = mapped_column(Integer)
    parent_id: Mapped[str | None] = mapped_column(ForeignKey("scenario_revisions.id"))
    body: Mapped[dict[str, Any]] = mapped_column(JSONB)
    network_revision_id: Mapped[str] = mapped_column(ForeignKey("network_revisions.id"))
    project_revision_ids: Mapped[list[str]] = mapped_column(ARRAY(String))
    requirement_refs: Mapped[list[Any]] = mapped_column(JSONB)
    content_hash: Mapped[str] = mapped_column(String(80))
    hypothetical: Mapped[bool] = mapped_column(Boolean, default=False)
    branch_of: Mapped[str | None] = mapped_column(String(120))
    created_at: Mapped[datetime] = _now()
    __table_args__ = (UniqueConstraint("scenario_id", "number"),)


class AnalysisRun(Base):
    __tablename__ = "analysis_runs"
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    scenario_revision_id: Mapped[str] = mapped_column(ForeignKey("scenario_revisions.id"))
    input_hash: Mapped[str] = mapped_column(String(80))
    verifier_version: Mapped[str] = mapped_column(String(40))
    freshness: Mapped[str] = mapped_column(String(20))
    stale_reason: Mapped[str | None] = mapped_column(Text)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    input_snapshot: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    timings: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    job_id: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = _now()
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        CheckConstraint("freshness in ('current','stale','failed')"),
        Index("ix_analysis_runs_scenario", "scenario_revision_id"),
    )


class AnalysisInputRef(Base):
    """Dependency index: which revision of what each run consumed."""

    __tablename__ = "analysis_inputs"
    run_id: Mapped[str] = mapped_column(ForeignKey("analysis_runs.id", ondelete="CASCADE"), primary_key=True)
    kind: Mapped[str] = mapped_column(String(30), primary_key=True)
    ref_id: Mapped[str] = mapped_column(String(120), primary_key=True)
    ref_version: Mapped[str] = mapped_column(String(120))
    __table_args__ = (Index("ix_analysis_inputs_ref", "kind", "ref_id"),)


class Finding(Base):
    __tablename__ = "findings"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("analysis_runs.id", ondelete="CASCADE"))
    requirement_id: Mapped[str] = mapped_column(String(80))
    requirement_version: Mapped[int] = mapped_column(Integer)
    state_label: Mapped[str] = mapped_column(Text)
    origin: Mapped[str] = mapped_column(String(80))
    destination: Mapped[str] = mapped_column(String(80))
    outcome: Mapped[str] = mapped_column(String(40))
    hypothetical: Mapped[bool] = mapped_column(Boolean)
    __table_args__ = (Index("ix_findings_run", "run_id"),)


class RepairRun(Base):
    __tablename__ = "repair_runs"
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    scenario_revision_id: Mapped[str] = mapped_column(ForeignKey("scenario_revisions.id"))
    problem: Mapped[dict[str, Any]] = mapped_column(JSONB)
    status: Mapped[str | None] = mapped_column(String(20))
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    freshness: Mapped[str] = mapped_column(String(20), default="current")
    stale_reason: Mapped[str | None] = mapped_column(Text)
    job_id: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = _now()


class RepairCandidateRow(Base):
    __tablename__ = "repair_candidates"
    id: Mapped[str] = mapped_column(String(120), primary_key=True)
    repair_run_id: Mapped[str] = mapped_column(ForeignKey("repair_runs.id", ondelete="CASCADE"))
    rank: Mapped[int] = mapped_column(Integer)
    selection: Mapped[dict[str, Any]] = mapped_column(JSONB)
    cost: Mapped[float] = mapped_column(Float)
    accepted: Mapped[bool] = mapped_column(Boolean)
    outcomes: Mapped[dict[str, Any]] = mapped_column(JSONB)
    reasons: Mapped[list[Any]] = mapped_column(JSONB)
    branch_scenario_revision_id: Mapped[str | None] = mapped_column(ForeignKey("scenario_revisions.id"))


class Job(Base):
    __tablename__ = "jobs"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(String(40))
    input_hash: Mapped[str] = mapped_column(String(80))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    state: Mapped[str] = mapped_column(String(20), default="queued")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3)
    lease_owner: Mapped[str | None] = mapped_column(String(120))
    lease_token: Mapped[int] = mapped_column(BigInteger, default=0)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    run_after: Mapped[datetime] = _now()
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    progress: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _now()
    updated_at: Mapped[datetime] = _now()
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        CheckConstraint("state in ('queued','running','succeeded','failed','cancelled')"),
        CheckConstraint("attempts >= 0 AND attempts <= max_attempts"),
        Index("ix_jobs_claim", "state", "run_after"),
        Index("uq_jobs_identity", "kind", "input_hash", unique=True,
              postgresql_where=text("state in ('queued','running','succeeded')")),
    )
