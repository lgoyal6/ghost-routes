"""Output validation rules for model proposals (no model involved)."""

from __future__ import annotations

import json

from ghost_routes.documents.transforms import crop_to_page
from ghost_routes.inference.extract import validate_output

W = H = 1008
C2P = crop_to_page(0, 0, 144)
LINES = [
    {"id": "L1", "text": "12. PHASE 1 LIMITS TO PROVIDE PEDESTRIAN ACCESS", "word_ids": [1, 2, 3], "cx0": 291, "cy0": 315, "cx1": 943, "cy1": 329},
    {"id": "L2", "text": "X 1\"", "word_ids": [4], "cx0": 100, "cy0": 600, "cx1": 130, "cy1": 612},
    {"id": "L3", "text": "MIN 5' PATH", "word_ids": [5, 6, 7], "cx0": 500, "cy0": 700, "cx1": 600, "cy1": 712},
]


def _one(**item):  # type: ignore[no-untyped-def]
    base = {"class": "note", "label": "", "bbox": [0, 0, 10, 10], "line_ids": [], "phase_refs": [], "uncertainty": ""}
    rep = validate_output(json.dumps({"observations": [{**base, **item}]}), LINES, C2P, W, H)
    assert rep.parse_error is None and len(rep.observations) == 1
    return rep.observations[0]


def test_cited_lines_are_model_cited() -> None:
    ob = _one(label="Phase 1 access", bbox=[291, 315, 943, 329], line_ids=["L1"], phase_refs=["PHASE 1"])
    assert ob.properties["anchor_source"] == "model_cited"
    assert ob.phase_refs == ["PHASE 1"] and ob.word_ids == [1, 2, 3]


def test_uncited_text_is_anchored_by_bbox_and_labeled() -> None:
    ob = _one(label="Pedestrian access from Phase 1", bbox=[291, 315, 943, 329], phase_refs=["PHASE 1"])
    assert ob.properties["anchor_source"] == "inferred_from_bbox"
    assert "anchor_inferred_from_bbox" in ob.uncertainty
    assert ob.phase_refs == ["PHASE 1"]


def test_phase_ref_not_in_anchor_is_rejected() -> None:
    ob = _one(label="access", bbox=[291, 315, 943, 329], phase_refs=["PHASE 2"])
    assert ob.phase_refs == [] and "phase_ref_unsupported:PHASE 2" in ob.uncertainty


def test_geometry_classes_never_get_text_anchors() -> None:
    ob = _one(**{"class": "barrier"}, label="Phase 1 fencing", bbox=[291, 315, 943, 329], phase_refs=["PHASE 1"])
    assert ob.properties["anchor_source"] == "none" and ob.supporting_text == ""
    assert ob.phase_refs == []


def test_label_anchor_disagreement_is_flagged() -> None:
    ob = _one(label="Contractor to perform work efficiently", bbox=[291, 315, 943, 329])
    assert "label_differs_from_anchor_text" in ob.uncertainty


def test_dimension_value_must_match_label() -> None:
    bad = _one(**{"class": "dimension"}, label="5'", bbox=[95, 595, 135, 615])
    assert "value_m" not in bad.properties and "dimension_label_disagrees_with_anchor" in bad.uncertainty
    good = _one(**{"class": "dimension"}, label="5'", bbox=[495, 695, 605, 715])
    assert good.properties["value_m"] == round(5 * 0.3048, 6)


def test_bbox_over_many_lines_is_not_anchored() -> None:
    many = [{"id": f"L{i}", "text": f"LINE {i}", "word_ids": [i], "cx0": 10, "cy0": 20 * i, "cx1": 200, "cy1": 20 * i + 10}
            for i in range(1, 10)]
    rep = validate_output(json.dumps({"observations": [{"class": "note", "label": "x", "bbox": [0, 0, 300, 300],
                                                         "line_ids": [], "phase_refs": [], "uncertainty": ""}]}),
                          many, C2P, W, H)
    ob = rep.observations[0]
    assert ob.properties["anchor_source"] == "none"
    assert any(u.startswith("bbox_covers_") for u in ob.uncertainty)
