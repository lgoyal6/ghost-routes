"""HTTP contract on real PostGIS: guards, structured errors, and the flagship flow through the API."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from ghost_routes.api.app import create_app
from ghost_routes.config import get_settings
from ghost_routes.jobs.worker import Worker
from ghost_routes.persistence.db import transaction
from ghost_routes.services import demo
from ghost_routes.synthetic.flagship import flagship_interventions

pytestmark = pytest.mark.integration
NS = "api"
SCRIPTED = {"reviewer": "fixture-script", "reviewer_kind": "scripted"}


@pytest.fixture
def client(clean: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[TestClient, str, dict[str, Any]]]:
    monkeypatch.setenv("GHOST_ROUTES_DATABASE_URL", clean)
    monkeypatch.setenv("GHOST_ROUTES_STATE_DIR", str(tmp_path))
    get_settings.cache_clear()
    with transaction(clean) as s:
        seeded = demo.seed_flagship(s, NS)
    with TestClient(create_app()) as c:
        yield c, clean, seeded
    get_settings.cache_clear()


def run_jobs(url: str, kind: str) -> None:
    while Worker(db_url=url, kinds=(kind,)).run_once():
        pass


def test_guards_and_structured_errors(client: tuple[TestClient, str, dict[str, Any]]) -> None:
    c, _, _ = client
    r = c.get("/api/health", headers={"host": "evil.example"})
    assert r.status_code == 403 and r.json()["error"]["code"] == "host_not_allowed"
    r = c.post("/api/scenario-revisions/x/analyze", headers={"origin": "https://evil.example"})
    assert r.status_code == 403 and r.json()["error"]["code"] == "origin_not_allowed"
    r = c.get("/api/scenario-revisions/missing@1")
    assert r.status_code == 404 and r.json()["error"]["code"] == "not_found"
    r = c.post("/api/observations/x/review", json={"action": "accept"})
    assert r.status_code == 422 and "error" in r.json()


def test_openapi_lists_operations(client: tuple[TestClient, str, dict[str, Any]]) -> None:
    c, _, _ = client
    spec = c.get("/openapi.json").json()
    ops = {op["operationId"] for path in spec["paths"].values() for op in path.values()}
    assert {"requestAnalysis", "resolveFact", "requestRepair", "reviewObservation", "exportBundle"} <= ops


def test_flagship_flow_over_http(client: tuple[TestClient, str, dict[str, Any]]) -> None:
    c, url, seeded = client
    srid = seeded["scenario_revision_id"]
    job = c.post(f"/api/scenario-revisions/{srid}/analyze").json()
    run_jobs(url, "analyze")
    assert c.get(f"/api/jobs/{job['job_id']}").json()["state"] == "succeeded"
    run = c.get(f"/api/scenario-revisions/{srid}/analysis").json()
    outs = {r["requirement_id"]: r["outcome"] for r in run["result"]["requirements"]}
    assert outs[f"{NS}-lab-to-library"] == "NEEDS_VERIFICATION" and run["freshness"] == "current"

    qs = c.get(f"/api/scenario-revisions/{srid}/questions").json()["questions"]
    assert qs and qs[0]["fact"]["id"]

    body = {"fact_id": "edge:e8:existence", "value": False, "rationale": "scripted fixture review", **SCRIPTED}
    res = c.post(f"/api/scenario-revisions/{srid}/resolve-fact", json=body)
    assert res.status_code == 200, res.text
    new_srid = res.json()["scenario_revision"]
    stale = c.post(f"/api/scenario-revisions/{srid}/resolve-fact", json=body)
    assert stale.status_code == 409 and stale.json()["error"]["code"] == "conflict"
    assert c.get(f"/api/scenario-revisions/{srid}/analysis").json()["freshness"] == "stale"

    rep = c.post(f"/api/scenario-revisions/{new_srid}/repairs", json={
        "interventions": [i.model_dump(mode="json") for i in flagship_interventions(f"{NS}-")],
        "restore": [f"{NS}-lab-to-library"], "protect": [f"{NS}-d-to-e"]}).json()
    run_jobs(url, "repair")
    out = c.get(f"/api/repairs/{rep['detail']['repair_run_id']}").json()
    assert out["status"] == "OPTIMAL" and set(out["result"]["candidates"][0]["selection"]) == {"keep-e7"}

    zipped = c.get(f"/api/analysis-runs/{run['id']}/bundle")
    assert zipped.status_code == 200 and zipped.content[:2] == b"PK"
    replayed = c.post("/api/bundles/replay", files={"file": ("run.zip", zipped.content, "application/zip")})
    assert replayed.status_code == 200, replayed.text
    assert replayed.json()["matches"] is True


def test_review_conflict_over_http(client: tuple[TestClient, str, dict[str, Any]]) -> None:
    c, _, seeded = client
    page = c.get(f"/api/sheets/{seeded['sources']['P']['sheet_id']}/observations").json()
    ob = page["items"][0]
    ok = c.post(f"/api/observations/{ob['id']}/review", json={"action": "accept", "expected_revision": ob["revision"], **SCRIPTED})
    assert ok.status_code == 200, ok.text
    dup = c.post(f"/api/observations/{ob['id']}/review", json={"action": "reject", "expected_revision": ob["revision"], **SCRIPTED})
    assert dup.status_code == 409 and dup.json()["error"]["detail"]["current_revision"] == ob["revision"] + 1
