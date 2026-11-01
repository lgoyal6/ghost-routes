"""Readiness: each dependency reported separately, never collapsed into one flag."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select, text

from ghost_routes.config import get_settings
from ghost_routes.inference.ollama import ModelState, OllamaClient
from ghost_routes.persistence import schema as S
from ghost_routes.persistence.db import migrations_current, transaction


def readiness(deep: bool = False) -> dict[str, Any]:
    cfg = get_settings()
    out: dict[str, Any] = {"checked_at": datetime.now(UTC).isoformat()}
    try:
        with transaction() as s:
            s.execute(text("SELECT 1"))
            postgis = s.execute(text("SELECT postgis_lib_version()")).scalar()
            ok, cur = migrations_current(s)
            out["database"] = {"ok": True, "postgis": postgis}
            out["migrations"] = {"ok": ok, "current": cur}
            recent = datetime.now(UTC) - timedelta(minutes=10)
            beats = s.scalar(select(func.max(S.Job.heartbeat_at)))
            queued = s.scalar(select(func.count()).select_from(S.Job).where(S.Job.state == "queued")) or 0
            running = s.scalar(select(func.count()).select_from(S.Job).where(S.Job.state == "running")) or 0
            out["worker"] = {"last_heartbeat": beats.isoformat() if beats else None,
                             "recently_active": bool(beats and beats > recent), "queued": queued, "running": running}
            last = s.scalars(select(S.ExtractionRun).where(S.ExtractionRun.status.in_(("succeeded", "empty")))
                             .order_by(S.ExtractionRun.created_at.desc()).limit(1)).first()
            out["last_successful_inference"] = (
                {"run_id": last.id, "model": last.model, "digest": last.model_digest, "at": last.created_at.isoformat(),
                 "status": last.status} if last else None)
    except Exception as exc:  # noqa: BLE001 - reported, not raised
        out["database"] = {"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:300]}"}
        out["migrations"] = {"ok": False}
        out["worker"] = {"recently_active": False}
        out["last_successful_inference"] = None
    st = OllamaClient(cfg.ollama_url, cfg.model).status()
    out["model"] = {"state": st.state.value, "model": st.model, "digest": st.digest, "server_version": st.server_version,
                    "details": st.details, "message": st.message, "url": cfg.ollama_url}
    out["ready_for_analysis"] = bool(out["database"].get("ok") and out["migrations"].get("ok"))
    out["ready_for_live_inference"] = out["ready_for_analysis"] and st.state == ModelState.READY
    _ = deep
    return out
