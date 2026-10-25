import { defineConfig, devices } from '@playwright/test';

/**
 * Full dry-run episode E2E (G-UI-1): the browser drives a REAL backend, no route mocks:
 *   scripts/e2e_backend.py = Temporal dev server + production worker + FastAPI app, DRY_RUN, temp dirs.
 * The Next app is the production build (`npm run build` first) with the same ADMIN_SECRET, so the
 * middleware verifies the API-signed session cookie for real.
 */
const ADMIN_SECRET = process.env.E2E_ADMIN_SECRET || 'e2e-only-admin-secret-not-used-anywhere-else';
const PYTHON = process.env.E2E_PYTHON || 'python';
const API_PORT = 8000;

export default defineConfig({
  testDir: './tests',
  testMatch: /full-episode-e2e\.spec\.ts$/,
  fullyParallel: false,
  workers: 1,
  forbidOnly: !!process.env.CI,
  retries: 0,
  reporter: process.env.CI ? [['list'], ['html', { open: 'never' }]] : 'list',
  timeout: 6 * 60_000,
  expect: { timeout: 60_000 },
  outputDir: 'test-results-e2e',
  use: {
    baseURL: 'http://localhost:3000',
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
  },
  projects: [{ name: 'chromium', use: { ...devices['Desktop Chrome'] } }],
  webServer: [
    {
      command: `${PYTHON} ../scripts/e2e_backend.py --port ${API_PORT}`,
      url: `http://127.0.0.1:${API_PORT}/api/health`,
      env: { ADMIN_SECRET },
      reuseExistingServer: false,
      timeout: 180_000,
      stdout: 'pipe',
      stderr: 'pipe',
    },
    {
      command: 'npm run start',
      url: 'http://localhost:3000/login',
      env: { ADMIN_SECRET, NEXT_PUBLIC_API_URL: `http://localhost:${API_PORT}` },
      reuseExistingServer: false,
      timeout: 120_000,
    },
  ],
  metadata: { adminSecret: ADMIN_SECRET },
});
