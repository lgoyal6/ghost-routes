"""Classify every edge of a compiled state for one mobility profile.

Pure: no persistence, model calls or network access.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import cached_property

from shapely.geometry import LineString, Point, Polygon
from shapely.ops import unary_union
from shapely.prepared import prep

from ghost_routes.domain.model import (
    AttributeValue,
    Comparator,
    Constraint,
    Edge,
    EvidenceState,
    MobilityProfile,
    NetworkSnapshot,
    ProjectSnapshot,
    RestrictionKind,
    Scalar,
)
from ghost_routes.domain.results import (
    BlockKind,
    BlockReason,
    ConstraintCheck,
    CoverageGap,
    EdgeAssessment,
    EdgeClass,
    Fact,
    FactKind,
    GapKind,
)
from ghost_routes.domain.units import UnitError, normalize
from ghost_routes.scenarios.compiler import ActiveRestriction, CompiledState


class InvalidModel(ValueError):
    """Inputs are structurally unusable (e.g. incompatible units)."""


def edge_fact_id(edge_id: str) -> str:
    return f"edge:{edge_id}:existence"


def attr_fact_id(edge_id: str, attr: str) -> str:
    return f"edge:{edge_id}:attr:{attr}"


def restriction_fact_id(rid: str) -> str:
    return f"restriction:{rid}"


def target_fact_id(rid: str, edge_id: str) -> str:
    return f"restriction:{rid}:target:{edge_id}"


def tempconn_fact_id(cid: str) -> str:
    return f"tempconn:{cid}"


def compare(value: Scalar, comparator: Comparator, threshold: Scalar) -> bool:
    if comparator == Comparator.EQ:
        return value == threshold
    if comparator == Comparator.NE:
        return value != threshold
    if isinstance(value, bool) or isinstance(threshold, bool):
        raise InvalidModel("ordering comparators need numeric values")
    if not isinstance(value, (int, float)) or not isinstance(threshold, (int, float)):
        raise InvalidModel("ordering comparators need numeric values")
    if comparator == Comparator.GE:
        return value >= threshold
    return value <= threshold


@dataclass(frozen=True)
class GeometryIndex:
    """Prepared coverage geometry for a network and the selected projects."""

    network: NetworkSnapshot
    projects: tuple[ProjectSnapshot, ...]

    @cached_property
    def network_cover(self):  # type: ignore[no-untyped-def]
        polys = [Polygon(c.polygon) for c in self.network.coverage]
        return prep(unary_union(polys)) if polys else None

    @cached_property
    def project_cover(self) -> dict[str, object]:
        out: dict[str, object] = {}
        for p in self.projects:
            polys = [Polygon(c.polygon) for c in p.coverage]
            out[p.project_id] = unary_union(polys) if polys else None
        return out

    @cached_property
    def project_extent(self) -> dict[str, Polygon | None]:
        return {p.project_id: (Polygon(p.extent) if p.extent else None) for p in self.projects}

    def line(self, edge: Edge) -> LineString:
        return LineString(edge.geometry)

    def node_covered(self, x: float, y: float) -> bool:
        return self.network_cover is not None and self.network_cover.covers(Point(x, y))

    def edge_network_covered(self, edge: Edge) -> bool:
        return self.network_cover is not None and self.network_cover.covers(self.line(edge))

    def point_project_gap(self, project_id: str, x: float, y: float) -> bool:
        """True when the point is inside the project's extent but not reviewed."""
        ext = self.project_extent.get(project_id)
        cover = self.project_cover.get(project_id)
        pt = Point(x, y)
        if ext is not None and not ext.covers(pt):  # type: ignore[attr-defined]
            return False
        return cover is None or not cover.covers(pt)  # type: ignore[attr-defined]

    def edge_project_gap(self, project_id: str, edge: Edge) -> bool:
        """True when part of the edge inside the project's extent is unreviewed.

        A project without a declared extent is treated as potentially acting
        anywhere, so every edge must be inside its reviewed coverage.
        """
        line = self.line(edge)
        ext = self.project_extent.get(project_id)
        cover = self.project_cover.get(project_id)
        relevant = line if ext is None else line.intersection(ext)  # type: ignore[attr-defined]
        if relevant.is_empty or relevant.length == 0:
            return False
        if cover is None:
            return True
        return not cover.buffer(1e-9).covers(relevant)  # type: ignore[attr-defined]


@dataclass
class _Acc:
    facts: list[Fact]
    gaps: list[CoverageGap]
    blocks: list[BlockReason]
    checks: list[ConstraintCheck]
    soft_penalty: float = 0.0


def _resolved_bool(assumptions: dict[str, Scalar | None], fid: str) -> bool | None:
    if fid in assumptions:
        v = assumptions[fid]
        return bool(v) if v is not None else None
    return None


def effective_attributes(
    edge: Edge,
    restrictions: list[tuple[ActiveRestriction, bool]],
) -> dict[str, AttributeValue]:
    """Apply attribute overrides. ``definite=False`` overrides widen candidates."""
    attrs = dict(edge.attributes)
    for ar, definite in restrictions:
        r = ar.restriction
        if r.kind != RestrictionKind.ATTRIBUTE_OVERRIDE:
            continue
        for name, ov in sorted(r.overrides.items()):
            if definite:
                attrs[name] = ov
            else:
                prior = attrs.get(name, AttributeValue())
                cands = tuple(dict.fromkeys([*prior.candidates(), *ov.candidates()]))
                known = tuple(c for c in cands if c is not None)
                if None in cands or len(known) < 2:
                    attrs[name] = AttributeValue(
                        value=None,
                        unit=ov.unit or prior.unit,
                        state=EvidenceState.UNRESOLVED,
                        evidence=prior.evidence + ov.evidence,
                    )
                else:
                    attrs[name] = AttributeValue(
                        unit=ov.unit or prior.unit,
                        state=EvidenceState.CONFLICTING,
                        alternatives=known,
                        evidence=prior.evidence + ov.evidence,
                    )
    return attrs


def _norm(value: Scalar, unit: str | None, what: str) -> tuple[Scalar, str | None]:
    try:
        return normalize(value, unit)
    except UnitError as exc:
        raise InvalidModel(f"{what}: {exc}") from exc


def check_constraint(
    edge_id: str,
    c: Constraint,
    attr: AttributeValue | None,
    assumptions: dict[str, Scalar | None],
) -> tuple[ConstraintCheck, Fact | None, bool | None]:
    """Return (check, fact if unresolved, satisfied) where None means unknown."""
    thr, thr_unit = _norm(c.threshold, c.unit, f"constraint {c.attribute}")
    fid = attr_fact_id(edge_id, c.attribute)
    unit = attr.unit if attr is not None else None
    if fid in assumptions:
        # Assumed attribute values are expressed in canonical units (metres, ratio).
        v = assumptions[fid]
        if v is None:
            attr = None
        else:
            unit = thr_unit
            attr = AttributeValue(value=v, unit=unit, state=EvidenceState.ACCEPTED, evidence=("assumption",))

    def sat(v: Scalar) -> bool:
        nv, nunit = _norm(v, unit, f"edge {edge_id} attribute {c.attribute}")
        if nunit != thr_unit:
            raise InvalidModel(
                f"edge {edge_id} attribute {c.attribute} unit {unit!r} is incompatible with "
                f"constraint unit {c.unit!r}"
            )
        return compare(nv, c.comparator, thr)

    def check(value: Scalar | None, state: EvidenceState | None, ok: bool | None) -> ConstraintCheck:
        return ConstraintCheck(
            attribute=c.attribute,
            comparator=c.comparator.value,
            threshold=c.threshold,
            unit=c.unit,
            hard=c.hard,
            value=value,
            state=state,
            satisfied=ok,
            evidence=attr.evidence if attr is not None else (),
        )

    if attr is None or (attr.value is None and attr.state != EvidenceState.CONFLICTING):
        fact = Fact(
            id=fid,
            kind=FactKind.ATTRIBUTE_UNKNOWN,
            target=edge_id,
            description=f"{c.attribute} is unknown on {edge_id}",
            favorable=c.threshold if c.comparator in (Comparator.GE, Comparator.LE, Comparator.EQ) else None,
        )
        return check(None, attr.state if attr else None, None), fact, None

    if attr.state == EvidenceState.CONFLICTING:
        oks = [sat(a) for a in attr.alternatives]
        if not any(oks):
            return check(None, attr.state, False), None, False
        good = next(a for a, ok in zip(attr.alternatives, oks, strict=True) if ok)
        bad = next((a for a, ok in zip(attr.alternatives, oks, strict=True) if not ok), None)
        fact = Fact(
            id=fid,
            kind=FactKind.ATTRIBUTE_CONFLICT,
            target=edge_id,
            description=f"conflicting {c.attribute} on {edge_id}: {list(attr.alternatives)}",
            alternatives=attr.alternatives,
            evidence=attr.evidence,
            favorable=good,
            unfavorable=bad,
        )
        if all(oks):
            # Every alternative satisfies this constraint; the conflict does not matter here.
            return check(None, attr.state, True), None, True
        return check(None, attr.state, None), fact, None

    assert attr.value is not None
    ok = sat(attr.value)
    if attr.state == EvidenceState.ACCEPTED:
        return check(attr.value, attr.state, ok), None, ok
    if attr.state == EvidenceState.REJECTED:
        fact = Fact(
            id=fid,
            kind=FactKind.ATTRIBUTE_UNKNOWN,
            target=edge_id,
            description=f"{c.attribute} on {edge_id} was rejected and remains unknown",
            evidence=attr.evidence,
        )
        return check(None, attr.state, None), fact, None
    fact = Fact(
        id=fid,
        kind=FactKind.ATTRIBUTE_UNRESOLVED,
        target=edge_id,
        description=f"proposed {c.attribute}={attr.value} on {edge_id} is not reviewed",
        alternatives=(attr.value,),
        evidence=attr.evidence,
        favorable=attr.value if ok else c.threshold,
        unfavorable=None if ok else attr.value,
    )
    return check(attr.value, attr.state, None), fact, None


def classify_edge(
    edge: Edge,
    state: CompiledState,
    profile: MobilityProfile,
    geo: GeometryIndex,
    assumptions: dict[str, Scalar | None],
    *,
    temporary_of: str | None = None,
    temporary_state: EvidenceState | None = None,
) -> EdgeAssessment:
    acc = _Acc([], [], [], [])

    # Existence.
    if temporary_of is not None:
        fid = tempconn_fact_id(temporary_of)
        st = temporary_state or EvidenceState.ACCEPTED
        resolved = _resolved_bool(assumptions, fid)
        if resolved is False:
            acc.blocks.append(BlockReason(kind=BlockKind.EXISTENCE, description=f"temporary connection {temporary_of} assumed absent"))
        elif resolved is None and st != EvidenceState.ACCEPTED:
            acc.facts.append(Fact(id=fid, kind=FactKind.TEMPORARY_CONNECTION, target=edge.id,
                                  description=f"temporary connection {temporary_of} is not reviewed",
                                  favorable=True, unfavorable=False))
    else:
        fid = edge_fact_id(edge.id)
        resolved = _resolved_bool(assumptions, fid)
        if edge.existence == EvidenceState.REJECTED or resolved is False:
            acc.blocks.append(BlockReason(kind=BlockKind.EXISTENCE, description=f"{edge.id} existence rejected", evidence=edge.existence_evidence))
        elif resolved is None and edge.existence != EvidenceState.ACCEPTED:
            acc.facts.append(Fact(id=fid, kind=FactKind.EDGE_EXISTENCE, target=edge.id,
                                  description=f"existence of {edge.id} is {edge.existence.value.lower()}",
                                  evidence=edge.existence_evidence, favorable=True, unfavorable=False))

    # Restrictions: which ones definitely / possibly apply to this edge.
    applying: list[tuple[ActiveRestriction, bool]] = []
    line = geo.line(edge) if temporary_of is None else None
    for ar in state.restrictions:
        r = ar.restriction
        if edge.id in ar.suppressed_edges:
            continue
        r_fid = restriction_fact_id(r.id)
        r_res = _resolved_bool(assumptions, r_fid)
        if r_res is False:
            continue
        r_definite = r.state == EvidenceState.ACCEPTED or r_res is True
        rel = next((t for t in r.targets if t.edge_id == edge.id), None)
        rel_fid = target_fact_id(r.id, edge.id)
        rel_res = _resolved_bool(assumptions, rel_fid)
        rel_kind: FactKind | None = None
        if rel is not None:
            if rel.state == EvidenceState.REJECTED or rel_res is False:
                continue
            rel_definite = rel.state == EvidenceState.ACCEPTED or rel_res is True
            rel_kind = FactKind.RESTRICTION_TARGET
        else:
            if rel_res is False:
                continue
            touches = (
                line is not None
                and r.polygon is not None
                and Polygon(r.polygon).intersects(line)
                and Polygon(r.polygon).intersection(line).length > 0
            )
            if not touches and rel_res is not True:
                continue
            rel_definite = rel_res is True
            rel_kind = FactKind.UNREVIEWED_INTERSECTION
        definite = r_definite and rel_definite
        if not r_definite:
            acc.facts.append(Fact(id=r_fid, kind=FactKind.RESTRICTION_UNRESOLVED, target=r.id,
                                  description=f"restriction {r.id} ({ar.project_id}/{ar.phase_id}) is not reviewed",
                                  evidence=r.evidence, favorable=False, unfavorable=True))
        if not rel_definite:
            desc = (f"whether {r.id} affects {edge.id} is not reviewed" if rel_kind == FactKind.RESTRICTION_TARGET
                    else f"{r.id} geometry intersects {edge.id} but the relation is not reviewed")
            acc.facts.append(Fact(id=rel_fid, kind=rel_kind or FactKind.RESTRICTION_TARGET, target=edge.id,
                                  description=desc, evidence=r.evidence, favorable=False, unfavorable=True))
        applying.append((ar, definite))
        if r.kind == RestrictionKind.CLOSURE and definite:
            acc.blocks.append(BlockReason(kind=BlockKind.RESTRICTION,
                                          description=f"closed by {r.label or r.id} ({ar.project_id}/{ar.phase_id})",
                                          restriction_id=r.id, project_id=ar.project_id, phase_id=ar.phase_id,
                                          evidence=r.evidence))

    # Hard constraints filter, soft ones add cost afterwards.
    attrs = effective_attributes(edge, applying)
    for c in sorted(profile.constraints, key=lambda c: (not c.hard, c.attribute)):
        chk, fact, ok = check_constraint(edge.id, c, attrs.get(c.attribute), assumptions)
        acc.checks.append(chk)
        if c.hard:
            if ok is False:
                a = attrs.get(c.attribute)
                acc.blocks.append(BlockReason(kind=BlockKind.CONSTRAINT,
                                              description=f"{c.attribute} {chk.value if chk.value is not None else list(a.alternatives) if a else '?'} fails {c.comparator.value} {c.threshold}{c.unit or ''}",
                                              attribute=c.attribute, value=chk.value, threshold=c.threshold,
                                              evidence=chk.evidence))
            elif ok is None and fact is not None:
                acc.facts.append(fact)
        elif ok is not True:
            acc.soft_penalty += c.weight

    # Coverage.
    if temporary_of is None:
        if not geo.edge_network_covered(edge):
            acc.gaps.append(CoverageGap(kind=GapKind.NETWORK_COVERAGE, target=edge.id,
                                        description=f"{edge.id} lies outside reviewed network coverage"))
    for pid in state.active_projects:
        if geo.edge_project_gap(pid, edge):
            acc.gaps.append(CoverageGap(kind=GapKind.PROJECT_COVERAGE, target=edge.id, project_id=pid,
                                        description=f"{edge.id} crosses part of {pid}'s work extent that no reviewed sheet covers"))

    facts = tuple(sorted({f.id: f for f in acc.facts}.values(), key=lambda f: f.id))
    if acc.blocks:
        cls = EdgeClass.BLOCKED
    elif acc.gaps:
        cls = EdgeClass.POSSIBLE_UNCOVERED
    elif facts:
        cls = EdgeClass.POSSIBLE_COVERED
    else:
        cls = EdgeClass.CONSERVATIVE
    evidence = tuple(sorted(set(edge.existence_evidence) | {e for c in acc.checks for e in c.evidence}))
    return EdgeAssessment(
        edge_id=edge.id,
        edge_class=cls,
        facts=facts,
        gaps=tuple(acc.gaps),
        blocks=tuple(acc.blocks),
        checks=tuple(acc.checks),
        soft_penalty=acc.soft_penalty,
        evidence=evidence,
    )


@dataclass(frozen=True)
class ClassifiedState:
    state: CompiledState
    edges: dict[str, Edge]
    assessments: dict[str, EdgeAssessment]
    temporary: frozenset[str]


def classify_state(
    network: NetworkSnapshot,
    state: CompiledState,
    profile: MobilityProfile,
    geo: GeometryIndex,
    assumptions: dict[str, Scalar | None],
) -> ClassifiedState:
    edges: dict[str, Edge] = {}
    out: dict[str, EdgeAssessment] = {}
    for e in network.edges:
        edges[e.id] = e
        out[e.id] = classify_edge(e, state, profile, geo, assumptions)
    temp: set[str] = set()
    node_ids = {n.id for n in network.nodes}
    for ac in state.connections:
        c = ac.connection
        if c.edge.id in edges:
            raise InvalidModel(f"temporary connection {c.id} reuses edge id {c.edge.id}")
        if c.edge.u not in node_ids or c.edge.v not in node_ids:
            raise InvalidModel(f"temporary connection {c.id} references unknown nodes")
        edges[c.edge.id] = c.edge
        temp.add(c.edge.id)
        out[c.edge.id] = classify_edge(
            c.edge, state, profile, geo, assumptions, temporary_of=c.id, temporary_state=c.state
        )
        # Temporary edges are part of the project's reviewed scope only if their
        # connection is; network coverage does not apply to them.
    return ClassifiedState(state, edges, out, frozenset(temp))
