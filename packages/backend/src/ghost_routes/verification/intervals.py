"""Exact sets of integer instants (milliseconds), as sorted disjoint half-open intervals."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass


@dataclass(frozen=True)
class IntervalSet:
    spans: tuple[tuple[int, int], ...] = ()

    @staticmethod
    def of(spans: Iterable[tuple[int, int]]) -> IntervalSet:
        items = sorted((a, b) for a, b in spans if b > a)
        merged: list[tuple[int, int]] = []
        for a, b in items:
            if merged and a <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(merged[-1][1], b))
            else:
                merged.append((a, b))
        return IntervalSet(tuple(merged))

    def __bool__(self) -> bool:
        return bool(self.spans)

    def union(self, other: IntervalSet) -> IntervalSet:
        return IntervalSet.of(self.spans + other.spans)

    def intersect(self, other: IntervalSet) -> IntervalSet:
        out: list[tuple[int, int]] = []
        i = j = 0
        a, b = self.spans, other.spans
        while i < len(a) and j < len(b):
            lo = max(a[i][0], b[j][0])
            hi = min(a[i][1], b[j][1])
            if lo < hi:
                out.append((lo, hi))
            if a[i][1] < b[j][1]:
                i += 1
            else:
                j += 1
        return IntervalSet(tuple(out))

    def subtract(self, other: IntervalSet) -> IntervalSet:
        out: list[tuple[int, int]] = []
        for a, b in self.spans:
            cur = a
            for c, d in other.spans:
                if d <= cur or c >= b:
                    continue
                if c > cur:
                    out.append((cur, c))
                cur = max(cur, d)
                if cur >= b:
                    break
            if cur < b:
                out.append((cur, b))
        return IntervalSet(tuple(out))

    def shift(self, delta: int) -> IntervalSet:
        return IntervalSet(tuple((a + delta, b + delta) for a, b in self.spans))

    def covers(self, other: IntervalSet) -> bool:
        return not other.subtract(self)

    def contains(self, t: int) -> bool:
        return any(a <= t < b for a, b in self.spans)


def traversal_departures(available: IntervalSet, offset: int, duration: int) -> IntervalSet:
    """Departure instants t such that ``[t+offset, t+offset+duration)`` is fully available.

    ``available`` must be normalized so adjacent spans are merged; otherwise a
    traversal straddling two abutting open spans would be wrongly rejected.
    """
    if duration < 1:
        raise ValueError("traversal duration must be at least 1 ms")
    spans = []
    for a, b in available.spans:
        if b - a >= duration:
            # t + offset >= a  and  t + offset + duration <= b
            spans.append((a - offset, b - duration - offset + 1))
    return IntervalSet.of(spans)
