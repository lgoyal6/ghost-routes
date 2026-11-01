"""Command line interface. Uses the same services as the HTTP API."""

from __future__ import annotations

import json
import logging
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import typer

from ghost_routes.config import get_settings

app = typer.Typer(no_args_is_help=True, add_completion=False, help="Ghost Routes: verify pedestrian access through construction.")
db_app = typer.Typer(no_args_is_help=True, help="Database migrations.")
bundle_app = typer.Typer(no_args_is_help=True, help="Portable analysis bundles.")
app.add_typer(db_app, name="db")
app.add_typer(bundle_app, name="bundle")


def _out(obj: Any) -> None:
    typer.echo(json.dumps(obj, indent=2, default=str))


def _alembic_cfg():  # type: ignore[no-untyped-def]
    from alembic.config import Config

    from ghost_routes.persistence import migrations

    cfg = Config()
    cfg.set_main_option("script_location", str(migrations.__path__[0]))
    return cfg


@db_app.command("upgrade")
def db_upgrade() -> None:
    """Apply migrations to head."""
    from alembic import command

    command.upgrade(_alembic_cfg(), "head")
    typer.echo("migrations at head")


@db_app.command("check")
def db_check() -> None:
    """Fail if the schema and models have drifted."""
    from alembic import command

    command.check(_alembic_cfg())
    typer.echo("no schema drift")


@app.command()
def readiness() -> None:
    """Report database, migrations, worker activity and model readiness separately."""
    from ghost_routes.services.health import readiness as r

    rep = r(deep=True)
    _out(rep)
    if not rep["ready_for_live_inference"]:
        raise typer.Exit(2)


@app.command()
def worker(once: bool = typer.Option(False, help="Process at most one job."),
           until_idle: bool = typer.Option(False, help="Exit when the queue is empty."),
           kinds: str = typer.Option("", help="Comma-separated job kinds to accept.")) -> None:
    """Run the durable job worker."""
    from ghost_routes.jobs.worker import Worker

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    w = Worker(kinds=tuple(k for k in kinds.split(",") if k) or None)
    if once:
        w.run_once()
        return
    if until_idle:
        while w.run_once():
            pass
        return
    w.run_forever()


@app.command()
def ingest(
    pdf: Path,
    project_id: str = typer.Option(...),
    project_title: str = typer.Option(...),
    revision: str = typer.Option(..., help="Revision label printed on or assigned to this source."),
    reuse_status: str = typer.Option("unknown"),
    source_url: str | None = typer.Option(None),
    retrieved_at: str | None = typer.Option(None, help="ISO instant the source was fetched."),
    synthetic: bool = typer.Option(False),
) -> None:
    """Import a PDF: store original bytes by hash, render, crop and extract native text."""
    from ghost_routes.persistence.db import transaction
    from ghost_routes.services.documents import ingest_pdf

    data = pdf.read_bytes()
    with transaction() as s:
        res = ingest_pdf(s, data, filename=pdf.name, project_id=project_id, project_title=project_title,
                         revision_label=revision, reuse_status=reuse_status, source_url=source_url,
                         retrieved_at=datetime.fromisoformat(retrieved_at) if retrieved_at else None,
                         synthetic=synthetic)
    _out(res)


@app.command()
def extract(sheet_id: str, crops: str = typer.Option("", help="Comma-separated crop ids (default: all)."),
            run: bool = typer.Option(True, help="Process the job in this process.")) -> None:
    """Run real local vision-model extraction on a sheet's crops."""
    from ghost_routes.jobs.worker import Worker
    from ghost_routes.persistence import schema as S
    from ghost_routes.persistence.db import transaction
    from ghost_routes.services.extraction import ModelUnavailable, request_extraction

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        with transaction() as s:
            job_id, created, model = request_extraction(s, sheet_id, [c for c in crops.split(",") if c] or None)
    except ModelUnavailable as exc:
        typer.echo(f"{exc.state}: {exc}", err=True)
        raise typer.Exit(3) from exc
    typer.echo(f"job {job_id} ({'created' if created else 'existing'}) model={model}")
    if run:
        # Older claimable extract jobs run first (FIFO), so keep going until ours is terminal.
        w = Worker(kinds=("extract",))
        while True:
            with transaction() as s:
                state = s.get(S.Job, job_id).state  # type: ignore[union-attr]
            if state in ("succeeded", "failed", "cancelled") or not w.run_once():
                break
    with transaction() as s:
        job = s.get(S.Job, job_id)
        assert job is not None
        _out({"job_id": job_id, "state": job.state, "result": job.result, "error": job.error})
        if job.state == "running":
            typer.echo(f"job {job_id} is held by another worker until {job.lease_expires_at}", err=True)
        if job.state != "succeeded":
            raise typer.Exit(1)


@app.command()
def revalidate(sheet_id: str) -> None:
    """Re-derive model observations from stored raw responses with the current validator (no inference)."""
    from ghost_routes.persistence.db import transaction
    from ghost_routes.services.extraction import revalidate_sheet

    with transaction() as s:
        _out(revalidate_sheet(s, sheet_id))


@app.command()
def seed(namespace: str = typer.Option("demo")) -> None:
    """Load the intentional synthetic demo (additive; never resets data)."""
    from ghost_routes.services.demo import seed_demo

    _out(seed_demo(namespace))


@app.command()
def analyze(scenario_revision: str, wait: bool = typer.Option(True)) -> None:
    """Enqueue an analysis and (by default) process it here."""
    from ghost_routes.jobs.worker import Worker
    from ghost_routes.persistence import schema as S
    from ghost_routes.persistence.db import transaction
    from ghost_routes.services.analysis import request_analysis

    with transaction() as s:
        job_id, created, ih = request_analysis(s, scenario_revision)
    if wait:
        Worker(kinds=("analyze",)).run_once()
    with transaction() as s:
        job = s.get(S.Job, job_id)
        assert job is not None
        out: dict[str, Any] = {"job_id": job_id, "created": created, "input_hash": ih, "state": job.state}
        if job.result:
            run = s.get(S.AnalysisRun, job.result["run_id"])
            assert run is not None and run.result is not None
            out["run_id"] = run.id
            out["freshness"] = run.freshness
            out["outcomes"] = {r["requirement_id"]: r["outcome"] for r in run.result["requirements"]}
        _out(out)


@app.command()
def questions(scenario_revision: str) -> None:
    """Rank unresolved facts by whether resolving them could change an outcome."""
    from ghost_routes.persistence.db import transaction
    from ghost_routes.services.analysis import questions as q

    with transaction() as s:
        res = q(s, scenario_revision)
    _out([{"fact": x["fact"]["id"], "affects": x["affected"], "score": x["score"]} for x in res["questions"]])


@bundle_app.command("export")
def bundle_export(run_id: str, out: Path = typer.Option(..., "--out", "-o")) -> None:
    """Export an analysis run (with its exact inputs) as a portable ZIP bundle."""
    from ghost_routes.persistence.db import transaction
    from ghost_routes.services.bundles import export_run

    with transaction() as s:
        manifest = export_run(s, run_id, out)
    _out({"bundle": str(out), "manifest": manifest})


@bundle_app.command("replay")
def bundle_replay(path: Path) -> None:
    """Validate a bundle and replay it with the pure verifier (no database or model)."""
    from ghost_routes.bundles.bundle import BundleError, replay

    try:
        rep = replay(path)
    except BundleError as exc:
        typer.echo(f"bundle rejected: {exc}", err=True)
        raise typer.Exit(4) from exc
    _out(rep)
    if not rep["matches"]:
        raise typer.Exit(5)


@bundle_app.command("import")
def bundle_import(path: Path, namespace: str = typer.Option("replay")) -> None:
    """Load a bundle into this (possibly empty) database and recompute its analysis there."""
    from ghost_routes.services.bundles import import_bundle

    _out(import_bundle(path, namespace))


@bundle_app.command("synthetic")
def bundle_synthetic(out: Path = typer.Option(..., "--out", "-o"), case: str = typer.Option("joint_disconnection")) -> None:
    """Deterministically generate a bundle from a synthetic fixture case (no database)."""
    from ghost_routes.bundles.bundle import write_bundle
    from ghost_routes.synthetic.cases import all_cases
    from ghost_routes.verification.verifier import verify

    c = next((x for x in all_cases() if x.name == case), None)
    if c is None:
        typer.echo(f"unknown case {case}; choose from {[x.name for x in all_cases()]}", err=True)
        raise typer.Exit(2)
    manifest = write_bundle(out, c.input, verify(c.input), provenance={"source": "synthetic fixture", "case": case},
                            created_at=datetime(2026, 1, 1, tzinfo=UTC))
    _out({"bundle": str(out), "manifest": manifest})


@app.command()
def benchmark(live: bool = typer.Option(True, help="Run the model baselines (real inference). --no-live reports them as not run."),
              root: Path = typer.Option(Path("benchmarks"), help="Directory holding manifest.json and labels/."),
              write_manifest: bool = typer.Option(False, help="Regenerate the tracked manifest and labels, then exit.")) -> None:
    """Fixed-manifest benchmark: native text, raw VLM, full pipeline and hand-built graph baselines."""
    from ghost_routes.benchmark import manifest as bm
    from ghost_routes.benchmark.run import ModelRequired, run, write_report

    if write_manifest:
        typer.echo(f"wrote {bm.write_manifest(root)}")
        return
    logging.basicConfig(level=logging.WARNING)
    try:
        report = run(root, live=live)
    except ModelRequired as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(3) from exc
    out = write_report(report)
    typer.echo((out / "report.md").read_text())
    typer.echo(f"report: {out}")
    failed = [t["check"] for t in report["thresholds"] if t["met"] is False]
    if failed:
        typer.echo(f"thresholds not met: {', '.join(failed)}", err=True)
        raise typer.Exit(1)


@app.command()
def openapi(out: Path = typer.Option(..., "--out", "-o")) -> None:
    """Write the API's OpenAPI document (the web client's types are generated from it)."""
    from ghost_routes.api.app import create_app

    out.write_text(json.dumps(create_app().openapi(), indent=1, sort_keys=True) + "\n")
    typer.echo(f"wrote {out}")


@app.command()
def serve() -> None:
    """Run the HTTP API on loopback."""
    import uvicorn

    cfg = get_settings()
    if cfg.api_host not in ("127.0.0.1", "localhost", "::1"):
        typer.echo("refusing to bind a no-auth API to a non-loopback address", err=True)
        raise typer.Exit(2)
    uvicorn.run("ghost_routes.api.app:create_app", factory=True, host=cfg.api_host, port=cfg.api_port, log_level="info")


def main() -> None:  # pragma: no cover
    t0 = time.monotonic()
    try:
        app()
    finally:
        if "--timing" in sys.argv:
            typer.echo(f"{time.monotonic() - t0:.2f}s", err=True)
