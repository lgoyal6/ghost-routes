"""Review decisions and fact resolution.

Observation decisions are append-only rows with optimistic concurrency.
Resolving a verifier fact writes a new immutable network or project revision
(with author, kind and rationale in its change log) and rebases the scenario,
all in one transaction together with dependent invalidation.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ghost_routes.domain.model import (
    AttributeValue,
    EvidenceState,
    Scenario,
    TargetRelation,
)
from ghost_routes.persistence import repo
from ghost_routes.persistence import schema as S

REVIEWER_KINDS = ("human_ui", "human_cli", "scripted")


def observation_dict(ob: S.Observation) -> dict[str, Any]:
    from geoalchemy2.shape import to_shape

    g = to_shape(ob.geom)
    coords = [next(iter(g.coords))] if g.geom_type == "Point" else list(g.coords)
    return {
        "id": ob.id, "sheet_id": ob.sheet_id, "extraction_run_id": ob.extraction_run_id, "provenance": ob.provenance,
        "cls": ob.cls, "label": ob.label, "geometry_page": [[round(x, 3), round(y, 3)] for x, y in coords],
        "bbox_page": [round(v, 3) for v in to_shape(ob.bbox).bounds], "properties": ob.properties,
        "phase_refs": ob.phase_refs, "supporting_text": ob.supporting_text, "uncertainty": ob.uncertainty,
        "review_state": ob.review_state, "current_value": ob.current_value, "revision": ob.revision,
        "created_at": ob.created_at.isoformat(),
    }


def review_observation(
    s: Session,
    observation_id: str,
    *,
    action: str,
    expected_revision: int,
    reviewer: str,
    reviewer_kind: str,
    rationale: str = "",
    edited_value: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if reviewer_kind not in REVIEWER_KINDS:
        raise ValueError(f"reviewer_kind must be one of {REVIEWER_KINDS}")
    if action not in ("accept", "reject", "edit"):
        raise ValueError("action must be accept, reject or edit")
    if action == "edit" and not edited_value:
        raise ValueError("edit needs an edited_value")
    if not reviewer.strip():
        raise ValueError("a reviewer label is required")
    ob = s.get(S.Observation, observation_id, with_for_update=True)
    if ob is None:
        raise repo.NotFound(f"observation {observation_id}")
    if ob.revision != expected_revision:
        raise repo.Conflict("observation changed since it was loaded", expected_revision=expected_revision,
                            current_revision=ob.revision, current=observation_dict(ob))
    snapshot = observation_dict(ob)
    new_rev = ob.revision + 1
    s.add(S.ReviewDecision(observation_id=ob.id, action=action, proposal_snapshot=snapshot, edited_value=edited_value,
                           reviewer=reviewer, reviewer_kind=reviewer_kind, rationale=rationale,
                           expected_revision=expected_revision, resulting_revision=new_rev))
    ob.revision = new_rev
    ob.review_state = {"accept": "accepted", "reject": "rejected", "edit": "edited"}[action]
    if action == "edit":
        allowed = {"cls", "label", "phase_refs", "properties", "geometry_page"}
        bad = set(edited_value or {}) - allowed
        if bad:
            raise ValueError(f"cannot edit fields {sorted(bad)}")
        ob.current_value = {**ob.current_value, **(edited_value or {})}
    s.flush()
    stale = repo.invalidate(s, "evidence", ob.id, str(new_rev), f"evidence {ob.id} reviewed ({action}) by {reviewer}")
    return {"observation": observation_dict(ob), "invalidated_runs": stale}


def decisions(s: Session, observation_id: str) -> list[dict[str, Any]]:
    rows = s.scalars(select(S.ReviewDecision).where(S.ReviewDecision.observation_id == observation_id)
                     .order_by(S.ReviewDecision.id)).all()
    return [{"id": r.id, "action": r.action, "reviewer": r.reviewer, "reviewer_kind": r.reviewer_kind,
             "rationale": r.rationale, "edited_value": r.edited_value, "expected_revision": r.expected_revision,
             "resulting_revision": r.resulting_revision, "created_at": r.created_at.isoformat(),
             "proposal_snapshot": r.proposal_snapshot} for r in rows]


def rebase_scenario(s: Session, scenario_id: str, *, expected_head: str | bool | None = False,
                    requirement_ids: list[str] | None = None) -> str:
    """New scenario revision pinned to every current head (network, projects, requirements)."""
    sc = s.get(S.ScenarioRow, scenario_id)
    if sc is None or sc.head_revision_id is None:
        raise repo.NotFound(f"scenario {scenario_id}")
    cur = s.get(S.ScenarioRevisionRow, sc.head_revision_id)
    assert cur is not None
    lineage = s.get(S.NetworkLineage, sc.network_lineage_id)
    assert lineage is not None and lineage.head_revision_id
    body = Scenario.model_validate(cur.body)
    projects = {}
    for pid in body.project_ids:
        p = s.get(S.Project, pid)
        assert p is not None and p.head_revision_id
        projects[pid] = p.head_revision_id
    reqs = []
    for rid in requirement_ids or [r[0] for r in cur.requirement_refs]:
        reqs.append((rid, repo.latest_requirement(s, rid).version))
    return repo.commit_scenario_revision(s, body, network_revision_id=lineage.head_revision_id,
                                         project_revision_ids=projects, requirement_refs=reqs,
                                         expected_head=sc.head_revision_id if expected_head is False else expected_head)


def resolve_fact(
    s: Session,
    scenario_revision_id: str,
    fact_id: str,
    *,
    value: Any,
    unit: str | None,
    reviewer: str,
    reviewer_kind: str,
    rationale: str,
    expected_network_revision: int | None = None,
) -> dict[str, Any]:
    """Record a reviewed resolution of one fact and rebase the scenario onto it."""
    if reviewer_kind not in REVIEWER_KINDS or not reviewer.strip():
        raise ValueError("a reviewer label and valid reviewer_kind are required")
    if not rationale.strip():
        raise ValueError("a rationale is required to resolve a fact")
    rev = s.get(S.ScenarioRevisionRow, scenario_revision_id)
    if rev is None:
        raise repo.NotFound(f"scenario revision {scenario_revision_id}")
    sc = s.get(S.ScenarioRow, rev.scenario_id)
    assert sc is not None
    if sc.head_revision_id != scenario_revision_id:
        raise repo.Conflict("scenario has a newer revision; reload before resolving facts",
                            current_revision=sc.head_revision_id)
    stamp = {"reviewer": reviewer, "reviewer_kind": reviewer_kind, "rationale": rationale,
             "at": datetime.now(UTC).isoformat(), "fact": fact_id, "value": value, "unit": unit}
    evidence_tag = f"review:{reviewer_kind}:{reviewer}"
    parts = fact_id.split(":")
    changed: dict[str, Any] = {"fact": fact_id}
    if parts[0] == "edge":
        net = repo.load_network(s, rev.network_revision_id)
        lineage = s.get(S.NetworkLineage, sc.network_lineage_id)
        assert lineage is not None
        if lineage.head_revision_id != rev.network_revision_id:
            raise repo.Conflict("network has a newer revision than this scenario; rebase first",
                                current_revision=lineage.head_revision_id)
        edges = []
        found = False
        for e in net.edges:
            if e.id != parts[1]:
                edges.append(e)
                continue
            found = True
            if parts[2] == "existence":
                st = EvidenceState.ACCEPTED if bool(value) else EvidenceState.REJECTED
                edges.append(e.model_copy(update={"existence": st, "existence_evidence": e.existence_evidence + (evidence_tag,)}))
            elif parts[2] == "attr":
                name = parts[3]
                prior = e.attributes.get(name, AttributeValue())
                attrs = {**e.attributes, name: AttributeValue(value=value, unit=unit, state=EvidenceState.ACCEPTED,
                                                              evidence=prior.evidence + (evidence_tag,))}
                edges.append(e.model_copy(update={"attributes": attrs}))
            else:
                raise ValueError(f"unsupported edge fact {fact_id}")
        if not found:
            raise repo.NotFound(f"edge {parts[1]}")
        changed["network_revision"] = repo.commit_network_revision(
            s, lineage.id, net.nodes, tuple(edges), tuple(c.model_copy(update={"id": c.id.split(":")[-1]}) for c in net.coverage),
            author=reviewer, author_kind=reviewer_kind, note=f"resolved {fact_id}: {rationale}",
            expected_head=expected_network_revision if expected_network_revision is not None else lineage.head_number,
            change=stamp, snap_tolerance=net.snap_tolerance,
        )
    elif parts[0] in ("restriction", "tempconn"):
        target_project = None
        for prid in rev.project_revision_ids:
            p = repo.load_project(s, prid)
            if parts[0] == "restriction" and any(r.id == parts[1] for r in p.restrictions):
                target_project = p
            if parts[0] == "tempconn" and any(c.id == parts[1] for c in p.temporary_connections):
                target_project = p
        if target_project is None:
            raise repo.NotFound(f"{parts[0]} {parts[1]}")
        p = target_project
        if parts[0] == "tempconn":
            temps = tuple(c.model_copy(update={"state": EvidenceState.ACCEPTED if value else EvidenceState.REJECTED,
                                               "evidence": c.evidence + (evidence_tag,)}) if c.id == parts[1] else c
                          for c in p.temporary_connections)
            snap = p.model_copy(update={"temporary_connections": temps})
        else:
            new_rs = []
            for r in p.restrictions:
                if r.id != parts[1]:
                    new_rs.append(r)
                    continue
                if len(parts) == 2:
                    new_rs.append(r.model_copy(update={"state": EvidenceState.ACCEPTED if value else EvidenceState.REJECTED,
                                                       "evidence": r.evidence + (evidence_tag,)}))
                elif parts[2] == "target":
                    edge_id = parts[3]
                    state = EvidenceState.ACCEPTED if value else EvidenceState.REJECTED
                    targets = [t for t in r.targets if t.edge_id != edge_id]
                    targets.append(TargetRelation(edge_id=edge_id, state=state, evidence=(evidence_tag,)))
                    new_rs.append(r.model_copy(update={"targets": tuple(sorted(targets, key=lambda t: t.edge_id))}))
                else:
                    raise ValueError(f"unsupported restriction fact {fact_id}")
            snap = p.model_copy(update={"restrictions": tuple(new_rs)})
        prow = s.get(S.ProjectRevision, p.revision_id)
        assert prow is not None
        changed["project_revision"] = repo.commit_project_revision(
            s, snap, frame_id=prow.frame_id, plan_revision_id=prow.plan_revision_id, expected_head=p.revision_id,
            note=f"resolved {fact_id} by {reviewer} ({reviewer_kind}): {rationale}",
        )
    else:
        raise ValueError(f"fact {fact_id} cannot be resolved by review")
    changed["scenario_revision"] = rebase_scenario(s, rev.scenario_id)
    changed["resolution"] = stamp
    return changed
