"""Repair service: persisted problems, solved by the worker, candidates stored as hypothetical branches."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ghost_routes.domain.model import Scenario
from ghost_routes.jobs import queue
from ghost_routes.jobs.worker import JobContext
from ghost_routes.optimization.repair import (
    Intervention,
    RepairProblem,
    apply_selection,
    solve,
)
from ghost_routes.persistence import repo
from ghost_routes.persistence import schema as S
from ghost_routes.persistence.db import transaction
from ghost_routes.verification.verifier import verify


def request_repair(
    s: Session,
    scenario_revision_id: str,
    interventions: list[Intervention],
    restore: list[str],
    protect: list[str],
    options: dict[str, Any] | None = None,
) -> tuple[str, int]:
    inp = repo.build_input(s, scenario_revision_id)
    problem = RepairProblem(base=inp, interventions=tuple(interventions), restore=tuple(restore),
                            protect=tuple(protect), **(options or {}))
    run_id = repo.new_id("repair")
    body = problem.model_dump(mode="json", exclude={"base"})
    s.add(S.RepairRun(id=run_id, scenario_revision_id=scenario_revision_id, problem=body, status=None))
    s.flush()
    job_id, _ = queue.enqueue(s, "repair", {"repair_run_id": run_id}, identity={"repair_run_id": run_id})
    run = s.get(S.RepairRun, run_id)
    assert run is not None
    run.job_id = job_id
    return run_id, job_id


def handle_repair_job(ctx: JobContext) -> None:
    rid = ctx.claim.payload["repair_run_id"]
    with transaction(ctx.db_url) as s:
        run = s.get(S.RepairRun, rid)
        assert run is not None
        srid = run.scenario_revision_id
        inp = repo.build_input(s, srid)
        problem = RepairProblem.model_validate({**run.problem, "base": inp.model_dump(mode="json")})
        rev = s.get(S.ScenarioRevisionRow, srid)
        assert rev is not None
        net_rev, proj_revs, req_refs = rev.network_revision_id, list(rev.project_revision_ids), list(rev.requirement_refs)
    result = solve(problem)
    ctx.heartbeat({"stage": "publishing", "status": result.status.value})

    def write(s: Session) -> dict[str, Any]:
        run = s.get(S.RepairRun, rid, with_for_update=True)
        assert run is not None
        run.status = result.status.value
        run.result = result.model_dump(mode="json")
        stale = repo.current_versions(s, repo.head_refs(s, inp, srid))
        if stale:
            run.freshness = "stale"
            run.stale_reason = "; ".join(stale)
        projects = {p.split("@")[0]: p for p in proj_revs}
        for rank, cand in enumerate([*result.candidates, *result.rejected]):
            branch_id = None
            if cand.accepted:
                branch = apply_selection(problem, cand.selection)
                scenario: Scenario = branch.scenario
                branch_id = repo.commit_scenario_revision(
                    s, scenario, network_revision_id=net_rev, project_revision_ids=projects,
                    requirement_refs=[(r[0], r[1]) for r in req_refs], branch_of=srid,
                )
                # Store the independent recheck as an ordinary (hypothetical) analysis run.
                bres = verify(branch.model_copy(update={"scenario": branch.scenario.model_copy(update={"revision_id": branch_id})}))
                repo.publish_analysis(s, run_id=repo.new_id("run"), scenario_revision_id=branch_id,
                                      inp=branch.model_copy(update={"scenario": branch.scenario.model_copy(update={"revision_id": branch_id})}),
                                      result=bres, timings={}, job_id=ctx.claim.job_id)
            s.add(S.RepairCandidateRow(
                id=f"{rid}:{rank}", repair_run_id=rid, rank=rank, selection=cand.selection, cost=cand.cost,
                accepted=cand.accepted, outcomes={k: v.value for k, v in cand.outcomes.items()},
                reasons=list(cand.reasons), branch_scenario_revision_id=branch_id,
            ))
        return {"repair_run_id": rid, "status": result.status.value, "candidates": len(result.candidates)}

    ctx.publish(write)


def get_repair(s: Session, run_id: str) -> dict[str, Any]:
    run = s.get(S.RepairRun, run_id)
    if run is None:
        raise repo.NotFound(f"repair run {run_id}")
    cands = s.scalars(select(S.RepairCandidateRow).where(S.RepairCandidateRow.repair_run_id == run_id)
                      .order_by(S.RepairCandidateRow.rank)).all()
    job = s.get(S.Job, run.job_id) if run.job_id else None
    return {
        "id": run.id, "scenario_revision_id": run.scenario_revision_id, "status": run.status,
        "freshness": run.freshness, "stale_reason": run.stale_reason, "problem": run.problem, "result": run.result,
        "job": {"id": job.id, "state": job.state, "error": job.error} if job else None,
        "candidates": [{"id": c.id, "rank": c.rank, "selection": c.selection, "cost": c.cost, "accepted": c.accepted,
                        "outcomes": c.outcomes, "reasons": c.reasons,
                        "branch_scenario_revision_id": c.branch_scenario_revision_id} for c in cands],
    }
