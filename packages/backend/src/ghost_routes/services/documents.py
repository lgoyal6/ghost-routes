"""Source ingestion: immutable originals, sheets, renders, crops and native observations."""

from __future__ import annotations

import hashlib
import io
from datetime import datetime
from pathlib import Path
from typing import Any

from geoalchemy2.shape import from_shape
from shapely.geometry import LineString, Point, Polygon
from sqlalchemy import select
from sqlalchemy.orm import Session

from ghost_routes.config import get_settings
from ghost_routes.documents.pdf import inspect_pdf, render_page, tiles
from ghost_routes.documents.transforms import fit_similarity
from ghost_routes.documents.vectors import vector_observations
from ghost_routes.inference.extract import native_observations
from ghost_routes.persistence import repo
from ghost_routes.persistence import schema as S
from ghost_routes.persistence.db import lock_heads

REUSE = ("redistributable", "reference_only", "unknown", "synthetic_original")


def _write_once(path: Path, data: bytes) -> None:
    if path.exists():
        if hashlib.sha256(path.read_bytes()).hexdigest() != hashlib.sha256(data).hexdigest():
            raise RuntimeError(f"stored artifact {path} does not match its content hash")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(data)
    tmp.replace(path)
    path.chmod(0o444)


def printed_sheet_id(words: list[dict[str, Any]]) -> str | None:
    """Read the sheet id that follows 'SHEET NUMBER' on the same text line."""
    for i, w in enumerate(words):
        if w["text"].upper() == "SHEET" and i + 1 < len(words) and words[i + 1]["text"].upper().startswith("NUMBER"):
            line = [x for x in words if abs(x["top"] - w["top"]) < 3 and x["x0"] > words[i + 1]["x1"]]
            line.sort(key=lambda x: x["x0"])
            tail = "".join(x["text"] for x in line[:3]).strip()
            rest = words[i + 1]["text"][len("NUMBER"):].strip(": ")
            sid = (rest + tail).replace(" ", "")
            return sid or None
    return None


def ingest_pdf(
    s: Session,
    data: bytes,
    *,
    filename: str,
    project_id: str,
    project_title: str,
    revision_label: str,
    reuse_status: str,
    source_url: str | None = None,
    retrieved_at: datetime | None = None,
    revision_date: datetime | None = None,
    synthetic: bool = False,
    note: str = "",
) -> dict[str, Any]:
    cfg = get_settings()
    if reuse_status not in REUSE:
        raise ValueError(f"reuse_status must be one of {REUSE}")
    pages, meta = inspect_pdf(data, max_bytes=cfg.max_upload_bytes, max_pages=cfg.max_pages)
    sha = hashlib.sha256(data).hexdigest()
    src_path = cfg.path("sources", f"{sha}.pdf")
    _write_once(src_path, data)

    artifact = s.scalars(select(S.SourceArtifact).where(S.SourceArtifact.sha256 == sha)).first()
    if artifact is None:
        artifact = S.SourceArtifact(
            id=f"src_{sha[:16]}", sha256=sha, mime="application/pdf", size_bytes=len(data),
            original_filename=Path(filename).name[:200], source_url=source_url, retrieved_at=retrieved_at,
            storage_path=str(src_path), reuse_status=reuse_status, page_count=len(pages), synthetic=synthetic,
            metadata_=meta,
        )
        s.add(artifact)
        s.flush()
    repo.ensure_project(s, project_id, project_title, synthetic=synthetic)
    lock_heads(s)
    superseded = select(S.PlanRevision.supersedes_id).where(S.PlanRevision.supersedes_id.is_not(None))
    prior = s.scalars(select(S.PlanRevision).where(S.PlanRevision.project_id == project_id,
                                                   S.PlanRevision.id.not_in(superseded))).first()
    if s.scalars(select(S.PlanRevision).where(S.PlanRevision.project_id == project_id,
                                              S.PlanRevision.revision_label == revision_label)).first():
        raise repo.Conflict(f"project {project_id} already has a plan revision {revision_label!r}",
                            project_id=project_id, revision_label=revision_label)
    plan = S.PlanRevision(id=repo.new_id("plan"), project_id=project_id, source_artifact_id=artifact.id,
                          revision_label=revision_label, revision_date=revision_date,
                          supersedes_id=prior.id if prior else None, note=note)
    s.add(plan)
    s.flush()

    sheets_out = []
    for p in pages:
        img = render_page(data, p.index, cfg.render_dpi)
        buf = io.BytesIO()
        img.save(buf, "PNG", optimize=True)
        rpath = cfg.path("renders", sha, f"p{p.index}@{cfg.render_dpi}.png")
        if not rpath.exists():
            rpath.write_bytes(buf.getvalue())
        sheet = S.Sheet(id=f"sheet_{plan.id[5:]}_{p.index}", plan_revision_id=plan.id, page_index=p.index,
                        printed_sheet_id=printed_sheet_id(p.words), width_pt=p.width_pt, height_pt=p.height_pt,
                        rotation=p.rotation, render_dpi=cfg.render_dpi, render_width_px=img.width,
                        render_height_px=img.height, render_path=str(rpath), native_words=p.words,
                        vector_summary=p.vectors)
        s.add(sheet)
        s.flush()
        crop_ids = []
        for rx0, ry0, rx1, ry1 in tiles(img.width, img.height, cfg.crop_px, cfg.crop_overlap_px):
            c = img.crop((rx0, ry0, rx1, ry1))
            cb = io.BytesIO()
            c.save(cb, "PNG")
            csha = hashlib.sha256(cb.getvalue()).hexdigest()
            cpath = cfg.path("crops", f"{csha}.png")
            if not cpath.exists():
                cpath.write_bytes(cb.getvalue())
            crop = S.Crop(id=f"crop_{csha[:16]}_{sheet.id[-6:]}", sheet_id=sheet.id, sha256=csha, rx0=rx0, ry0=ry0,
                          rx1=rx1, ry1=ry1, width_px=c.width, height_px=c.height, storage_path=str(cpath), purpose="tile")
            s.add(crop)
            crop_ids.append(crop.id)
        n_native = 0
        for ob in native_observations(p.words):
            x0, y0, x1, y1 = ob["bbox"]
            key = hashlib.sha256(f"native:{sheet.id}:{ob['label']}:{ob['bbox']}".encode()).hexdigest()[:40]
            s.add(S.Observation(
                id=repo.new_id("obs"), idempotency_key=key, sheet_id=sheet.id, provenance="native", cls=ob["cls"],
                label=ob["label"], geom=from_shape(Point((x0 + x1) / 2, (y0 + y1) / 2), srid=0),
                bbox=from_shape(Polygon([(x0, y0), (x1, y0), (x1, y1), (x0, y1)]), srid=0),
                properties={**ob["properties"], "word_ids": ob["word_ids"]}, phase_refs=ob["phase_refs"],
                supporting_text=ob["label"], uncertainty=[], review_state="pending",
                current_value={"cls": ob["cls"], "label": ob["label"], "phase_refs": ob["phase_refs"],
                               "properties": ob["properties"]},
            ))
            n_native += 1
        n_vector = add_vector_observations(s, sheet, p.words, p.strokes)
        sheets_out.append({"sheet_id": sheet.id, "printed_sheet_id": sheet.printed_sheet_id, "crops": crop_ids,
                           "native_observations": n_native, "vector_observations": n_vector, "rotation": p.rotation,
                           "size_pt": [p.width_pt, p.height_pt]})
    s.flush()
    # A new source revision makes results derived from older sources stale.
    repo.invalidate(s, "project_source", project_id, plan.id,
                    f"plan revision {revision_label} imported for {project_id}; results used an older source")
    return {"source_artifact_id": artifact.id, "sha256": sha, "plan_revision_id": plan.id,
            "supersedes": plan.supersedes_id, "sheets": sheets_out}


def add_vector_observations(s: Session, sheet: S.Sheet, words: list[dict[str, Any]], strokes: list[dict[str, Any]]) -> int:
    """Store path and barrier proposals found in the sheet's vector strokes (idempotent per geometry)."""
    found = vector_observations(words, strokes)
    sheet.vector_summary = {**(sheet.vector_summary or {}), "geometry": found["summary"]}
    n = 0
    for ob in found["observations"]:
        key = hashlib.sha256(f"vector:{sheet.id}:{ob['cls']}:{ob['geometry']}".encode()).hexdigest()[:40]
        if s.scalar(select(S.Observation.id).where(S.Observation.idempotency_key == key)) is not None:
            continue
        x0, y0, x1, y1 = ob["bbox"]
        # A horizontal or vertical stroke needs a nonzero selection box, not a degenerate polygon.
        if x0 == x1:
            x0, x1 = x0 - 0.5, x1 + 0.5
        if y0 == y1:
            y0, y1 = y0 - 0.5, y1 + 0.5
        s.add(S.Observation(
            id=repo.new_id("obs"), idempotency_key=key, sheet_id=sheet.id, provenance="native", cls=ob["cls"],
            label=ob["label"], geom=from_shape(LineString(ob["geometry"]), srid=0),
            bbox=from_shape(Polygon([(x0, y0), (x1, y0), (x1, y1), (x0, y1)]), srid=0),
            properties={**ob["properties"], "word_ids": ob["word_ids"]}, phase_refs=ob["phase_refs"],
            supporting_text="", uncertainty=ob["flags"], review_state="pending",
            current_value={"cls": ob["cls"], "label": ob["label"], "phase_refs": ob["phase_refs"],
                           "properties": ob["properties"]},
        ))
        n += 1
    s.flush()
    return n


def vectorize_sheet(s: Session, sheet_id: str) -> dict[str, Any]:
    """Re-read a stored source PDF and add vector geometry proposals to an existing sheet."""
    cfg = get_settings()
    sheet = s.get(S.Sheet, sheet_id)
    if sheet is None:
        raise repo.NotFound(f"sheet {sheet_id}")
    plan = s.get(S.PlanRevision, sheet.plan_revision_id)
    art = s.get(S.SourceArtifact, plan.source_artifact_id) if plan else None
    if art is None or not art.storage_path:
        raise repo.NotFound(f"source file for sheet {sheet_id}")
    pages, _ = inspect_pdf(Path(art.storage_path).read_bytes(), max_bytes=cfg.max_upload_bytes, max_pages=cfg.max_pages)
    page = pages[sheet.page_index]
    added = add_vector_observations(s, sheet, page.words, page.strokes)
    return {"sheet_id": sheet_id, "added": added, "summary": sheet.vector_summary["geometry"]}


def vector_classes(s: Session, sheet_id: str) -> set[str]:
    """Geometry classes that vector strokes already cover on a sheet."""
    rows = s.scalars(select(S.Observation.cls).where(
        S.Observation.sheet_id == sheet_id, S.Observation.provenance == "native",
        S.Observation.properties["source"].astext == "vector")).all()
    return set(rows)


def calibrate_sheet(
    s: Session,
    sheet_id: str,
    control_points: list[dict[str, Any]],
    *,
    frame_id: str,
    reviewer: str,
    reviewer_kind: str,
) -> S.SheetCalibration:
    """Fit page points -> metric site frame from reviewed control points.

    Each control point is {"page": [x, y], "site": [X, Y], "note": str}. Distances
    along a printed graphic scale bar are the expected source.
    """
    sheet = s.get(S.Sheet, sheet_id)
    if sheet is None:
        raise repo.NotFound(f"sheet {sheet_id}")
    cal = fit_similarity([tuple(cp["page"]) for cp in control_points], [tuple(cp["site"]) for cp in control_points])
    row = S.SheetCalibration(id=repo.new_id("cal"), sheet_id=sheet_id, frame_id=frame_id, method=cal.method,
                             control_points=[{**cp, "residual_m": r} for cp, r in zip(control_points, cal.residuals, strict=True)],
                             matrix=cal.transform.as_list(), rms_residual=cal.rms, reviewer=reviewer,
                             reviewer_kind=reviewer_kind)
    s.add(row)
    s.flush()
    return row


def sheet_dict(s: Session, sheet: S.Sheet) -> dict[str, Any]:
    crops = s.scalars(select(S.Crop).where(S.Crop.sheet_id == sheet.id).order_by(S.Crop.ry0, S.Crop.rx0)).all()
    cals = s.scalars(select(S.SheetCalibration).where(S.SheetCalibration.sheet_id == sheet.id)
                     .order_by(S.SheetCalibration.created_at.desc())).all()
    return {
        "id": sheet.id, "plan_revision_id": sheet.plan_revision_id, "page_index": sheet.page_index,
        "printed_sheet_id": sheet.printed_sheet_id, "width_pt": sheet.width_pt, "height_pt": sheet.height_pt,
        "rotation": sheet.rotation, "render_dpi": sheet.render_dpi, "render_width_px": sheet.render_width_px,
        "render_height_px": sheet.render_height_px, "vector_summary": sheet.vector_summary,
        "word_count": len(sheet.native_words or []),
        "crops": [{"id": c.id, "rx0": c.rx0, "ry0": c.ry0, "rx1": c.rx1, "ry1": c.ry1, "sha256": c.sha256} for c in crops],
        "calibrations": [{"id": c.id, "frame_id": c.frame_id, "method": c.method, "matrix": c.matrix,
                          "rms_residual_m": c.rms_residual, "control_points": c.control_points, "reviewer": c.reviewer,
                          "reviewer_kind": c.reviewer_kind, "created_at": c.created_at.isoformat()} for c in cals],
    }
