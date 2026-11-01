import { defineConfig, devices } from "@playwright/test";

// Browser tests run against an isolated stack: a throwaway database (scripts/e2e_stack.py),
// the API on 8078 and this studio on 5178, so they never touch the developer's data or dev servers.
const API_PORT = "8078";
const WEB_PORT = "5178";

export default defineConfig({
  testDir: "e2e",
  fullyParallel: false,
  workers: 1,
  timeout: 120_000,
  // Generous waits: these run beside a database, API and worker on a developer machine.
  expect: { timeout: 30_000 },
  reporter: [["list"]],
  use: {
    baseURL: `http://127.0.0.1:${WEB_PORT}`,
    trace: "retain-on-failure",
    acceptDownloads: true,
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
  webServer: [
    {
      command: "uv run python scripts/e2e_stack.py",
      cwd: "../..",
      env: { PYTHONPATH: ".", GHOST_ROUTES_API_PORT: API_PORT, GHOST_ROUTES_WEB_PORT: WEB_PORT },
      url: `http://127.0.0.1:${API_PORT}/api/health`,
      timeout: 120_000,
      reuseExistingServer: false,
      stdout: "ignore",
      stderr: "pipe",
    },
    {
      command: "npm run dev",
      env: { GHOST_ROUTES_API_PORT: API_PORT, GHOST_ROUTES_WEB_PORT: WEB_PORT },
      url: `http://127.0.0.1:${WEB_PORT}`,
      timeout: 60_000,
      reuseExistingServer: false,
    },
  ],
});
