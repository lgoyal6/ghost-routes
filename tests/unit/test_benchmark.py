"""Benchmark scoring rules and the fixed-manifest guarantee (no model involved)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ghost_routes.benchmark import manifest as M
from ghost_routes.benchmark.evaluate import Tolerances, aggregate, false_connections, score_sheet

TOL = Tolerances()
ROOT = Path(__file__).resolve().parents[2] / "benchmarks"


def item(cls: str, bbox: list[float], geometry: list[list[float]] | None = None, phases: list[str] | None = None,
         **props: float) -> dict:
    return {"cls": cls, "label": cls, "bbox": bbox, "geometry": geometry or [], "phase_refs": phases or [],
            "properties": props, "flags": []}


def test_text_matches_by_center_and_merges_phase_classes() -> None:
    labels = [item("legend_entry", [100, 100, 160, 110], phases=["PHASE 1"])]
    preds = [item("phase_label", [105, 101, 158, 111], phases=["PHASE 1"])]
    s = score_sheet(preds, labels, TOL)
    assert s["groups"]["phase_text"]["tp"] == 1 and s["phase_assignment"] == {"correct": 1, "of": 1}


def test_far_text_is_a_miss_and_a_false_positive() -> None:
    s = score_sheet([item("note", [400, 400, 460, 410])], [item("note", [100, 100, 160, 110])], TOL)
    assert s["groups"]["note"] == {**s["groups"]["note"], "tp": 0, "fp": 1, "fn": 1}
    assert s["correction_burden"]["total"] == 2


def test_wrong_phase_counts_as_an_edit() -> None:
    s = score_sheet([item("note", [100, 100, 160, 110], phases=["PHASE 2"])], [item("note", [100, 100, 160, 110], phases=["PHASE 1"])], TOL)
    assert s["phase_assignment"] == {"correct": 0, "of": 1} and s["correction_burden"]["edit_phase"] == 1


def test_matching_is_one_to_one() -> None:
    labels = [item("note", [100, 100, 160, 110])]
    s = score_sheet([item("note", [100, 100, 160, 110]), item("note", [101, 100, 161, 110])], labels, TOL)
    assert (s["groups"]["note"]["tp"], s["groups"]["note"]["fp"]) == (1, 1)


def test_geometry_uses_hausdorff_distance() -> None:
    labels = [item("path", [0, 0, 100, 0], [[0, 0], [100, 0]])]
    near = score_sheet([item("path", [0, 5, 100, 5], [[0, 5], [100, 5]])], labels, TOL)
    far = score_sheet([item("path", [0, 40, 100, 40], [[0, 40], [100, 40]])], labels, TOL)
    assert near["groups"]["path"]["tp"] == 1 and far["groups"]["path"]["tp"] == 0
    assert near["groups"]["path"]["median_error_m"] == pytest.approx(5 / 6, abs=1e-3)


def test_dimension_value_must_be_right() -> None:
    labels = [item("dimension", [0, 0, 50, 10], value_m=1.8288)]
    assert score_sheet([item("dimension", [0, 0, 50, 10], value_m=1.8288)], labels, TOL)["dimension_values"]["correct"] == 1
    assert score_sheet([item("dimension", [0, 0, 50, 10], value_m=0.0254)], labels, TOL)["dimension_values"]["correct"] == 0


def test_false_connection_joins_nodes_no_true_path_joins() -> None:
    # True paths A-B and B-C; a predicted A-C shortcut is a false connection, a predicted A-B is not.
    labels = [item("path", [0, 0, 100, 0], [[0, 0], [100, 0]]), item("path", [100, 0, 100, 100], [[100, 0], [100, 100]])]
    assert false_connections([item("path", [0, 0, 100, 100], [[0, 0], [100, 100]])], labels, TOL) == 1
    assert false_connections([item("path", [0, 0, 100, 0], [[0, 0], [100, 0]])], labels, TOL) == 0


def test_context_text_is_neither_credited_nor_penalized() -> None:
    labels = [item("context", [500, 500, 600, 510])]
    s = score_sheet([item("note", [510, 500, 590, 510])], labels, TOL)
    assert s["ignored_on_context_text"] == 1 and s["correction_burden"]["reject"] == 0


def test_aggregate_recomputes_rates_from_counts() -> None:
    a = score_sheet([item("note", [0, 0, 10, 10])], [item("note", [0, 0, 10, 10])], TOL)
    b = score_sheet([], [item("note", [0, 0, 10, 10]), item("note", [50, 50, 60, 60]), item("note", [90, 90, 99, 99])], TOL)
    agg = aggregate([a, b], TOL)
    assert agg["groups"]["note"]["recall"] == 0.25  # 1 of 4, not the mean of 1.0 and 0.0


def test_tracked_manifest_and_labels_match_the_generator() -> None:
    manifest, labels = M.load(ROOT)
    fresh, fresh_labels, _ = M.build()
    assert manifest == json.loads(json.dumps(fresh)), "benchmarks/manifest.json is out of date: run `ghost-routes benchmark --write-manifest`"
    assert labels == json.loads(json.dumps(fresh_labels))
    M.check_inputs(manifest)


def test_changed_inputs_are_refused() -> None:
    manifest, _ = M.load(ROOT)
    tampered = {**manifest, "sheets": [{**manifest["sheets"][0], "pdf_sha256": "0" * 64}, *manifest["sheets"][1:]]}
    with pytest.raises(ValueError, match="changed"):
        M.check_inputs(tampered)


def test_raw_reading_takes_model_output_at_face_value() -> None:
    from ghost_routes.benchmark.run import _raw_items
    from ghost_routes.documents.transforms import crop_to_page

    c2p = crop_to_page(200, 100, 144)  # crop origin at render px (200, 100); 2 px per pt
    content = json.dumps({"observations": [
        {"class": "dimension", "label": "5'", "bbox": [0, 0, 20, 10], "line_ids": [], "phase_refs": ["phase 2"], "uncertainty": ""},
        {"class": "path", "label": "walk", "bbox": [0, 0, 40, 40], "points": [[0, 0], [40, 40]], "line_ids": [], "phase_refs": [], "uncertainty": ""},
        {"class": "note", "label": "no box", "line_ids": [], "phase_refs": [], "uncertainty": ""},
    ]})
    items, bad = _raw_items(content, c2p)
    assert bad == 1 and len(items) == 2
    dim, path = items
    assert dim["bbox"] == pytest.approx([100, 50, 110, 55])
    assert dim["phase_refs"] == ["PHASE 2"]  # unsupported by any text, kept anyway: that is the raw baseline
    assert dim["properties"]["value_m"] == pytest.approx(1.524)
    assert path["geometry"] == [pytest.approx([100, 50]), pytest.approx([120, 70])]
    assert _raw_items("not json", c2p) == ([], 1)
