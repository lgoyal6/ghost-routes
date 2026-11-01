"""PostgreSQL job queue with leases and fencing tokens.

Claiming uses ``FOR UPDATE SKIP LOCKED``. Every claim increments
``lease_token``; completion, heartbeats and result publication check the token
inside the committing transaction, so a worker whose lease expired (and whose
job was reclaimed) can never overwrite state or publish success.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ghost_routes.domain.canonical import content_hash
from ghost_routes.persistence import schema as S


class LeaseLost(Exception):
    """The caller no longer holds the job's current lease."""


class Cancelled(Exception):
    pass


@dataclass(frozen=True)
class Claim:
    job_id: int
    kind: str
    payload: dict[str, Any]
    token: int
    attempt: int
    owner: str


def enqueue(s: Session, kind: str, payload: dict[str, Any], *, max_attempts: int = 3, identity: Any = None) -> tuple[int, bool]:
    """Insert a job unless an equivalent live or succeeded job exists.

    Returns ``(job_id, created)``. Identity defaults to the canonical payload.
    """
    ih = content_hash({"kind": kind, "identity": identity if identity is not None else payload})
    existing = s.scalar(
        select(S.Job.id).where(S.Job.kind == kind, S.Job.input_hash == ih,
                               S.Job.state.in_(("queued", "running", "succeeded")))
    )
    if existing is not None:
        return existing, False
    job = S.Job(kind=kind, input_hash=ih, payload=payload, max_attempts=max_attempts)
    nested = s.begin_nested()
    try:
        s.add(job)
        s.flush()
        nested.commit()
    except IntegrityError:
        nested.rollback()
        existing = s.scalar(select(S.Job.id).where(S.Job.kind == kind, S.Job.input_hash == ih,
                                                   S.Job.state.in_(("queued", "running", "succeeded"))))
        assert existing is not None
        return existing, False
    return job.id, True


def claim(s: Session, owner: str, lease_s: float, kinds: tuple[str, ...] | None = None) -> Claim | None:
    """Claim one runnable job: queued, or running with an expired lease (crash recovery)."""
    kinds_sql = "AND kind = ANY(:kinds)" if kinds else ""
    row = s.execute(
        text(f"""
        WITH c AS (
          SELECT id FROM jobs
          WHERE ((state = 'queued' AND run_after <= now())
             OR (state = 'running' AND lease_expires_at < now()))
            AND NOT cancel_requested
            {kinds_sql}
          ORDER BY created_at, id
          FOR UPDATE SKIP LOCKED
          LIMIT 1
        )
        UPDATE jobs j SET
          state = 'running',
          attempts = j.attempts + 1,
          lease_owner = :owner,
          lease_token = j.lease_token + 1,
          lease_expires_at = now() + make_interval(secs => :lease),
          heartbeat_at = now(),
          updated_at = now(),
          error = CASE WHEN j.state = 'running' THEN 'previous lease expired; reclaimed' ELSE j.error END
        FROM c WHERE j.id = c.id AND j.attempts < j.max_attempts
        RETURNING j.id, j.kind, j.payload, j.lease_token, j.attempts
        """),
        {"owner": owner, "lease": lease_s, "kinds": list(kinds or ())},
    ).first()
    if row is None:
        _fail_exhausted(s)
        return None
    return Claim(job_id=row.id, kind=row.kind, payload=row.payload, token=row.lease_token, attempt=row.attempts, owner=owner)


def _fail_exhausted(s: Session) -> None:
    s.execute(text("""
        UPDATE jobs SET state = 'failed', finished_at = now(), updated_at = now(),
          error = coalesce(error, '') || ' | attempts exhausted after lease expiry'
        WHERE state = 'running' AND lease_expires_at < now() AND attempts >= max_attempts
    """))


def assert_lease(s: Session, c: Claim) -> S.Job:
    """Lock the job row and verify this claim still holds the current lease."""
    job = s.get(S.Job, c.job_id, with_for_update=True, populate_existing=True)
    if job is None or job.state != "running" or job.lease_token != c.token:
        raise LeaseLost(f"job {c.job_id}: lease token {c.token} is no longer current")
    if job.lease_expires_at is not None and job.lease_expires_at < datetime.now(UTC):
        raise LeaseLost(f"job {c.job_id}: lease expired at {job.lease_expires_at.isoformat()}")
    if job.cancel_requested:
        raise Cancelled(f"job {c.job_id} was cancelled")
    return job


def heartbeat(s: Session, c: Claim, lease_s: float, progress: dict[str, Any] | None = None) -> None:
    job = assert_lease(s, c)
    job.lease_expires_at = datetime.now(UTC) + timedelta(seconds=lease_s)
    job.heartbeat_at = datetime.now(UTC)
    job.updated_at = datetime.now(UTC)
    if progress is not None:
        job.progress = progress


def complete(s: Session, c: Claim, result: dict[str, Any]) -> None:
    """Mark success. Call in the same transaction that committed the outputs."""
    job = assert_lease(s, c)
    job.state = "succeeded"
    job.result = result
    job.finished_at = datetime.now(UTC)
    job.updated_at = datetime.now(UTC)
    job.lease_expires_at = None


def fail(s: Session, c: Claim, error: str, *, retry_delay_s: float = 2.0, permanent: bool = False) -> str:
    job = assert_lease(s, c)
    job.error = error[:4000]
    job.updated_at = datetime.now(UTC)
    job.lease_expires_at = None
    if permanent or job.attempts >= job.max_attempts:
        job.state = "failed"
        job.finished_at = datetime.now(UTC)
    else:
        job.state = "queued"
        job.run_after = datetime.now(UTC) + timedelta(seconds=retry_delay_s * job.attempts)
    return job.state


def mark_cancelled(s: Session, c: Claim) -> None:
    job = s.get(S.Job, c.job_id, with_for_update=True, populate_existing=True)
    if job is not None and job.lease_token == c.token:
        job.state = "cancelled"
        job.finished_at = datetime.now(UTC)
        job.lease_expires_at = None


def request_cancel(s: Session, job_id: int) -> S.Job:
    job = s.get(S.Job, job_id, with_for_update=True)
    if job is None:
        raise KeyError(job_id)
    if job.state == "queued":
        job.state = "cancelled"
        job.finished_at = datetime.now(UTC)
    elif job.state == "running":
        job.cancel_requested = True
    job.updated_at = datetime.now(UTC)
    return job
