"""Model extraction jobs: serialized, bounded, idempotent and fenced."""

from __future__ import annotations

import hashlib
import json
import time
from typing import Any

from geoalchemy2.shape import from_shape
from shapely.geometry import LineString, Point, Polygon
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from ghost_routes.config import get_settings
from ghost_routes.documents.transforms import crop_to_page
from ghost_routes.inference.extract import (
    OUTPUT_SCHEMA,
    PROMPT_VERSION,
    VALIDATOR_VERSION,
    ValidationReport,
    build_prompt,
    group_lines,
    prompt_sha256,
    validate_output,
    words_in_crop,
)
from ghost_routes.inference.ollama import InferenceError, ModelState, OllamaClient
from ghost_routes.jobs import queue
from ghost_routes.jobs.worker import JobContext, PermanentError
from ghost_routes.persistence import repo
from ghost_routes.persistence import schema as S
from ghost_routes.persistence.db import transaction

PIPELINE = "gr-pipeline/1 native-words+qwen-vl crop extraction"


class ModelUnavailable(RuntimeError):
    def __init__(self, state: str, message: str):
        super().__init__(message)
        self.state = state


def client() -> OllamaClient:
    cfg = get_settings()
    return OllamaClient(cfg.ollama_url, cfg.model, cfg.inference_timeout_s)


def inference_options() -> dict[str, Any]:
    cfg = get_settings()
    return {"temperature": 0, "seed": 0, "num_predict": cfg.inference_max_tokens, "num_ctx": 8192}


def run_identity(crop_sha: str, digest: str, options: dict[str, Any]) -> str:
    blob = json.dumps({"crop": crop_sha, "digest": digest, "prompt": prompt_sha256(), "pipeline": PIPELINE,
                       "options": options}, sort_keys=True)
    return "ext_" + hashlib.sha256(blob.encode()).hexdigest()[:32]


def request_extraction(s: Session, sheet_id: str, crop_ids: list[str] | None = None) -> tuple[int, bool, dict[str, Any]]:
    status = client().status()
    if status.state != ModelState.READY:
        raise ModelUnavailable(status.state.value, status.message)
    sheet = s.get(S.Sheet, sheet_id)
    if sheet is None:
        raise repo.NotFound(f"sheet {sheet_id}")
    crops = s.scalars(select(S.Crop).where(S.Crop.sheet_id == sheet_id).order_by(S.Crop.ry0, S.Crop.rx0)).all()
    if crop_ids:
        crops = [c for c in crops if c.id in set(crop_ids)]
    if not crops:
        raise ValueError("no crops selected")
    payload = {"sheet_id": sheet_id, "crop_ids": [c.id for c in crops]}
    identity = {**payload, "digest": status.digest, "prompt": prompt_sha256(), "options": inference_options()}
    job_id, created = queue.enqueue(s, "extract", payload, identity=identity, max_attempts=2)
    return job_id, created, {"model": status.model, "digest": status.digest}


def add_observations(s: Session, report: ValidationReport | None, ident: str, run_id: str, crop: S.Crop) -> int:
    n = 0
    for ob in (report.observations if report else []):
        geom = Point(ob.geometry_page[0]) if len(ob.geometry_page) == 1 else LineString(ob.geometry_page)
        s.add(S.Observation(
            id=repo.new_id("obs"), idempotency_key=ob.key(f"{ident}:{VALIDATOR_VERSION}"), sheet_id=crop.sheet_id,
            extraction_run_id=run_id, provenance="model", cls=ob.cls, label=ob.label,
            geom=from_shape(geom, srid=0), bbox=from_shape(Polygon(ob.bbox_page), srid=0),
            properties={**ob.properties, "word_ids": ob.word_ids, "crop_id": crop.id},
            phase_refs=ob.phase_refs, supporting_text=ob.supporting_text, uncertainty=ob.uncertainty,
            review_state="pending",
            current_value={"cls": ob.cls, "label": ob.label, "phase_refs": ob.phase_refs, "properties": ob.properties},
        ))
        n += 1
    return n


def revalidate_sheet(s: Session, sheet_id: str) -> dict[str, Any]:
    """Re-derive model observations from stored raw responses with the current validator.

    No inference happens. A run is skipped when any of its observations has
    review activity, because reviewed evidence must not be replaced silently.
    """
    sheet = s.get(S.Sheet, sheet_id)
    if sheet is None:
        raise repo.NotFound(f"sheet {sheet_id}")
    words = list(sheet.native_words or [])
    out: list[dict[str, Any]] = []
    runs = s.scalars(select(S.ExtractionRun).where(S.ExtractionRun.sheet_id == sheet_id,
                                                   S.ExtractionRun.status != "failed")
                     .order_by(S.ExtractionRun.created_at)).all()
    for run in runs:
        if (run.validation or {}).get("validator") == VALIDATOR_VERSION:
            out.append({"run_id": run.id, "skipped": "already current"})
            continue
        obs_ids = select(S.Observation.id).where(S.Observation.extraction_run_id == run.id)
        reviewed = s.scalars(select(S.Observation.id).where(
            S.Observation.extraction_run_id == run.id,
            (S.Observation.review_state != "pending") | S.Observation.id.in_(
                select(S.ReviewDecision.observation_id)))).first()
        if reviewed is not None:
            out.append({"run_id": run.id, "skipped": "has review activity"})
            continue
        if not run.raw_response_path:
            out.append({"run_id": run.id, "skipped": "no stored raw response"})
            continue
        raw = json.loads(open(run.raw_response_path).read())  # noqa: SIM115 - small local file
        crop = s.get(S.Crop, run.crop_id)
        assert crop is not None
        c2p = crop_to_page(crop.rx0, crop.ry0, sheet.render_dpi)
        clines = group_lines(words_in_crop(words, c2p.inverse(), crop.width_px, crop.height_px))
        report = validate_output(raw["final_content"] or "", clines, c2p, crop.width_px, crop.height_px)
        if report.parse_error:
            out.append({"run_id": run.id, "skipped": f"stored response no longer parses: {report.parse_error}"})
            continue
        s.execute(delete(S.Observation).where(S.Observation.id.in_(obs_ids)))
        n = add_observations(s, report, run.identity_hash, run.id, crop)
        run.validation = report.summary()
        run.status = "succeeded" if n else "empty"
        out.append({"run_id": run.id, "crop_id": crop.id, "observations": n})
    # Analyses consume only reviewed evidence, and reviewed runs are skipped above,
    # so no analysis run can depend on an observation replaced here.
    return {"sheet_id": sheet_id, "validator": VALIDATOR_VERSION, "runs": out}


def handle_extract_job(ctx: JobContext) -> None:
    cfg = get_settings()
    cl = client()
    status = cl.status()
    if status.state != ModelState.READY or not status.digest:
        raise RuntimeError(f"model not ready: {status.state.value} {status.message}")
    options = inference_options()
    with transaction(ctx.db_url) as s:
        sheet = s.get(S.Sheet, ctx.claim.payload["sheet_id"])
        if sheet is None:
            raise PermanentError("sheet vanished")
        words = list(sheet.native_words or [])
        dpi = sheet.render_dpi
        crops = [s.get(S.Crop, cid) for cid in ctx.claim.payload["crop_ids"]]
    summary: dict[str, Any] = {"runs": [], "model": status.model, "digest": status.digest}
    for i, crop in enumerate(crops):
        assert crop is not None
        ident = run_identity(crop.sha256, status.digest, options)
        with transaction(ctx.db_url) as s:
            existing = s.scalars(select(S.ExtractionRun).where(S.ExtractionRun.identity_hash == ident)).first()
        if existing is not None:
            summary["runs"].append({"crop_id": crop.id, "run_id": existing.id, "status": existing.status, "reused": True})
            continue
        ctx.heartbeat({"stage": "inference", "crop": i + 1, "of": len(crops), "crop_id": crop.id})
        c2p = crop_to_page(crop.rx0, crop.ry0, dpi)
        clines = group_lines(words_in_crop(words, c2p.inverse(), crop.width_px, crop.height_px))
        prompt = build_prompt(clines, crop.width_px, crop.height_px)
        image = open(crop.storage_path, "rb").read()  # noqa: SIM115 - small local file
        attempts: list[dict[str, Any]] = []
        report = None
        content = None
        t0 = time.monotonic()
        for attempt in range(cfg.inference_retries + 1):
            p = prompt if attempt == 0 else (
                prompt + f"\n\nYour previous answer could not be used ({attempts[-1]['error']}). "
                "Return only JSON that matches the schema, with at most 8 observations and no repetition.")
            try:
                res = cl.chat_image(p, image, OUTPUT_SCHEMA, options)
            except InferenceError as exc:
                attempts.append({"attempt": attempt + 1, "error": str(exc)})
                continue
            content = res.content
            report = validate_output(content, clines, c2p, crop.width_px, crop.height_px)
            if res.raw.get("done_reason") == "length" and report.parse_error:
                report.parse_error = f"output truncated at the token limit ({report.parse_error})"
            attempts.append({"attempt": attempt + 1, "error": report.parse_error, "stats": res.raw,
                             "duration_ms": res.duration_ms})
            if report.parse_error is None:
                break
            ctx.heartbeat({"stage": "retry", "crop": i + 1, "of": len(crops)})
        duration = int((time.monotonic() - t0) * 1000)
        run_id = repo.new_id("ext")
        raw_path = cfg.path("raw", f"{run_id}.json")
        raw_path.write_text(json.dumps({"prompt_version": PROMPT_VERSION, "prompt": prompt, "attempts": attempts,
                                        "final_content": content}, indent=1))
        ok = report is not None and report.parse_error is None
        status_s = "failed" if not ok else ("succeeded" if report and report.observations else "empty")

        def write(s: Session, report=report, run_id=run_id, ident=ident, crop=crop, status_s=status_s,
                  attempts=attempts, duration=duration, raw_path=raw_path) -> dict[str, Any]:  # type: ignore[no-untyped-def]
            # Failed runs stay as history; only a usable run claims the identity.
            stored_identity = ident if status_s != "failed" else f"{ident}#failed#{run_id}"
            s.add(S.ExtractionRun(
                id=run_id, identity_hash=stored_identity, sheet_id=crop.sheet_id, crop_id=crop.id, job_id=ctx.claim.job_id,
                pipeline=PIPELINE, model=status.model, model_digest=status.digest or "", prompt_version=PROMPT_VERSION,
                prompt_sha256=prompt_sha256(), settings=options, status=status_s, attempts=len(attempts),
                raw_response_path=str(raw_path), error=None if status_s != "failed" else attempts[-1].get("error"),
                validation=report.summary() if report else {}, duration_ms=duration,
            ))
            s.flush()
            n = add_observations(s, report if status_s != "failed" else None, ident, run_id, crop)
            return {"crop_id": crop.id, "run_id": run_id, "status": status_s, "observations": n}

        summary["runs"].append(ctx.publish(write, final=False))
    if summary["runs"] and all(r["status"] == "failed" for r in summary["runs"]):
        raise RuntimeError("every crop failed inference; see extraction runs for raw errors")
    ctx.publish(lambda s: summary)
