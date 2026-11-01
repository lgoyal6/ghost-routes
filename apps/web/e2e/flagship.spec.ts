import AxeBuilder from "@axe-core/playwright";
import { expect, test, type Page } from "@playwright/test";
import { execFileSync } from "node:child_process";
import path from "node:path";
import { fileURLToPath } from "node:url";

// One serial story through the real browser, API, worker and a throwaway PostGIS database.
// Review actions here are attributed to "e2e-browser-test" and are dropped with that database.
test.describe.configure({ mode: "serial" });

const REVIEWER = "e2e-browser-test";
const REPO = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../../..");

async function axe(page: Page) {
  const res = await new AxeBuilder({ page }).analyze();
  const bad = res.violations.filter((v) => v.impact === "serious" || v.impact === "critical");
  expect(bad.map((v) => `${v.id}: ${v.nodes.map((n) => n.target.join(" ")).slice(0, 3).join(", ")}`)).toEqual([]);
}

async function runAnalysis(page: Page) {
  await page.getByRole("button", { name: "Run analysis" }).click();
  await expect(page.getByRole("status").filter({ hasText: "Analysis" }).filter({ hasText: "succeeded" })).toBeVisible({ timeout: 30_000 });
}

function overall(page: Page) {
  return page.getByText(/^Overall for/).locator("..");
}

test("cold load, accessibility and structured errors", async ({ page }) => {
  await page.goto("/scenarios");
  await expect(page.getByRole("heading", { level: 1, name: "Scenarios" })).toBeVisible();
  await expect(page.getByText("Database:")).toBeVisible();
  await axe(page);
  await page.goto("/scenario-revisions/missing@9");
  await expect(page.getByRole("alert")).toContainText("Scenario revision not found");
});

test("overlapping projects need verification; each alone is supported", async ({ page }) => {
  await page.goto("/scenario-revisions/ashgrove-works@1");
  await runAnalysis(page);
  await expect(overall(page)).toContainText("ashgrove-lab-to-library");
  await expect(overall(page)).toContainText("Needs verification");
  const rows = page.getByRole("table", { name: "Outcome by state" }).getByRole("row");
  await expect(rows.filter({ hasText: "ashgrove-P: ph1, ashgrove-Q: ph1" })).toContainText("Needs verification");
  await expect(rows.filter({ hasText: "ashgrove-P: ph1" }).filter({ hasNotText: "ashgrove-Q" })).toContainText("Supported");
  await expect(page.getByRole("img", { name: /prior route segments now unused/ })).toBeVisible();
  await axe(page);
});

test("resolving the courtyard path in review recomputes to disconnected and stales the old run", async ({ page }) => {
  await page.goto("/scenario-revisions/ashgrove-works@1");
  await page.getByLabel("Reviewing as").fill(REVIEWER);
  const q = page.getByRole("listitem").filter({ hasText: "edge:e8:existence" });
  await q.getByRole("button", { name: "Resolve this fact" }).click();
  await q.getByLabel("Finding").selectOption({ label: "Does not exist / not walkable" });
  await q.getByLabel(/Rationale/).fill("Browser test: the courtyard path is fenced off.");
  await q.getByRole("button", { name: "Record and create new revision" }).click();
  await expect(page).toHaveURL(/ashgrove-works(@|%40)2/);
  await expect(page.getByRole("status").filter({ hasText: "Recorded the resolution" })).toBeVisible();
  await runAnalysis(page);
  await expect(overall(page)).toContainText("Disconnected within coverage");
  await expect(page.getByRole("region", { name: "Blockers" })).toBeVisible();

  await page.goto("/scenario-revisions/ashgrove-works@1");
  await expect(page.getByText(/⚠ stale/)).toBeVisible();
  await expect(page.getByText(/Stale:/)).toBeVisible();
});

test("repair search keeps the protected route and rejects the harmful ramp", async ({ page }) => {
  await page.goto("/scenario-revisions/ashgrove-works@2");
  const form = page.getByRole("form", { name: "Add an allowed intervention" });
  await form.getByLabel("Kind").selectOption("keep_segment_open");
  await form.getByLabel("Applies to").selectOption({ label: "ashgrove-Q: trench closes B-E crossing and E-F walk" });
  await form.getByRole("checkbox", { name: "e7" }).check();
  await form.getByLabel("Label").fill("Keep the E-F walk open");
  await form.getByLabel(/^Cost/).fill("4");
  await form.getByRole("button", { name: "Add intervention" }).click();

  await form.getByLabel("Kind").selectOption("enable_temporary_connection");
  await form.getByLabel("Applies to").selectOption({ label: "ashgrove-P: temporary ramp C-F" });
  await form.getByLabel("Label").fill("Build the temporary ramp");
  await form.getByLabel(/^Cost/).fill("3");
  await form.getByLabel(/Side effect/).fill("e6");
  await form.getByRole("button", { name: "Add intervention" }).click();

  await page.getByRole("group", { name: "Restore" }).getByRole("checkbox", { name: "ashgrove-lab-to-library", exact: true }).check();
  await page.getByRole("group", { name: /Protect/ }).getByRole("checkbox", { name: "ashgrove-d-to-e" }).check();
  await page.getByRole("button", { name: "Search for repairs" }).click();

  const table = page.getByRole("table", { name: "Repair candidates" });
  await expect(table).toBeVisible({ timeout: 60_000 });
  await expect(page.getByText("OPTIMAL", { exact: true })).toBeVisible();
  const keep = table.getByRole("row").filter({ hasText: "keep-the-e-f-walk-open" }).first();
  await expect(keep).toContainText("accepted");
  await expect(table.getByRole("row").filter({ hasText: "build-the-temporary-ramp" }).filter({ hasText: "protected ashgrove-d-to-e regresses" }).first()).toBeVisible();
  await keep.getByRole("link", { name: "Open branch" }).click();
  await expect(page.getByText(/Branch of/)).toBeVisible();
  await expect(overall(page)).not.toContainText("Disconnected within coverage");
});

test("bundle export replays identically without the database", async ({ page }) => {
  await page.goto("/scenario-revisions/ashgrove-works@2");
  const [download] = await Promise.all([page.waitForEvent("download"), page.getByRole("link", { name: "Export bundle" }).click()]);
  const file = test.info().outputPath("run.zip");
  await download.saveAs(file);
  await page.goto("/compare");
  await page.getByLabel("Bundle (.zip)").setInputFiles(file);
  await page.getByRole("button", { name: "Replay" }).click();
  await expect(page.getByRole("status").filter({ hasText: "Replay matches the recorded result" })).toBeVisible();
});

test("keyboard review on a sheet records an attributed decision", async ({ page }) => {
  await page.goto("/");
  await page.getByLabel("Reviewing as").fill(REVIEWER);
  await page.getByRole("link", { name: "Sheet ASH-P1" }).click();
  await expect(page.getByRole("heading", { level: 1, name: "Sheet ASH-P1" })).toBeVisible();
  await page.locator("body").press("j");
  const detail = page.getByRole("region", { name: "Selected observation" });
  await expect(detail).toBeVisible();
  await page.locator("body").press("a");
  await expect(detail.getByText("accepted", { exact: true })).toBeVisible();
  await expect(detail.getByRole("listitem").filter({ hasText: `accept by ${REVIEWER}` })).toContainText("human ui");
  await axe(page);
});

test("importing a revised sheet marks dependent results stale", async ({ page }) => {
  const pdf = test.info().outputPath("ASH-Q1-revB.pdf");
  execFileSync("uv", ["run", "python", "-c",
    "import sys; from ghost_routes.synthetic.plans import project_q_plan; open(sys.argv[1],'wb').write(project_q_plan('B')[0])", pdf],
  { cwd: REPO });
  await page.goto("/");
  const form = page.getByRole("form", { name: "Import a plan PDF" });
  await form.getByLabel("PDF file").setInputFiles(pdf);
  await form.getByLabel("Project id").fill("ashgrove-Q");
  await form.getByLabel("Project title").fill("Project Q Utility Trench (Fictional)");
  await form.getByLabel("Revision label").fill("B");
  await form.getByLabel("Reuse status").selectOption("synthetic_original");
  await form.getByRole("button", { name: "Import" }).click();
  await expect(page.getByRole("status").filter({ hasText: "Ingested 1 sheet(s)" })).toBeVisible({ timeout: 30_000 });
  await expect(page.getByText("superseded by a later revision")).toBeVisible();

  await page.goto("/scenario-revisions/ashgrove-works@2");
  await expect(page.getByText(/⚠ stale/)).toBeVisible();
  await expect(page.getByText(/^Stale: plan revision B imported/)).toBeVisible();
  await expect(page.getByText(/^stale: plan revision B imported/)).toBeVisible(); // the repair search is stale too
});
