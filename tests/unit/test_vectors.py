"""Vector geometry rules: legend binding, chaining, inline-symbol fences and phase from leaders."""

from __future__ import annotations

from typing import Any

from ghost_routes.documents.vectors import chain, vector_observations

_ids = iter(range(10_000))


def word(text: str, x0: float, top: float, size: float = 8.0) -> dict[str, Any]:
    return {"id": next(_ids), "text": text, "x0": x0, "top": top, "x1": x0 + 0.6 * size * len(text), "bottom": top + size}


def line(text: str, x0: float, top: float, size: float = 8.0) -> list[dict[str, Any]]:
    out, x = [], x0
    for t in text.split():
        out.append(word(t, x, top, size))
        x = out[-1]["x1"] + 0.25 * size
    return out


def stroke(pts: list[tuple[float, float]], w: float = 2.0, dash: list[float] | None = None, color: float = 0.35) -> dict[str, Any]:
    return {"pts": [list(p) for p in pts], "w": w, "dash": dash or [], "color": [color]}


FENCE = {"w": 1.4, "dash": [6.0, 3.0], "color": 0.0}
PATH = {"w": 2.0, "color": 0.35}


def legend() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    words = line("TEMPORARY FENCING", 960, 80) + line("PEDESTRIAN PATH", 960, 98)
    strokes = [stroke([(900, 84), (950, 84)], **FENCE), stroke([(900, 102), (950, 102)], **PATH)]
    return words, strokes


def test_legend_binds_styles_and_classifies_matching_strokes() -> None:
    words, strokes = legend()
    strokes += [stroke([(100, 500), (300, 500)], **PATH), stroke([(300, 500), (300, 300)], **PATH),
                stroke([(150, 450), (250, 450), (250, 400)], **FENCE), stroke([(100, 600), (200, 650)], w=0.5, color=0.0)]
    res = vector_observations(words, strokes)
    assert {b["cls"] for b in res["summary"]["bindings"]} == {"path", "barrier"}
    by_cls = {c: [o for o in res["observations"] if o["cls"] == c] for c in ("path", "barrier")}
    assert len(by_cls["path"]) == 2  # a corner splits the walk into its two drawn segments
    assert len(by_cls["barrier"]) == 1
    assert all(o["properties"]["source"] == "vector" for o in res["observations"])  # legend samples are not proposals


def test_style_claimed_by_two_classes_is_not_used() -> None:
    words = line("TEMPORARY FENCING", 960, 80) + line("PEDESTRIAN PATH", 960, 98)
    strokes = [stroke([(900, 84), (950, 84)], **PATH), stroke([(900, 102), (950, 102)], **PATH), stroke([(100, 500), (300, 500)], **PATH)]
    res = vector_observations(words, strokes)
    assert res["observations"] == [] and res["summary"]["notes"]


def test_chain_joins_straight_continuations_only() -> None:
    straight = chain([[(0, 0), (10, 0)], [(10, 0), (20, 0)]])
    corner = chain([[(0, 0), (10, 0)], [(10, 0), (10, 10)]])
    junction = chain([[(0, 0), (10, 0)], [(10, 0), (20, 0)], [(10, 0), (10, 10)]])
    assert straight == [[(0, 0), (10, 0), (20, 0)]]
    assert len(corner) == 2 and len(junction) == 3


def test_inline_x_fence_is_chained_through_glyphs_and_corners() -> None:
    # --X--X--+ then down --X-- : segments of one style, X glyphs in the gaps, a bend with no glyph.
    words = [word("X", 118.5, 96, 3), word("X", 138.5, 96, 3), word("X", 156, 113.5, 3)]
    ink = {"w": 0.48, "color": 0.0}
    strokes = [stroke([(100, 100), (118, 100)], **ink), stroke([(122, 100), (138, 100)], **ink),
               stroke([(142, 100), (157.5, 100)], **ink), stroke([(157.5, 100), (157.5, 113)], **ink),
               stroke([(157.5, 117), (157.5, 135)], **ink),
               stroke([(400, 400), (420, 400)], **ink)]  # unrelated line with no glyphs
    res = vector_observations(words, strokes)
    fences = [o for o in res["observations"] if o["cls"] == "barrier"]
    assert len(fences) == 1
    g = fences[0]["geometry"]
    assert g[0] == [100, 100] and g[-1] == [157.5, 135]
    assert fences[0]["properties"]["binding"] == "inline_symbol:X"


def test_phase_follows_the_leader_of_a_barrier_callout() -> None:
    words, strokes = legend()
    fence_a, fence_b = [(100, 300), (200, 300)], [(100, 500), (200, 500)]
    strokes += [stroke(fence_a, **FENCE), stroke(fence_b, **FENCE)]
    # "PHASE 2 FENCING" sits nearer fence A, but its leader ends on fence B.
    words += line("PHASE 2 FENCING", 120, 320)
    strokes.append(stroke([(185, 333), (150, 498)], w=0.36, color=0.0))
    res = vector_observations(words, strokes)
    phases = {tuple(o["geometry"][0]): o["phase_refs"] for o in res["observations"] if o["cls"] == "barrier"}
    assert phases[(100, 500)] == ["PHASE 2"]
    assert phases[(100, 300)] == []  # the leader points elsewhere, so proximity is not evidence


def test_callouts_about_other_features_never_phase_a_fence() -> None:
    words, strokes = legend()
    strokes.append(stroke([(100, 300), (200, 300)], **FENCE))
    words += line("SIDEWALK RECONSTRUCTED IN PHASE 1", 110, 310)
    res = vector_observations(words, strokes)
    fence = next(o for o in res["observations"] if o["cls"] == "barrier")
    assert fence["phase_refs"] == [] and fence["flags"] == ["no_phase_callout_nearby"]


def test_bare_phase_label_is_a_flagged_fallback() -> None:
    words, strokes = legend()
    strokes.append(stroke([(100, 300), (200, 300)], **FENCE))
    words += line("PHASE 1", 130, 310)
    res = vector_observations(words, strokes)
    fence = next(o for o in res["observations"] if o["cls"] == "barrier")
    assert fence["phase_refs"] == ["PHASE 1"] and fence["flags"] == ["phase_from_nearest_callout"]


def test_leaderless_barrier_callout_is_a_flagged_fallback() -> None:
    words, strokes = legend()
    strokes.append(stroke([(100, 300), (200, 300)], **FENCE))
    words += line("PHASE 1 FENCING", 130, 310)
    fence = next(o for o in vector_observations(words, strokes)["observations"] if o["cls"] == "barrier")
    assert fence["phase_refs"] == ["PHASE 1"] and fence["flags"] == ["phase_from_nearest_callout"]
