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
