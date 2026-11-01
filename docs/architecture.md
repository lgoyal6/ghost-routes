# Architecture

Ghost Routes is a modular monolith. One Python package (`packages/backend/src/ghost_routes`) holds a pure analysis core and the I/O adapters around it; a React/TypeScript studio (`apps/web`) talks to it over a typed HTTP API generated from OpenAPI.

```text
PDF bytes ──► documents (hash, native text/vectors, render, crop transforms)
                 │
                 ▼
            inference (Ollama qwen2.5vl, schema-validated observations)
                 │
                 ▼
            evidence (append-only review decisions, conflicts)
                 │
                 ▼
            network revisions + coverage  ◄── project revisions (phases, restrictions,
                 │                             temporary connections, project coverage)
                 ▼
            scenarios (compile states: phase combos or dated elementary intervals)
                 │
                 ▼
            verification (pure: classify edges, conservative / permissive graphs,
                 │         coverage precedence, explanations, sensitivity)
                 ▼
            optimization (enumeration or CP-SAT master + fresh verifier recheck)
                 │
                 ▼
            bundles (portable ZIP, deterministic replay without DB or model)
```

## Decisions

### ADR-1: pure core, adapters at the edge

`domain/`, `scenarios/`, `verification/`, `optimization/` and `bundles/` import no FastAPI, SQLAlchemy session or HTTP client. The verifier receives a fully materialized `AnalysisInput` and returns typed results. This makes property tests, bundle replay and the API share one code path.

### ADR-2: every fact carries an evidence state

Edge existence, every edge attribute, every restriction and every restriction-to-edge relation carries `ACCEPTED`, `UNRESOLVED` or `CONFLICTING`. A missing attribute is unknown, never a default. Conflicts keep all alternatives until a recorded review decision picks one; neither model confidence nor recency breaks ties.

### ADR-3: two graphs plus executable coverage

For each scenario state the verifier classifies every edge:

| Class | Meaning |
| --- | --- |
| `CONSERVATIVE` | Accepted existence, every hard attribute known, accepted and satisfied, no possibly-active restriction, covered by network coverage and by every active project's reviewed coverage. |
| `POSSIBLE_COVERED` | Could be usable under some resolution of unresolved facts; covered. |
| `POSSIBLE_UNCOVERED` | Could be usable, but lies outside reviewed network coverage or inside an active project's extent that no reviewed sheet covers. |
| `BLOCKED` | A definite fact rules it out (accepted closure, known hard-constraint violation, rejected existence). |

Outcome precedence per origin/destination pair:

1. Route over `CONSERVATIVE` edges within the requirement's budget: `SUPPORTED`.
2. Else route over `CONSERVATIVE ∪ POSSIBLE_COVERED`: `NEEDS_VERIFICATION`, listing each unresolved fact on the route.
3. Else route that needs a `POSSIBLE_UNCOVERED` edge: `INSUFFICIENT_COVERAGE`.
4. Else, if the origin's possible component (or the destination's reverse component) is closed, covered and has no open boundary node: `DISCONNECTED_WITHIN_COVERAGE`; otherwise `INSUFFICIENT_COVERAGE`.

Endpoints outside network coverage short-circuit to `INSUFFICIENT_COVERAGE`. Unrelated gaps outside every candidate route and component do not affect the result.

### ADR-4: scenario states, not samples

Undated phases are never assumed to overlap. A scenario lists explicit states: one phase of one project in isolation, or a declared concurrent combination that is visibly labeled hypothetical. Dated schedules produce elementary intervals between every change boundary (half-open `[start, end)`). Window requirements are checked with an exact interval-set search: a traversed edge must be usable for its whole traversal `[enter, exit)`; the set of feasible departure times is computed symbolically, so short restrictions cannot fall between samples. Window analysis does not model waiting; it reports this as an assumption.

### ADR-5: repairs are hypotheses rechecked by the same verifier

Interventions (schedule shift, enable a reviewed temporary connection, keep a segment open during an interval) are selected by exhaustive enumeration for small option sets and by a CP-SAT master problem with verifier-generated cuts otherwise. Monotone interventions (those that only add capability) produce subset cuts; others produce exact no-good cuts. Every returned candidate is verified again from scratch against every restored and protected requirement. Statuses come from what was actually proven: `OPTIMAL`, `FEASIBLE`, `INFEASIBLE`, `UNKNOWN`, `INVALID_MODEL`. Mixed cost units are rejected rather than summed.

### ADR-6: PostgreSQL is the only service

PostGIS stores local-frame geometry (SRID 0 plus an explicit `frame_id`). Revisions are copy-on-write rows. The job queue is a table claimed with `FOR UPDATE SKIP LOCKED`; every claim increments a `lease_token`, and publication re-checks the token inside the committing transaction so an expired worker cannot publish.

### ADR-7: freshness is transactional

Analysis runs record every input revision in `analysis_inputs`. Superseding a revision (network edit, new plan revision, new profile or requirement version, review decision) marks dependent runs `STALE` with a reason in the same transaction. A worker publishing a run re-checks that its inputs are still current under row locks; if not, the run is stored as historical and stale, never current.

### ADR-8: model output is a proposal, validated against native text

The vision model sees one crop at a time with the crop's native PDF text lines listed by id, and must answer in a fixed JSON schema. `inference/extract.py` then checks every proposal against the page itself:
- Text anchors come from the line ids the model cites. When it cites none, from the native lines its box covers, marked `inferred_from_bbox`; never from the model's own wording.
- A phase reference survives only if the anchored text contains it.
- A dimension value is recorded only if the printed text and the model's label agree.

Every failed check stays on the observation as a flag, and the raw response is stored so observations can be re-derived when the validator changes (`ghost-routes revalidate`, recorded as the validator version).

### ADR-9: the studio is a client of the same API

`apps/web` is a React and TypeScript single-page app. Its types are generated from the API's OpenAPI document (`npm run gen:api`), so enums and shapes are never retyped by hand. It has no state of its own beyond a per-browser reviewer label: every view loads from the API, long work goes through job ids, and job progress comes from the durable job row. Decisions made in the studio are recorded as `human_ui` review under that label.

### ADR-10: the benchmark scores against independent labels

`benchmarks/manifest.json` fixes the inputs (hashes of generated synthetic PDFs), the labels (from the generator's ground truth, never from model output), the split by plan family, the matching tolerances and the acceptance thresholds, plus a history of every change to them. One inference per crop is read two ways, raw and through the validator, so the raw-model and full-pipeline baselines see identical model output. Run reports go to local state, not the repository.
