"""Canonical serialization and content identities.

Identities sort keys, normalize instants to UTC and render floats with
``repr`` so the same logical input always hashes identically.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel


def _normalize(obj: Any) -> Any:
    if isinstance(obj, BaseModel):
        return _normalize(obj.model_dump(mode="python"))
    if isinstance(obj, dict):
        return {str(k): _normalize(v) for k, v in sorted(obj.items(), key=lambda kv: str(kv[0]))}
    if isinstance(obj, (list, tuple)):
        items = [_normalize(v) for v in obj]
        # Collections of identified entities are sets; sort them so input order
        # never changes an identity. Geometry and other sequences keep order.
        for key in ("id", "project_id"):
            if items and all(isinstance(v, dict) and key in v for v in items):
                return sorted(items, key=lambda v: str(v[key]))
        return items
    if isinstance(obj, Enum):
        return obj.value
    if isinstance(obj, datetime):
        if obj.tzinfo is None:
            raise ValueError("naive datetimes cannot be canonicalized")
        return obj.astimezone(UTC).isoformat().replace("+00:00", "Z")
    if isinstance(obj, float):
        if obj != obj or obj in (float("inf"), float("-inf")):
            raise ValueError("non-finite float in canonical input")
        if obj == int(obj) and abs(obj) < 2**53:
            return int(obj)
        return float(repr(obj))
    return obj


def canonical_json(obj: Any) -> str:
    return json.dumps(_normalize(obj), sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def content_hash(obj: Any) -> str:
    return "sha256:" + hashlib.sha256(canonical_json(obj).encode()).hexdigest()


def bytes_hash(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()
