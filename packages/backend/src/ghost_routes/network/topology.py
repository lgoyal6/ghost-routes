"""Build explicit pedestrian topology and restrictions from reviewed observations.

Geometry is separate from topology: only endpoints become nodes, and an
endpoint joins an existing node only within the declared snap tolerance and
only if the snap segment does not cross a barrier. Interior line crossings
never create junctions.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from shapely.geometry import LineString

from ghost_routes.documents.transforms import Affine
from ghost_routes.domain.model import (
    AttributeValue,
    Edge,
    EdgeKind,
    EvidenceState,
    Node,
    NodeKind,
    Point,
    Restriction,
    RestrictionKind,
)

STATE_FOR_REVIEW = {"accepted": EvidenceState.ACCEPTED, "edited": EvidenceState.ACCEPTED,
                    "pending": EvidenceState.UNRESOLVED, "rejected": EvidenceState.REJECTED}


@dataclass(frozen=True)
class ObsInput:
    id: str
    cls: str
    review_state: str
    geometry: tuple[Point, ...]  # page points
    phase_refs: tuple[str, ...] = ()
    label: str = ""
    attributes: dict[str, Any] = field(default_factory=dict)  # name -> {"value", "unit"} set by review


@dataclass
class BuildReport:
    nodes: list[Node]
    edges: list[Edge]
    restrictions: list[Restriction]
    entrance_nodes: dict[str, str]
    snaps: list[dict[str, Any]]
    refused_snaps: list[dict[str, Any]]
    skipped: list[dict[str, Any]]


def build(
    observations: list[ObsInput],
    *,
    transform: Affine | None,
    snap_tolerance: float,
    phase_map: dict[str, str],
    barrier_buffer: float,
) -> BuildReport:
    """``transform`` maps page points to the target frame (None keeps page points).

    ``snap_tolerance`` and ``barrier_buffer`` are in target-frame units.
    ``phase_map`` maps normalized phase labels ("PHASE 1") to phase ids.
    """
    tf = transform.apply if transform else (lambda p: p)
    metric = transform is not None
    nodes: list[Node] = []
    edges: list[Edge] = []
    snaps: list[dict[str, Any]] = []
    refused: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []

    barriers = [o for o in observations if o.cls == "barrier" and o.review_state != "rejected" and len(o.geometry) >= 2]
    barrier_lines = [LineString([tf(p) for p in b.geometry]) for b in barriers]

    def crosses_barrier(a: Point, b: Point) -> bool:
        if a == b:
            return False
        seg = LineString([a, b])
        return any(seg.intersects(bl) for bl in barrier_lines)

    def node_for(p: Point, obs_id: str, kind: NodeKind = NodeKind.JUNCTION) -> str:
        best = None
        for n in nodes:
            d = math.dist((n.x, n.y), p)
            if d <= snap_tolerance and (best is None or d < best[0]):
                best = (d, n)
        if best is not None:
            if crosses_barrier((best[1].x, best[1].y), p):
                refused.append({"observation": obs_id, "node": best[1].id, "distance": round(best[0], 4),
                                "reason": "snap would cross a barrier"})
            else:
                snaps.append({"observation": obs_id, "node": best[1].id, "distance": round(best[0], 4)})
                return best[1].id
        nid = f"n{len(nodes) + 1}"
        nodes.append(Node(id=nid, x=p[0], y=p[1], kind=kind))
        return nid

    lines = [o for o in observations if o.cls in ("path", "crossing") and o.review_state != "rejected"]
    for o in sorted(lines, key=lambda o: o.id):
        if len(o.geometry) < 2:
            skipped.append({"observation": o.id, "reason": "no line geometry"})
            continue
        pts = tuple(tf(p) for p in o.geometry)
        u = node_for(pts[0], o.id)
        v = node_for(pts[-1], o.id)
        if u == v:
            skipped.append({"observation": o.id, "reason": "both endpoints snap to one node"})
            continue
        state = STATE_FOR_REVIEW.get(o.review_state, EvidenceState.UNRESOLVED)
        attrs = {}
        for name, spec in sorted(o.attributes.items()):
            attrs[name] = AttributeValue(value=spec.get("value"), unit=spec.get("unit"),
                                         state=EvidenceState.ACCEPTED if state == EvidenceState.ACCEPTED else EvidenceState.UNRESOLVED,
                                         evidence=(o.id,))
        edges.append(Edge(
            id=f"e-{o.id}", u=u, v=v, kind=EdgeKind.CROSSING if o.cls == "crossing" else EdgeKind.PATH,
            geometry=pts, length_m=LineString(pts).length if metric else None, existence=state,
            existence_evidence=(o.id,), attributes=attrs, label=o.label[:120],
        ))

    entrances: dict[str, str] = {}
    for o in sorted((o for o in observations if o.cls == "entrance" and o.review_state != "rejected"), key=lambda o: o.id):
        p = tf(o.geometry[0])
        nid = node_for(p, o.id, NodeKind.ENTRANCE)
        n = next(x for x in nodes if x.id == nid)
        if n.kind != NodeKind.ENTRANCE or not n.label:
            nodes[nodes.index(n)] = n.model_copy(update={"kind": NodeKind.ENTRANCE, "label": o.label[:120]})
        entrances[o.id] = nid

    restrictions: list[Restriction] = []
    for b, line in zip(barriers, barrier_lines, strict=True):
        phases = sorted({phase_map[p] for p in b.phase_refs if p in phase_map})
        if not phases:
            skipped.append({"observation": b.id, "reason": f"barrier has no mapped phase ({list(b.phase_refs)})"})
            continue
        poly = line.buffer(barrier_buffer, cap_style="flat")
        coords = tuple((float(x), float(y)) for x, y in list(poly.exterior.coords)[:-1])
        restrictions.append(Restriction(
            id=f"r-{b.id}", phase_ids=tuple(phases), kind=RestrictionKind.CLOSURE, targets=(), polygon=coords,
            state=STATE_FOR_REVIEW.get(b.review_state, EvidenceState.UNRESOLVED), evidence=(b.id,),
            label=b.label[:120] or "barrier",
        ))
    return BuildReport(nodes, edges, restrictions, entrances, snaps, refused, skipped)
