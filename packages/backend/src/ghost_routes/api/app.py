"""HTTP API. Loopback only, no auth; state-changing requests must come from an allowed origin."""

from __future__ import annotations

import logging
import tempfile
import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Annotated, Any

from fastapi import Depends, FastAPI, File, Form, Query, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ghost_routes.api import schemas as A
from ghost_routes.bundles.bundle import BundleError, replay
from ghost_routes.config import get_settings
from ghost_routes.documents.pdf import DocumentRejected
from ghost_routes.domain.model import AccessRequirement, MobilityProfile, Scenario
from ghost_routes.jobs import queue
from ghost_routes.persistence import repo
from ghost_routes.persistence import schema as S
from ghost_routes.persistence.db import session_factory
from ghost_routes.scenarios.compiler import ScenarioError
from ghost_routes.services import analysis as analysis_svc
from ghost_routes.services import bundles as bundle_svc
from ghost_routes.services import diff as diff_svc
from ghost_routes.services import documents as doc_svc
from ghost_routes.services import extraction as ext_svc
from ghost_routes.services import networks as net_svc
from ghost_routes.services import repairs as repair_svc
from ghost_routes.services import review as review_svc
from ghost_routes.services.health import readiness
from ghost_routes.verification.classify import InvalidModel

log = logging.getLogger("ghost_routes.api")
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "[::1]", "testserver"}


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str, detail: dict[str, Any] | None = None):
        self.status, self.code, self.message, self.detail = status, code, message, detail or {}


def _err(status: int, code: str, message: str, detail: dict[str, Any] | None = None) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": {"code": code, "message": message, "detail": detail or {}}})


def get_db() -> Iterator[Session]:
    s = session_factory()()
    try:
        with s.begin():
            yield s
    finally:
        s.close()


DB = Annotated[Session, Depends(get_db)]


def _iso(v: Any) -> str | None:
    return v.isoformat() if v is not None else None


def job_out(j: S.Job) -> A.JobOut:
    return A.JobOut(id=j.id, kind=j.kind, state=j.state, attempts=j.attempts, max_attempts=j.max_attempts,  # type: ignore[arg-type]
                    progress=j.progress or {}, result=j.result, error=j.error, created_at=j.created_at.isoformat(),
                    updated_at=j.updated_at.isoformat(), finished_at=_iso(j.finished_at),
                    cancel_requested=j.cancel_requested)


def run_out(r: S.AnalysisRun) -> A.AnalysisRunOut:
    return A.AnalysisRunOut(id=r.id, scenario_revision_id=r.scenario_revision_id, input_hash=r.input_hash,
                            verifier_version=r.verifier_version, freshness=r.freshness,  # type: ignore[arg-type]
                            stale_reason=r.stale_reason, result=r.result, timings=r.timings,  # type: ignore[arg-type]
                            job_id=r.job_id, created_at=r.created_at.isoformat(), published_at=_iso(r.published_at))


def create_app() -> FastAPI:
    cfg = get_settings()
    app = FastAPI(title="Ghost Routes API", version="0.1.0",
                  description="Local analysis of pedestrian access through construction phases. Results describe the "
                              "reviewed model and its coverage, not real-world safety or legal compliance.")
    origins = cfg.origins()
    app.add_middleware(CORSMiddleware, allow_origins=origins, allow_methods=["GET", "POST", "OPTIONS"],
                       allow_headers=["content-type"], allow_credentials=False)

    @app.middleware("http")
    async def guard(request: Request, call_next):  # type: ignore[no-untyped-def]
        host = (request.headers.get("host") or "").rsplit(":", 1)[0]
        if host not in LOOPBACK_HOSTS:
            return _err(403, "host_not_allowed", "this local API only answers loopback hosts")
        if request.method not in SAFE_METHODS:
            origin = request.headers.get("origin")
            if origin is not None and origin not in origins:
                return _err(403, "origin_not_allowed", f"state-changing requests from {origin} are refused")
        rid = uuid.uuid4().hex[:12]
        t0 = time.perf_counter()
        response = await call_next(request)
        response.headers["x-request-id"] = rid
        log.info("request", extra={"request_id": rid, "method": request.method, "path": request.url.path,
                                   "status": response.status_code, "ms": round((time.perf_counter() - t0) * 1000, 1)})
        return response

    @app.exception_handler(ApiError)
    async def _api_error(_: Request, exc: ApiError):  # type: ignore[no-untyped-def]
        return _err(exc.status, exc.code, exc.message, exc.detail)

    @app.exception_handler(repo.Conflict)
    async def _conflict(_: Request, exc: repo.Conflict):  # type: ignore[no-untyped-def]
        return _err(409, "conflict", str(exc), exc.detail)

    @app.exception_handler(repo.NotFound)
    async def _nf(_: Request, exc: repo.NotFound):  # type: ignore[no-untyped-def]
        return _err(404, "not_found", str(exc))

    @app.exception_handler(ext_svc.ModelUnavailable)
    async def _model(_: Request, exc: ext_svc.ModelUnavailable):  # type: ignore[no-untyped-def]
        return _err(409, exc.state, str(exc), {"state": exc.state})

    for exc_type, code in ((DocumentRejected, "document_rejected"), (ScenarioError, "scenario_invalid"),
                           (InvalidModel, "invalid_model"), (BundleError, "bundle_rejected"), (ValueError, "invalid_request")):
        def make(c: str):  # type: ignore[no-untyped-def]
            async def h(_: Request, exc: Exception):  # type: ignore[no-untyped-def]
                return _err(422, c, str(exc))
            return h
        app.add_exception_handler(exc_type, make(code))

    @app.exception_handler(RequestValidationError)
    async def _val(_: Request, exc: RequestValidationError):  # type: ignore[no-untyped-def]
        return _err(422, "validation_error", "request body or parameters are invalid", {"errors": exc.errors()[:20]})

    # ---------------------------------------------------------------- health

    @app.get("/api/health", operation_id="health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/readiness", response_model=A.ReadinessOut, operation_id="getReadiness")
    def get_readiness() -> dict[str, Any]:
        return readiness()

    # ---------------------------------------------------------------- jobs

    @app.get("/api/jobs/{job_id}", response_model=A.JobOut, operation_id="getJob")
    def get_job(job_id: int, s: DB) -> A.JobOut:
        j = s.get(S.Job, job_id)
        if j is None:
            raise repo.NotFound(f"job {job_id}")
        return job_out(j)

    @app.get("/api/jobs", response_model=list[A.JobOut], operation_id="listJobs")
    def list_jobs(s: DB, limit: int = Query(50, le=200), offset: int = 0) -> list[A.JobOut]:
        rows = s.scalars(select(S.Job).order_by(S.Job.id.desc()).limit(limit).offset(offset)).all()
        return [job_out(j) for j in rows]

    @app.post("/api/jobs/{job_id}/cancel", response_model=A.JobOut, operation_id="cancelJob")
    def cancel_job(job_id: int, s: DB) -> A.JobOut:
        try:
            return job_out(queue.request_cancel(s, job_id))
        except KeyError:
            raise repo.NotFound(f"job {job_id}") from None

    # ---------------------------------------------------------------- sources

    @app.post("/api/sources", operation_id="uploadSource")
    async def upload_source(
        s: DB,
        file: UploadFile = File(...),
        project_id: str = Form(..., pattern=r"^[A-Za-z0-9][A-Za-z0-9-]{1,60}$"),
        project_title: str = Form(...),
        revision_label: str = Form(...),
        reuse_status: str = Form("unknown"),
        source_url: str | None = Form(None),
    ) -> dict[str, Any]:
        data = await file.read(cfg.max_upload_bytes + 1)
        return doc_svc.ingest_pdf(s, data, filename=file.filename or "upload.pdf", project_id=project_id,
                                  project_title=project_title, revision_label=revision_label,
                                  reuse_status=reuse_status, source_url=source_url)

    @app.get("/api/projects", response_model=list[A.ProjectOut], operation_id="listProjects")
    def list_projects(s: DB) -> list[A.ProjectOut]:
        return [project_out(s, p) for p in s.scalars(select(S.Project).order_by(S.Project.created_at))]

    @app.get("/api/projects/{project_id}", response_model=A.ProjectOut, operation_id="getProject")
    def get_project(project_id: str, s: DB) -> A.ProjectOut:
        p = s.get(S.Project, project_id)
        if p is None:
            raise repo.NotFound(f"project {project_id}")
        return project_out(s, p)

    @app.get("/api/project-revisions/{revision_id}", operation_id="getProjectRevision")
    def get_project_revision(revision_id: str, s: DB) -> dict[str, Any]:
        return repo.load_project(s, revision_id).model_dump(mode="json")

    @app.get("/api/sheets/{sheet_id}", response_model=A.SheetOut, operation_id="getSheet")
    def get_sheet(sheet_id: str, s: DB) -> dict[str, Any]:
        sh = s.get(S.Sheet, sheet_id)
        if sh is None:
            raise repo.NotFound(f"sheet {sheet_id}")
        return doc_svc.sheet_dict(s, sh)

    @app.get("/api/sheets/{sheet_id}/render", operation_id="getSheetRender", response_class=Response,
             responses={200: {"content": {"image/png": {}}}})
    def get_sheet_render(sheet_id: str, s: DB) -> Response:
        sh = s.get(S.Sheet, sheet_id)
        if sh is None:
            raise repo.NotFound(f"sheet {sheet_id}")
        path = Path(sh.render_path)
        if not path.is_file() or not path.resolve().is_relative_to(cfg.state_dir.resolve()):
            raise ApiError(404, "render_missing", "render is not in local state")
        return FileResponse(path, media_type="image/png", headers={"cache-control": "private, max-age=3600"})

    @app.get("/api/sheets/{sheet_id}/words", operation_id="getSheetWords")
    def get_sheet_words(sheet_id: str, s: DB) -> list[dict[str, Any]]:
        sh = s.get(S.Sheet, sheet_id)
        if sh is None:
            raise repo.NotFound(f"sheet {sheet_id}")
        return list(sh.native_words or [])

    @app.post("/api/sheets/{sheet_id}/calibrations", operation_id="calibrateSheet")
    def calibrate(sheet_id: str, body: A.CalibrationIn, s: DB) -> dict[str, Any]:
        row = doc_svc.calibrate_sheet(s, sheet_id, [cp.model_dump() for cp in body.control_points], frame_id=body.frame_id,
                                      reviewer=body.reviewer, reviewer_kind=body.reviewer_kind)
        return {"id": row.id, "rms_residual_m": row.rms_residual, "matrix": row.matrix, "control_points": row.control_points}

    @app.post("/api/sheets/{sheet_id}/extractions", response_model=A.JobRef, operation_id="requestExtraction")
    def request_extraction(sheet_id: str, s: DB, crop_ids: list[str] | None = None) -> A.JobRef:
        job_id, created, model = ext_svc.request_extraction(s, sheet_id, crop_ids)
        return A.JobRef(job_id=job_id, created=created, detail=model)

    @app.get("/api/sheets/{sheet_id}/extraction-runs", response_model=list[A.ExtractionRunOut], operation_id="listExtractionRuns")
    def extraction_runs(sheet_id: str, s: DB) -> list[A.ExtractionRunOut]:
        counts = dict(s.execute(select(S.Observation.extraction_run_id, func.count()).where(S.Observation.sheet_id == sheet_id)
                                .group_by(S.Observation.extraction_run_id)).all())
        rows = s.scalars(select(S.ExtractionRun).where(S.ExtractionRun.sheet_id == sheet_id).order_by(S.ExtractionRun.created_at)).all()
        return [A.ExtractionRunOut(id=r.id, sheet_id=r.sheet_id, crop_id=r.crop_id, job_id=r.job_id, model=r.model,
                                   model_digest=r.model_digest, prompt_version=r.prompt_version, prompt_sha256=r.prompt_sha256,
                                   settings=r.settings, status=r.status, attempts=r.attempts, error=r.error,  # type: ignore[arg-type]
                                   validation=r.validation, duration_ms=r.duration_ms, created_at=r.created_at.isoformat(),
                                   observation_count=int(counts.get(r.id, 0))) for r in rows]

    # ---------------------------------------------------------------- observations & review

    @app.get("/api/sheets/{sheet_id}/observations", response_model=A.ObservationPage, operation_id="listObservations")
    def list_observations(sheet_id: str, s: DB, cls: str | None = None, review_state: str | None = None,
                          provenance: str | None = None, limit: int = Query(100, ge=1, le=500), offset: int = Query(0, ge=0)) -> A.ObservationPage:
        q = select(S.Observation).where(S.Observation.sheet_id == sheet_id)
        if cls:
            q = q.where(S.Observation.cls == cls)
        if review_state:
            q = q.where(S.Observation.review_state == review_state)
        if provenance:
            q = q.where(S.Observation.provenance == provenance)
        total = s.scalar(select(func.count()).select_from(q.subquery())) or 0
        rows = s.scalars(q.order_by(S.Observation.provenance, S.Observation.cls, S.Observation.id).limit(limit).offset(offset)).all()
        return A.ObservationPage(items=[review_svc.observation_dict(o) for o in rows], total=total, offset=offset, limit=limit)  # type: ignore[misc]

    @app.post("/api/sheets/{sheet_id}/observations", response_model=A.ObservationOut, operation_id="createObservation")
    def create_observation(sheet_id: str, body: A.ObservationCreate, s: DB) -> dict[str, Any]:
        from geoalchemy2.shape import from_shape
        from shapely.geometry import LineString, Point, box

        sh = s.get(S.Sheet, sheet_id)
        if sh is None:
            raise repo.NotFound(f"sheet {sheet_id}")
        pts = [(float(x), float(y)) for x, y in body.geometry_page]
        for x, y in pts:
            if not (0 <= x <= sh.width_pt and 0 <= y <= sh.height_pt):
                raise ValueError("geometry must lie on the sheet (page points)")
        geom = Point(pts[0]) if len(pts) == 1 else LineString(pts)
        minx, miny, maxx, maxy = geom.buffer(1.0).bounds
        prov = "scripted" if body.reviewer_kind == "scripted" else "human"
        ob = S.Observation(id=repo.new_id("obs"), idempotency_key=f"manual:{uuid.uuid4().hex}", sheet_id=sheet_id,
                           provenance=prov, cls=body.cls, label=body.label, geom=from_shape(geom, srid=0),
                           bbox=from_shape(box(minx, miny, maxx, maxy), srid=0),
                           properties={**body.properties, "added_by": body.reviewer, "added_by_kind": body.reviewer_kind,
                                       "rationale": body.rationale},
                           phase_refs=body.phase_refs, supporting_text="", uncertainty=[], review_state="pending",
                           current_value={"cls": body.cls, "label": body.label, "phase_refs": body.phase_refs,
                                          "properties": body.properties})
        s.add(ob)
        s.flush()
        return review_svc.observation_dict(ob)

    @app.get("/api/observations/{observation_id}", response_model=A.ObservationOut, operation_id="getObservation")
    def get_observation(observation_id: str, s: DB) -> dict[str, Any]:
        ob = s.get(S.Observation, observation_id)
        if ob is None:
            raise repo.NotFound(f"observation {observation_id}")
        return review_svc.observation_dict(ob)

    @app.get("/api/observations/{observation_id}/decisions", response_model=list[A.DecisionOut], operation_id="listDecisions")
    def get_decisions(observation_id: str, s: DB) -> list[dict[str, Any]]:
        return review_svc.decisions(s, observation_id)

    @app.post("/api/observations/{observation_id}/review", response_model=A.ReviewOut, operation_id="reviewObservation")
    def review(observation_id: str, body: A.ReviewIn, s: DB) -> dict[str, Any]:
        return review_svc.review_observation(s, observation_id, action=body.action, expected_revision=body.expected_revision,
                                             reviewer=body.reviewer, reviewer_kind=body.reviewer_kind,
                                             rationale=body.rationale, edited_value=body.edited_value)

    # ---------------------------------------------------------------- networks

    @app.get("/api/networks", response_model=list[A.NetworkLineageOut], operation_id="listNetworks")
    def list_networks(s: DB) -> list[A.NetworkLineageOut]:
        out = []
        for lin in s.scalars(select(S.NetworkLineage).order_by(S.NetworkLineage.id)):
            revs = s.scalars(select(S.NetworkRevision).where(S.NetworkRevision.lineage_id == lin.id).order_by(S.NetworkRevision.number)).all()
            out.append(A.NetworkLineageOut(id=lin.id, title=lin.title, frame_id=lin.frame_id, frame_kind=lin.frame_kind,
                                           metric=lin.metric, head_revision_id=lin.head_revision_id, head_number=lin.head_number,
                                           synthetic=lin.synthetic,
                                           revisions=[{"id": r.id, "number": r.number, "author": r.author, "author_kind": r.author_kind,
                                                       "note": r.note, "created_at": r.created_at.isoformat(), "change": r.change,
                                                       "content_hash": r.content_hash} for r in revs]))
        return out

    @app.get("/api/network-revisions/{revision_id}", operation_id="getNetworkRevision")
    def get_network_revision(revision_id: str, s: DB) -> dict[str, Any]:
        return repo.load_network(s, revision_id).model_dump(mode="json")

    @app.post("/api/networks/{lineage_id}/build", operation_id="buildNetwork")
    def build_network(lineage_id: str, body: A.BuildNetworkIn, s: DB) -> dict[str, Any]:
        return net_svc.build_from_observations(s, lineage_id, **body.model_dump())

    # ---------------------------------------------------------------- profiles & requirements

    @app.get("/api/profiles", response_model=list[MobilityProfile], operation_id="listProfiles")
    def list_profiles(s: DB) -> list[MobilityProfile]:
        ids = s.scalars(select(S.ProfileRow.id).distinct().order_by(S.ProfileRow.id)).all()
        return [repo.latest_profile(s, i) for i in ids]

    @app.get("/api/profiles/{profile_id}/versions", response_model=list[MobilityProfile], operation_id="listProfileVersions")
    def profile_versions(profile_id: str, s: DB) -> list[MobilityProfile]:
        rows = s.scalars(select(S.ProfileRow).where(S.ProfileRow.id == profile_id).order_by(S.ProfileRow.version)).all()
        return [MobilityProfile.model_validate(r.body) for r in rows]

    @app.post("/api/profiles", response_model=MobilityProfile, operation_id="saveProfile")
    def save_profile(body: MobilityProfile, s: DB) -> MobilityProfile:
        if body.review_state == "reviewed":
            raise ValueError("use the review action to mark a profile reviewed")
        return repo.save_profile(s, body)

    @app.post("/api/profiles/{profile_id}/review", response_model=MobilityProfile, operation_id="reviewProfile")
    def review_profile(profile_id: str, body: A.ProfileReviewIn, s: DB) -> MobilityProfile:
        cur = repo.latest_profile(s, profile_id)
        state = "scripted_review" if body.reviewer_kind == "scripted" else "reviewed"
        return repo.save_profile(s, cur.model_copy(update={
            "review_state": state, "reviewer": f"{body.reviewer} ({body.reviewer_kind}): {body.rationale}"}))

    @app.get("/api/requirements", response_model=list[AccessRequirement], operation_id="listRequirements")
    def list_requirements(s: DB) -> list[AccessRequirement]:
        ids = s.scalars(select(S.RequirementRow.id).distinct().order_by(S.RequirementRow.id)).all()
        return [repo.latest_requirement(s, i) for i in ids]

    @app.post("/api/requirements", response_model=AccessRequirement, operation_id="saveRequirement")
    def save_requirement(body: AccessRequirement, s: DB) -> AccessRequirement:
        repo.latest_profile(s, body.profile_id)
        return repo.save_requirement(s, body)

    # ---------------------------------------------------------------- scenarios

    @app.get("/api/scenarios", response_model=list[A.ScenarioOut], operation_id="listScenarios")
    def list_scenarios(s: DB) -> list[A.ScenarioOut]:
        out = []
        for sc in s.scalars(select(S.ScenarioRow).order_by(S.ScenarioRow.created_at)):
            revs = s.scalars(select(S.ScenarioRevisionRow).where(S.ScenarioRevisionRow.scenario_id == sc.id)
                             .order_by(S.ScenarioRevisionRow.number)).all()
            out.append(A.ScenarioOut(id=sc.id, title=sc.title, network_lineage_id=sc.network_lineage_id,
                                     head_revision_id=sc.head_revision_id,
                                     revisions=[{"id": r.id, "number": r.number, "hypothetical": r.hypothetical,
                                                 "branch_of": r.branch_of, "created_at": r.created_at.isoformat()} for r in revs]))
        return out

    @app.post("/api/scenarios", response_model=A.ScenarioRevisionOut, operation_id="createScenario")
    def create_scenario(body: A.ScenarioCreate, s: DB) -> A.ScenarioRevisionOut:
        repo.create_scenario(s, body.id, body.title, body.network_lineage_id)
        scn = body.scenario.model_copy(update={"id": body.id})
        lin = s.get(S.NetworkLineage, body.network_lineage_id)
        if lin is None or lin.head_revision_id is None:
            raise repo.NotFound(f"network lineage {body.network_lineage_id}")
        projects = {}
        for pid in scn.project_ids:
            p = s.get(S.Project, pid)
            if p is None or p.head_revision_id is None:
                raise repo.NotFound(f"project {pid} has no revision")
            projects[pid] = p.head_revision_id
        refs = [(rid, repo.latest_requirement(s, rid).version) for rid in body.requirement_ids]
        rid = repo.commit_scenario_revision(s, scn, network_revision_id=lin.head_revision_id, project_revision_ids=projects,
                                            requirement_refs=refs, expected_head=None)
        return scenario_revision_out(s, rid)

    @app.post("/api/scenarios/{scenario_id}/revisions", response_model=A.ScenarioRevisionOut, operation_id="updateScenario")
    def update_scenario(scenario_id: str, body: A.ScenarioUpdate, s: DB) -> A.ScenarioRevisionOut:
        sc = s.get(S.ScenarioRow, scenario_id)
        if sc is None:
            raise repo.NotFound(f"scenario {scenario_id}")
        if sc.head_revision_id != body.expected_head:
            raise repo.Conflict("scenario revision is outdated", expected_revision=body.expected_head,
                                current_revision=sc.head_revision_id)
        cur = s.get(S.ScenarioRevisionRow, sc.head_revision_id)
        assert cur is not None
        scn = body.scenario.model_copy(update={"id": scenario_id})
        projects = {}
        for pid in scn.project_ids:
            p = s.get(S.Project, pid)
            if p is None or p.head_revision_id is None:
                raise repo.NotFound(f"project {pid}")
            projects[pid] = p.head_revision_id
        req_ids = body.requirement_ids if body.requirement_ids is not None else [r[0] for r in cur.requirement_refs]
        refs = [(rid, repo.latest_requirement(s, rid).version) for rid in req_ids]
        lin = s.get(S.NetworkLineage, sc.network_lineage_id)
        assert lin is not None and lin.head_revision_id
        rid = repo.commit_scenario_revision(s, scn, network_revision_id=lin.head_revision_id, project_revision_ids=projects,
                                            requirement_refs=refs, expected_head=body.expected_head)
        return scenario_revision_out(s, rid)

    @app.post("/api/scenarios/{scenario_id}/rebase", response_model=A.ScenarioRevisionOut, operation_id="rebaseScenario")
    def rebase(scenario_id: str, s: DB) -> A.ScenarioRevisionOut:
        return scenario_revision_out(s, review_svc.rebase_scenario(s, scenario_id))

    @app.get("/api/scenario-revisions/{revision_id}", response_model=A.ScenarioRevisionOut, operation_id="getScenarioRevision")
    def get_scenario_revision(revision_id: str, s: DB) -> A.ScenarioRevisionOut:
        return scenario_revision_out(s, revision_id)

    @app.get("/api/scenario-revisions/{revision_id}/geometry", response_model=A.GeometryOut, operation_id="getScenarioGeometry")
    def geometry(revision_id: str, s: DB) -> A.GeometryOut:
        inp = repo.build_input(s, revision_id)
        return A.GeometryOut(scenario_revision_id=revision_id, network=inp.network.model_dump(mode="json"),
                             projects=[p.model_dump(mode="json") for p in inp.projects],
                             frame=inp.network.frame.model_dump(mode="json"))

    @app.post("/api/scenario-revisions/{revision_id}/analyze", response_model=A.JobRef, operation_id="requestAnalysis")
    def analyze(revision_id: str, s: DB) -> A.JobRef:
        job_id, created, ih = analysis_svc.request_analysis(s, revision_id)
        return A.JobRef(job_id=job_id, created=created, detail={"input_hash": ih})

    @app.get("/api/scenario-revisions/{revision_id}/analysis", response_model=A.AnalysisRunOut | None, operation_id="getLatestAnalysis")
    def latest_analysis(revision_id: str, s: DB) -> A.AnalysisRunOut | None:
        run = analysis_svc.latest_run(s, revision_id)
        return run_out(run) if run else None

    @app.get("/api/scenario-revisions/{revision_id}/runs", response_model=list[A.AnalysisRunOut], operation_id="listRuns")
    def list_runs(revision_id: str, s: DB) -> list[A.AnalysisRunOut]:
        rows = s.scalars(select(S.AnalysisRun).where(S.AnalysisRun.scenario_revision_id == revision_id)
                         .order_by(S.AnalysisRun.published_at.desc())).all()
        return [run_out(repo.recheck_freshness(s, r.id)) for r in rows]

    @app.get("/api/analysis-runs/{run_id}", response_model=A.AnalysisRunOut, operation_id="getAnalysisRun")
    def get_run(run_id: str, s: DB) -> A.AnalysisRunOut:
        return run_out(repo.recheck_freshness(s, run_id))

    @app.get("/api/scenario-revisions/{revision_id}/questions", response_model=A.QuestionsOut, operation_id="getQuestions")
    def get_questions(revision_id: str, s: DB) -> dict[str, Any]:
        return analysis_svc.questions(s, revision_id)

    @app.post("/api/scenario-revisions/{revision_id}/resolve-fact", response_model=A.ResolveFactOut, operation_id="resolveFact")
    def resolve_fact(revision_id: str, body: A.ResolveFactIn, s: DB) -> dict[str, Any]:
        return review_svc.resolve_fact(s, revision_id, body.fact_id, value=body.value, unit=body.unit,
                                       reviewer=body.reviewer, reviewer_kind=body.reviewer_kind, rationale=body.rationale)

    # ---------------------------------------------------------------- repairs

    @app.post("/api/scenario-revisions/{revision_id}/repairs", response_model=A.JobRef, operation_id="requestRepair")
    def request_repair(revision_id: str, body: A.RepairIn, s: DB) -> A.JobRef:
        run_id, job_id = repair_svc.request_repair(
            s, revision_id, list(body.interventions), body.restore, body.protect,
            {"method": body.method, "max_candidates": body.max_candidates, "max_evaluations": body.max_evaluations,
             "time_limit_s": body.time_limit_s})
        return A.JobRef(job_id=job_id, created=True, detail={"repair_run_id": run_id})

    @app.get("/api/repairs/{run_id}", response_model=A.RepairOut, operation_id="getRepair")
    def get_repair(run_id: str, s: DB) -> dict[str, Any]:
        return repair_svc.get_repair(s, run_id)

    @app.get("/api/scenario-revisions/{revision_id}/repairs", response_model=list[A.RepairOut], operation_id="listRepairs")
    def list_repairs(revision_id: str, s: DB) -> list[dict[str, Any]]:
        ids = s.scalars(select(S.RepairRun.id).where(S.RepairRun.scenario_revision_id == revision_id)
                        .order_by(S.RepairRun.created_at.desc())).all()
        return [repair_svc.get_repair(s, i) for i in ids]

    # ---------------------------------------------------------------- diffs & bundles

    @app.get("/api/diff/{kind}", response_model=A.DiffOut, operation_id="getDiff")
    def get_diff(kind: str, from_id: str, to_id: str, s: DB) -> dict[str, Any]:
        if kind == "network":
            return diff_svc.network_diff(s, from_id, to_id)
        if kind == "project":
            return diff_svc.project_diff(s, from_id, to_id)
        if kind == "analysis":
            return diff_svc.analysis_diff(s, from_id, to_id)
        raise ApiError(404, "unknown_diff", f"unknown diff kind {kind}")

    @app.get("/api/analysis-runs/{run_id}/bundle", operation_id="exportBundle", response_class=Response,
             responses={200: {"content": {"application/zip": {}}}})
    def export_bundle(run_id: str, s: DB) -> Response:
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / f"{run_id}.zip"
            bundle_svc.export_run(s, run_id, out)
            data = out.read_bytes()
        return Response(content=data, media_type="application/zip",
                        headers={"content-disposition": f'attachment; filename="ghost-routes-{run_id}.zip"'})

    @app.post("/api/bundles/replay", response_model=A.ReplayOut, operation_id="replayBundle")
    async def replay_bundle(file: UploadFile = File(...)) -> dict[str, Any]:
        data = await file.read(60 * 1024 * 1024)
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "bundle.zip"
            p.write_bytes(data)
            return replay(p)

    return app


def project_out(s: Session, p: S.Project) -> A.ProjectOut:
    plans = s.scalars(select(S.PlanRevision).where(S.PlanRevision.project_id == p.id).order_by(S.PlanRevision.learned_at)).all()
    superseded_by = {pl.supersedes_id: pl.id for pl in plans if pl.supersedes_id}
    plan_out = []
    for pl in plans:
        art = s.get(S.SourceArtifact, pl.source_artifact_id)
        assert art is not None
        sheets = s.scalars(select(S.Sheet).where(S.Sheet.plan_revision_id == pl.id).order_by(S.Sheet.page_index)).all()
        plan_out.append(A.PlanRevisionOut(
            id=pl.id, project_id=pl.project_id, revision_label=pl.revision_label, revision_date=_iso(pl.revision_date),
            supersedes_id=pl.supersedes_id, superseded_by=superseded_by.get(pl.id), learned_at=pl.learned_at.isoformat(),
            note=pl.note,
            source={"id": art.id, "sha256": art.sha256, "size_bytes": art.size_bytes, "filename": art.original_filename,
                    "source_url": art.source_url, "retrieved_at": _iso(art.retrieved_at), "reuse_status": art.reuse_status,
                    "page_count": art.page_count, "synthetic": art.synthetic},
            sheets=[doc_svc.sheet_dict(s, sh) for sh in sheets]))
    revs = s.scalars(select(S.ProjectRevision).where(S.ProjectRevision.project_id == p.id).order_by(S.ProjectRevision.number)).all()
    return A.ProjectOut(id=p.id, title=p.title, description=p.description, synthetic=p.synthetic, hypothetical=p.hypothetical,
                        head_revision_id=p.head_revision_id, plan_revisions=plan_out,
                        revisions=[A.ProjectRevisionSummary(id=r.id, number=r.number, plan_revision_id=r.plan_revision_id,
                                                            note=r.note, created_at=r.created_at.isoformat(),
                                                            hypothetical=r.hypothetical, is_head=r.id == p.head_revision_id)
                                   for r in revs])


def scenario_revision_out(s: Session, revision_id: str) -> A.ScenarioRevisionOut:
    rev = s.get(S.ScenarioRevisionRow, revision_id)
    if rev is None:
        raise repo.NotFound(f"scenario revision {revision_id}")
    sc = s.get(S.ScenarioRow, rev.scenario_id)
    assert sc is not None
    inp = repo.build_input(s, revision_id)
    outdated = repo.current_versions(s, repo.head_refs(s, inp, revision_id))
    return A.ScenarioRevisionOut(
        id=rev.id, scenario_id=rev.scenario_id, number=rev.number, is_head=sc.head_revision_id == rev.id,
        hypothetical=rev.hypothetical, branch_of=rev.branch_of, scenario=Scenario.model_validate(rev.body),
        network_revision_id=rev.network_revision_id, project_revision_ids=list(rev.project_revision_ids),
        requirement_refs=[(r[0], r[1]) for r in rev.requirement_refs], requirements=list(inp.requirements),
        profiles=list(inp.profiles), pinned_inputs_current=not outdated, outdated_inputs=outdated,
        created_at=rev.created_at.isoformat())

