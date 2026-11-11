import { readFileSync } from 'node:fs';
import path from 'node:path';
import { test, expect, type Page } from '@playwright/test';

/**
 * A run that has ended, against the REAL dry-run backend (scripts/e2e_backend.py, no API mocks).
 * A dry-run story-shaped episode started with only a beatmap fails at preflight once its outline is
 * approved (no shots.json / refs.json / prompt_kit.json). The episode page must say the run failed and
 * why, actions on it must get the API's 409 (they used to be bare 500s), and Start again must start a
 * new run with the same inputs. Every run here ends failed, which frees the account's run lock again
 * for wizard-member-e2e (files run in name order, 1 worker).
 */

const E2E_USER = 'e2e@test.local';
const E2E_PASSWORD = 'e2e-test-password';
const API = 'http://localhost:8000';
const EPISODE = 'ep83';
const BEATMAP = readFileSync(path.join(__dirname, 'fixtures', 'e2e', 'BEATMAP.md'), 'utf-8');

async function runOf(page: Page) {
  const res = await page.request.get(`${API}/api/episodes/${EPISODE}`);
  expect(res.status(), await res.text()).toBe(200);
  return (await res.json()).run;
}

test('a failed run says why, refuses actions with a 409 and can be started again', async ({ page }) => {
  await page.goto('/login');
  await page.locator('#email').fill(E2E_USER);
  await page.locator('#password').fill(E2E_PASSWORD);
  await page.getByRole('button', { name: 'Sign In', exact: true }).first().click();
  await expect(page).toHaveURL(/\/studio$/);

  const started = await page.request.post(`${API}/api/episodes`, {
    data: { episode_id: EPISODE, beatmap_content: BEATMAP, dry_run: true, enable_story_shaping: true },
  });
  expect(started.status(), await started.text()).toBe(200);
  const firstRunId = (await started.json()).run_id;

  await page.goto(`/studio/episodes/${EPISODE}`);
  await expect(page.getByTestId('story-logline')).toBeVisible();
  await expect(page.getByTestId('run-ended')).toHaveCount(0);
  await page.getByTestId('story-approve').click();

  // the run fails at preflight: the page says so, with the short reason, and offers Start again
  await expect(page.getByTestId('run-ended-title')).toHaveText('This run failed.');
  await expect(page.getByTestId('run-ended-reason')).toContainText('preflight_validate_episode_activity');
  await expect(page.getByTestId('run-ended-reason')).toContainText('Missing required file: shots.json');
  await expect(page.getByTestId('start-again')).toBeVisible();
  const failed = await runOf(page);
  expect(failed).toMatchObject({
    run_id: firstRunId, status: 'FAILED', ended: true, outcome: 'failed', dry_run: true, can_start_again: true,
  });
  const refused = `This run has ended (failed: ${failed.reason}). Start it again.`;

  // approve, the story-outline save and the page's own actions get that 409
  const approve = await page.request.post(`${API}/api/episodes/${EPISODE}/approve`, { data: { gate_id: 'story_shape' } });
  expect([approve.status(), (await approve.json()).detail]).toEqual([409, refused]);
  const outline = (await (await page.request.get(`${API}/api/episodes/${EPISODE}/story-shape`)).json()).story_shape;
  const save = await page.request.put(`${API}/api/episodes/${EPISODE}/story-shape`, { data: { ...outline, logline: 'An edit' } });
  expect([save.status(), (await save.json()).detail]).toEqual([409, refused]);
  await page.getByTestId('new-cap').fill('700');
  await page.getByTestId('raise-cap').click();
  await expect(page.getByTestId('run-message')).toHaveText(`Raise cap refused (409): ${refused}`);

  // Start again (a dry run: no confirmation): a new, open run with the same inputs
  await page.getByTestId('start-again').click();
  await expect(page.getByTestId('run-message')).toHaveText('Start again: done');
  await expect(page.getByTestId('run-ended')).toHaveCount(0);
  const again = await runOf(page);
  expect(again).toMatchObject({ status: 'RUNNING', ended: false });
  expect(again.run_id).not.toBe(firstRunId);

  // the files are still missing, so the new run fails the same way once its outline is approved
  await expect(page.getByTestId('story-logline')).toBeVisible();
  await page.getByTestId('story-approve').click();
  await expect(page.getByTestId('run-ended-title')).toHaveText('This run failed.');
  expect(await runOf(page)).toMatchObject({ run_id: again.run_id, status: 'FAILED', reason: failed.reason });
});
