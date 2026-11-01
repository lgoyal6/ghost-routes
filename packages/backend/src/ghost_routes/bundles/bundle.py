"""Portable analysis bundles: write, validate and replay without a database or model.

A bundle is a ZIP with fixed entry timestamps and sorted members, so the same
inputs always produce byte-identical archives.
"""

from __future__ import annotations

import hashlib
import json
import zipfile
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any

from ghost_routes.domain.canonical import canonical_json
from ghost_routes.domain.model import VERIFIER_VERSION, AnalysisInput
from ghost_routes.domain.results import AnalysisResult
from ghost_routes.verification.verifier import analysis_input_hash, verify

BUNDLE_FORMAT = "ghost-routes-bundle/1"
MAX_MEMBER_BYTES = 50 * 1024 * 1024
MAX_MEMBERS = 500
ZIP_TIME = (1980, 1, 1, 0, 0, 0)
REQUIRED = ("manifest.json", "input.json", "result.json")


class BundleError(ValueError):
    pass


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _safe_name(name: str) -> str:
    p = PurePosixPath(name)
    if p.is_absolute() or ".." in p.parts or name.startswith("\\") or ":" in name or not name or "\x00" in name:
        raise BundleError(f"unsafe member path {name!r}")
    return str(p)


def write_bundle(
    path: Path,
    inp: AnalysisInput,
    result: AnalysisResult,
    *,
    provenance: dict[str, Any],
    created_at: datetime,
    extras: dict[str, Any] | None = None,
    binaries: dict[str, bytes] | None = None,
    omissions: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    if result.input_hash != analysis_input_hash(inp):
        raise BundleError("result does not belong to this input")
    files: dict[str, bytes] = {
        "input.json": canonical_json(inp).encode(),
        "result.json": canonical_json(result).encode(),
    }
    for name, obj in sorted((extras or {}).items()):
        files[_safe_name(name)] = canonical_json(obj).encode()
    for name, data in sorted((binaries or {}).items()):
        files[_safe_name(name)] = data
    manifest = {
        "bundle_format": BUNDLE_FORMAT,
        "verifier_version": VERIFIER_VERSION,
        "canonical_input_hash": result.input_hash,
        "created_at": created_at.isoformat(),
        "provenance": provenance,
        "omissions": omissions or [],
        "files": {k: _sha(v) for k, v in sorted(files.items())},
        "claims": "Replays the recorded model of access; it is not evidence of physical site conditions.",
    }
    files["manifest.json"] = json.dumps(manifest, indent=2, sort_keys=True).encode()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_DEFLATED) as z:
        for name in sorted(files):
            info = zipfile.ZipInfo(name, date_time=ZIP_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            z.writestr(info, files[name])
    tmp.replace(path)
    return manifest


def read_bundle(path: Path) -> tuple[dict[str, Any], dict[str, bytes]]:
    try:
        z = zipfile.ZipFile(path)
    except (zipfile.BadZipFile, OSError) as exc:
        raise BundleError(f"not a readable ZIP: {exc}") from exc
    with z:
        infos = z.infolist()
        if len(infos) > MAX_MEMBERS:
            raise BundleError("too many members")
        files: dict[str, bytes] = {}
        for info in infos:
            name = _safe_name(info.filename)
            if info.is_dir():
                continue
            if (info.external_attr >> 16) & 0o170000 == 0o120000:
                raise BundleError(f"symlink member {name!r} is not allowed")
            if info.file_size > MAX_MEMBER_BYTES:
                raise BundleError(f"member {name!r} is too large")
            if name in files:
                raise BundleError(f"duplicate member {name!r}")
            files[name] = z.read(info)
    for req in REQUIRED:
        if req not in files:
            raise BundleError(f"missing {req}")
    manifest = json.loads(files["manifest.json"])
    if manifest.get("bundle_format") != BUNDLE_FORMAT:
        raise BundleError(f"unsupported bundle format {manifest.get('bundle_format')!r}")
    vv = str(manifest.get("verifier_version", ""))
    if vv.split(".")[0] != VERIFIER_VERSION.split(".")[0]:
        raise BundleError(f"bundle verifier {vv} is incompatible with {VERIFIER_VERSION}")
    listed = manifest.get("files") or {}
    for name, data in files.items():
        if name == "manifest.json":
            continue
        if name not in listed:
            raise BundleError(f"member {name!r} is not listed in the manifest")
        if _sha(data) != listed[name]:
            raise BundleError(f"hash mismatch for {name!r} (tampered or corrupted)")
    for name in listed:
        if name not in files:
            raise BundleError(f"manifest lists missing member {name!r}")
    return manifest, files


def replay(path: Path) -> dict[str, Any]:
    manifest, files = read_bundle(path)
    inp = AnalysisInput.model_validate_json(files["input.json"])
    recorded = AnalysisResult.model_validate_json(files["result.json"])
    ih = analysis_input_hash(inp)
    if ih != manifest["canonical_input_hash"] or recorded.input_hash != ih:
        raise BundleError("canonical input hash does not match the manifest")
    fresh = verify(inp)
    matches = canonical_json(fresh) == canonical_json(recorded)
    diffs = []
    if not matches:
        rec = {r.requirement_id: r.outcome.value for r in recorded.requirements}
        new = {r.requirement_id: r.outcome.value for r in fresh.requirements}
        diffs = [{"requirement": k, "recorded": rec.get(k), "replayed": new.get(k)} for k in sorted(set(rec) | set(new))
                 if rec.get(k) != new.get(k)] or [{"detail": "outcomes equal; evidence or route detail differs"}]
    return {
        "matches": matches,
        "canonical_input_hash": ih,
        "verifier_version": VERIFIER_VERSION,
        "recorded_verifier_version": recorded.verifier_version,
        "outcomes": {r.requirement_id: r.outcome.value for r in fresh.requirements},
        "differences": diffs,
        "omissions": manifest.get("omissions", []),
        "provenance": manifest.get("provenance", {}),
    }
