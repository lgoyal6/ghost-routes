"""Unit normalization. Lengths normalize to metres; other quantities must match exactly."""

from __future__ import annotations

from ghost_routes.domain.model import Scalar

_LENGTH_TO_M = {"m": 1.0, "cm": 0.01, "mm": 0.001, "ft": 0.3048, "in": 0.0254}
_RATIO = {"%": 0.01, "percent": 0.01, "ratio": 1.0}


class UnitError(ValueError):
    pass


def normalize(value: Scalar, unit: str | None) -> tuple[Scalar, str | None]:
    """Return ``(value, canonical_unit)``. Non-numeric values pass through."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        if unit not in (None, ""):
            raise UnitError(f"non-numeric value {value!r} cannot carry unit {unit!r}")
        return value, None
    if unit in (None, ""):
        return float(value), None
    u = unit.strip().lower()
    if u in _LENGTH_TO_M:
        return float(value) * _LENGTH_TO_M[u], "m"
    if u in _RATIO:
        return float(value) * _RATIO[u], "ratio"
    raise UnitError(f"unsupported unit {unit!r}")


def compatible(a: str | None, b: str | None) -> bool:
    return a == b
