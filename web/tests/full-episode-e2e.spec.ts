import { test, expect, type Page } from '@playwright/test';
import path from 'node:path';

/**
 * ONE end-to-end dry-run episode through the studio UI against the real backend
 * (playwright.e2e.config.ts: Temporal dev server + production worker + FastAPI, DRY_RUN).
 * No page.route mocks: every click goes through the API and the workflow.
 */

const ADMIN_SECRET = process.env.E2E_ADMIN_SECRET || 'e2e-only-admin-secret-not-used-anywhere-else';
const FIXTURES = path.join(__dirname, 'fixtures', 'e2e');
const EPISODE = 'ep71';

async function expectMessage(page: Page, re: RegExp) {
  await expect(page.getByTestId('run-message')).toHaveText(re);
}

test('full dry-run episode: new episode -> G1 -> per-shot G2 -> clip retry -> cap -> final cut', async ({ page }) => {
  // --- auth: the middleware refuses /studio without a valid signed session ---
  await page.goto('/studio/episodes/new');
  await expect(page).toHaveURL(/\/login\?from=/);
  await page.context().addCookies([{ name: 'studio_admin_token', value: 'v1.9999999999.forgedforgedforgedforged.AAAA', url: 'http://localhost:3000' }]);
  await page.goto('/studio/episodes/new');
  await expect(page).toHaveURL(/\/login\?.*reason=invalid/);
  await page.context().clearCookies();

  await page.locator('#secret').fill(ADMIN_SECRET);
  await page.getByRole('button', { name: 'Access Console' }).click();
  await expect(page).toHaveURL(/\/studio\/episodes\/new$/);

  // --- new episode: a bad shots.json is caught by preflight and Start stays disabled ---
  await page.getByTestId('episode-id').fill(EPISODE);
  await page.getByTestId('beatmap-file').setInputFiles(path.join(FIXTURES, 'BEATMAP.md'));
  await page.getByTestId('shots-file').setInputFiles(path.join(FIXTURES, 'bad-shots.json'));
  await page.getByTestId('refs-file').setInputFiles(path.join(FIXTURES, 'refs.json'));
  await page.getByTestId('prompt_kit-file').setInputFiles(path.join(FIXTURES, 'prompt_kit.json'));
  await page.getByTestId('validate-button').click();
  await expect(page.getByTestId('preflight-verdict')).toContainText('errors');
  await expect(page.getByTestId('preflight-shot-S01')).toContainText('3 to 15 seconds');
  await expect(page.getByTestId('start-button')).toBeDisabled();

  // --- fixed shots.json: lint + preflight pass, cost estimate shown, start the dry run ---
  await page.getByTestId('shots-file').setInputFiles(path.join(FIXTURES, 'shots.json'));
  await page.getByTestId('validate-button').click();
  await expect(page.getByTestId('preflight-verdict')).toContainText('all shots pass');
  await expect(page.getByTestId('lint-report')).toContainText('pass');
  await expect(page.getByTestId('cost-estimate')).toContainText('$0.68');
  await expect(page.getByTestId('dry-run')).toBeChecked();
  await page.getByTestId('start-button').click();
  await expect(page).toHaveURL(new RegExp(`/studio/episodes/${EPISODE}$`));

  // --- G1 ---
  await expect(page.getByTestId('current-gate')).toHaveText('G1_SHOT_PLAN');
  await page.getByTestId('approve-g1').click();
  await expectMessage(page, /Approve G1: done/);

  // --- G2: per-shot decisions on the real workflow (approve one, reject one with a reason) ---
  await expect(page.getByTestId('current-gate')).toHaveText('G2_SCENE_STILLS');
  await expect(page.getByTestId('shot-S01-still')).toHaveText('done');
  await expect(page.getByTestId('shot-S02-still')).toHaveText('done');
  await expect(page.getByTestId('moderation-none')).toBeVisible();
  await expect(page.getByTestId('stall-banner')).toHaveCount(0);
  await page.getByTestId('approve-shot-S01').click();
  await expectMessage(page, /Approve S01: done/);
  await expect(page.getByTestId('shot-S01-decision')).toHaveText('approved');
  await expect(page.getByTestId('reject-shot-S02')).toBeDisabled(); // a reason is required
  await page.getByTestId('reject-reason-S02').fill('framing too loose');
  await page.getByTestId('reject-shot-S02').click();
  await expectMessage(page, /Reject S02: done/);
  await expect(page.getByTestId('shot-S02-decision')).toHaveText('rejected');
  await page.getByTestId('approve-g2').click();
  await expectMessage(page, /Approve G2: done/);

  // --- clips -> picture assembly -> delivery -> G_FINAL_CUT, QC verdicts from the workflow ---
  await expect(page.getByTestId('current-gate')).toHaveText('G_FINAL_CUT', { timeout: 120_000 });
  await expect(page.getByTestId('assembly-qc-verdict')).toHaveText('DRY_RUN');
  await expect(page.getByTestId('assembly-qc-blocking')).toHaveText('not blocking');
  await expect(page.getByTestId('delivery-qc-verdict')).toHaveText('DRY_RUN');
  await expect(page.getByTestId('delivery-qc-blocking')).toHaveText('not blocking');
  // dry run renders no media: the authenticated file route reports it instead of a broken player
  await expect(page.getByTestId('preview-master_subs-missing')).toBeVisible();
  await expect(page.getByTestId('preview-phone-missing')).toBeVisible();

  // --- credit cap raise is applied to the ledger by the workflow ---
  await expect(page.getByTestId('credit-cap')).toHaveText('500');
  await page.getByTestId('new-cap').fill('650');
  await page.getByTestId('raise-cap').click();
  await expectMessage(page, /Raise cap: done/);
  await expect(page.getByTestId('credit-cap')).toHaveText('650');
  await page.getByTestId('new-cap').fill('600');
  await page.getByTestId('raise-cap').click();
  await expectMessage(page, /Raise cap refused \(409\)/);

  // --- clip retry at the final gate, then reject the cut with a reason (gate stays held) ---
  await page.getByTestId('clip-reason-S01').fill('motion too fast');
  await page.getByTestId('retry-clip-S01').click();
  await expectMessage(page, /Retry clip S01: done/);
  await expect(page.getByTestId('shot-S01')).toContainText('clip retries: 1');
  await page.getByTestId('final-reject-reason').fill('pacing needs another pass');
  await page.getByTestId('reject-final').click();
  await expectMessage(page, /Reject final cut: done/);
  await expect(page.getByTestId('final-rejections')).toContainText('pacing needs another pass');
  await expect(page.getByTestId('current-gate')).toHaveText('G_FINAL_CUT');

  // --- approve the final cut once nothing blocks ---
  await expect(page.getByTestId('final-blockers')).toHaveCount(0);
  await page.getByTestId('approve-final').click();
  await expectMessage(page, /Approve final cut: done/);
  await expect(page.getByTestId('final-approved')).toBeVisible();
  await expect(page.getByTestId('passed-gates')).toContainText('G_FINAL_CUT');
});
