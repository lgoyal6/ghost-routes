"""Service flows on real PostGIS: the flagship demo, review, fact resolution, repair, bundles, revisions."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from ghost_routes.config import get_settings
from ghost_routes.domain.model import Outcome
from ghost_routes.jobs.worker import Worker
from ghost_routes.persistence import repo
from ghost_routes.persistence import schema as S
from ghost_routes.persistence.db import transaction
from ghost_routes.services import analysis, bundles, demo, repairs, review
from ghost_routes.synthetic.flagship import flagship_interventions

pytestmark = pytest.mark.integration
NS = "ash"


@pytest.fixture
def state_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    monkeypatch.setenv("GHOST_ROUTES_STATE_DIR", str(tmp_path))
    get_settings.cache_clear()
    yield tmp_path
    get_settings.cache_clear()


@pytest.fixture
def flagship(clean: str, state_dir: Path) -> dict[str, Any]:
    with transaction(clean) as s:
        return demo.seed_flagship(s, NS)


def analyze(url: str, srid: str) -> S.AnalysisRun:
    with transaction(url) as s:
        job_id, _, _ = analysis.request_analysis(s, srid)
    assert Worker(db_url=url, kinds=("analyze",)).run_once()
    with transaction(url) as s:
        job = s.get(S.Job, job_id)
        assert job is not None and job.state == "succeeded", job and job.error
        run = s.get(S.AnalysisRun, job.result["run_id"])  # type: ignore[index]
        assert run is not None
        s.expunge(run)
        return run


def outcomes(run: S.AnalysisRun) -> dict[str, str]:
    return {r["requirement_id"]: r["outcome"] for r in run.result["requirements"]}  # type: ignore[index]


def state_outcomes(run: S.AnalysisRun, req: str) -> dict[str, str]:
    r = next(x for x in run.result["requirements"] if x["requirement_id"] == req)  # type: ignore[index]
    return {st["label"]: st["outcome"] for st in r["states"]}


def test_seed_is_idempotent_and_links_sources(clean: str, flagship: dict[str, Any]) -> None:
    assert flagship["already_seeded"] is False
    with transaction(clean) as s:
        again = demo.seed_flagship(s, NS)
        assert again["already_seeded"] is True and again["scenario_revision_id"] == flagship["scenario_revision_id"]
        for key in ("P", "Q"):
            prid = flagship["project_revisions"][f"{NS}-{key}"]
            row = s.get(S.ProjectRevision, prid)
            assert row is not None and row.plan_revision_id == flagship["sources"][key]["plan_revision_id"]
        sheet = s.get(S.Sheet, flagship["sources"]["Q"]["sheet_id"])
        assert sheet is not None and sheet.printed_sheet_id == "ASH-Q1"


def test_flagship_overlap_needs_verification_then_disconnects(clean: str, flagship: dict[str, Any]) -> None:
    srid = flagship["scenario_revision_id"]
    run = analyze(clean, srid)
    out = outcomes(run)
    assert out[f"{NS}-lab-to-library"] == Outcome.NEEDS_VERIFICATION.value
    assert out[f"{NS}-d-to-e"] == Outcome.SUPPORTED.value
    states = state_outcomes(run, f"{NS}-lab-to-library")
    assert Outcome.NEEDS_VERIFICATION.value in states.values() and Outcome.SUPPORTED.value in states.values()

    with transaction(clean) as s:
        qs = analysis.questions(s, srid)["questions"]
        assert any(q["fact"]["id"] == "edge:e8:existence" for q in qs)

    with transaction(clean) as s:
        res = review.resolve_fact(s, srid, "edge:e8:existence", value=False, unit=None,
                                  reviewer="fixture-script", reviewer_kind="scripted",
                                  rationale="scripted fixture: courtyard path rejected")
    new_srid = res["scenario_revision"]
    assert new_srid != srid
    with transaction(clean) as s:
        old = analysis.latest_run(s, srid)
        assert old is not None and old.freshness == "stale"
    run2 = analyze(clean, new_srid)
    assert outcomes(run2)[f"{NS}-lab-to-library"] == Outcome.DISCONNECTED_WITHIN_COVERAGE.value


def test_repair_keeps_protected_requirement(clean: str, flagship: dict[str, Any]) -> None:
    with transaction(clean) as s:
        res = review.resolve_fact(s, flagship["scenario_revision_id"], "edge:e8:existence", value=False, unit=None,
                                  reviewer="fixture-script", reviewer_kind="scripted", rationale="scripted fixture")
    srid = res["scenario_revision"]
    with transaction(clean) as s:
        rid, _ = repairs.request_repair(s, srid, flagship_interventions(f"{NS}-"),
                                        restore=[f"{NS}-lab-to-library"], protect=[f"{NS}-d-to-e"])
    assert Worker(db_url=clean, kinds=("repair",)).run_once()
    with transaction(clean) as s:
        rep = repairs.get_repair(s, rid)
    assert rep["status"] == "OPTIMAL", rep["job"]
    best = rep["result"]["candidates"][0]
    assert best["cost"] == 4 and set(best["selection"]) == {"keep-e7"}
    assert any("enable-ramp" in r["selection"] and not r["accepted"] for r in rep["result"]["rejected"])
    with transaction(clean) as s:
        cand = s.scalars(select(S.RepairCandidateRow).where(S.RepairCandidateRow.repair_run_id == rid,
                                                            S.RepairCandidateRow.accepted)).first()
        assert cand is not None and cand.branch_scenario_revision_id
        branch_run = analysis.latest_run(s, cand.branch_scenario_revision_id)
        assert branch_run is not None
        assert outcomes(branch_run)[f"{NS}-lab-to-library"] != Outcome.DISCONNECTED_WITHIN_COVERAGE.value


def test_bundle_roundtrip_replays_identically(clean: str, flagship: dict[str, Any], tmp_path: Path) -> None:
    run = analyze(clean, flagship["scenario_revision_id"])
    out = tmp_path / "run.zip"
    with transaction(clean) as s:
        bundles.export_run(s, run.id, out)
    first = out.read_bytes()
    with transaction(clean) as s:
        bundles.export_run(s, run.id, out)
    assert out.read_bytes() == first, "bundle export must be deterministic"
    from ghost_routes.bundles.bundle import replay

    rep = replay(out)
    assert rep["matches"] is True and rep["outcomes"] == outcomes(run)


def test_revised_sheet_supersedes_plan_and_stales_analysis(clean: str, flagship: dict[str, Any]) -> None:
    run = analyze(clean, flagship["scenario_revision_id"])
    with transaction(clean) as s:
        res = demo.import_revised_q(s, NS)
        new = s.get(S.PlanRevision, res["plan_revision_id"])
        assert new is not None and new.supersedes_id == flagship["sources"]["Q"]["plan_revision_id"]
    with transaction(clean) as s:
        r = analysis.latest_run(s, flagship["scenario_revision_id"])
        assert r is not None and r.id == run.id and r.freshness == "stale"
        assert "plan revision B imported" in (r.stale_reason or "")


def test_review_is_optimistic_and_append_only(clean: str, flagship: dict[str, Any]) -> None:
    with transaction(clean) as s:
        ob = s.scalars(select(S.Observation).where(S.Observation.sheet_id == flagship["sources"]["P"]["sheet_id"])).first()
        assert ob is not None
        oid = ob.id
    with transaction(clean) as s:
        review.review_observation(s, oid, action="accept", expected_revision=0, reviewer="fixture-script",
                                  reviewer_kind="scripted")
    with pytest.raises(repo.Conflict), transaction(clean) as s:
        review.review_observation(s, oid, action="reject", expected_revision=0, reviewer="other", reviewer_kind="scripted")
    with transaction(clean) as s:
        assert [d["action"] for d in review.decisions(s, oid)] == ["accept"]
    with pytest.raises(DBAPIError), transaction(clean) as s:
        s.execute(text("UPDATE review_decisions SET action='reject'"))


def test_ingest_stores_vector_geometry_and_suppresses_model_geometry(clean: str, flagship: dict[str, Any]) -> None:
    import json as _json

    from ghost_routes.documents.transforms import crop_to_page
    from ghost_routes.inference.extract import validate_output
    from ghost_routes.services.documents import vector_classes
    from ghost_routes.services.extraction import add_observations

    sheet_id = flagship["sources"]["P"]["sheet_id"]
    with transaction(clean) as s:
        vec = s.scalars(select(S.Observation).where(S.Observation.sheet_id == sheet_id,
                                                    S.Observation.properties["source"].astext == "vector")).all()
        assert sorted(o.cls for o in vec).count("path") == 7 and [o.cls for o in vec].count("barrier") == 1
        assert next(o for o in vec if o.cls == "barrier").phase_refs == ["PHASE 1"]
        assert vector_classes(s, sheet_id) == {"path", "barrier"}
        sheet = s.get(S.Sheet, sheet_id)
        assert sheet is not None and sheet.vector_summary["geometry"]["proposals"] == {"path": 7, "barrier": 1}
        crop = s.scalars(select(S.Crop).where(S.Crop.sheet_id == sheet_id)).first()
        assert crop is not None
        content = _json.dumps({"observations": [
            {"class": "barrier", "label": "fence", "bbox": [10, 10, 60, 30], "points": [[10, 10], [60, 30]],
             "line_ids": [], "phase_refs": [], "uncertainty": ""},
            {"class": "place_label", "label": "HALDEN LAB", "bbox": [10, 40, 60, 50], "line_ids": [], "phase_refs": [], "uncertainty": ""}]})
        rep = validate_output(content, [], crop_to_page(crop.rx0, crop.ry0, 144), crop.width_px, crop.height_px)
        s.add(S.ExtractionRun(id="ext_test", identity_hash="ext_test", sheet_id=sheet_id, crop_id=crop.id, pipeline="test",
                              model="test", model_digest="", prompt_version="test", prompt_sha256="", settings={},
                              status="succeeded", validation={}))
        s.flush()
        assert add_observations(s, rep, "ext_test", "ext_test", crop) == 1  # the barrier is covered by vectors
        assert s.scalar(select(text("bool_and(ST_IsValid(bbox))")).select_from(S.Observation)
                        .where(S.Observation.sheet_id == sheet_id)) is True


@pytest.mark.parametrize("reviewed", [False, True])
def test_vector_revalidation_updates_pending_runs_and_preserves_reviews(
    clean: str, flagship: dict[str, Any], state_dir: Path, reviewed: bool,
) -> None:
    import json

    from sqlalchemy import delete

    from ghost_routes.documents.transforms import crop_to_page
    from ghost_routes.inference.extract import validate_output
    from ghost_routes.services.documents import vectorize_sheet
    from ghost_routes.services.extraction import add_observations, revalidate_sheet

    sheet_id = flagship["sources"]["P"]["sheet_id"]
    content = json.dumps({"observations": [
        {"class": "barrier", "label": "model fence", "bbox": [10, 10, 60, 30], "points": [[10, 10], [60, 30]],
         "line_ids": [], "phase_refs": [], "uncertainty": ""}]})
    raw_path = state_dir / "response.json"
    raw_path.write_text(json.dumps({"final_content": content}))
    with transaction(clean) as s:
        # Reproduce a sheet extracted before native vector proposals were supported.
        s.execute(delete(S.Observation).where(S.Observation.sheet_id == sheet_id,
                                             S.Observation.properties["source"].astext == "vector"))
        crop = s.scalars(select(S.Crop).where(S.Crop.sheet_id == sheet_id)).first()
        assert crop is not None
        rep = validate_output(content, [], crop_to_page(crop.rx0, crop.ry0, 144), crop.width_px, crop.height_px)
        s.add(S.ExtractionRun(id="ext_old", identity_hash="ext_old", sheet_id=sheet_id, crop_id=crop.id, pipeline="old",
                              model="test", model_digest="", prompt_version="test", prompt_sha256="", settings={},
                              status="succeeded", validation=rep.summary(), raw_response_path=str(raw_path)))
        s.flush()
        assert add_observations(s, rep, "ext_old", "ext_old", crop) == 1
    if reviewed:
        with transaction(clean) as s:
            ob = s.scalars(select(S.Observation).where(S.Observation.extraction_run_id == "ext_old")).one()
            review.review_observation(s, ob.id, action="accept", expected_revision=0, reviewer="test",
                                      reviewer_kind="scripted")
    with transaction(clean) as s:
        assert vectorize_sheet(s, sheet_id)["added"] == 8
        assert vectorize_sheet(s, sheet_id)["added"] == 0
        result = revalidate_sheet(s, sheet_id)["runs"][0]
        remaining = s.scalars(select(S.Observation).where(S.Observation.extraction_run_id == "ext_old")).all()
        if reviewed:
            assert result["skipped"] == "has review activity"
            assert len(remaining) == 1 and remaining[0].review_state == "accepted"
        else:
            assert result["observations"] == 0 and remaining == []
            assert revalidate_sheet(s, sheet_id)["runs"][0]["skipped"] == "already current"
