"""Real PostGIS behaviour: revisions, concurrency, invalidation, fencing and jobs."""

from __future__ import annotations

import threading
import time

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.exc import DBAPIError

from ghost_routes.domain.model import Outcome
from ghost_routes.jobs import queue
from ghost_routes.jobs.worker import JobContext, Worker
from ghost_routes.persistence import repo
from ghost_routes.persistence import schema as S
from ghost_routes.persistence.db import transaction
from ghost_routes.services import analysis
from ghost_routes.services.seed import seed_input
from ghost_routes.synthetic import cases as C
from ghost_routes.verification.verifier import verify

pytestmark = pytest.mark.integration


def seed(url: str, case: C.Case | None = None, ns: str = "t"):  # type: ignore[no-untyped-def]
    case = case or C.case_joint_disconnection()
    with transaction(url) as s:
        return seed_input(s, case.input, namespace=ns, title=case.name)


def test_migrations_are_current_and_reversible(db_url: str) -> None:
    from alembic import command
    from alembic.config import Config  # noqa: F401

    from tests.integration.conftest import alembic_cfg

    cfg = alembic_cfg(db_url)
    command.check(cfg)
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")
    with transaction(db_url) as s:
        assert s.execute(text("SELECT postgis_lib_version()")).scalar()


def test_network_roundtrip_preserves_geometry_and_evidence(clean: str) -> None:
    case = C.case_conflicting_note()
    sd = seed(clean, case)
    with transaction(clean) as s:
        net = repo.load_network(s, sd.network_revision_id)
    orig = case.input.network
    assert {n.id: (n.x, n.y) for n in net.nodes} == {n.id: (n.x, n.y) for n in orig.nodes}
    e7 = next(e for e in net.edges if e.id == "e7")
    assert e7.attributes["clear_width"].alternatives == (1.2, 1.8)
    assert net.frame == orig.frame
    with transaction(clean) as s:
        inp = repo.build_input(s, sd.scenario_revision_id)
    assert verify(inp).requirements[0].outcome == Outcome.NEEDS_VERIFICATION


def test_postgis_spatial_index_answers_intersections(clean: str) -> None:
    sd = seed(clean, C.case_unreviewed_intersection())
    with transaction(clean) as s:
        rows = s.execute(text("""
            SELECT e.edge_id FROM restrictions r
            JOIN network_edges e ON ST_Intersects(r.polygon, e.geom)
            WHERE r.project_revision_id = :p AND e.revision_id = :n ORDER BY e.edge_id
        """), {"p": next(iter(sd.project_revision_ids.values())), "n": sd.network_revision_id}).scalars().all()
    assert rows == ["e5", "e7"]


def test_outdated_network_edit_is_a_structured_conflict(clean: str) -> None:
    sd = seed(clean)
    with transaction(clean) as s:
        net = repo.load_network(s, sd.network_revision_id)
        repo.commit_network_revision(s, sd.lineage_id, net.nodes, net.edges, net.coverage, author="a",
                                     author_kind="human_cli", note="edit 1", expected_head=1)
    with pytest.raises(repo.Conflict) as ei, transaction(clean) as s:
        repo.commit_network_revision(s, sd.lineage_id, net.nodes, net.edges, net.coverage, author="b",
                                     author_kind="human_cli", note="edit from stale view", expected_head=1)
    assert ei.value.detail["current_revision"] == 2


def _run_analysis(url: str, srid: str) -> S.AnalysisRun:
    with transaction(url) as s:
        job_id, _, _ = analysis.request_analysis(s, srid)
    assert Worker(db_url=url, kinds=("analyze",)).run_once()
    with transaction(url) as s:
        job = s.get(S.Job, job_id)
        assert job is not None and job.state == "succeeded", job.error if job else None
        run = s.get(S.AnalysisRun, job.result["run_id"])  # type: ignore[index]
        assert run is not None
        return run


def test_new_network_revision_invalidates_dependent_runs(clean: str) -> None:
    sd = seed(clean)
    run = _run_analysis(clean, sd.scenario_revision_id)
    assert run.freshness == "current"
    with transaction(clean) as s:
        net = repo.load_network(s, sd.network_revision_id)
        repo.commit_network_revision(s, sd.lineage_id, net.nodes, net.edges, net.coverage, author="rev",
                                     author_kind="human_cli", note="survey update", expected_head=1)
    with transaction(clean) as s:
        r = s.get(S.AnalysisRun, run.id)
        assert r is not None and r.freshness == "stale"
        assert "survey update" in (r.stale_reason or "")
        assert r.result is not None  # history preserved


def test_profile_revision_invalidates(clean: str) -> None:
    sd = seed(clean)
    run = _run_analysis(clean, sd.scenario_revision_id)
    with transaction(clean) as s:
        prof = repo.latest_profile(s, "any-walker")
        repo.save_profile(s, prof.model_copy(update={"description": "clarified"}))
    with transaction(clean) as s:
        r = s.get(S.AnalysisRun, run.id)
        assert r is not None and r.freshness == "stale" and "profile any-walker" in (r.stale_reason or "")


def test_run_computed_on_superseded_inputs_publishes_stale(clean: str) -> None:
    """A slow analysis whose inputs move before publication is never current."""
    sd = seed(clean)
    with transaction(clean) as s:
        job_id, _, _ = analysis.request_analysis(s, sd.scenario_revision_id)
    with transaction(clean) as s:
        c = queue.claim(s, "slow", 60)
    assert c is not None
    with transaction(clean) as s:
        inp = repo.build_input(s, sd.scenario_revision_id)
    result = verify(inp)
    # Meanwhile a reviewer edits the network.
    with transaction(clean) as s:
        net = repo.load_network(s, sd.network_revision_id)
        repo.commit_network_revision(s, sd.lineage_id, net.nodes, net.edges, net.coverage, author="rev",
                                     author_kind="human_ui", note="concurrent edit", expected_head=1)
    ctx = JobContext(c, 60, clean)
    out = ctx.publish(lambda s: {"run_id": repo.publish_analysis(
        s, run_id="run_late", scenario_revision_id=sd.scenario_revision_id, inp=inp, result=result,
        timings={}, job_id=job_id).freshness})
    assert out["run_id"] == "stale"
    with transaction(clean) as s:
        r = s.get(S.AnalysisRun, "run_late")
        assert r is not None and r.freshness == "stale" and "network_lineage" in (r.stale_reason or "")


def test_expired_lease_cannot_publish_after_retry_succeeds(clean: str) -> None:
    """Forced fencing scenario: slow worker outlives its lease, a retry completes, the old worker returns."""
    sd = seed(clean)
    with transaction(clean) as s:
        job_id, _, _ = analysis.request_analysis(s, sd.scenario_revision_id)
    with transaction(clean) as s:
        old = queue.claim(s, "old-worker", 0.5)
    assert old is not None and old.token == 1
    time.sleep(0.8)  # the old worker is still "inferring" when its lease expires
    assert Worker(db_url=clean, owner="retry-worker", lease_s=60, kinds=("analyze",)).run_once()
    with transaction(clean) as s:
        job = s.get(S.Job, job_id)
        assert job is not None and job.state == "succeeded" and job.lease_token == 2
        good_run = job.result["run_id"]  # type: ignore[index]
    with transaction(clean) as s:
        inp = repo.build_input(s, sd.scenario_revision_id)
    ctx = JobContext(old, 0.5, clean)
    with pytest.raises(queue.LeaseLost):
        ctx.publish(lambda s: {"run_id": repo.publish_analysis(
            s, run_id="run_zombie", scenario_revision_id=sd.scenario_revision_id, inp=inp, result=verify(inp),
            timings={}, job_id=job_id).id})
    with transaction(clean) as s:
        assert s.get(S.AnalysisRun, "run_zombie") is None
        job = s.get(S.Job, job_id)
        assert job is not None and job.result["run_id"] == good_run and job.state == "succeeded"  # type: ignore[index]


def test_enqueue_is_idempotent_and_claims_do_not_overlap(clean: str) -> None:
    with transaction(clean) as s:
        a, created_a = queue.enqueue(s, "noop", {"x": 1})
        b, created_b = queue.enqueue(s, "noop", {"x": 1})
        for i in range(10):
            queue.enqueue(s, "noop", {"x": 100 + i})
    assert a == b and created_a and not created_b
    claimed: list[int] = []
    lock = threading.Lock()

    def grab() -> None:
        while True:
            with transaction(clean) as s:
                c = queue.claim(s, threading.current_thread().name, 30, ("noop",))
            if c is None:
                return
            with lock:
                claimed.append(c.job_id)

    threads = [threading.Thread(target=grab, name=f"w{i}") for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(claimed) == sorted(set(claimed)) and len(claimed) == 11


def test_crashed_worker_job_is_recovered_and_retries_are_bounded(clean: str) -> None:
    with transaction(clean) as s:
        jid, _ = queue.enqueue(s, "boom", {"n": 1}, max_attempts=2)
    calls: list[int] = []

    def boom(ctx: JobContext) -> None:
        calls.append(ctx.claim.attempt)
        raise RuntimeError("handler exploded")

    w = Worker({"boom": boom}, db_url=clean, kinds=("boom",), lease_s=30)
    assert w.run_once()
    with transaction(clean) as s:
        s.execute(text("UPDATE jobs SET run_after = now() WHERE id = :i"), {"i": jid})
    assert w.run_once()
    with transaction(clean) as s:
        job = s.get(S.Job, jid)
        assert job is not None and job.state == "failed" and job.attempts == 2 and "handler exploded" in (job.error or "")
    assert calls == [1, 2]
    # Crash recovery: a running job whose worker died is reclaimed after its lease.
    with transaction(clean) as s:
        jid2, _ = queue.enqueue(s, "ok", {"n": 2})
        c = queue.claim(s, "dead-worker", 0.2, ("ok",))
    assert c is not None
    time.sleep(0.4)
    done: list[int] = []
    w2 = Worker({"ok": lambda ctx: (done.append(ctx.claim.token), ctx.publish(lambda s: {"ok": True}))[1] and None},
                db_url=clean, kinds=("ok",), lease_s=30)
    assert w2.run_once()
    with transaction(clean) as s:
        job = s.get(S.Job, jid2)
        assert job is not None and job.state == "succeeded" and done == [2]


def test_cancellation(clean: str) -> None:
    with transaction(clean) as s:
        jid, _ = queue.enqueue(s, "slow", {"n": 1})
    def slow(ctx: JobContext) -> None:
        with transaction(clean) as s:
            queue.request_cancel(s, ctx.claim.job_id)
        ctx.heartbeat({"stage": "checking"})
    assert Worker({"slow": slow}, db_url=clean, kinds=("slow",)).run_once()
    with transaction(clean) as s:
        job = s.get(S.Job, jid)
        assert job is not None and job.state == "cancelled"


def test_review_decisions_are_append_only(clean: str) -> None:
    eng = create_engine(clean)
    with eng.begin() as c:
        c.execute(text("""INSERT INTO projects(id,title,description,synthetic,hypothetical) VALUES ('p','p','',true,false);
            INSERT INTO source_artifacts(id,sha256,mime,size_bytes,original_filename,storage_path,reuse_status,page_count,synthetic,metadata)
              VALUES ('a',repeat('0',64),'application/pdf',1,'x.pdf','/x','synthetic_original',1,true,'{}');
            INSERT INTO plan_revisions(id,project_id,source_artifact_id,revision_label,note) VALUES ('pr','p','a','A','');
            INSERT INTO sheets(id,plan_revision_id,page_index,width_pt,height_pt,rotation,render_dpi,render_width_px,render_height_px,render_path,native_words,vector_summary)
              VALUES ('sh','pr',0,10,10,0,72,10,10,'/r','[]','{}');
            INSERT INTO observations(id,idempotency_key,sheet_id,provenance,cls,label,geom,bbox,properties,phase_refs,supporting_text,uncertainty,review_state,current_value,revision)
              VALUES ('o','k','sh','human','path','', ST_GeomFromText('POINT(1 1)',0), ST_GeomFromText('POLYGON((0 0,1 0,1 1,0 1,0 0))',0),'{}','{}','','[]','pending','{}',0);
            INSERT INTO review_decisions(observation_id,action,proposal_snapshot,reviewer,reviewer_kind,rationale,expected_revision,resulting_revision)
              VALUES ('o','accept','{}','r','human_cli','',0,1);"""))
    with pytest.raises(DBAPIError, match="append-only"), eng.begin() as c:
        c.execute(text("UPDATE review_decisions SET rationale='rewritten'"))
    with pytest.raises(DBAPIError, match="append-only"), eng.begin() as c:
        c.execute(text("DELETE FROM review_decisions"))
    with eng.begin() as c:
        assert c.execute(select(S.ReviewDecision.rationale)).scalar() == ""
    eng.dispose()
