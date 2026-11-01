"""Turn reviewed sheet observations into network and project revisions."""

from __future__ import annotations

from typing import Any

from geoalchemy2.shape import to_shape
from shapely.geometry import Polygon
from sqlalchemy import select
from sqlalchemy.orm import Session

from ghost_routes.documents.transforms import Affine
from ghost_routes.domain.model import CoverageAssertion, Phase, ProjectSnapshot
from ghost_routes.network.topology import ObsInput, build
from ghost_routes.persistence import repo
from ghost_routes.persistence import schema as S


def _obs_inputs(s: Session, sheet_id: str, include_pending: bool) -> list[ObsInput]:
    out = []
    for ob in s.scalars(select(S.Observation).where(S.Observation.sheet_id == sheet_id).order_by(S.Observation.id)):
        if ob.review_state == "rejected" or (ob.review_state == "pending" and not include_pending):
            continue
        cv = ob.current_value or {}
        geom = cv.get("geometry_page")
        if geom is None:
            g = to_shape(ob.geom)
            geom = [next(iter(g.coords))] if g.geom_type == "Point" else list(g.coords)
        out.append(ObsInput(
            id=ob.id, cls=cv.get("cls", ob.cls), review_state=ob.review_state,
            geometry=tuple((float(x), float(y)) for x, y in geom),
            phase_refs=tuple(cv.get("phase_refs", ob.phase_refs) or ()), label=cv.get("label", ob.label),
            attributes=(cv.get("properties") or {}).get("attributes", {}),
        ))
    return out


def build_from_observations(
    s: Session,
    lineage_id: str,
    *,
    sheet_id: str,
    calibration_id: str | None,
    snap_tolerance: float,
    barrier_buffer: float,
    project_id: str,
    phase_map: dict[str, str],
    include_pending: bool,
    author: str,
    author_kind: str,
    expected_head: int | None,
) -> dict[str, Any]:
    lineage = s.get(S.NetworkLineage, lineage_id)
    if lineage is None:
        raise repo.NotFound(f"network lineage {lineage_id}")
    sheet = s.get(S.Sheet, sheet_id)
    if sheet is None:
        raise repo.NotFound(f"sheet {sheet_id}")
    transform = None
    if calibration_id:
        cal = s.get(S.SheetCalibration, calibration_id)
        if cal is None or cal.sheet_id != sheet_id:
            raise repo.NotFound(f"calibration {calibration_id} for sheet {sheet_id}")
        if not lineage.metric or cal.frame_id != lineage.frame_id:
            raise ValueError("calibration frame must match a metric network lineage")
        transform = Affine.from_list(cal.matrix)
    elif lineage.metric:
        raise ValueError("a metric network needs a reviewed sheet calibration; metric claims require calibration")
    rep = build(_obs_inputs(s, sheet_id, include_pending), transform=transform, snap_tolerance=snap_tolerance,
                phase_map=phase_map, barrier_buffer=barrier_buffer)
    if not rep.edges:
        raise ValueError("no path or crossing observations to build from")
    net_rev = repo.commit_network_revision(
        s, lineage_id, tuple(rep.nodes), tuple(rep.edges), (), author=author, author_kind=author_kind,
        note=f"built from sheet {sheet.printed_sheet_id or sheet_id} observations", expected_head=expected_head,
        change={"sheet_id": sheet_id, "calibration_id": calibration_id, "snaps": rep.snaps,
                "refused_snaps": rep.refused_snaps, "skipped": rep.skipped},
        snap_tolerance=snap_tolerance,
    )
    project = s.get(S.Project, project_id)
    if project is None:
        raise repo.NotFound(f"project {project_id}")
    prev = repo.load_project(s, project.head_revision_id) if project.head_revision_id else None
    phases = prev.phases if prev else tuple(Phase(id=pid, label=label, order=i + 1)
                                            for i, (label, pid) in enumerate(sorted(phase_map.items())))
    snap = ProjectSnapshot(project_id=project_id, revision_id="pending", title=project.title, phases=phases,
                           restrictions=tuple(rep.restrictions), temporary_connections=prev.temporary_connections if prev else (),
                           extent=prev.extent if prev else None, coverage=prev.coverage if prev else (),
                           hypothetical=project.hypothetical)
    proj_rev = repo.commit_project_revision(s, snap, frame_id=lineage.frame_id, plan_revision_id=sheet.plan_revision_id,
                                            note=f"restrictions built from {sheet.printed_sheet_id or sheet_id}")
    return {"network_revision": net_rev, "project_revision": proj_rev, "nodes": len(rep.nodes), "edges": len(rep.edges),
            "restrictions": len(rep.restrictions), "entrances": rep.entrance_nodes, "snaps": rep.snaps,
            "refused_snaps": rep.refused_snaps, "skipped": rep.skipped}


def assert_coverage(
    s: Session,
    *,
    lineage_id: str | None,
    project_id: str | None,
    polygon: list[tuple[float, float]],
    sheets: list[str],
    reviewer: str,
    reviewer_kind: str,
    assumptions: str,
    attributes_complete: list[str],
) -> dict[str, Any]:
    """Record a reviewed coverage assertion as a new revision. AI never declares coverage."""
    if reviewer_kind == "scripted" and not assumptions:
        raise ValueError("scripted coverage needs explicit assumptions")
    if not Polygon(polygon).is_valid:
        raise ValueError("coverage polygon is not valid")
    cov = CoverageAssertion(id=repo.new_id("cov"), scope="network", polygon=tuple(polygon), sheets=tuple(sheets),
                            reviewer=f"{reviewer} ({reviewer_kind})", assumptions=assumptions,
                            attributes_complete=tuple(attributes_complete))
    if lineage_id:
        lin = s.get(S.NetworkLineage, lineage_id)
        if lin is None or lin.head_revision_id is None:
            raise repo.NotFound(f"network lineage {lineage_id}")
        net = repo.load_network(s, lin.head_revision_id)
        rid = repo.commit_network_revision(
            s, lineage_id, net.nodes, net.edges,
            tuple(c.model_copy(update={"id": c.id.split(":")[-1]}) for c in net.coverage) + (cov,),
            author=reviewer, author_kind=reviewer_kind, note="coverage asserted", expected_head=lin.head_number,
            change={"coverage": cov.model_dump(mode="json")}, snap_tolerance=net.snap_tolerance)
        return {"network_revision": rid}
    assert project_id
    project = s.get(S.Project, project_id)
    if project is None or project.head_revision_id is None:
        raise repo.NotFound(f"project {project_id}")
    p = repo.load_project(s, project.head_revision_id)
    prow = s.get(S.ProjectRevision, p.revision_id)
    assert prow is not None
    snap = p.model_copy(update={"coverage": p.coverage + (cov.model_copy(update={"scope": project_id}),)})
    rid = repo.commit_project_revision(s, snap, frame_id=prow.frame_id, plan_revision_id=prow.plan_revision_id,
                                       expected_head=p.revision_id, note=f"coverage asserted by {reviewer}")
    return {"project_revision": rid}

