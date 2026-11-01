# Demo guide

Everything here runs locally. Ashgrove is fictional: its sheets are generated, visibly marked SYNTHETIC, and every review action the seed performs is labeled as a scripted fixture.

## Start

```sh
make setup
make model   # only needed for live extraction
make demo    # PostGIS, migrations, API, worker, studio, and the Ashgrove seed
```

Open `http://127.0.0.1:5177`, and enter a name under "Reviewing as". Decisions you make are recorded under that name as human UI review.

## The flagship story

1. **Sources.** Projects P (stair repair) and Q (utility trench) each have a synthetic sheet. Open "Sheet ASH-P1" to see native text and vector proposals over the rendered sheet. Filter provenance to "native text and vectors" and class to "path" to inspect the seven paths. Selecting one explains which legend stroke style it matches. Use `j` and `k` to move between proposals, and `a` or `r` to accept or reject. These proposals do not silently replace the scripted demonstration network.
2. **Scenarios, then Ashgrove spring works, then Run analysis.** The analysis runs as a durable job. For "Halden Lab to Merrow Library, clear width >= 1.5 m":
   - P alone (08:00 to 09:00 UTC) and Q alone (10:00 to 11:00) are **Supported**.
   - Their overlap (09:00 to 10:00) **Needs verification**.

   The map shows the prior route A-B-C-F as a faded ghost, the X marks for P's stair closure and Q's trench, and the only remaining route running over the unreviewed courtyard path e8.
3. **What to review next.** `edge:e8:existence` ranks first: confirming it would support the route, and rejecting it would disconnect it. Resolve it as "Does not exist", with a rationale. This creates revision 2 and marks revision 1's result stale.
4. **Run analysis on revision 2.** The overlap is now **Disconnected within coverage**, with the blockers listed with their source restrictions.
5. **Repairs.**
   - Allow "keep segment open" for the trench's e7 at cost 4.
   - Allow "build a temporary connection" for the ramp C-F at cost 3, with the side effect that staging closes e6.
   - Restore the lab-to-library requirement and protect "West lawn D to E".

   The search returns **OPTIMAL** at cost 4 (keep e7 open). It rejects the cheaper ramp because it would make the protected D-to-E route worse. Open the branch to see the verified route.
6. **Export bundle** on the analysis, then use **Compare and replay** to upload it. The replay reruns the verifier on the bundled inputs and reports a match.
7. **Revised source.** Import Project Q revision B, which you can generate with `uv run python -c "from ghost_routes.synthetic.plans import project_q_plan; open('ASH-Q1-revB.pdf','wb').write(project_q_plan('B')[0])"`. Upload it on Sources as project `ashgrove-Q`, revision `B`. Dependent analyses and repair searches show as stale, with the reason.

The browser suite (`make test-e2e`) performs exactly this story against a throwaway database.

## Without the studio

```sh
uv run ghost-routes analyze ashgrove-works@1
uv run ghost-routes questions ashgrove-works@1
uv run ghost-routes bundle export <run-id> --out run.zip
uv run ghost-routes bundle replay run.zip
```

## The real plan

`uv run ghost-routes ingest` stores the University of Iowa sheet J.01 by hash. The sheet is used for reference only and is not redistributed. `uv run ghost-routes extract <sheet-id>` then runs the vision model on its six crops. Open the sheet in the studio to review the proposals. Each one shows its native-text anchor and the validator's flags. See the README for what the model did and did not find on this sheet.

For sources ingested before vector extraction existed, run `uv run ghost-routes vectorize <sheet-id>` and then `uv run ghost-routes revalidate <sheet-id>`. Both use stored source/response bytes and do no inference. Revalidation updates only runs without review activity; reviewed proposals and decisions are preserved. On Iowa, vector extraction adds three fence candidates and does not recover a complete pedestrian network.
