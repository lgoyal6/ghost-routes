# Limitations

## What a result means

A result describes the reviewed model: its network, coverage, phases, restrictions and profiles. It is not a finding about the physical site, and not a determination of legal compliance. Thresholds in the demo profiles are illustrative and marked as such. Applicable accessibility requirements depend on the jurisdiction and on reviewed criteria.

## Extraction

- On the Iowa sheet, the vision model (`qwen2.5vl:7b`, Q4_K_M) never cited the line ids it was asked for. Text anchors there are inferred from boxes and marked as such. It proposed only 3 path and 2 barrier geometries for the whole sheet, so most of a real network still has to be drawn by a reviewer.
- Crops overlap and are processed independently, so the same note can be proposed more than once. Duplicates are left for review rather than merged silently.
- Inference runs one crop at a time on the local machine, roughly a minute per crop on the development laptop.
- The Iowa sheet has no human-checked labels, so its extraction is reported as counts, not accuracy.

## Analysis

- Window requirements assume walking without waiting. The assumption is reported on each result.
- Undated phases are never assumed to overlap. Concurrency must be declared, and is then labeled hypothetical.
- The repair search only considers interventions the reviewer allows. Costs must share one unit, and mixed units are rejected rather than summed.

## Verification gaps at the time of writing

- `make test-live-model`, the live browser test and the live benchmark baselines need a running Ollama with the model. They fail rather than skip when it is missing.
- On the benchmark, the model's path and barrier geometry found none of the 28 labeled lines. A usable network still has to be drawn in review.
- The declared benchmark thresholds did not cover precision or geometry. Both are recorded as gaps for the next declaration.
- No Iowa network or scenario exists yet, because none of the Iowa proposals has been reviewed by a person.
- `npm audit` reports no advisories for the studio's runtime dependencies. Advisories remain in development tooling: Vite 5's esbuild dev server, Playwright 1.49, minimatch and an eslint plugin. These affect the local dev and test environment only, and the dev server binds to loopback. Upgrading Vite and Playwright across major versions is pending.
- Accessibility is checked with axe in the browser suite, which also exercises the sheet-review keyboard shortcuts. A complete keyboard-only pass through every view, and testing with assistive-technology users, have not been done.
