"""Run the baselines against the fixed manifest and write a report to local state."""

from __future__ import annotations

import hashlib
import io
import json
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ghost_routes.benchmark import manifest as M
from ghost_routes.benchmark.evaluate import Tolerances, aggregate, score_sheet
from ghost_routes.config import get_settings
from ghost_routes.documents.pdf import inspect_pdf, render_page, tiles
from ghost_routes.documents.transforms import crop_to_page
from ghost_routes.documents.vectors import vector_observations
from ghost_routes.inference.extract import (
    GEOMETRY_CLASSES,
    OUTPUT_SCHEMA,
    PROMPT_VERSION,
    VALIDATOR_VERSION,
    build_prompt,
    group_lines,
    native_observations,
    normalize_phase,
    parse_dimension_m,
    prompt_sha256,
    validate_output,
    words_in_crop,
)
from ghost_routes.inference.ollama import ModelState, OllamaClient
from ghost_routes.optimization.repair import solve
from ghost_routes.services.extraction import inference_options
from ghost_routes.synthetic.cases import all_cases
from ghost_routes.synthetic.repairs import all_repair_cases
from ghost_routes.verification.verifier import verify

BASELINES = ("native_text", "native_vector", "raw_vlm", "full_pipeline", "manual_graph")
EXTRACTION_BASELINES = ("native_text", "native_vector", "raw_vlm", "full_pipeline")


class ModelRequired(RuntimeError):
    pass


def native_predictions(pdf: bytes) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """(native text proposals, vector geometry proposals) for page 1. Neither uses a model."""
    cfg = get_settings()
    pages, _ = inspect_pdf(pdf, max_bytes=cfg.max_upload_bytes, max_pages=1)
    text = [{"cls": o["cls"], "label": o["label"], "bbox": list(o["bbox"]), "geometry": [], "phase_refs": o["phase_refs"],
             "properties": o["properties"], "flags": []} for o in native_observations(pages[0].words)]
    return text, vector_observations(pages[0].words, pages[0].strokes)["observations"]


def _raw_items(content: str, c2p: Any) -> tuple[list[dict[str, Any]], int]:
    """Model output taken at face value: no evidence, phase or dimension checks."""
    try:
        items = json.loads(content).get("observations") or []
    except (json.JSONDecodeError, AttributeError):
        return [], 1
    out, bad = [], 0
    for it in items:
        try:
            x0, y0, x1, y1 = (float(v) for v in it["bbox"])
        except (KeyError, TypeError, ValueError):
            bad += 1
            continue
        (a, b), (c, d) = c2p.apply((x0, y0)), c2p.apply((x1, y1))
        pts = [list(c2p.apply((float(p[0]), float(p[1])))) for p in it.get("points") or [] if isinstance(p, list) and len(p) == 2]
        label = str(it.get("label") or "")
        props = {"value_m": v} if it.get("class") == "dimension" and (v := parse_dimension_m(label)) is not None else {}
        out.append({"cls": str(it.get("class")), "label": label, "bbox": [min(a, c), min(b, d), max(a, c), max(b, d)],
                    "geometry": pts, "phase_refs": sorted({p for r in it.get("phase_refs") or [] if (p := normalize_phase(str(r)))}),
                    "properties": props, "flags": []})
    return out, bad


def model_predictions(pdf: bytes, client: OllamaClient, digest: str, cache: Path, *, cached_only: bool = False) -> dict[str, Any]:
    """One inference per crop (cached), read two ways: raw, and through the full validator."""
    cfg = get_settings()
    pages, _ = inspect_pdf(pdf, max_bytes=cfg.max_upload_bytes, max_pages=1)
    img = render_page(pdf, 0, cfg.render_dpi)
    raw_preds: list[dict[str, Any]] = []
    full_preds: list[dict[str, Any]] = []
    stats = {"crops": 0, "cached": 0, "inference_ms": 0, "unparseable_items": 0, "failed_crops": 0}
    for rx0, ry0, rx1, ry1 in tiles(img.width, img.height, cfg.crop_px, cfg.crop_overlap_px):
        w, h = rx1 - rx0, ry1 - ry0
        buf = io.BytesIO()
        img.crop((rx0, ry0, rx1, ry1)).save(buf, format="PNG")
        png = buf.getvalue()
        c2p = crop_to_page(rx0, ry0, cfg.render_dpi)
        lines = group_lines(words_in_crop(pages[0].words, c2p.inverse(), w, h))
        options = inference_options()
        key = hashlib.sha256(json.dumps([hashlib.sha256(png).hexdigest(), digest, prompt_sha256(), options], sort_keys=True).encode()).hexdigest()
        hit = cache / f"{key}.json"
        stats["crops"] += 1
        if hit.exists():
            rec = json.loads(hit.read_text())
            stats["cached"] += 1
        else:
            if cached_only:
                raise ModelRequired(f"missing cached response {key}; cached-only mode never runs inference")
            t0 = time.monotonic()
            res = client.chat_image(build_prompt(lines, w, h), png, OUTPUT_SCHEMA, options)
            rec = {"content": res.content, "done_reason": res.raw.get("done_reason"), "duration_ms": int((time.monotonic() - t0) * 1000)}
            hit.write_text(json.dumps(rec))
        stats["inference_ms"] += rec.get("duration_ms", 0)
        raw, bad = _raw_items(rec["content"], c2p)
        stats["unparseable_items"] += bad
        raw_preds += raw
        rep = validate_output(rec["content"], lines, c2p, w, h)
        if rep.parse_error:
            stats["failed_crops"] += 1
            continue
        for ob in rep.observations:
            xs = [p[0] for p in ob.bbox_page]
            ys = [p[1] for p in ob.bbox_page]
            full_preds.append({"cls": ob.cls, "label": ob.label, "bbox": [min(xs), min(ys), max(xs), max(ys)],
                               "geometry": [list(p) for p in ob.geometry_page] if len(ob.geometry_page) > 1 else [],
                               "phase_refs": ob.phase_refs, "properties": ob.properties,
                               "flags": [u for u in ob.uncertainty if not u.startswith("model:")]})
    return {"raw_vlm": raw_preds, "full_pipeline": full_preds, "stats": stats}


def manual_graph() -> dict[str, Any]:
    """Hand-built graphs with known outcomes through the same verifier and repair engine."""
    rows = []
    for case in all_cases():
        got = {r.requirement_id: r.outcome.value for r in verify(case.input).requirements}
        for rid, want in case.expected.items():
            rows.append({"case": case.name, "requirement": rid, "expected": want.value, "got": got.get(rid)})
    repairs = []
    for rc in all_repair_cases():
        res = solve(rc.problem)
        repairs.append({"case": rc.name, "expected": rc.expected_status.value, "got": res.status.value})
    correct = sum(r["expected"] == r["got"] for r in rows)
    false_supported = sum(r["got"] == "SUPPORTED" and r["expected"] != "SUPPORTED" for r in rows)
    return {"status_correctness": {"correct": correct, "of": len(rows)}, "false_supported": false_supported,
            "repair_status": {"correct": sum(r["expected"] == r["got"] for r in repairs), "of": len(repairs)},
            "mismatches": [r for r in rows + repairs if r["expected"] != r["got"]]}


def _thresholds(report: dict[str, Any], manifest: dict[str, Any]) -> list[dict[str, Any]]:
    t = manifest["thresholds"]
    mg = report["manual_graph"]
    out = [
        {"check": "manual_graph status correctness", "target": t["manual_graph"]["status_correctness"],
         "value": round(mg["status_correctness"]["correct"] / mg["status_correctness"]["of"], 4)},
        {"check": "manual_graph false-supported outcomes", "target": t["manual_graph"]["false_supported"], "value": mg["false_supported"]},
    ]
    out[0]["met"] = out[0]["value"] >= out[0]["target"]
    out[1]["met"] = out[1]["value"] <= out[1]["target"]
    fp = report["baselines"].get("full_pipeline", {}).get("held_out")
    tf = t["full_pipeline_held_out"]
    if fp is None:
        out.append({"check": "full_pipeline held-out thresholds", "met": None, "value": "not run (requires live model)"})
    else:
        g = fp["groups"]
        out += [
            {"check": "full_pipeline held-out false connections", "target": tf["false_connections"], "value": fp["false_connections"],
             "met": fp["false_connections"] <= tf["false_connections"]},
            {"check": "full_pipeline held-out phase-text recall", "target": tf["phase_text_recall_min"], "value": g.get("phase_text", {}).get("recall"),
             "met": (g.get("phase_text", {}).get("recall") or 0) >= tf["phase_text_recall_min"]},
            {"check": "full_pipeline held-out note recall", "target": tf["note_recall_min"], "value": g.get("note", {}).get("recall"),
             "met": (g.get("note", {}).get("recall") or 0) >= tf["note_recall_min"]},
        ]
        for grp in ("path", "barrier"):
            for metric in ("recall", "precision"):
                key = f"{grp}_{metric}_min"
                if key in tf:
                    v = g.get(grp, {}).get(metric)
                    out.append({"check": f"full_pipeline held-out {grp} {metric}", "target": tf[key], "value": v,
                                "met": (v or 0) >= tf[key]})
        if "geometry_median_error_m_max" in tf:
            errs = [g.get(k, {}).get("median_error_m") for k in ("path", "barrier")]
            worst = max((e for e in errs if e is not None), default=None)
            out.append({"check": "full_pipeline held-out geometry median error (m, worse of path and barrier)",
                        "target": tf["geometry_median_error_m_max"], "value": worst,
                        "met": worst is not None and worst <= tf["geometry_median_error_m_max"]})
        if "phase_assignment_min" in tf:
            pa = fp["phase_assignment"]
            v = round(pa["correct"] / pa["of"], 4) if pa["of"] else None
            out.append({"check": "full_pipeline held-out phase assignment", "target": tf["phase_assignment_min"], "value": v,
                        "met": v is not None and v >= tf["phase_assignment_min"]})
    return out


def run(root: Path, *, live: bool, cached_only: bool = False) -> dict[str, Any]:
    if cached_only and not live:
        raise ValueError("--cached-only cannot be combined with --no-live")
    manifest, labels = M.load(root)
    pdfs = M.check_inputs(manifest)
    tol = Tolerances(**manifest["tolerances"])
    cfg = get_settings()
    report: dict[str, Any] = {"schema": manifest["schema"], "started_at": datetime.now(UTC).isoformat(),
                              "live_model": live and not cached_only,
                              "model_mode": "cached_only" if cached_only else "live_allowed" if live else "not_run",
                              "prompt_version": PROMPT_VERSION, "validator_version": VALIDATOR_VERSION, "baselines": {},
                              "per_sheet": {}, "real_plans": manifest["real_plans"]}
    preds: dict[str, dict[str, list[dict[str, Any]]]] = {b: {} for b in EXTRACTION_BASELINES}
    for s in manifest["sheets"]:
        preds["native_text"][s["id"]], preds["native_vector"][s["id"]] = native_predictions(pdfs[s["id"]])
    if live:
        client = OllamaClient(cfg.ollama_url, cfg.model, cfg.inference_timeout_s)
        st = client.status()
        if st.state != ModelState.READY or not st.digest:
            raise ModelRequired(f"{st.state.value}: {st.message}. Start Ollama and run `make model`, or run without --live "
                                "(the model baselines are then reported as not run).")
        report["model"] = {"name": st.model, "digest": st.digest, "server_version": st.server_version, "options": inference_options()}
        cache = cfg.path("benchmarks", "cache")
        cache.mkdir(parents=True, exist_ok=True)
        stats = {}
        for s in manifest["sheets"]:
            out = model_predictions(pdfs[s["id"]], client, st.digest, cache, cached_only=cached_only)
            preds["raw_vlm"][s["id"]] = out["raw_vlm"]
            # As stored by the app: model geometry is dropped for classes the vector strokes cover.
            vec = preds["native_vector"][s["id"]]
            covered = {p["cls"] for p in vec}
            preds["full_pipeline"][s["id"]] = [p for p in out["full_pipeline"]
                                               if not (p["cls"] in covered and p["cls"] in GEOMETRY_CLASSES)] + vec
            stats[s["id"]] = out["stats"]
        report["model_stats"] = stats
    for b, by_sheet in preds.items():
        if not by_sheet:
            report["baselines"][b] = {"status": "not run", "reason": "requires the live model (--live)"}
            continue
        scored = {sid: score_sheet(p, labels[sid], tol) for sid, p in by_sheet.items()}
        report["per_sheet"][b] = scored
        split = {s["id"]: s["split"] for s in manifest["sheets"]}
        report["baselines"][b] = {
            part: aggregate([v for sid, v in scored.items() if split[sid] == part], tol) for part in ("dev", "held_out")
        }
        if b == "native_text":
            # Native text cannot propose geometry, and its rules have no notion of place names.
            report["baselines"][b]["unsupported"] = ["path", "barrier", "place_label"]
        if b == "native_vector":
            report["baselines"][b]["unsupported"] = ["phase_text", "note", "dimension", "place_label"]
    report["manual_graph"] = manual_graph()
    report["thresholds"] = _thresholds(report, manifest)
    report["finished_at"] = datetime.now(UTC).isoformat()
    return report


def write_report(report: dict[str, Any]) -> Path:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    out = get_settings().path("benchmarks", "runs", stamp)
    out.mkdir(parents=True, exist_ok=True)
    (out / "report.json").write_text(json.dumps(report, indent=1, default=str) + "\n")
    (out / "report.md").write_text(render_markdown(report))
    return out


def render_markdown(r: dict[str, Any]) -> str:
    lines = ["# Ghost Routes benchmark", "", f"Run {r['started_at']}; live model: {'yes' if r['live_model'] else 'no'}."]
    if r.get("model_mode") == "cached_only":
        lines.append("Cached model responses only; no inference was permitted in this run.")
    if r.get("model"):
        lines.append(f"Model {r['model']['name']} digest `{r['model']['digest']}`, prompt {r['prompt_version']}, validator {r['validator_version']}.")
    lines += ["", "## Extraction (synthetic, exact labels)", "",
              "| Baseline | Split | Group | TP | FP | FN | Precision | Recall | Median error (m) |",
              "| --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    for b in EXTRACTION_BASELINES:
        base = r["baselines"][b]
        if base.get("status") == "not run":
            lines.append(f"| {b} | | not run: {base['reason']} | | | | | | |")
            continue
        for part in ("dev", "held_out"):
            for g, v in base[part]["groups"].items():
                unsup = g in base.get("unsupported", [])
                lines.append(f"| {b} | {part} | {g}{' (unsupported)' if unsup else ''} | {v['tp']} | {v['fp']} | {v['fn']} | "
                             f"{v['precision'] if v['precision'] is not None else 'n/a'} | {v['recall'] if v['recall'] is not None else 'n/a'} | "
                             f"{v['median_error_m'] if v.get('median_error_m') is not None else 'n/a'} |")
    lines += ["", "| Baseline | Split | Phase assignment | Dimension values | False connections | Flagged | Correction burden |",
              "| --- | --- | --- | --- | --- | --- | --- |"]
    for b in EXTRACTION_BASELINES:
        base = r["baselines"][b]
        if base.get("status") == "not run":
            continue
        for part in ("dev", "held_out"):
            a = base[part]
            lines.append(f"| {b} | {part} | {a['phase_assignment']['correct']}/{a['phase_assignment']['of']} | "
                         f"{a['dimension_values']['correct']}/{a['dimension_values']['of']} | {a['false_connections']} | "
                         f"{a['flagged_for_review']['count']}/{a['flagged_for_review']['of']} | {a['correction_burden']} |")
    mg = r["manual_graph"]
    lines += ["", "## Verifier on hand-built graphs", "",
              f"Status correctness {mg['status_correctness']['correct']}/{mg['status_correctness']['of']}, "
              f"false-supported outcomes {mg['false_supported']}, repair status {mg['repair_status']['correct']}/{mg['repair_status']['of']}."]
    if mg["mismatches"]:
        lines += ["", "Mismatches:", *[f"- {m}" for m in mg["mismatches"]]]
    lines += ["", "## Declared thresholds", "", "| Check | Target | Value | Met |", "| --- | --- | --- | --- |"]
    for t in r["thresholds"]:
        lines.append(f"| {t['check']} | {t.get('target', '')} | {t['value']} | {'yes' if t['met'] else 'no' if t['met'] is False else 'not run'} |")
    lines += ["", "## Real plans", ""]
    for p in r["real_plans"]:
        lines.append(f"- {p['id']} (`{p['sha256'][:12]}`): not scored, {p['reason']}.")
    return "\n".join(lines) + "\n"
