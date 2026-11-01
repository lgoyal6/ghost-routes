"""Coordinate frames and transforms.

Frames:
- page: PDF points in the displayed (rotation-applied) page, origin top-left, y down.
  pdfplumber and PDFium both report this frame for rotated pages.
- render: pixels of the full-page render, ``render = page * dpi / 72``.
- crop: pixels inside a crop, ``crop = render - (rx0, ry0)``.
- site: calibrated metric frame from reviewed control points (y up).
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

Point = tuple[float, float]


@dataclass(frozen=True)
class Affine:
    """x' = a*x + b*y + c ; y' = d*x + e*y + f"""

    a: float
    b: float
    c: float
    d: float
    e: float
    f: float

    def apply(self, p: Point) -> Point:
        x, y = p
        return (self.a * x + self.b * y + self.c, self.d * x + self.e * y + self.f)

    def then(self, other: Affine) -> Affine:
        """Compose: first self, then other."""
        return Affine(
            other.a * self.a + other.b * self.d, other.a * self.b + other.b * self.e, other.a * self.c + other.b * self.f + other.c,
            other.d * self.a + other.e * self.d, other.d * self.b + other.e * self.e, other.d * self.c + other.e * self.f + other.f,
        )

    def inverse(self) -> Affine:
        det = self.a * self.e - self.b * self.d
        if abs(det) < 1e-15:
            raise ValueError("transform is singular")
        a, b, d, e = self.e / det, -self.b / det, -self.d / det, self.a / det
        return Affine(a, b, -(a * self.c + b * self.f), d, e, -(d * self.c + e * self.f))

    def scale_factor(self) -> float:
        return math.sqrt(abs(self.a * self.e - self.b * self.d))

    def as_list(self) -> list[float]:
        return [self.a, self.b, self.c, self.d, self.e, self.f]

    @staticmethod
    def from_list(v: list[float]) -> Affine:
        return Affine(*v)


def page_to_render(dpi: int) -> Affine:
    s = dpi / 72.0
    return Affine(s, 0, 0, 0, s, 0)


def render_to_crop(rx0: int, ry0: int) -> Affine:
    return Affine(1, 0, -rx0, 0, 1, -ry0)


def crop_to_page(rx0: int, ry0: int, dpi: int) -> Affine:
    return render_to_crop(rx0, ry0).inverse().then(page_to_render(dpi).inverse())


@dataclass(frozen=True)
class Calibration:
    transform: Affine
    residuals: tuple[float, ...]
    rms: float
    method: str


def fit_similarity(src: list[Point], dst: list[Point], *, flip_y: bool = True) -> Calibration:
    """Least-squares similarity (scale, rotation, translation; optional y flip) from control points.

    Two points determine it exactly; more points yield residuals. Page y points
    down, site y points up, so ``flip_y`` mirrors before fitting.
    """
    if len(src) != len(dst) or len(src) < 2:
        raise ValueError("need at least two matching control points")
    s = np.array([(x, -y if flip_y else y) for x, y in src], dtype=float)
    t = np.array(dst, dtype=float)
    # Solve [x -y 1 0; y x 0 1] [a b c d]^T = [x' y'] for x' = a x - b y + c, y' = b x + a y + d.
    rows, rhs = [], []
    for (x, y), (u, v) in zip(s, t, strict=True):
        rows.append([x, -y, 1, 0])
        rhs.append(u)
        rows.append([y, x, 0, 1])
        rhs.append(v)
    sol, *_ = np.linalg.lstsq(np.array(rows), np.array(rhs), rcond=None)
    a, b, c, d = (float(v) for v in sol)
    sim = Affine(a, -b, c, b, a, d)
    flip = Affine(1, 0, 0, 0, -1 if flip_y else 1, 0)
    tf = flip.then(sim)
    res = tuple(math.dist(tf.apply(p), q) for p, q in zip(src, dst, strict=True))
    rms = math.sqrt(sum(r * r for r in res) / len(res))
    return Calibration(tf, res, rms, "similarity_least_squares" + ("_flip_y" if flip_y else ""))
