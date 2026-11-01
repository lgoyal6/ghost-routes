"""Bundle export from persisted runs and import into a (possibly empty) database."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from geoalchemy2.shape import to_shape
from sqlalchemy import select
from sqlalchemy.orm import Session

from ghost_routes.bundles.bundle import read_bundle, replay, write_bundle
from ghost_routes.domain.model import AnalysisInput
from ghost_routes.domain.results import AnalysisResult
from ghost_routes.jobs.worker import Worker
from ghost_routes.persistence import repo
from ghost_routes.persistence import schema as S
from ghost_routes.persistence.db import transaction
from ghost_routes.services.analysis import request_analysis
from ghost_routes.services.seed import seed_input

_OBS = re.compile(r"\bobs_[0-9a-f]{16}\b")
REDISTRIBUTABLE = ("redistributable", "synthetic_original")


def _evidence_ids(inp: AnalysisInput) -> set[str]:
    return set(_OBS.findall(inp.model_dump_json()))


def export_run(s: Session, run_id: str, out: Path) -> dict[str, Any]:
    run = repo.recheck_freshness(s, run_id)
    if run.result is None or run.input_snapshot is None:
        raise repo.NotFound(f"run {run_id} has no result")
    inp = AnalysisInput.model_validate(run.input_snapshot)
    result = AnalysisResult.model_validate(run.result)
    evidence = []
    binaries: dict[str, bytes] = {}
    omissions: list[dict[str, Any]] = []
    models: dict[str, dict[str, Any]] = {}
    for oid in sorted(_evidence_ids(inp)):
        ob = s.get(S.Observation, oid)
        if ob is None:
            omissions.append({"evidence": oid, "reason": "observation not found in exporting database"})
            continue
        sheet = s.get(S.Sheet, ob.sheet_id)
        plan = s.get(S.PlanRevision, sheet.plan_revision_id) if sheet else None
        art = s.get(S.SourceArtifact, plan.source_artifact_id) if plan else None
        b = to_shape(ob.bbox).bounds
        entry = {"id": oid, "class": ob.cls, "label": ob.label, "provenance": ob.provenance,
                 "review_state": ob.review_state, "supporting_text": ob.supporting_text,
                 "sheet": sheet.printed_sheet_id if sheet else None, "page_bbox_pt": [round(v, 3) for v in b],
                 "source_sha256": art.sha256 if art else None, "source_url": art.source_url if art else None,
                 "reuse_status": art.reuse_status if art else None}
        if ob.extraction_run_id:
            er = s.get(S.ExtractionRun, ob.extraction_run_id)
            if er:
                entry["extraction_run"] = er.id
                models[er.model_digest] = {"model": er.model, "digest": er.model_digest, "prompt_version": er.prompt_version,
                                           "prompt_sha256": er.prompt_sha256, "settings": er.settings}
        evidence.append(entry)
        if art and art.reuse_status in REDISTRIBUTABLE and ob.extraction_run_id:
            er = s.get(S.ExtractionRun, ob.extraction_run_id)
            crop = s.get(S.Crop, er.crop_id) if er else None
            if crop and f"crops/{crop.sha256}.png" not in binaries:
                binaries[f"crops/{crop.sha256}.png"] = Path(crop.storage_path).read_bytes()
        elif art:
            omissions.append({"evidence": oid, "reason": f"source reuse status is {art.reuse_status}; crop referenced "
                                                         f"by source URL and hash only"})
    repairs = [
        {"id": r.id, "status": r.status, "problem": r.problem, "result": r.result}
        for r in s.scalars(select(S.RepairRun).where(S.RepairRun.scenario_revision_id == run.scenario_revision_id))
    ]
    extras: dict[str, Any] = {"evidence.json": evidence}
    if repairs:
        extras["repairs.json"] = repairs
    manifest = write_bundle(
        out, inp, result,
        provenance={"run_id": run.id, "scenario_revision_id": run.scenario_revision_id,
                    "freshness_at_export": run.freshness, "stale_reason": run.stale_reason,
                    "model_identities": sorted(models.values(), key=lambda m: m["digest"])},
        created_at=run.published_at or datetime.now(UTC), extras=extras, binaries=binaries, omissions=omissions,
    )
    return manifest


def import_bundle(path: Path, namespace: str, db_url: str | None = None) -> dict[str, Any]:
    """Validate, replay purely, then load into this database and recompute through the worker."""
    rep = replay(path)
    _, files = read_bundle(path)
    inp = AnalysisInput.model_validate_json(files["input.json"])
    with transaction(db_url) as s:
        seeded = seed_input(s, inp, namespace=namespace, title=f"bundle replay {path.name}", synthetic=True)
        job_id, _, _ = request_analysis(s, seeded.scenario_revision_id)
    Worker(db_url=db_url, kinds=("analyze",)).run_once()
    with transaction(db_url) as s:
        job = s.get(S.Job, job_id)
        assert job is not None
        if job.state != "succeeded":
            raise RuntimeError(f"recompute failed: {job.error}")
        run = s.get(S.AnalysisRun, job.result["run_id"])  # type: ignore[index]
        assert run is not None and run.result is not None
        db_outcomes = {r["requirement_id"].removeprefix(f"{namespace}-"): r["outcome"] for r in run.result["requirements"]}
    return {"pure_replay": rep, "database_run": run.id, "database_outcomes": db_outcomes,
            "database_matches_bundle": db_outcomes == rep["outcomes"]}
