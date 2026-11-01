import { expect, test } from "@playwright/test";
import { execFileSync } from "node:child_process";
import path from "node:path";
import { fileURLToPath } from "node:url";

// Real local vision-model inference through the browser, API, worker and database.
// Fails (never skips) when Ollama or the model is missing; `npm run e2e:offline` excludes it explicitly.
const REPO = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../../..");

test("@live import a sheet and run real extraction from the studio", async ({ page }) => {
  test.setTimeout(30 * 60_000);
  const pdf = test.info().outputPath("ASH-Q1-revA.pdf");
  execFileSync("uv", ["run", "python", "-c",
    "import sys; from ghost_routes.synthetic.plans import project_q_plan; open(sys.argv[1],'wb').write(project_q_plan('A')[0])", pdf],
  { cwd: REPO });

  await page.goto("/");
  await expect(page.getByText("Model:").locator("..")).toContainText("qwen2.5vl", { timeout: 30_000 });
  const form = page.getByRole("form", { name: "Import a plan PDF" });
  await form.getByLabel("PDF file").setInputFiles(pdf);
  await form.getByLabel("Project id").fill("e2e-live");
  await form.getByLabel("Project title").fill("Live extraction check (synthetic)");
  await form.getByLabel("Revision label").fill("A");
  await form.getByLabel("Reuse status").selectOption("synthetic_original");
  await form.getByRole("button", { name: "Import" }).click();
  await expect(page.getByRole("status").filter({ hasText: "Ingested 1 sheet(s)" })).toBeVisible();

  await page.getByRole("region", { name: /Live extraction check/ }).getByRole("link", { name: "Sheet ASH-Q1" }).click();
  await page.getByRole("button", { name: "Run live extraction" }).click();
  const status = page.getByRole("status").filter({ hasText: "Extraction" });
  await expect(status).toContainText("running", { timeout: 60_000 });
  await expect(status).toContainText(/succeeded|failed/, { timeout: 25 * 60_000 });
  await expect(status).toContainText("succeeded");

  const runs = page.getByRole("table", { name: "Extraction runs" });
  await expect(runs.getByRole("row").filter({ hasText: "succeeded" }).first()).toBeVisible();
  await expect(runs).toContainText("qwen2.5vl:7b");
  await page.getByLabel("Provenance").selectOption("model");
  await expect(page.getByRole("table", { name: "Observations" }).getByRole("row").nth(1)).toBeVisible();
});
