# Ghost Routes

Verify how construction phases, overlapping projects, and plan revisions affect pedestrian access to essential destinations.

Ghost Routes turns construction plan sheets into source-linked observations, lets a reviewer confirm a pedestrian network, checks explicit access requirements across phases and dated schedules, and searches for low-cost repairs that a fresh verification run confirms. Results describe the reviewed model and its declared coverage. They are not a statement of real-world safety or legal compliance.

## Status

The backend, the browser studio and the benchmark are implemented and tested. This section lists what was actually run.

| Area | State | Evidence |
| --- | --- | --- |
| Verifier (4 outcomes, coverage, phases, dated schedules, departure windows) | implemented | unit and property tests against independent brute-force oracles |
| Repair search (enumeration and CP-SAT, protected requirements, fresh recheck) | implemented | 7 synthetic repair cases match brute-force optima |
| PostGIS persistence, revisions, review concurrency, stale results, durable jobs | implemented | integration tests on a throwaway database, including lease fencing |
| HTTP API (loopback only, structured errors, OpenAPI) | implemented | API integration tests through the flagship flow |
| PDF ingest, rendering, crops, calibration, native vector proposals | implemented | legend-bound paths and barriers on synthetic sheets; three drawn fence candidates on Iowa J.01 |
| Live vision-model extraction | implemented, run on Iowa | 6 of 6 crops succeeded with `qwen2.5vl:7b` (digest `5ced39df…`) |
| Portable bundles with deterministic replay | implemented | byte-identical re-export; replay matches |
| Web studio (React, TypeScript, types generated from OpenAPI) | implemented | 8 Playwright tests: the flagship workflow with axe checks, plus one real extraction started from the studio |
| Benchmark (5 baselines, fixed manifest) | implemented, live responses cached | native vectors recover the synthetic geometry the model misses; see [docs/benchmark.md](docs/benchmark.md) |

Run on 2026-10-31: `make test` passes 84 tests, `make test-integration` passes 22, `make test-live-model` passes (one real inference, 47 s), and `make test-e2e` passes 8, including the live extraction.

The vector branch was checked again with 93 unit tests, 25 PostGIS integration tests, 8 browser tests without live inference, lint/type checks, a production frontend build, and a cache-only benchmark. No new inference was run during that verification. The earlier live-model results above describe the core build, not a fresh live-model check of this branch.

### What the benchmark shows

The raw model found none of the 28 held-out synthetic path and barrier lines. The pipeline now reads the PDF's own strokes, binds their styles to a nearby legend, and proposes 21 of 21 paths and 7 of 7 barriers with no geometry false positives on those sheets. Barriers get a phase from a leader or a flagged nearby callout. Every proposal still needs review. This is a small generated benchmark, not measured accuracy on unseen real construction plans.

Text extraction still has poor precision: the full pipeline finds every note and 12 of 13 phase-text items, but retains 20 false phase-text proposals. Its correction burden drops from 77 to 34 actions after adding native geometry. On hand-built graphs, the verifier gets 15 of 15 outcomes right, with no false "supported" results. The cache-only benchmark reuses all 24 earlier crop responses and does not call inference.

### What the live model did on the Iowa sheet

The model read the sheet: its boxes line up with the printed notes. But it never filled in the line ids it was asked to cite. The validator therefore anchors uncited text proposals to the native PDF lines inside their box, and marks those anchors `inferred_from_bbox`. Of the original 71 validated proposals, 59 were anchored this way and 12 had no text anchor. 25 kept a phase reference their text supported, and 39 phase references were rejected as unsupported. The model proposed only 3 paths and 2 barriers. Native vector extraction separately finds three fence candidates from lines interleaved with X symbols, but no paths. These counts are not real-plan accuracy measurements. Most of the Iowa network still needs a reviewer.

## Quick start

Requirements: Docker, [uv](https://docs.astral.sh/uv/), and [Ollama](https://ollama.com) for live extraction. No cloud API key, account, or Hugging Face token is needed.

```sh
make setup   # locked Python dependencies, a local .env with a random DB password
make model   # pull qwen2.5vl:7b (about 6 GB) and print its digest
make demo    # PostGIS, migrations, API and worker, then the fictional Ashgrove demo
```

The API serves interactive docs at `http://127.0.0.1:8077/docs`. `make stop` stops only this project's processes and its database container.

### Command reference

| Command | What it does |
| --- | --- |
| `make test` | unit and property checks; no services |
| `make test-integration` | real PostGIS, migrations, jobs and API |
| `make test-live-model` | one real inference on a synthetic sheet; fails, never skips, if Ollama or the model is missing |
| `make lint` | ruff and mypy |
| `make test-e2e` | browser suite on an isolated stack, including one live extraction from the studio (`npm --prefix apps/web run e2e:offline` skips that one explicitly) |
| `make benchmark` | fixed-manifest benchmark with all five baselines; `uv run ghost-routes benchmark --no-live` runs the three that need no model |
| `uv run ghost-routes benchmark --cached-only` | evaluate stored model responses; fail on a cache miss without inference; reads the installed model digest from Ollama |
| `make verify` | every required check; fails if any is missing |
| `uv run ghost-routes ingest <pdf> ...` | hash, render and crop a sheet; store native text and vector proposals |
| `uv run ghost-routes vectorize <sheet>` | add native path/barrier proposals to a previously ingested sheet; no inference |
| `uv run ghost-routes extract <sheet>` | run live extraction on every crop of a sheet; reruns reuse finished crops |
| `uv run ghost-routes revalidate <sheet>` | re-derive pending observations from stored responses and the current geometry policy; preserve runs with review activity; no inference |
| `uv run ghost-routes analyze <scenario-revision>` | queue and run an analysis |
| `uv run ghost-routes bundle export\|replay\|import\|synthetic` | portable result bundles |

### Live-model states

`uv run ghost-routes readiness` reports one of the following:
- `ready`: the server is up and the model is installed. The digest is printed.
- `server_unavailable`: Ollama is not reachable. Start `ollama serve`, or set `GHOST_ROUTES_OLLAMA_URL`. From inside a container, use `http://host.docker.internal:11434`, because `localhost` there is the container.
- `model_not_pulled`: run `make model`.

## The studio

`make demo` serves the studio at `http://127.0.0.1:5177`. It has five connected views:
- **Sources:** imports, plan revision lineage, and supersession.
- **Sheet review:** the rendered sheet with every observation overlaid. You can accept, reject or edit each one (keys `j`, `k`, `a`, `r`), with the validator's flags in plain language, the decision history, live extraction runs, and calibration.
- **Scenario workspace:** project toggles, analysis jobs with their real job state, outcomes by requirement and by state, and the network map. The prior route stays visible as a muted ghost while closures cut it. The workspace also shows blockers and missing evidence, and ranks the questions worth reviewing next, each of which can be resolved in place.
- **Repairs:** you build the allowed interventions and see each candidate's recheck against protected requirements.
- **Compare and replay:** revision diffs, and bundle replay.

Decisions made in the studio are attributed to the "Reviewing as" label, as human UI review.

`make test-e2e` runs the browser suite against its own throwaway database (`scripts/e2e_stack.py`) on ports 8078 and 5178, so test review actions never reach your data.

## The demo

Ashgrove is a fictional campus with two overlapping projects. Each project alone keeps the Halden Lab to Merrow Library route open. Together they leave only an unreviewed courtyard path, so the result is NEEDS_VERIFICATION. Rejecting that path in review makes it DISCONNECTED_WITHIN_COVERAGE. The repair search then picks "keep the E-F walk open" (cost 4). It rejects the cheaper temporary ramp because the ramp's staging would close a protected route. All demo review actions are labeled as a scripted fixture, never as human review.

## Documentation

- [docs/demo.md](docs/demo.md): the flagship walkthrough, step by step
- [docs/architecture.md](docs/architecture.md): module layout and architecture decisions
- [docs/benchmark.md](docs/benchmark.md): fixed inputs, baselines, metrics and declared thresholds
- [docs/iowa-source-check.md](docs/iowa-source-check.md): where the expected content sits on the real sheet, and its scale discrepancy
- [docs/limitations.md](docs/limitations.md): what results mean, and what is not verified
- [BUILD_PLAN.md](BUILD_PLAN.md): the implementation contract
- [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) and `third_party_manifest.json`: licenses, model identity, data provenance

## License

Original code and documentation are MIT licensed. Third-party code, model weights, map data and source plan documents keep their own licenses; see the notices.
