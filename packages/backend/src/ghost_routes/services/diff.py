"""Revision comparison: network, project (geometry, notes, constraints, phases) and analysis outcomes."""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from ghost_routes.domain.canonical import canonical_json
from ghost_routes.persistence import repo
from ghost_routes.persistence import schema as S


def _by_id(items: Any, key: str = "id") -> dict[str, Any]:
    return {getattr(i, key): i for i in items}


def _summary(changes: list[dict[str, Any]]) -> dict[str, int]:
    out: dict[str, int] = {}
    for c in changes:
        out[c["change"]] = out.get(c["change"], 0) + 1
    return out


def network_diff(s: Session, a: str, b: str) -> dict[str, Any]:
    na, nb = repo.load_network(s, a), repo.load_network(s, b)
    changes: list[dict[str, Any]] = []
    for kind, xa, xb in (("node", _by_id(na.nodes), _by_id(nb.nodes)), ("edge", _by_id(na.edges), _by_id(nb.edges))):
        for i in sorted(set(xa) | set(xb)):
            if i not in xb:
                changes.append({"entity": kind, "id": i, "change": "removed"})
            elif i not in xa:
                changes.append({"entity": kind, "id": i, "change": "added"})
            elif canonical_json(xa[i]) != canonical_json(xb[i]):
                fields = sorted(k for k in xa[i].model_fields if canonical_json(getattr(xa[i], k)) != canonical_json(getattr(xb[i], k)))
                detail: dict[str, Any] = {"entity": kind, "id": i, "change": "modified", "fields": fields}
                if kind == "edge" and "attributes" in fields:
                    ea, eb = xa[i].attributes, xb[i].attributes
                    detail["attributes"] = {
                        k: {"from": ea[k].model_dump(mode="json") if k in ea else None,
                            "to": eb[k].model_dump(mode="json") if k in eb else None}
                        for k in sorted(set(ea) | set(eb)) if canonical_json(ea.get(k)) != canonical_json(eb.get(k))
                    }
                if "geometry" in fields:
                    detail["geometry"] = {"from": xa[i].geometry, "to": xb[i].geometry}
                changes.append(detail)
    if canonical_json([c.model_copy(update={"id": ""}) for c in na.coverage]) != canonical_json([c.model_copy(update={"id": ""}) for c in nb.coverage]):
        changes.append({"entity": "coverage", "id": "network", "change": "modified"})
    return {"kind": "network", "from_id": a, "to_id": b, "changes": changes, "summary": _summary(changes)}


def project_diff(s: Session, a: str, b: str) -> dict[str, Any]:
    pa, pb = repo.load_project(s, a), repo.load_project(s, b)
    if pa.project_id != pb.project_id:
        raise ValueError("can only diff revisions of one project; cross-project feature identity needs explicit review")
    changes: list[dict[str, Any]] = []
    for kind, attr in (("phase", "phases"), ("restriction", "restrictions"), ("temporary_connection", "temporary_connections"),
                       ("coverage", "coverage")):
        xa, xb = _by_id(getattr(pa, attr)), _by_id(getattr(pb, attr))
        for i in sorted(set(xa) | set(xb)):
            if i not in xb:
                changes.append({"entity": kind, "id": i, "change": "removed"})
            elif i not in xa:
                changes.append({"entity": kind, "id": i, "change": "added"})
            elif canonical_json(xa[i]) != canonical_json(xb[i]):
                fields = sorted(k for k in xa[i].model_fields if canonical_json(getattr(xa[i], k)) != canonical_json(getattr(xb[i], k)))
                changes.append({"entity": kind, "id": i, "change": "modified", "fields": fields,
                                "from": {f: _json(getattr(xa[i], f)) for f in fields},
                                "to": {f: _json(getattr(xb[i], f)) for f in fields}})
    if canonical_json(pa.extent) != canonical_json(pb.extent):
        changes.append({"entity": "extent", "id": pa.project_id, "change": "modified"})
    ra, rb = s.get(S.ProjectRevision, a), s.get(S.ProjectRevision, b)
    if ra and rb and ra.plan_revision_id != rb.plan_revision_id:
        changes.append({"entity": "source", "id": pa.project_id, "change": "modified",
                        "from": ra.plan_revision_id, "to": rb.plan_revision_id})
    return {"kind": "project", "from_id": a, "to_id": b, "changes": changes, "summary": _summary(changes)}


def _json(v: Any) -> Any:
    import json

    return json.loads(canonical_json(v))


def analysis_diff(s: Session, a: str, b: str) -> dict[str, Any]:
    ra, rb = s.get(S.AnalysisRun, a), s.get(S.AnalysisRun, b)
    if ra is None or rb is None or ra.result is None or rb.result is None:
        raise repo.NotFound("both analysis runs must exist and have results")
    changes: list[dict[str, Any]] = []

    def index(res: dict[str, Any]) -> dict[tuple[str, str, str, str], str]:
        out = {}
        for r in res["requirements"]:
            for st in r["states"]:
                for p in st["pairs"]:
                    out[(r["requirement_id"], st["label"], p["origin"], p["destination"])] = p["outcome"]
        return out

    ia, ib = index(ra.result), index(rb.result)
    for k in sorted(set(ia) | set(ib)):
        if ia.get(k) != ib.get(k):
            changes.append({"entity": "pair", "id": "|".join(k), "requirement": k[0], "state": k[1], "origin": k[2],
                            "destination": k[3], "change": "outcome", "from": ia.get(k), "to": ib.get(k)})
    ta = {r["requirement_id"]: r["outcome"] for r in ra.result["requirements"]}
    tb = {r["requirement_id"]: r["outcome"] for r in rb.result["requirements"]}
    for k in sorted(set(ta) | set(tb)):
        if ta.get(k) != tb.get(k):
            changes.append({"entity": "requirement", "id": k, "change": "requirement_outcome", "from": ta.get(k), "to": tb.get(k)})
    return {"kind": "analysis", "from_id": a, "to_id": b, "changes": changes, "summary": _summary(changes)}
