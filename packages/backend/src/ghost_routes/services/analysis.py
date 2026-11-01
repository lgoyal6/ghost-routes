"""Analysis service shared by the API, CLI and worker."""

from __future__ import annotations

import time
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ghost_routes.domain.model import AnalysisInput
from ghost_routes.domain.results import AnalysisResult
from ghost_routes.jobs import queue
from ghost_routes.jobs.worker import JobContext, PermanentError
from ghost_routes.persistence import repo
from ghost_routes.persistence import schema as S
from ghost_routes.persistence.db import transaction
from ghost_routes.scenarios.compiler import ScenarioError
from ghost_routes.verification.classify import InvalidModel
from ghost_routes.verification.sensitivity import review_priorities
from ghost_routes.verification.verifier import analysis_input_hash, verify


def request_analysis(s: Session, scenario_revision_id: str, assumptions: dict[str, Any] | None = None) -> tuple[int, bool, str]:
    """Enqueue an analysis keyed by the exact input identity. Returns (job id, created, input hash)."""
    inp = repo.build_input(s, scenario_revision_id, assumptions)
    ih = analysis_input_hash(inp)
    job_id, created = queue.enqueue(
        s, "analyze", {"scenario_revision_id": scenario_revision_id, "assumptions": assumptions or {}},
        identity={"input_hash": ih, "scenario_revision_id": scenario_revision_id},
    )
    return job_id, created, ih


def compute(inp: AnalysisInput) -> tuple[AnalysisResult, dict[str, Any]]:
    t0 = time.perf_counter()
    result = verify(inp)
    return result, {"verify_ms": round((time.perf_counter() - t0) * 1000, 2)}


def handle_analyze_job(ctx: JobContext) -> None:
    payload = ctx.claim.payload
    srid = payload["scenario_revision_id"]
    with transaction(ctx.db_url) as s:
        inp = repo.build_input(s, srid, payload.get("assumptions") or {})
    try:
        result, timings = compute(inp)
    except (ScenarioError, InvalidModel, ValueError) as exc:
        raise PermanentError(str(exc)) from exc
    ctx.heartbeat({"stage": "publishing"})
    run_id = repo.new_id("run")

    def write(s: Session) -> dict[str, Any]:
        run = repo.publish_analysis(s, run_id=run_id, scenario_revision_id=srid, inp=inp, result=result,
                                    timings=timings, job_id=ctx.claim.job_id)
        return {"run_id": run.id, "freshness": run.freshness, "input_hash": result.input_hash}

    ctx.publish(write)


def latest_run(s: Session, scenario_revision_id: str) -> S.AnalysisRun | None:
    run = s.scalars(
        select(S.AnalysisRun).where(S.AnalysisRun.scenario_revision_id == scenario_revision_id)
        .order_by(S.AnalysisRun.published_at.desc().nulls_last(), S.AnalysisRun.created_at.desc()).limit(1)
    ).first()
    if run is not None:
        repo.recheck_freshness(s, run.id)
    return run


def questions(s: Session, scenario_revision_id: str, max_facts: int = 120) -> dict[str, Any]:
    inp = repo.build_input(s, scenario_revision_id)
    qs, meta = review_priorities(inp, max_facts=max_facts)
    return {"questions": [q.model_dump(mode="json") for q in qs], "meta": meta,
            "input_hash": analysis_input_hash(inp)}
