"""Compile a scenario into explicit analysis states.

Baseline plus immutable overlays. Restrictions from simultaneously active
projects combine by union; nothing one project does can cancel another
project's restriction. Undated phases never imply overlap.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from ghost_routes.domain.model import (
    AnalysisInput,
    EvidenceState,
    Interval,
    Phase,
    ProjectSnapshot,
    Restriction,
    RestrictionWindowOverride,
    Scenario,
    StateBasis,
    StateSpec,
    TemporaryConnection,
)


class ScenarioError(ValueError):
    """The scenario cannot be compiled as declared (not an analysis outcome)."""


@dataclass(frozen=True)
class ActiveRestriction:
    project_id: str
    phase_id: str
    restriction: Restriction
    # Edge ids kept open by a hypothetical override in this state.
    suppressed_edges: frozenset[str] = frozenset()


@dataclass(frozen=True)
class ActiveConnection:
    project_id: str
    phase_id: str | None
    connection: TemporaryConnection
    hypothetical: bool = False


@dataclass(frozen=True)
class CompiledState:
    label: str
    basis: StateBasis
    active: dict[str, str]
    interval: Interval | None
    restrictions: tuple[ActiveRestriction, ...]
    connections: tuple[ActiveConnection, ...]
    hypothetical: bool = False
    active_projects: tuple[str, ...] = field(default_factory=tuple)


def shifted_interval(project_id: str, phase: Phase, scenario: Scenario) -> Interval | None:
    if phase.effective is None:
        return None
    shift = scenario.phase_shifts.get(f"{project_id}/{phase.id}", 0)
    if not shift:
        return phase.effective
    delta = timedelta(seconds=shift)
    end = phase.effective.end + delta if phase.effective.end is not None else None
    return Interval(start=phase.effective.start + delta, end=end)


def check_prerequisites(project: ProjectSnapshot, scenario: Scenario) -> list[str]:
    """Return violations: a dated phase may not start before a prerequisite ends."""
    problems: list[str] = []
    for ph in project.phases:
        iv = shifted_interval(project.project_id, ph, scenario)
        if iv is None:
            continue
        for pre_id in ph.prerequisites:
            pre = shifted_interval(project.project_id, project.phase(pre_id), scenario)
            if pre is None:
                problems.append(f"{project.project_id}/{ph.id}: prerequisite {pre_id} is undated")
            elif pre.end is None or iv.start < pre.end:
                problems.append(
                    f"{project.project_id}/{ph.id} starts before prerequisite {pre_id} ends"
                )
    return problems


def _restrictions_for(
    project: ProjectSnapshot,
    phase_id: str,
    overrides: tuple[RestrictionWindowOverride, ...],
    window: Interval | None,
    scenario: Scenario | None = None,
) -> list[ActiveRestriction]:
    out = []
    added = [a.restriction for a in (scenario.added_restrictions if scenario else ()) if a.project_id == project.project_id]
    for r in sorted([*project.restrictions, *added], key=lambda r: r.id):
        if phase_id not in r.phase_ids or r.state == EvidenceState.REJECTED:
            continue
        if window is not None and r.effective is not None and not _overlaps(r.effective, window):
            continue
        suppressed: set[str] = set()
        for ov in overrides:
            if ov.restriction_id != r.id:
                continue
            if ov.window is None or window is None or _covers(ov.window, window):
                suppressed.update(ov.edge_ids)
        out.append(ActiveRestriction(project.project_id, phase_id, r, frozenset(suppressed)))
    return out


def _connections_for(
    project: ProjectSnapshot, phase_id: str, scenario: Scenario, window: Interval | None
) -> list[ActiveConnection]:
    out = []
    for c in sorted(project.temporary_connections, key=lambda c: c.id):
        if phase_id not in c.phase_ids or c.state == EvidenceState.REJECTED:
            continue
        if window is not None and c.effective is not None and not _covers(c.effective, window):
            continue
        enabled = c.id in scenario.enabled_temporary_connections
        if c.active_by_default or enabled:
            out.append(
                ActiveConnection(project.project_id, phase_id, c, hypothetical=not c.active_by_default)
            )
    return out


def _overlaps(a: Interval, b: Interval) -> bool:
    a_end = a.end or datetime.max.replace(tzinfo=a.start.tzinfo)
    b_end = b.end or datetime.max.replace(tzinfo=b.start.tzinfo)
    return a.start < b_end and b.start < a_end


def _covers(outer: Interval, inner: Interval) -> bool:
    if inner.start < outer.start:
        return False
    if outer.end is None:
        return True
    return inner.end is not None and inner.end <= outer.end


def _state_from_spec(inp: AnalysisInput, spec: StateSpec) -> CompiledState:
    scenario = inp.scenario
    restrictions: list[ActiveRestriction] = []
    connections: list[ActiveConnection] = []
    for pid, phase_id in sorted(spec.active.items()):
        if pid not in scenario.project_ids:
            raise ScenarioError(f"state {spec.label!r} activates project {pid} outside the scenario")
        project = inp.project(pid)
        try:
            project.phase(phase_id)
        except KeyError:
            raise ScenarioError(f"state {spec.label!r}: unknown phase {pid}/{phase_id}") from None
        restrictions += _restrictions_for(project, phase_id, scenario.disabled_restriction_windows, None, scenario)
        connections += _connections_for(project, phase_id, scenario, None)
    hypothetical = (
        spec.basis == StateBasis.DECLARED_CONCURRENT
        or scenario.hypothetical
        or any(inp.project(p).hypothetical for p in spec.active)
        or any(c.hypothetical for c in connections)
        or any(r.suppressed_edges for r in restrictions)
        or bool(scenario.added_restrictions)
    )
    return CompiledState(
        label=spec.label,
        basis=spec.basis,
        active=dict(sorted(spec.active.items())),
        interval=None,
        restrictions=tuple(restrictions),
        connections=tuple(connections),
        hypothetical=hypothetical,
        active_projects=tuple(sorted(spec.active)),
    )


def baseline_state() -> CompiledState:
    return CompiledState("baseline", StateBasis.BASELINE, {}, None, (), ())


def phase_states(inp: AnalysisInput) -> list[CompiledState]:
    scenario = inp.scenario
    specs: list[StateSpec]
    if scenario.states:
        specs = list(scenario.states)
    else:
        specs = []
        for pid in sorted(scenario.project_ids):
            for ph in sorted(inp.project(pid).phases, key=lambda p: (p.order, p.id)):
                specs.append(
                    StateSpec(
                        label=f"{pid} / {ph.label}",
                        active={pid: ph.id},
                        basis=StateBasis.PROJECT_ISOLATED,
                    )
                )
    for spec in specs:
        if len(spec.active) > 1 and spec.basis != StateBasis.DECLARED_CONCURRENT:
            raise ScenarioError(
                f"state {spec.label!r} combines undated projects; declare it as a "
                "hypothetical concurrent state instead of inferring overlap from phase order"
            )
    return [_state_from_spec(inp, s) for s in specs]


def dated_boundaries(inp: AnalysisInput) -> list[datetime]:
    scenario = inp.scenario
    assert scenario.horizon is not None
    h = scenario.horizon
    points: set[datetime] = {h.start}
    if h.end is not None:
        points.add(h.end)

    def add(iv: Interval | None) -> None:
        if iv is None:
            return
        points.add(iv.start)
        if iv.end is not None:
            points.add(iv.end)

    for pid in scenario.project_ids:
        project = inp.project(pid)
        for ph in project.phases:
            iv = shifted_interval(pid, ph, scenario)
            if iv is None:
                raise ScenarioError(
                    f"{pid}/{ph.id} has no dated schedule; dated composition with this project "
                    "is unresolved"
                )
            add(iv)
        for r in project.restrictions:
            add(r.effective)
        for a in scenario.added_restrictions:
            if a.project_id == pid:
                add(a.restriction.effective)
        for c in project.temporary_connections:
            add(c.effective)
    for ov in scenario.disabled_restriction_windows:
        add(ov.window)
    return sorted(
        p for p in points if p >= h.start and (h.end is None or p <= h.end)
    )


def dated_states(inp: AnalysisInput) -> list[CompiledState]:
    """One state per elementary interval between consecutive change boundaries."""
    scenario = inp.scenario
    assert scenario.horizon is not None
    if scenario.horizon.end is None:
        raise ScenarioError("dated analysis needs a bounded horizon")
    for pid in scenario.project_ids:
        problems = check_prerequisites(inp.project(pid), scenario)
        if problems:
            raise ScenarioError("; ".join(problems))
    pts = dated_boundaries(inp)
    states: list[CompiledState] = []
    for a, b in itertools.pairwise(pts):
        window = Interval(start=a, end=b)
        active: dict[str, str] = {}
        restrictions: list[ActiveRestriction] = []
        connections: list[ActiveConnection] = []
        for pid in sorted(scenario.project_ids):
            project = inp.project(pid)
            live = [
                ph
                for ph in project.phases
                if (iv := shifted_interval(pid, ph, scenario)) is not None and _covers(iv, window)
            ]
            if len(live) > 1:
                raise ScenarioError(f"{pid} has overlapping phases {[p.id for p in live]}")
            if not live:
                continue
            ph = live[0]
            active[pid] = ph.id
            restrictions += _restrictions_for(
                project, ph.id, scenario.disabled_restriction_windows, window, scenario
            )
            connections += _connections_for(project, ph.id, scenario, window)
        hypothetical = (
            scenario.hypothetical
            or any(inp.project(p).hypothetical for p in active)
            or any(c.hypothetical for c in connections)
            or any(r.suppressed_edges for r in restrictions)
            or bool(scenario.phase_shifts)
            or bool(scenario.added_restrictions)
        )
        label = ", ".join(f"{p}:{ph}" for p, ph in sorted(active.items())) or "no active work"
        states.append(
            CompiledState(
                label=f"[{a.isoformat()}, {b.isoformat()}) {label}",
                basis=StateBasis.SCHEDULE,
                active=active,
                interval=window,
                restrictions=tuple(restrictions),
                connections=tuple(connections),
                hypothetical=hypothetical,
                active_projects=tuple(sorted(active)),
            )
        )
    return states


def compile_states(inp: AnalysisInput) -> list[CompiledState]:
    if inp.scenario.mode == "phases":
        return phase_states(inp)
    return dated_states(inp)
