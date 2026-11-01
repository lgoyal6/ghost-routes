"""Match predictions to independent ground-truth labels and compute metrics.

Predictions and labels share one shape (page points, top-left origin):
``{"cls", "label", "bbox": [x0, y0, x1, y1], "geometry": [[x, y], ...], "phase_refs": [...],
"properties": {...}, "flags": [...]}``. Nothing here looks at how a prediction was made.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field
from typing import Any

from shapely.geometry import LineString

# Classes that answer the same reviewer question are scored together.
GROUPS = {
    "legend_entry": "phase_text", "phase_label": "phase_text",
    "note": "note", "dimension": "dimension", "place_label": "place_label",
    "path": "path", "barrier": "barrier", "crossing": "path",
}
GEOMETRY_GROUPS = {"path", "barrier"}


@dataclass(frozen=True)
class Tolerances:
    text_center_pt: float = 24.0  # labels' text boxes are approximate (estimated glyph widths)
    geometry_hausdorff_pt: float = 15.0
    node_snap_pt: float = 10.0
    metres_per_pt: float = 1 / 6.0  # synthetic sheets draw 1 m as 6 pt


@dataclass
class GroupScore:
    tp: int = 0
    fp: int = 0
    fn: int = 0
    loc_errors_pt: list[float] = field(default_factory=list)

    def summary(self, m_per_pt: float) -> dict[str, Any]:
        p = self.tp / (self.tp + self.fp) if self.tp + self.fp else None
        r = self.tp / (self.tp + self.fn) if self.tp + self.fn else None
        med = statistics.median(self.loc_errors_pt) if self.loc_errors_pt else None
        return {"tp": self.tp, "fp": self.fp, "fn": self.fn, "precision": _r(p), "recall": _r(r),
                "median_error_m": _r(med * m_per_pt) if med is not None else None}


def _r(x: float | None) -> float | None:
    return None if x is None else round(x, 4)


def _center(b: list[float]) -> tuple[float, float]:
    return ((b[0] + b[2]) / 2, (b[1] + b[3]) / 2)


def _line(item: dict[str, Any]) -> LineString | None:
    g = item.get("geometry") or []
    return LineString(g) if len(g) >= 2 else None


def _distance(pred: dict[str, Any], label: dict[str, Any], group: str, tol: Tolerances) -> float | None:
    """Matching distance in points, or None when the pair cannot match."""
    if group in GEOMETRY_GROUPS:
        a, b = _line(pred), _line(label)
        if a is None or b is None:
            return None
        d = a.hausdorff_distance(b)
        return d if d <= tol.geometry_hausdorff_pt else None
    (px, py), (lx, ly) = _center(pred["bbox"]), _center(label["bbox"])
    d = math.hypot(px - lx, py - ly)
    return d if d <= tol.text_center_pt else None


def match(preds: list[dict[str, Any]], labels: list[dict[str, Any]], tol: Tolerances) -> list[tuple[int, int, float]]:
    """Greedy one-to-one matching within a class group, closest pairs first."""
    pairs = []
    for i, p in enumerate(preds):
        gp = GROUPS.get(p["cls"])
        for j, lab in enumerate(labels):
            if gp is None or gp != GROUPS.get(lab["cls"]):
                continue
            d = _distance(p, lab, gp, tol)
            if d is not None:
                pairs.append((d, i, j))
    used_p: set[int] = set()
    used_l: set[int] = set()
    out = []
    for d, i, j in sorted(pairs):
        if i in used_p or j in used_l:
            continue
        used_p.add(i)
        used_l.add(j)
        out.append((i, j, d))
    return out


def false_connections(preds: list[dict[str, Any]], labels: list[dict[str, Any]], tol: Tolerances) -> int:
    """Predicted paths whose two ends land on different true nodes that no true path joins."""
    truth = [_line(lab) for lab in labels if GROUPS.get(lab["cls"]) == "path"]
    nodes: list[tuple[float, float]] = []
    for ln in truth:
        if ln is None:
            continue
        for pt in (ln.coords[0], ln.coords[-1]):
            if all(math.dist(pt, n) > 1e-6 for n in nodes):
                nodes.append((pt[0], pt[1]))

    def node_of(pt: tuple[float, ...]) -> int | None:
        best = min(range(len(nodes)), key=lambda k: math.dist(pt, nodes[k]), default=None)
        return best if best is not None and math.dist(pt, nodes[best]) <= tol.node_snap_pt else None

    adjacent = set()
    for ln in truth:
        if ln is not None:
            a, b = node_of(ln.coords[0]), node_of(ln.coords[-1])
            if a is not None and b is not None:
                adjacent.add(frozenset((a, b)))
    count = 0
    for p in preds:
        ln = _line(p)
        if GROUPS.get(p["cls"]) != "path" or ln is None:
            continue
        a, b = node_of(ln.coords[0]), node_of(ln.coords[-1])
        if a is not None and b is not None and a != b and frozenset((a, b)) not in adjacent:
            count += 1
    return count


def _on_context(pred: dict[str, Any], context: list[dict[str, Any]], pad: float = 6.0) -> bool:
    cx, cy = _center(pred["bbox"])
    return any(b[0] - pad <= cx <= b[2] + pad and b[1] - pad <= cy <= b[3] + pad for b in (c["bbox"] for c in context))


def score_sheet(preds: list[dict[str, Any]], all_labels: list[dict[str, Any]], tol: Tolerances) -> dict[str, Any]:
    # "context" labels mark real text outside every scored class (title block, headers, scale):
    # predictions there are neither credited nor counted as false positives.
    labels = [lab for lab in all_labels if lab["cls"] in GROUPS]
    context = [lab for lab in all_labels if lab["cls"] == "context"]
    pairs = match(preds, labels, tol)
    groups: dict[str, GroupScore] = {}
    for lab in labels:
        groups.setdefault(GROUPS[lab["cls"]], GroupScore())
    matched_p = {i for i, _, _ in pairs}
    matched_l = {j for _, j, _ in pairs}
    for i, _j, d in pairs:
        g = groups.setdefault(GROUPS[preds[i]["cls"]], GroupScore())
        g.tp += 1
        g.loc_errors_pt.append(d)
    unknown_cls = ignored = 0
    for i, p in enumerate(preds):
        if i in matched_p:
            continue
        if _on_context(p, context):
            ignored += 1
            continue
        if p["cls"] not in GROUPS:
            unknown_cls += 1
            continue
        groups.setdefault(GROUPS[p["cls"]], GroupScore()).fp += 1
    for j, lab in enumerate(labels):
        if j not in matched_l:
            groups[GROUPS[lab["cls"]]].fn += 1

    phase_total = phase_ok = 0
    dim_total = dim_ok = 0
    wrong_phase = 0
    for i, j, _ in pairs:
        p, lab = preds[i], labels[j]
        if lab.get("phase_refs"):
            phase_total += 1
            if sorted(p.get("phase_refs") or []) == sorted(lab["phase_refs"]):
                phase_ok += 1
            else:
                wrong_phase += 1
        if lab["cls"] == "dimension":
            dim_total += 1
            v = (p.get("properties") or {}).get("value_m")
            if v is not None and abs(v - lab["properties"]["value_m"]) <= 1e-3:
                dim_ok += 1
    fp = sum(g.fp for g in groups.values()) + unknown_cls
    fn = sum(g.fn for g in groups.values())
    flagged = sum(1 for p in preds if p.get("flags"))
    return {
        "predictions": len(preds), "labels": len(labels),
        "groups": {k: v.summary(tol.metres_per_pt) for k, v in sorted(groups.items())},
        "unscorable_class_predictions": unknown_cls,
        "ignored_on_context_text": ignored,
        "phase_assignment": {"correct": phase_ok, "of": phase_total},
        "dimension_values": {"correct": dim_ok, "of": dim_total},
        "false_connections": false_connections(preds, labels, tol),
        "flagged_for_review": {"count": flagged, "of": len(preds)},
        # Review actions to reach the labels: reject false positives, add misses, fix wrong phases.
        "correction_burden": {"reject": fp, "add": fn, "edit_phase": wrong_phase, "total": fp + fn + wrong_phase},
    }


def aggregate(sheets: list[dict[str, Any]], tol: Tolerances) -> dict[str, Any]:
    """Sum counts across sheets; rates are recomputed from the sums, never averaged."""
    groups: dict[str, dict[str, int]] = {}
    for sh in sheets:
        for k, g in sh["groups"].items():
            acc = groups.setdefault(k, {"tp": 0, "fp": 0, "fn": 0})
            for f in ("tp", "fp", "fn"):
                acc[f] += g[f]
    out_groups = {}
    for k, g in sorted(groups.items()):
        p = g["tp"] / (g["tp"] + g["fp"]) if g["tp"] + g["fp"] else None
        r = g["tp"] / (g["tp"] + g["fn"]) if g["tp"] + g["fn"] else None
        out_groups[k] = {**g, "precision": _r(p), "recall": _r(r)}

    def total(path: tuple[str, str]) -> dict[str, int]:
        return {f: sum(sh[path[0]][f] for sh in sheets) for f in (path[1], "of")}

    return {
        "sheets": len(sheets),
        "groups": out_groups,
        "phase_assignment": total(("phase_assignment", "correct")),
        "dimension_values": total(("dimension_values", "correct")),
        "false_connections": sum(sh["false_connections"] for sh in sheets),
        "correction_burden": sum(sh["correction_burden"]["total"] for sh in sheets),
        "flagged_for_review": total(("flagged_for_review", "count")),
    }
