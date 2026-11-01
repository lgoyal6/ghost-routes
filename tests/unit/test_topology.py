from __future__ import annotations

import math

import pytest

from ghost_routes.documents.transforms import (
    Affine,
    crop_to_page,
    fit_similarity,
    page_to_render,
)
from ghost_routes.domain.model import EvidenceState
from ghost_routes.network.topology import ObsInput, build


def test_crossing_lines_do_not_create_a_junction() -> None:
    r = build([
        ObsInput("a", "path", "accepted", ((0, 0), (10, 10))),
        ObsInput("b", "path", "accepted", ((0, 10), (10, 0))),
    ], transform=None, snap_tolerance=0.5, phase_map={}, barrier_buffer=0.5)
    assert len(r.nodes) == 4 and len(r.edges) == 2


def test_snap_joins_endpoints_within_tolerance_only() -> None:
    r = build([
        ObsInput("a", "path", "accepted", ((0, 0), (10, 0))),
        ObsInput("b", "path", "accepted", ((10.3, 0), (20, 0))),
        ObsInput("c", "path", "accepted", ((21.0, 0), (30, 0))),
    ], transform=None, snap_tolerance=0.5, phase_map={}, barrier_buffer=0.5)
    assert len(r.nodes) == 5
    assert r.edges[0].v == r.edges[1].u
    assert r.edges[1].v != r.edges[2].u


def test_snap_never_bridges_a_barrier() -> None:
    r = build([
        ObsInput("a", "path", "accepted", ((0, 0), (10, 0))),
        ObsInput("b", "path", "accepted", ((10.4, 0), (20, 0))),
        ObsInput("f", "barrier", "accepted", ((10.2, -5), (10.2, 5)), phase_refs=("PHASE 1",)),
    ], transform=None, snap_tolerance=0.5, phase_map={"PHASE 1": "ph1"}, barrier_buffer=0.1)
    assert r.edges[0].v != r.edges[1].u
    assert r.refused_snaps and r.refused_snaps[0]["reason"] == "snap would cross a barrier"
    assert r.restrictions[0].phase_ids == ("ph1",) and r.restrictions[0].targets == ()


def test_review_state_maps_to_evidence_and_unknown_attributes_stay_unknown() -> None:
    r = build([ObsInput("p", "path", "pending", ((0, 0), (1, 0)))], transform=None, snap_tolerance=0,
              phase_map={}, barrier_buffer=0)
    assert r.edges[0].existence == EvidenceState.UNRESOLVED
    assert r.edges[0].attributes == {} and r.edges[0].length_m is None


def test_transforms_compose_and_invert() -> None:
    c2p = crop_to_page(880, 576, 144)
    assert c2p.apply((0, 0)) == pytest.approx((440, 288))
    p2r = page_to_render(144)
    back = c2p.then(p2r)
    assert back.apply((10, 20)) == pytest.approx((890, 596))
    inv = Affine(2, 1, 3, -1, 4, 5).inverse()
    assert Affine(2, 1, 3, -1, 4, 5).then(inv).apply((7, -3)) == pytest.approx((7, -3))


def test_similarity_fit_recovers_scale_bar_and_flips_y() -> None:
    # 72 pt = 80 ft along the bar, page y down, site y up.
    m_per_pt = 80 * 0.3048 / 72
    src = [(989.34, 714.48), (1061.34, 714.48), (989.34, 642.48)]
    dst = [(0.0, 0.0), (72 * m_per_pt, 0.0), (0.0, 72 * m_per_pt)]
    cal = fit_similarity(src, dst)
    assert cal.rms < 1e-9
    assert cal.transform.scale_factor() == pytest.approx(m_per_pt)
    x, y = cal.transform.apply((1025.34, 714.48))
    assert (x, y) == pytest.approx((36 * m_per_pt, 0.0), abs=1e-9)
    assert math.isclose(cal.transform.apply((989.34, 704.48))[1], 10 * m_per_pt)
