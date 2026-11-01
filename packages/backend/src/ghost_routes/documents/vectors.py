"""Path and barrier geometry from a page's own vector strokes.

The vision model reads text well but does not recover line geometry, so
geometry comes from the PDF's drawing operators instead, classified by two
sheet conventions:

- Legend binding: a short legend line naming a class ("PEDESTRIAN PATH",
  "TEMPORARY FENCING") with a sample stroke beside it fixes that class's stroke
  style (width, dash, color). Every other stroke drawn in that style is a
  candidate of that class. A style claimed by two classes is ambiguous and is
  not used.
- Inline symbols: CAD fences are often short strokes interleaved with an "X"
  glyph (--X--X--). Collinear strokes whose gaps hold X glyphs are chained into
  one fence.

Barriers take their phase from a barrier callout's leader or a nearby leaderless
"PHASE n" label, and stay unresolved when phases conflict. All coordinates are page
points in the displayed frame, origin top-left, as for native words.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any

from shapely.geometry import LineString, Point

LEGEND_CLASSES = (
    ("barrier", re.compile(r"\bFENC(E|ING)\b|\bBARRIC|\bBARRIER\b")),
    ("path", re.compile(r"\bPEDESTRIAN\s+(PATH|ROUTE|WAY)\b|\bSIDEWALK\b|\bWALKWAY\b")),
)
LEGEND_MAX_CHARS = 40
SAMPLE_GAP_PT = 40.0
SAMPLE_LENGTH_PT = (15.0, 150.0)
INLINE_GLYPHS = {"X", "x"}
PHASE_RADIUS_PT = 60.0
PHASE_TIE_RATIO = 1.25
LEADER_TIP_PT = 6.0
LEADER_START_PT = 12.0  # leaders usually start a little clear of the text
_BARRIER_WORDS = re.compile(r"FENC|BARRIER|BARRICADE", re.I)
_OTHER_FEATURE = re.compile(r"SIDEWALK|PATH|CONNECTION|RECONSTRUCT|ACCESS|LIMIT|ROAD|AVE", re.I)
TURN_MAX_DEG = 20.0
_PHASE = re.compile(r"PHASE\s*(\d+[A-Z]?)", re.I)

Style = tuple[float, tuple[float, ...], tuple[float, ...]]


@dataclass(frozen=True)
class Stroke:
    pts: tuple[tuple[float, float], ...]
    width: float
    dash: tuple[float, ...]
    color: tuple[float, ...]

    @property
    def style(self) -> Style:
        return (round(self.width, 2), self.dash, self.color)

    @property
    def length(self) -> float:
        return sum(math.dist(a, b) for a, b in zip(self.pts, self.pts[1:], strict=False))


@dataclass
class Binding:
    cls: str
    text: str
    style: Style
    sample: int  # index of the legend sample stroke
    word_ids: list[int] = field(default_factory=list)


def stroke_from_raw(o: dict[str, Any]) -> Stroke | None:
    """Normalize one serialized stroke ({pts, w, dash, color}) from the PDF child process."""
    pts = tuple((float(x), float(y)) for x, y in o.get("pts") or [])
    if len(pts) < 2:
        return None
    dash = tuple(round(float(v), 2) for v in (o.get("dash") or []) if v)
    color = tuple(round(float(v), 3) for v in (o.get("color") or []))
    return Stroke(pts, float(o.get("w") or 0.0), dash, color)


def text_lines(words: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Words grouped into lines (same baseline, small gaps), with their box and word ids."""
    lines: list[list[dict[str, Any]]] = []
    for wd in sorted(words, key=lambda d: (round(d["top"] / 3), d["x0"])):
        if lines:
            last = lines[-1][-1]
            if abs(wd["top"] - last["top"]) < 2.5 and 0 <= wd["x0"] - last["x1"] < 6:
                lines[-1].append(wd)
                continue
        lines.append([wd])
    return [{"text": " ".join(w["text"] for w in ln), "word_ids": [w["id"] for w in ln],
             "box": (min(w["x0"] for w in ln), min(w["top"] for w in ln), max(w["x1"] for w in ln), max(w["bottom"] for w in ln))}
            for ln in lines]


def _bbox(pts: tuple[tuple[float, float], ...] | list[tuple[float, float]]) -> tuple[float, float, float, float]:
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    return (min(xs), min(ys), max(xs), max(ys))


def bind_legend(lines: list[dict[str, Any]], strokes: list[Stroke]) -> tuple[list[Binding], list[str]]:
    """Find legend lines with a sample stroke beside them. Returns bindings and notes on rejected ones."""
    found: list[Binding] = []
    for ln in lines:
        up = ln["text"].upper().strip()
        if len(up) > LEGEND_MAX_CHARS:
            continue
        cls = next((c for c, rx in LEGEND_CLASSES if rx.search(up)), None)
        if cls is None:
            continue
        x0, y0, x1, y1 = ln["box"]
        cy, h = (y0 + y1) / 2, max(y1 - y0, 1.0)
        best: tuple[float, int] | None = None
        for i, s in enumerate(strokes):
            bx0, by0, bx1, by1 = _bbox(s.pts)
            if not SAMPLE_LENGTH_PT[0] <= s.length <= SAMPLE_LENGTH_PT[1] or by1 - by0 > 1.5 * h:
                continue
            if abs((by0 + by1) / 2 - cy) > 1.2 * h:
                continue
            gap = x0 - bx1 if bx1 <= x0 else bx0 - x1 if bx0 >= x1 else None
            if gap is None or gap > SAMPLE_GAP_PT:
                continue
            if best is None or gap < best[0]:
                best = (gap, i)
        if best is not None:
            found.append(Binding(cls, ln["text"], strokes[best[1]].style, best[1], list(ln["word_ids"])))
    by_style: dict[Style, set[str]] = {}
    for b in found:
        by_style.setdefault(b.style, set()).add(b.cls)
    notes = [f"style {s} is drawn for both {sorted(c)} in the legend; not used" for s, c in by_style.items() if len(c) > 1]
    keep = [b for b in found if len(by_style[b.style]) == 1]
    return keep, notes


def _turn_deg(a: tuple[float, float], b: tuple[float, float], c: tuple[float, float]) -> float:
    v1, v2 = (b[0] - a[0], b[1] - a[1]), (c[0] - b[0], c[1] - b[1])
    n = math.hypot(*v1) * math.hypot(*v2)
    if n == 0:
        return 0.0
    cos = max(-1.0, min(1.0, (v1[0] * v2[0] + v1[1] * v2[1]) / n))
    return math.degrees(math.acos(cos))


def chain(polys: list[list[tuple[float, float]]], join_pt: float = 0.75) -> list[list[tuple[float, float]]]:
    """Join polylines end to end where exactly two meet and the line continues nearly straight.

    Corners and junctions stay split, so each drawn segment between junctions stays one edge.
    """
    polys = [list(p) for p in polys]
    changed = True
    while changed:
        changed = False
        ends: dict[tuple[int, int], list[tuple[int, int]]] = {}
        for i, p in enumerate(polys):
            for k, pt in ((0, p[0]), (1, p[-1])):
                ends.setdefault((round(pt[0] / join_pt), round(pt[1] / join_pt)), []).append((i, k))
        for members in ends.values():
            if len(members) != 2 or members[0][0] == members[1][0]:
                continue
            (i, ki), (j, kj) = members
            a = polys[i] if ki == 1 else polys[i][::-1]
            b = polys[j] if kj == 0 else polys[j][::-1]
            if len(a) < 2 or len(b) < 2 or _turn_deg(a[-2], a[-1], b[1]) > TURN_MAX_DEG:
                continue
            polys[i] = a + b[1:]
            polys.pop(j)
            changed = True
            break
    return polys


def inline_symbol_fences(words: list[dict[str, Any]], strokes: list[Stroke]) -> list[list[tuple[float, float]]]:
    """Fences drawn as same-style segments interleaved with X glyphs (--X--X--), walked in order.

    A glyph links the two segment ends that touch it when both segments share a style;
    fences turn at glyphs too, so direction is not checked, and requiring exactly two
    same-style ends at the glyph is what keeps unrelated linework out. Same-style ends that meet with no glyph (a
    bend in the fence) link too when exactly two meet there. A run needs two glyphs.
    """
    glyphs = [((w["x0"] + w["x1"]) / 2, (w["top"] + w["bottom"]) / 2, max(w["x1"] - w["x0"], w["bottom"] - w["top"]))
              for w in words if w["text"] in INLINE_GLYPHS]
    if len(glyphs) < 2:
        return []
    # Fence pieces are short simple polylines; corner pieces bend, so straightness is not required.
    segs = [s for s in strokes if len(s.pts) <= 6 and 0.5 <= s.length <= 60.0 and math.dist(s.pts[0], s.pts[-1]) >= 0.5]
    ends = [(s.pts[0], s.pts[-1]) for s in segs]
    link: dict[tuple[int, int], tuple[int, int]] = {}
    glyph_of: dict[tuple[int, int], int] = {}

    for g, (gx, gy, size) in enumerate(glyphs):
        r = 0.9 * size + 1.5
        touching = [(i, k) for i in range(len(segs)) for k in (0, 1) if math.dist(ends[i][k], (gx, gy)) <= r]
        by_style: dict[Style, list[tuple[int, int]]] = {}
        for i, k in touching:
            by_style.setdefault(segs[i].style, []).append((i, k))
        for members in by_style.values():
            if len({i for i, _ in members}) != 2 or len(members) != 2:
                continue
            (i, ki), (j, kj) = members
            if (i, ki) in link or (j, kj) in link:
                continue
            link[(i, ki)], link[(j, kj)] = (j, kj), (i, ki)
            glyph_of[(i, ki)] = glyph_of[(j, kj)] = g
    # Bends: two same-style ends meeting with no glyph between them.
    meet: dict[tuple[Style, int, int], list[tuple[int, int]]] = {}
    for i, s in enumerate(segs):
        for k in (0, 1):
            if (i, k) not in link:
                x, y = ends[i][k]
                meet.setdefault((s.style, round(x / 0.75), round(y / 0.75)), []).append((i, k))
    for members in meet.values():
        if len(members) == 2 and members[0][0] != members[1][0]:
            a, b = members
            link[a], link[b] = b, a
    fences = []
    seen: set[int] = set()
    for start in range(len(segs)):
        if start in seen or not any((start, k) in link for k in (0, 1)):
            continue
        # Walk to one free end of this run, then collect the run from there.
        i, k = start, 0
        visited = {start}
        while (i, k) in link and link[(i, k)][0] not in visited:
            j, kj = link[(i, k)]
            visited.add(j)
            i, k = j, 1 - kj
        cur, enter = i, 1 - k  # leave `cur` through its far end
        pts: list[tuple[float, float]] = []
        run: set[int] = set()
        glyph_count: set[int] = set()
        while True:
            run.add(cur)
            piece = list(segs[cur].pts) if enter == 1 else list(segs[cur].pts[::-1])
            pts += piece if not pts else piece[1:] if math.dist(pts[-1], piece[0]) < 1e-6 else piece
            nxt = link.get((cur, enter))
            if nxt is None or nxt[0] in run:
                break
            if (cur, enter) in glyph_of:
                glyph_count.add(glyph_of[(cur, enter)])
            cur, enter = nxt[0], 1 - nxt[1]
        seen |= run
        if len(glyph_count) >= 2:
            fences.append(pts)
    return fences


def phase_callouts(lines: list[dict[str, Any]], strokes: list[Stroke]) -> list[dict[str, Any]]:
    """Short "PHASE n" texts, each with the tips of leader lines that start at its box."""
    out = []
    for ln in lines:
        m = _PHASE.search(ln["text"])
        if not m or len(ln["text"]) > LEGEND_MAX_CHARS:
            continue
        x0, y0, x1, y1 = ln["box"]
        box = LineString([(x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)])
        def out_box(p: tuple[float, float], b: tuple[float, float, float, float] = (x0, y0, x1, y1)) -> bool:
            return not (b[0] - 1 <= p[0] <= b[2] + 1 and b[1] - 1 <= p[1] <= b[3] + 1)

        # Leaders can be several strokes (a shoulder, then arrows): follow joined ends up to three hops.
        ink = [st for st in strokes if st.width > 0 and st.color not in ((1.0,), (1.0, 1.0, 1.0))]
        frontier = [(st, k) for st in ink if st.length >= 5
                    for k in (0, -1) if box.distance(Point(st.pts[k])) <= LEADER_START_PT and out_box(st.pts[-1 - k])]
        tips: list[tuple[float, float]] = []
        seen_strokes: set[int] = set()
        for _hop in range(3):
            nxt = []
            for st, k in frontier:
                if id(st) in seen_strokes:
                    continue
                seen_strokes.add(id(st))
                end = st.pts[-1 - k]
                cont = [(o, j) for o in ink if id(o) not in seen_strokes and o.length >= 5
                        for j in (0, -1) if math.dist(o.pts[j], end) <= 1.5]
                if cont:
                    nxt += cont
                else:
                    tips.append(end)
            frontier = nxt
        tips += [st.pts[-1 - k] for st, k in frontier]
        text = ln["text"].upper()
        # A callout that names a barrier speaks for barriers; a bare "PHASE n" labels an area; one that
        # names another feature (a sidewalk being rebuilt) says nothing about fences.
        kind = "barrier" if _BARRIER_WORDS.search(text) else "other" if _OTHER_FEATURE.search(text) else "bare"
        out.append({"phase": f"PHASE {m.group(1).upper()}", "center": ((x0 + x1) / 2, (y0 + y1) / 2),
                    "word_ids": ln["word_ids"], "tips": tips, "kind": kind})
    return out


def assign_phase(geometry: list[tuple[float, float]], callouts: list[dict[str, Any]]) -> tuple[list[str], list[int], list[str]]:
    """Phase from a barrier leader, else the nearest leaderless phase callout. (refs, word_ids, flags)"""
    line = LineString(geometry) if len(geometry) > 1 else Point(geometry[0])
    led = {c["phase"]: c for c in callouts
           if c["kind"] == "barrier" and any(line.distance(Point(t)) <= LEADER_TIP_PT for t in c["tips"])}
    if len(led) == 1:
        c = next(iter(led.values()))
        return [c["phase"]], list(c["word_ids"]), []
    if len(led) > 1:
        return [], [], ["phase_ambiguous:" + "|".join(sorted(led))]
    near = sorted(((line.distance(Point(c["center"])), i) for i, c in enumerate(callouts)
                   if (c["kind"] == "bare" or (c["kind"] == "barrier" and not c["tips"]))
                   and line.distance(Point(c["center"])) <= PHASE_RADIUS_PT))
    if not near:
        return [], [], ["no_phase_callout_nearby"]
    best_d, best = near[0][0], callouts[near[0][1]]
    if any(callouts[i]["phase"] != best["phase"] and d <= PHASE_TIE_RATIO * max(best_d, 1.0) for d, i in near[1:]):
        return [], [], [f"phase_ambiguous:{best['phase']}"]
    return [best["phase"]], list(best["word_ids"]), ["phase_from_nearest_callout"]


def vector_observations(words: list[dict[str, Any]], raw_strokes: list[dict[str, Any]]) -> dict[str, Any]:
    """Path and barrier proposals from vector strokes, plus a summary of how they were found."""
    strokes = [s for s in (stroke_from_raw(o) for o in raw_strokes) if s is not None]
    lines = text_lines(words)
    bindings, notes = bind_legend(lines, strokes)
    feature_styles = {b.style for b in bindings}
    callouts = phase_callouts(lines, [s for s in strokes if s.style not in feature_styles])
    samples = {b.sample for b in bindings}
    out: list[dict[str, Any]] = []
    for b in bindings:
        polys = [list(s.pts) for i, s in enumerate(strokes) if s.style == b.style and i not in samples]
        for geom in chain(polys):
            out.append(_obs(b.cls, geom, f"legend:{b.text}", b.style, callouts, b.word_ids))
    if not any(b.cls == "barrier" for b in bindings):
        for geom in inline_symbol_fences(words, strokes):
            out.append(_obs("barrier", geom, "inline_symbol:X", None, callouts, []))
    summary = {"strokes": len(strokes),
               "bindings": [{"cls": b.cls, "legend_text": b.text,
                             "style": {"width": b.style[0], "dash": list(b.style[1]), "color": list(b.style[2])}} for b in bindings],
               "notes": notes,
               "proposals": {c: sum(1 for o in out if o["cls"] == c) for c in ("path", "barrier")}}
    return {"observations": out, "summary": summary}


def _obs(cls: str, geom: list[tuple[float, float]], source: str, style: Style | None,
         callouts: list[dict[str, Any]], legend_word_ids: list[int]) -> dict[str, Any]:
    phases, phase_words, flags = assign_phase(geom, callouts) if cls == "barrier" else ([], [], [])
    props: dict[str, Any] = {"source": "vector", "binding": source}
    if style is not None:
        props["style"] = {"width": style[0], "dash": list(style[1]), "color": list(style[2])}
    return {"cls": cls, "label": f"{cls} ({source})", "bbox": list(_bbox(geom)),
            "geometry": [[round(x, 3), round(y, 3)] for x, y in geom], "phase_refs": phases,
            "word_ids": legend_word_ids + phase_words, "properties": props, "flags": flags}
