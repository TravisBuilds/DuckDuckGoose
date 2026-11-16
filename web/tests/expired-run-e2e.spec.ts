import { readFileSync } from 'node:fs';
import path from 'node:path';
import { test, expect } from './e2e-cleanup';

/**
 * A run the workflow server purged before it finished, against the REAL dry-run backend
 * (scripts/e2e_backend.py, no API mocks). POST /api/e2e/purge-history (this launcher only) deletes the
 * run the way Temporal's retention does a day after a run closes. Nothing saved shows how it ended, so
 * the page says its history expired (never "This video hasn't started yet", never a step to take or a
 * stepper), and stops polling; the studio home says the same.
 */

const E2E_USER = 'e2e@test.local';
const E2E_PASSWORD = 'e2e-test-password';
const API = 'http://localhost:8000';
const EPISODE = 'ep84gone';
const BEATMAP = readFileSync(path.join(__dirname, 'fixtures', 'e2e', 'BEATMAP.md'), 'utf-8');

test('a run purged before it finished says its history expired, and the page stops polling', async ({ page }) => {
  await page.goto('/login');
  await page.locator('#email').fill(E2E_USER);
  await page.locator('#password').fill(E2E_PASSWORD);
  await page.getByRole('button', { name: 'Sign In', exact: true }).first().click();
  await expect(page).toHaveURL(/\/studio$/);

  const started = await page.request.post(`${API}/api/episodes`, {
    data: { episode_id: EPISODE, beatmap_content: BEATMAP, dry_run: true, enable_story_shaping: true },
  });
  expect(started.status(), await started.text()).toBe(200);
  const runId = (await started.json()).run_id;
  await page.goto(`/studio/episodes/${EPISODE}`);
  await expect(page.getByTestId('story-logline')).toBeVisible();

  const admin = { Authorization: `Bearer ${test.info().config.metadata.adminSecret}` };
  const purged = await page.request.post(`${API}/api/e2e/purge-history/${EPISODE}`, { headers: admin });
  expect(purged.status(), await purged.text()).toBe(200);
  const state = await (await page.request.get(`${API}/api/episodes/${EPISODE}`)).json();
  expect(state.run).toEqual({
    run_id: runId, status: 'ARCHIVED', ended: true, outcome: 'expired', can_start_again: false, archived: true,
    reason: 'How this run ended is no longer known: no finished state was saved for it.', dry_run: true, saved_at: null,
  });

  await page.reload();
  await expect(page.getByTestId('run-ended')).toHaveAttribute('data-tone', 'expired');
  await expect(page.getByTestId('run-ended-title')).toHaveText("This run's history expired");
  await expect(page.getByTestId('run-ended')).toContainText('no video file is left for it');
  await expect(page.getByTestId('run-ended-reason')).toHaveText(state.run.reason);
  await expect(page.getByTestId('episode-status')).toHaveText('History expired');
  await expect(page.getByText('The story outline of this run is no longer available.')).toBeVisible();
  for (const id of ['next-step', 'stepper', 'ready-video', 'downloads', 'start-again', 'g1-card', 'shots-review', 'final-cut-card']) {
    await expect(page.getByTestId(id)).toHaveCount(0);
  }
  await expect(page.getByText("This video hasn't started yet")).toHaveCount(0);

  await page.waitForLoadState('networkidle');
  const reads: string[] = [];
  page.on('request', (req) => {
    if (['fetch', 'xhr'].includes(req.resourceType()) && req.url().startsWith(`${API}/api/episodes/${EPISODE}`)) reads.push(req.url());
  });
  await page.waitForTimeout(7_000);
  expect(reads).toEqual([]);
  const details = page.getByTestId('production-details');
  await details.locator('summary').click();
  await expect(page.getByTestId('run-finished')).toHaveText('History expired');
  await expect(page.getByTestId('run-archived')).toContainText('Nothing saved shows how it ended.');

  await page.goto('/studio');
  await expect(page.getByTestId(`episode-status-${EPISODE}`)).toHaveText('History expired');
});
