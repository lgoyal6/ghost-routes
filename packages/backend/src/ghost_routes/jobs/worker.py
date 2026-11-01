"""Worker loop: claim, run handler, publish under the lease fence."""

from __future__ import annotations

import logging
import os
import socket
import time
import traceback
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from ghost_routes.config import get_settings
from ghost_routes.jobs import queue
from ghost_routes.persistence.db import transaction

log = logging.getLogger("ghost_routes.worker")


class PermanentError(Exception):
    """Retrying cannot succeed (invalid input)."""


@dataclass
class JobContext:
    claim: queue.Claim
    lease_s: float
    db_url: str | None = None

    def heartbeat(self, progress: dict[str, Any] | None = None) -> None:
        with transaction(self.db_url) as s:
            queue.heartbeat(s, self.claim, self.lease_s, progress)

    def fence(self, s: Session) -> None:
        queue.assert_lease(s, self.claim)

    def publish(self, write: Callable[[Session], dict[str, Any]], *, final: bool = True) -> dict[str, Any]:
        """Write outputs and (optionally) mark success atomically under the fence."""
        with transaction(self.db_url) as s:
            queue.assert_lease(s, self.claim)
            result = write(s)
            if final:
                queue.complete(s, self.claim, result)
            return result


Handler = Callable[[JobContext], None]


def default_handlers() -> dict[str, Handler]:
    from ghost_routes.services import analysis, extraction, repairs

    return {
        "analyze": analysis.handle_analyze_job,
        "repair": repairs.handle_repair_job,
        "extract": extraction.handle_extract_job,
    }


class Worker:
    def __init__(self, handlers: dict[str, Handler] | None = None, *, owner: str | None = None,
                 lease_s: float | None = None, db_url: str | None = None, kinds: tuple[str, ...] | None = None):
        self.handlers = handlers or default_handlers()
        self.owner = owner or f"{socket.gethostname()}:{os.getpid()}"
        self.lease_s = lease_s or get_settings().job_lease_s
        self.db_url = db_url
        self.kinds = kinds

    def run_once(self) -> bool:
        with transaction(self.db_url) as s:
            c = queue.claim(s, self.owner, self.lease_s, self.kinds)
        if c is None:
            return False
        ctx = JobContext(c, self.lease_s, self.db_url)
        handler = self.handlers.get(c.kind)
        t0 = time.monotonic()
        log.info("job claimed", extra={"job_id": c.job_id, "kind": c.kind, "attempt": c.attempt, "token": c.token})
        try:
            if handler is None:
                raise PermanentError(f"no handler for job kind {c.kind}")
            handler(ctx)
        except queue.LeaseLost as exc:
            log.warning("lease lost; discarding outputs", extra={"job_id": c.job_id, "error": str(exc)})
        except queue.Cancelled:
            with transaction(self.db_url) as s:
                queue.mark_cancelled(s, c)
        except Exception as exc:  # noqa: BLE001 - recorded on the job
            permanent = isinstance(exc, PermanentError)
            detail = f"{type(exc).__name__}: {exc}\n{traceback.format_exc(limit=6)}"
            try:
                with transaction(self.db_url) as s:
                    state = queue.fail(s, c, detail, permanent=permanent)
                log.error("job failed", extra={"job_id": c.job_id, "state": state, "error": str(exc)})
            except queue.LeaseLost:
                log.warning("lease lost while recording failure", extra={"job_id": c.job_id})
        finally:
            log.info("job finished", extra={"job_id": c.job_id, "ms": int((time.monotonic() - t0) * 1000)})
        return True

    def run_forever(self, idle_sleep: float = 0.5, stop: Callable[[], bool] | None = None) -> None:
        while not (stop and stop()):
            try:
                busy = self.run_once()
            except Exception:
                log.exception("worker loop error")
                busy = False
            if not busy:
                time.sleep(idle_sleep)
