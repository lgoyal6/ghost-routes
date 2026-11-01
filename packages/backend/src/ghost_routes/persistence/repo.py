"""Repositories: immutable revisions, freshness invalidation and fenced publication.

All head changes take the heads lock and invalidate dependent analysis runs in
the same transaction.
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime
from typing import Any, cast

from geoalchemy2.shape import from_shape, to_shape
from psycopg.types.range import Range
from shapely.geometry import LineString, Point, Polygon
from sqlalchemy import func, select, text, update
from sqlalchemy.orm import Session

from ghost_routes.domain.canonical import content_hash
from ghost_routes.domain.model import (
    AccessRequirement,
    AnalysisInput,
    AttributeValue,
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
    Phase,
    ProjectSnapshot,
    Restriction,
    RestrictionKind,
    Scenario,
    TargetRelation,
    TemporaryConnection,
)
from ghost_routes.domain.results import AnalysisResult
from ghost_routes.persistence import schema as S
from ghost_routes.persistence.db import lock_heads

_OBS_ID = re.compile(r"\bobs_[0-9a-f]{16}\b")


class Conflict(Exception):
    """Optimistic concurrency failure; carries structured detail for the API."""

    def __init__(self, message: str, **detail: Any):
        super().__init__(message)
        self.detail = {"message": message, **detail}


class NotFound(Exception):
    pass


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


# --------------------------------------------------------------- geometry


def _poly(points: tuple[tuple[float, float], ...] | None) -> Any:
    return from_shape(Polygon(points), srid=0) if points else None


def _points(geom: Any) -> tuple[tuple[float, float], ...]:
    shp = to_shape(geom)
    if isinstance(shp, Polygon):
        coords = list(shp.exterior.coords)[:-1]
    else:
        coords = list(shp.coords)
    return tuple((float(x), float(y)) for x, y in coords)


def _range(iv: Interval | None) -> Range | None:  # type: ignore[type-arg]
    if iv is None:
        return None
    return Range(iv.start, iv.end, bounds="[)")


def _interval(r: Any) -> Interval | None:
    if r is None:
        return None
    if r.lower_inc is False or (r.upper is not None and r.upper_inc):
        raise ValueError("stored ranges must be half-open [start, end)")
    return Interval(start=r.lower, end=r.upper)


# --------------------------------------------------------------- invalidation


def invalidate(s: Session, kind: str, ref_id: str, head_version: str, reason: str) -> int:
    """Mark current runs that consumed a non-head version of ``ref_id`` stale."""
    sub = select(S.AnalysisInputRef.run_id).where(
        S.AnalysisInputRef.kind == kind,
        S.AnalysisInputRef.ref_id == ref_id,
        S.AnalysisInputRef.ref_version != head_version,
    )
    res = s.execute(
        update(S.AnalysisRun)
        .where(S.AnalysisRun.freshness == "current", S.AnalysisRun.id.in_(sub))
        .values(freshness="stale", stale_reason=reason)
    )
    rs = s.execute(
        update(S.RepairRun)
        .where(S.RepairRun.freshness == "current", S.RepairRun.scenario_revision_id.in_(
            select(S.AnalysisRun.scenario_revision_id).where(S.AnalysisRun.id.in_(sub))))
        .values(freshness="stale", stale_reason=reason)
    )
    return (getattr(res, "rowcount", 0) or 0) + (getattr(rs, "rowcount", 0) or 0)


# --------------------------------------------------------------- networks


def create_lineage(s: Session, lineage_id: str, title: str, frame: Frame, synthetic: bool = False) -> S.NetworkLineage:
    row = S.NetworkLineage(id=lineage_id, title=title, frame_id=frame.id, frame_kind=frame.kind,
                           metric=frame.metric, frame_description=frame.description, head_number=0, synthetic=synthetic)
    s.add(row)
    s.flush()
    return row


def commit_network_revision(
    s: Session,
    lineage_id: str,
    nodes: tuple[Node, ...],
    edges: tuple[Edge, ...],
    coverage: tuple[CoverageAssertion, ...],
    *,
    author: str,
    author_kind: str,
    note: str,
    expected_head: int | None,
    change: dict[str, Any] | None = None,
    snap_tolerance: float = 0.0,
) -> str:
    """Create a new immutable network revision and move the lineage head."""
    lock_heads(s)
    lineage = s.get(S.NetworkLineage, lineage_id, with_for_update=True)
    if lineage is None:
        raise NotFound(f"network lineage {lineage_id}")
    if expected_head is not None and lineage.head_number != expected_head:
        raise Conflict("network revision is outdated", expected_revision=expected_head,
                       current_revision=lineage.head_number, current_revision_id=lineage.head_revision_id)
    number = lineage.head_number + 1
    rid = f"{lineage_id}@{number}"
    frame = Frame(id=lineage.frame_id, kind=lineage.frame_kind, metric=lineage.metric,  # type: ignore[arg-type]
                  description=lineage.frame_description)
    snap = NetworkSnapshot(revision_id=rid, frame=frame, nodes=nodes, edges=edges,
                           coverage=tuple(c.model_copy(update={"id": f"{rid}:{c.id.split(':')[-1]}"}) for c in coverage),
                           snap_tolerance=snap_tolerance)
    s.add(S.NetworkRevision(id=rid, lineage_id=lineage_id, number=number, parent_id=lineage.head_revision_id,
                            author=author, author_kind=author_kind, note=note, change=change or {},
                            content_hash=content_hash({"nodes": snap.nodes, "edges": snap.edges, "coverage": [
                                c.model_copy(update={"id": c.id.split(":")[-1]}) for c in snap.coverage]}),
                            snap_tolerance=snap_tolerance))
    s.flush()
    for n in snap.nodes:
        s.add(S.NetworkNode(revision_id=rid, node_id=n.id, geom=from_shape(Point(n.x, n.y), srid=0), kind=n.kind.value,
                            level=n.level, label=n.label, open_boundary=n.open_boundary, wait_allowed=n.wait_allowed))
    s.flush()
    for e in snap.edges:
        s.add(S.NetworkEdge(revision_id=rid, edge_id=e.id, u=e.u, v=e.v, kind=e.kind.value, directed=e.directed,
                            geom=from_shape(LineString(e.geometry), srid=0), length_m=e.length_m,
                            existence=e.existence.value, existence_evidence=list(e.existence_evidence),
                            attributes={k: v.model_dump(mode="json") for k, v in e.attributes.items()}, label=e.label))
    for c in snap.coverage:
        s.add(S.CoverageRow(id=c.id, network_revision_id=rid, scope="network", polygon=_poly(c.polygon),
                            attributes_complete=list(c.attributes_complete), sheets=list(c.sheets),
                            reviewer=c.reviewer, assumptions=c.assumptions))
    lineage.head_number = number
    lineage.head_revision_id = rid
    s.flush()
    invalidate(s, "network_lineage", lineage_id, rid, f"network revision superseded by {rid} ({note})")
    return rid


def load_network(s: Session, revision_id: str) -> NetworkSnapshot:
    rev = s.get(S.NetworkRevision, revision_id)
    if rev is None:
        raise NotFound(f"network revision {revision_id}")
    lineage = s.get(S.NetworkLineage, rev.lineage_id)
    assert lineage is not None
    nodes = []
    for n in s.scalars(select(S.NetworkNode).where(S.NetworkNode.revision_id == revision_id).order_by(S.NetworkNode.node_id)):
        p = cast(Point, to_shape(n.geom))
        nodes.append(Node(id=n.node_id, x=p.x, y=p.y, kind=NodeKind(n.kind), level=n.level, label=n.label,
                          open_boundary=n.open_boundary, wait_allowed=n.wait_allowed))
    edges = []
    for e in s.scalars(select(S.NetworkEdge).where(S.NetworkEdge.revision_id == revision_id).order_by(S.NetworkEdge.edge_id)):
        edges.append(Edge(id=e.edge_id, u=e.u, v=e.v, kind=EdgeKind(e.kind), directed=e.directed,
                          geometry=_points(e.geom), length_m=e.length_m, existence=EvidenceState(e.existence),
                          existence_evidence=tuple(e.existence_evidence),
                          attributes={k: AttributeValue.model_validate(v) for k, v in e.attributes.items()}, label=e.label))
    cov = []
    for c in s.scalars(select(S.CoverageRow).where(S.CoverageRow.network_revision_id == revision_id).order_by(S.CoverageRow.id)):
        cov.append(CoverageAssertion(id=c.id, scope="network", polygon=_points(c.polygon),
                                     attributes_complete=tuple(c.attributes_complete), sheets=tuple(c.sheets),
                                     reviewer=c.reviewer, assumptions=c.assumptions))
    frame = Frame(id=lineage.frame_id, kind=lineage.frame_kind, metric=lineage.metric,  # type: ignore[arg-type]
                  description=lineage.frame_description)
    return NetworkSnapshot(revision_id=revision_id, frame=frame, nodes=tuple(nodes), edges=tuple(edges),
                           coverage=tuple(cov), snap_tolerance=rev.snap_tolerance)


# --------------------------------------------------------------- projects


def ensure_project(s: Session, project_id: str, title: str, *, synthetic: bool = False, hypothetical: bool = False,
                   description: str = "") -> S.Project:
    row = s.get(S.Project, project_id)
    if row is None:
        row = S.Project(id=project_id, title=title, synthetic=synthetic, hypothetical=hypothetical, description=description)
        s.add(row)
        s.flush()
    return row


def commit_project_revision(
    s: Session,
    snap: ProjectSnapshot,
    *,
    frame_id: str,
    plan_revision_id: str | None,
    expected_head: str | bool | None = False,
    note: str = "",
) -> str:
    """Store a new project revision and move the project's head.

    ``expected_head=False`` skips the optimistic check; otherwise it must equal
    the current head revision id (``None`` for a brand new project).
    """
    lock_heads(s)
    project = s.get(S.Project, snap.project_id, with_for_update=True)
    if project is None:
        raise NotFound(f"project {snap.project_id}")
    if expected_head is not False and project.head_revision_id != expected_head:
        raise Conflict("project revision is outdated", expected_revision=expected_head,
                       current_revision=project.head_revision_id)
    number = (s.scalar(select(func.max(S.ProjectRevision.number)).where(S.ProjectRevision.project_id == snap.project_id)) or 0) + 1
    rid = f"{snap.project_id}@{number}"
    body = snap.model_copy(update={"revision_id": rid})
    s.add(S.ProjectRevision(id=rid, project_id=snap.project_id, number=number, parent_id=project.head_revision_id,
                            plan_revision_id=plan_revision_id, frame_id=frame_id, extent=_poly(snap.extent),
                            hypothetical=snap.hypothetical, note=note,
                            content_hash=content_hash(body.model_copy(update={"revision_id": ""}))))
    s.flush()
    for ph in snap.phases:
        s.add(S.PhaseRow(project_revision_id=rid, phase_id=ph.id, label=ph.label, ord=ph.order,
                         prerequisites=list(ph.prerequisites), effective=_range(ph.effective)))
    for r in snap.restrictions:
        s.add(S.RestrictionRow(project_revision_id=rid, restriction_id=r.id, phase_ids=list(r.phase_ids), kind=r.kind.value,
                               targets=[t.model_dump(mode="json") for t in r.targets], polygon=_poly(r.polygon),
                               overrides={k: v.model_dump(mode="json") for k, v in r.overrides.items()},
                               effective=_range(r.effective), state=r.state.value, evidence=list(r.evidence), label=r.label))
    for c in snap.temporary_connections:
        s.add(S.TemporaryConnectionRow(project_revision_id=rid, connection_id=c.id, phase_ids=list(c.phase_ids),
                                       edge=c.edge.model_dump(mode="json"), geom=from_shape(LineString(c.edge.geometry), srid=0),
                                       effective=_range(c.effective), state=c.state.value,
                                       active_by_default=c.active_by_default, evidence=list(c.evidence)))
    for cov in snap.coverage:
        s.add(S.CoverageRow(id=f"{rid}:{cov.id}", project_revision_id=rid, scope=snap.project_id, polygon=_poly(cov.polygon),
                            attributes_complete=list(cov.attributes_complete), sheets=list(cov.sheets),
                            reviewer=cov.reviewer, assumptions=cov.assumptions))
    project.head_revision_id = rid
    s.flush()
    invalidate(s, "project", snap.project_id, rid, f"project revision superseded by {rid}{': ' + note if note else ''}")
    return rid


def load_project(s: Session, revision_id: str) -> ProjectSnapshot:
    rev = s.get(S.ProjectRevision, revision_id)
    if rev is None:
        raise NotFound(f"project revision {revision_id}")
    project = s.get(S.Project, rev.project_id)
    assert project is not None
    phases = tuple(
        Phase(id=p.phase_id, label=p.label, order=p.ord, prerequisites=tuple(p.prerequisites), effective=_interval(p.effective))
        for p in s.scalars(select(S.PhaseRow).where(S.PhaseRow.project_revision_id == revision_id).order_by(S.PhaseRow.ord, S.PhaseRow.phase_id))
    )
    restrictions = tuple(
        Restriction(id=r.restriction_id, phase_ids=tuple(r.phase_ids), kind=RestrictionKind(r.kind),
                    targets=tuple(TargetRelation.model_validate(t) for t in r.targets),
                    polygon=_points(r.polygon) if r.polygon is not None else None,
                    overrides={k: AttributeValue.model_validate(v) for k, v in r.overrides.items()},
                    effective=_interval(r.effective), state=EvidenceState(r.state), evidence=tuple(r.evidence), label=r.label)
        for r in s.scalars(select(S.RestrictionRow).where(S.RestrictionRow.project_revision_id == revision_id).order_by(S.RestrictionRow.restriction_id))
    )
    temps = tuple(
        TemporaryConnection(id=c.connection_id, phase_ids=tuple(c.phase_ids), edge=Edge.model_validate(c.edge),
                            effective=_interval(c.effective), state=EvidenceState(c.state),
                            active_by_default=c.active_by_default, evidence=tuple(c.evidence))
        for c in s.scalars(select(S.TemporaryConnectionRow).where(S.TemporaryConnectionRow.project_revision_id == revision_id).order_by(S.TemporaryConnectionRow.connection_id))
    )
    cov = tuple(
        CoverageAssertion(id=c.id.split(":", 1)[1], scope=c.scope, polygon=_points(c.polygon),
                          attributes_complete=tuple(c.attributes_complete), sheets=tuple(c.sheets),
                          reviewer=c.reviewer, assumptions=c.assumptions)
        for c in s.scalars(select(S.CoverageRow).where(S.CoverageRow.project_revision_id == revision_id).order_by(S.CoverageRow.id))
    )
    return ProjectSnapshot(project_id=rev.project_id, revision_id=revision_id, title=project.title, phases=phases,
                           restrictions=restrictions, temporary_connections=temps,
                           extent=_points(rev.extent) if rev.extent is not None else None, coverage=cov,
                           hypothetical=rev.hypothetical)


# --------------------------------------------------------------- profiles & requirements


def save_profile(s: Session, profile: MobilityProfile) -> MobilityProfile:
    """Store as the next version of ``profile.id``; returns the stored value."""
    lock_heads(s)
    cur = s.scalar(select(func.max(S.ProfileRow.version)).where(S.ProfileRow.id == profile.id)) or 0
    stored = profile.model_copy(update={"version": cur + 1})
    s.add(S.ProfileRow(id=stored.id, version=stored.version, body=stored.model_dump(mode="json"),
                       review_state=stored.review_state))
    s.flush()
    invalidate(s, "profile", stored.id, str(stored.version), f"profile {stored.id} revised to v{stored.version}")
    return stored


def latest_profile(s: Session, profile_id: str, version: int | None = None) -> MobilityProfile:
    q = select(S.ProfileRow).where(S.ProfileRow.id == profile_id)
    q = q.where(S.ProfileRow.version == version) if version else q.order_by(S.ProfileRow.version.desc())
    row = s.scalars(q.limit(1)).first()
    if row is None:
        raise NotFound(f"profile {profile_id}")
    return MobilityProfile.model_validate(row.body)


def save_requirement(s: Session, req: AccessRequirement) -> AccessRequirement:
    lock_heads(s)
    cur = s.scalar(select(func.max(S.RequirementRow.version)).where(S.RequirementRow.id == req.id)) or 0
    stored = req.model_copy(update={"version": cur + 1})
    s.add(S.RequirementRow(id=stored.id, version=stored.version, profile_id=stored.profile_id,
                           body=stored.model_dump(mode="json")))
    s.flush()
    invalidate(s, "requirement", stored.id, str(stored.version), f"requirement {stored.id} revised to v{stored.version}")
    return stored


def latest_requirement(s: Session, req_id: str, version: int | None = None) -> AccessRequirement:
    q = select(S.RequirementRow).where(S.RequirementRow.id == req_id)
    q = q.where(S.RequirementRow.version == version) if version else q.order_by(S.RequirementRow.version.desc())
    row = s.scalars(q.limit(1)).first()
    if row is None:
        raise NotFound(f"requirement {req_id}")
    return AccessRequirement.model_validate(row.body)


# --------------------------------------------------------------- scenarios


def create_scenario(s: Session, scenario_id: str, title: str, lineage_id: str) -> S.ScenarioRow:
    row = S.ScenarioRow(id=scenario_id, title=title, network_lineage_id=lineage_id)
    s.add(row)
    s.flush()
    return row


def commit_scenario_revision(
    s: Session,
    scenario: Scenario,
    *,
    network_revision_id: str,
    project_revision_ids: dict[str, str],
    requirement_refs: list[tuple[str, int]],
    branch_of: str | None = None,
    expected_head: str | bool | None = False,
) -> str:
    lock_heads(s)
    sc = s.get(S.ScenarioRow, scenario.id, with_for_update=True)
    if sc is None:
        raise NotFound(f"scenario {scenario.id}")
    if expected_head is not False and sc.head_revision_id != expected_head:
        raise Conflict("scenario revision is outdated", expected_revision=expected_head, current_revision=sc.head_revision_id)
    number = (s.scalar(select(func.max(S.ScenarioRevisionRow.number)).where(S.ScenarioRevisionRow.scenario_id == scenario.id)) or 0) + 1
    rid = f"{scenario.id}@{number}"
    body = scenario.model_copy(update={"revision_id": rid})
    if set(project_revision_ids) != set(body.project_ids):
        raise ValueError("project revisions must match the scenario's projects")
    s.add(S.ScenarioRevisionRow(
        id=rid, scenario_id=scenario.id, number=number, parent_id=sc.head_revision_id,
        body=body.model_dump(mode="json"), network_revision_id=network_revision_id,
        project_revision_ids=[project_revision_ids[p] for p in sorted(project_revision_ids)],
        requirement_refs=[list(r) for r in sorted(requirement_refs)],
        content_hash=content_hash({"scenario": body.model_copy(update={"revision_id": ""}), "network": network_revision_id,
                                   "projects": project_revision_ids, "requirements": sorted(requirement_refs)}),
        hypothetical=body.hypothetical, branch_of=branch_of,
    ))
    if branch_of is None:
        sc.head_revision_id = rid
        s.flush()
        invalidate(s, "scenario", scenario.id, rid, f"scenario revised to {rid}")
    s.flush()
    return rid


def build_input(s: Session, scenario_revision_id: str, assumptions: dict[str, Any] | None = None) -> AnalysisInput:
    rev = s.get(S.ScenarioRevisionRow, scenario_revision_id)
    if rev is None:
        raise NotFound(f"scenario revision {scenario_revision_id}")
    scenario = Scenario.model_validate(rev.body)
    network = load_network(s, rev.network_revision_id)
    projects = tuple(load_project(s, pid) for pid in rev.project_revision_ids)
    reqs = tuple(latest_requirement(s, rid, ver) for rid, ver in rev.requirement_refs)
    profile_ids = sorted({r.profile_id for r in reqs})
    profiles = tuple(latest_profile(s, pid) for pid in profile_ids)
    return AnalysisInput(network=network, projects=projects, scenario=scenario, profiles=profiles,
                         requirements=reqs, assumptions=assumptions or {})


def head_refs(s: Session, inp: AnalysisInput, scenario_revision_id: str) -> list[tuple[str, str, str]]:
    """(kind, ref_id, version) for every input of an analysis."""
    rev = s.get(S.NetworkRevision, inp.network.revision_id)
    assert rev is not None
    refs = [("network_lineage", rev.lineage_id, inp.network.revision_id),
            ("scenario", inp.scenario.id, scenario_revision_id)]
    refs += [("project", p.project_id, p.revision_id) for p in inp.projects]
    for p in inp.projects:
        prow = s.get(S.ProjectRevision, p.revision_id)
        if prow is not None and prow.plan_revision_id:
            refs.append(("project_source", p.project_id, prow.plan_revision_id))
    for oid in sorted(set(_OBS_ID.findall(inp.model_dump_json()))):
        ob = s.get(S.Observation, oid)
        if ob is not None:
            refs.append(("evidence", oid, str(ob.revision)))
    refs += [("profile", p.id, str(p.version)) for p in inp.profiles]
    refs += [("requirement", r.id, str(r.version)) for r in inp.requirements]
    return refs


def current_versions(s: Session, refs: list[tuple[str, str, str]]) -> list[str]:
    """Reasons an input is no longer the head (empty list means all current)."""
    reasons = []
    for kind, ref_id, version in refs:
        if kind == "network_lineage":
            lin = s.get(S.NetworkLineage, ref_id)
            head = lin.head_revision_id if lin else None
        elif kind == "project":
            p = s.get(S.Project, ref_id)
            head = p.head_revision_id if p else None
        elif kind == "scenario":
            sc = s.get(S.ScenarioRow, ref_id)
            head = sc.head_revision_id if sc else None
            if head != version:
                # Hypothetical branches are never heads but stay comparable to their base.
                row = s.get(S.ScenarioRevisionRow, version)
                if row is not None and row.branch_of == head:
                    continue
        elif kind == "project_source":
            # The head source is the plan revision no later revision supersedes.
            superseded = select(S.PlanRevision.supersedes_id).where(S.PlanRevision.supersedes_id.is_not(None))
            head = s.scalars(select(S.PlanRevision.id).where(S.PlanRevision.project_id == ref_id,
                                                             S.PlanRevision.id.not_in(superseded))).first()
        elif kind == "evidence":
            ob = s.get(S.Observation, ref_id)
            head = str(ob.revision) if ob else None
        elif kind == "profile":
            head = str(s.scalar(select(func.max(S.ProfileRow.version)).where(S.ProfileRow.id == ref_id)))
        else:
            head = str(s.scalar(select(func.max(S.RequirementRow.version)).where(S.RequirementRow.id == ref_id)))
        if head != version:
            reasons.append(f"{kind} {ref_id} is at {head}, analysis used {version}")
    return reasons


def publish_analysis(
    s: Session,
    *,
    run_id: str,
    scenario_revision_id: str,
    inp: AnalysisInput,
    result: AnalysisResult,
    timings: dict[str, Any],
    job_id: int | None,
    fence: Any = None,
) -> S.AnalysisRun:
    """Persist a run. It is ``current`` only if every input is still the head.

    ``fence`` is an optional callable that raises if the publishing worker's
    lease has been lost; it runs inside this transaction after the heads lock.
    """
    lock_heads(s)
    if fence is not None:
        fence(s)
    refs = head_refs(s, inp, scenario_revision_id)
    stale = current_versions(s, refs)
    run = S.AnalysisRun(
        id=run_id, scenario_revision_id=scenario_revision_id, input_hash=result.input_hash,
        verifier_version=result.verifier_version, freshness="stale" if stale else "current",
        stale_reason="; ".join(stale) if stale else None, result=result.model_dump(mode="json"),
        input_snapshot=inp.model_dump(mode="json"), timings=timings, job_id=job_id,
        published_at=datetime.now(UTC),
    )
    s.add(run)
    s.flush()
    for kind, ref_id, version in refs:
        s.add(S.AnalysisInputRef(run_id=run_id, kind=kind, ref_id=ref_id, ref_version=version))
    for r in result.requirements:
        for st in r.states:
            for p in st.pairs:
                s.add(S.Finding(run_id=run_id, requirement_id=r.requirement_id, requirement_version=r.requirement_version,
                                state_label=st.label, origin=p.origin, destination=p.destination,
                                outcome=p.outcome.value, hypothetical=st.hypothetical))
    s.flush()
    return run


def recheck_freshness(s: Session, run_id: str) -> S.AnalysisRun:
    """Re-evaluate a current run against heads (conservative safety net for reads)."""
    run = s.get(S.AnalysisRun, run_id)
    if run is None:
        raise NotFound(f"analysis run {run_id}")
    if run.freshness == "current":
        refs = [(r.kind, r.ref_id, r.ref_version) for r in s.scalars(
            select(S.AnalysisInputRef).where(S.AnalysisInputRef.run_id == run_id))]
        stale = current_versions(s, refs)
        if stale:
            run.freshness = "stale"
            run.stale_reason = "; ".join(stale)
    return run


def table_counts(s: Session) -> dict[str, int]:
    out = {}
    for t in ("projects", "source_artifacts", "observations", "review_decisions", "network_revisions",
              "project_revisions", "scenario_revisions", "analysis_runs", "jobs"):
        out[t] = int(s.execute(text(f"SELECT count(*) FROM {t}")).scalar() or 0)
    return out
