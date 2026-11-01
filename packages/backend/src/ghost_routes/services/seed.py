"""Load intentional synthetic demo data through the normal repositories.

Seeding is additive and namespaced; it never resets or deletes other data.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from ghost_routes.domain.model import AnalysisInput
from ghost_routes.persistence import repo
from ghost_routes.persistence import schema as S


@dataclass
class Seeded:
    lineage_id: str
    network_revision_id: str
    project_revision_ids: dict[str, str]
    scenario_id: str
    scenario_revision_id: str
    requirement_refs: list[tuple[str, int]]


def seed_input(s: Session, inp: AnalysisInput, *, namespace: str, title: str, synthetic: bool = True) -> Seeded:
    """Store ``inp`` under ids prefixed by ``namespace`` (projects keep their ids, prefixed)."""
    lineage_id = f"{namespace}-net"
    if s.get(S.NetworkLineage, lineage_id) is not None:
        raise repo.Conflict(f"namespace {namespace} is already seeded", namespace=namespace)
    repo.create_lineage(s, lineage_id, f"{title} network", inp.network.frame, synthetic=synthetic)
    net_rev = repo.commit_network_revision(
        s, lineage_id, inp.network.nodes, inp.network.edges, inp.network.coverage,
        author="seed", author_kind="scripted", note=f"seeded {title}", expected_head=0,
    )
    proj_map: dict[str, str] = {}
    rename = {p.project_id: f"{namespace}-{p.project_id}" for p in inp.projects}
    for p in inp.projects:
        pid = rename[p.project_id]
        repo.ensure_project(s, pid, p.title, synthetic=synthetic, hypothetical=p.hypothetical)
        snap = p.model_copy(update={
            "project_id": pid,
            "coverage": tuple(c.model_copy(update={"scope": pid}) for c in p.coverage),
        })
        proj_map[pid] = repo.commit_project_revision(s, snap, frame_id=inp.network.frame.id, plan_revision_id=None,
                                                     expected_head=None, note="seeded")
    for prof in inp.profiles:
        try:
            repo.latest_profile(s, prof.id)
        except repo.NotFound:
            repo.save_profile(s, prof)
    refs: list[tuple[str, int]] = []
    for r in inp.requirements:
        rid = f"{namespace}-{r.id}"
        stored = repo.save_requirement(s, r.model_copy(update={"id": rid}))
        refs.append((rid, stored.version))
    scenario_id = f"{namespace}-scn"
    repo.create_scenario(s, scenario_id, title, lineage_id)
    scn = inp.scenario
    states = tuple(st.model_copy(update={"active": {rename[k]: v for k, v in st.active.items()}}) for st in scn.states)
    scn = scn.model_copy(update={
        "id": scenario_id, "label": title, "project_ids": tuple(rename[p] for p in scn.project_ids), "states": states,
        "phase_shifts": {f"{rename[k.split('/')[0]]}/{k.split('/')[1]}": v for k, v in scn.phase_shifts.items()},
    })
    srid = repo.commit_scenario_revision(s, scn, network_revision_id=net_rev, project_revision_ids=proj_map,
                                         requirement_refs=refs, expected_head=None)
    return Seeded(lineage_id, net_rev, proj_map, scenario_id, srid, refs)
