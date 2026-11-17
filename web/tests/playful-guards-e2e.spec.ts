import { request as apiRequest, type Page } from '@playwright/test';
import { test, expect } from './e2e-cleanup';
import { readFileSync } from 'node:fs';
import path from 'node:path';

/**
 * The guarantees the studio's look must keep (DESIGN-TARGETS-v2 §6.7), against the real dry-run backend
 * (playwright.e2e.config.ts, no API mocks): the mood board's price is on screen before its button turns
 * on; an episode whose character has no locked look keeps its start button off (and the server refuses
 * it too); a member who may see a Universe without owning it gets no lock or voice controls; and at
 * 390 px the studio's bottom bar replaces the top tabs. The Universe is made through the API: nothing
 * here is generated or charged.
 */

const API = 'http://localhost:8000';
const OWNER = { name: 'Pebble Owner', email: 'e2e-pebble-owner@test.local', password: 'e2e-pebble-owner-password' };
const VIEWER = { name: 'Pebble Viewer', email: 'e2e-pebble-viewer@test.local', password: 'e2e-pebble-viewer-password' };
const UNIVERSE = 'Pebble Cove';
const CHARACTER = { name: 'Juniper Reed', slug: 'juniper-reed', description: 'a young mapmaker with a green scarf and round glasses' };
const CARD = 'cartoon-made-real';
const CARD_NAME = 'Cartoon world, made real';
const VIEWER_EPISODE = 'ep93pbv';
const BEATMAP = readFileSync(path.join(__dirname, 'fixtures', 'e2e', 'BEATMAP.md'));

let universeId = '';
let episodeId = '';

async function signIn(page: Page, who: { email: string; password: string }) {
  await page.goto('/login');
  await page.locator('#email').fill(who.email);
  await page.locator('#password').fill(who.password);
  await page.getByRole('button', { name: 'Sign In', exact: true }).first().click();
  await expect(page).toHaveURL(/\/studio$/);
}

/** Each stop's state on the creation path, and the one stop marked as the screen you are on. */
async function expectPath(page: Page, states: Record<string, string>, here: string) {
  for (const [key, state] of Object.entries(states)) {
    await expect(page.getByTestId(`creation-path-${key}`)).toHaveAttribute('data-state', state);
  }
  await expect(page.getByTestId('creation-path').locator('[aria-current="step"]')).toHaveCount(1);
  await expect(page.getByTestId(`creation-path-${here}`).locator('[aria-current="step"]')).toHaveCount(1);
}

test.describe.serial('Playful studio guarantees', () => {
  test.use({ viewport: { width: 1440, height: 900 } });

  test.beforeAll(async ({}, testInfo) => {
    const admin = await apiRequest.newContext({
      extraHTTPHeaders: { Authorization: `Bearer ${testInfo.config.metadata.adminSecret}` },
    });
    for (const who of [OWNER, VIEWER]) {
      const res = await admin.post(`${API}/api/users`, { data: { ...who, role: 'member' } });
      expect(res.status(), await res.text()).toBe(200);
    }
    await admin.dispose();
  });

  test('the mood board shows its price before its button turns on', async ({ page }) => {
    await signIn(page, OWNER);
    const made = await page.request.post(`${API}/api/worlds`, {
      data: { name: UNIVERSE, logline: 'A windy seaside village of tide pools and paper maps', preset_id: CARD } });
    expect(made.status(), await made.text()).toBe(200);
    universeId = (await made.json()).world.series_id;
    const note = await page.request.post(`${API}/api/worlds/${universeId}/notes`, {
      data: { topic: 'tone', body: 'Bright, breezy mornings with chalky blues and sandy yellows' } });
    expect(note.status(), await note.text()).toBe(200);

    await page.goto(`/studio/universes/${universeId}/create?step=moodboard`);
    await expect(page.getByTestId('board-check-price')).toBeVisible();
    // no price on screen yet: the button that makes the board stays off
    await expect(page.getByTestId('board-estimate')).toHaveCount(0);
    await expect(page.getByTestId('board-make')).toBeDisabled();
    await page.getByTestId('board-check-price').click();
    await expect(page.getByTestId('board-estimate')).toContainText('4 placeholder images, free in dry-run mode');
    await expect(page.getByTestId('board-make')).toBeEnabled();
  });

  test('a creation screen highlights itself on the path, and the steps already done read done', async ({ page }) => {
    await signIn(page, OWNER);
    // the note opened the mood board: References is done and the mood board is the Universe's next step
    await page.goto(`/studio/universes/${universeId}/create?step=references`);
    await expect(page.getByTestId('creation-step-2')).toBeVisible();
    await expectPath(page, { spark: 'done', references: 'now', moodboard: 'todo', bible: 'todo', lock: 'todo' }, 'references');
    await expect(page.getByTestId('creation-path-references')).toHaveAttribute('data-complete', 'true');
    await page.goto(`/studio/universes/${universeId}/create?step=moodboard`);
    await expectPath(page, { spark: 'done', references: 'done', moodboard: 'now', bible: 'todo', lock: 'todo' }, 'moodboard');
    await page.goto(`/studio/universes/${universeId}/create?step=spark`);
    await expectPath(page, { spark: 'now', references: 'done', moodboard: 'todo', bible: 'todo', lock: 'todo' }, 'spark');
  });

  test('an episode with an unlocked character keeps its start button off', async ({ page }) => {
    await signIn(page, OWNER);
    const added = await page.request.post(`${API}/api/worlds/${universeId}/cast`, {
      data: { name: CHARACTER.name, description: CHARACTER.description } });
    expect(added.status(), await added.text()).toBe(200);

    await page.goto(`/studio/create?world=${universeId}`);
    await expect(page.getByTestId('wizard-universe')).toHaveText(`In: ${UNIVERSE}`);
    await expect(page.getByTestId('selected-style')).toContainText(`Style: ${CARD_NAME}`);
    await page.getByTestId('idea-input').fill(`${CHARACTER.name} charts the tide pools before the spring tide comes in`);
    await page.getByTestId('generate-button').click();
    await expect(page.getByText('Step 3: Review & Start')).toBeVisible({ timeout: 60_000 });
    episodeId = (await page.locator('text=/Episode: ep[0-9]{2}[a-z]{3}/').textContent())!.replace('Episode: ', '');
    await expect(page.getByTestId('wizard-from-A')).toContainText(CHARACTER.name);
    await expect(page.getByTestId('wizard-from-A')).toContainText('Not locked yet');

    await page.getByTestId('preview-button').click();
    await page.waitForURL(new RegExp(`/studio/episodes/${episodeId}#cast$`), { timeout: 60_000 });
    await expect(page.getByTestId('lock-gate')).toBeVisible();
    await expect(page.getByTestId('start-lock')).toContainText(`Lock the look of ${CHARACTER.name} before this video starts`);
    await expect(page.getByTestId('start-checklist-locked')).toHaveAttribute('data-done', 'false');
    await expect(page.getByTestId('start-episode')).toBeDisabled();
    await expect(page.getByTestId(`strip-character-${CHARACTER.slug}-badge`)).toHaveText('Not locked yet');
    // the page only explains the gate: the server refuses the start as well
    const refused = await page.request.post(`${API}/api/episodes`, {
      data: { episode_id: episodeId, dry_run: true, enable_story_shaping: true } });
    expect(refused.status()).toBe(409);
    expect((await refused.json()).detail).toContain(`Lock the look of ${CHARACTER.name} before this video starts`);

    // on a phone the gate is a bottom sheet: opened, its start button is still off
    await page.setViewportSize({ width: 390, height: 844 });
    await page.reload();
    await expect(page.getByTestId('gate-sheet-toggle')).toBeVisible();
    await expect(page.getByTestId('start-episode')).toBeHidden();
    await page.getByTestId('gate-sheet-toggle').click();
    await expect(page.getByTestId('start-episode')).toBeVisible();
    await expect(page.getByTestId('start-episode')).toBeDisabled();
  });

  test("a member who doesn't own the Universe sees no lock or voice controls", async ({ page }, testInfo) => {
    // the owner does see them
    await signIn(page, OWNER);
    await page.goto(`/studio/universes/${universeId}`);
    await expect(page.getByTestId(`cast-character-${CHARACTER.slug}`)).toBeVisible();
    await expect(page.getByTestId(`universe-lock-${CHARACTER.slug}`)).toBeVisible();
    await expect(page.locator(`[data-testid*="voice-${CHARACTER.slug}"]`).first()).toBeVisible();
    await expect(page.getByTestId('universe-narrator')).toBeVisible();
    await expect(page.getByTestId('universe-new-episode')).toBeVisible();

    // the viewer owns an episode an admin filed in the Universe, so may see it, but not change it
    await page.context().clearCookies();
    await signIn(page, VIEWER);
    const uploaded = await page.request.post(`${API}/api/episodes/upload`, {
      multipart: { episode_id: VIEWER_EPISODE, beatmap_file: { name: 'BEATMAP.md', mimeType: 'text/markdown', buffer: BEATMAP } } });
    expect(uploaded.status(), await uploaded.text()).toBe(200);
    const admin = await apiRequest.newContext({
      extraHTTPHeaders: { Authorization: `Bearer ${testInfo.config.metadata.adminSecret}` },
    });
    const filed = await admin.post(`${API}/api/episodes/${VIEWER_EPISODE}/series`, { data: { series_id: universeId } });
    expect(filed.status(), await filed.text()).toBe(200);
    await admin.dispose();

    await page.goto(`/studio/universes/${universeId}`);
    await expect(page.getByTestId('universe-name')).toHaveText(UNIVERSE);
    await expect(page.getByTestId('universe-owner')).toHaveText(`By ${OWNER.name}`);
    await expect(page.getByTestId(`cast-character-${CHARACTER.slug}`)).toBeVisible();
    await expect(page.getByTestId('universe-new-episode-note')).toBeVisible();
    await expect(page.getByTestId('universe-new-episode')).toHaveCount(0);
    await expect(page.getByTestId(`universe-lock-${CHARACTER.slug}`)).toHaveCount(0);
    await expect(page.locator(`[data-testid*="voice-${CHARACTER.slug}"]`)).toHaveCount(0);
    await expect(page.getByTestId('universe-narrator')).toHaveCount(0);
    await expect(page.locator('[data-testid^="cast-check-"], [data-testid^="cast-upload-"], [data-testid^="cast-approve-"]')).toHaveCount(0);
    await expect(page.getByRole('button', { name: /lock/i })).toHaveCount(0);
    await expect(page.getByTestId('universe-journey')).toHaveCount(0);
    await expect(page.getByTestId('universe-bible')).toHaveCount(0);
  });

  test('at 390 px the studio has a bottom bar instead of top tabs', async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await signIn(page, OWNER);
    const bar = page.getByTestId('studio-bottom-bar');
    await expect(bar).toBeVisible();
    await expect(page.locator('header nav[aria-label="Studio"]')).toBeHidden();
    const box = (await bar.boundingBox())!;
    expect(box.y + box.height).toBeGreaterThan(844 - 40);
    await expect(bar.getByRole('link', { name: 'Universes' })).toBeVisible();
    await expect(bar.getByRole('link', { name: 'Episodes' })).toBeVisible();
    await expect(bar.getByRole('button', { name: 'You' })).toBeVisible();
    await expect(page.getByTestId('studio-bottom-bar-new')).toBeVisible();
    await page.getByTestId('studio-bottom-bar-new').click();
    await expect(page.locator('#studio-make-menu').getByRole('link', { name: /New Universe/ })).toBeVisible();
    await expect(page.locator('#studio-make-menu').getByRole('link', { name: /New episode/ })).toBeVisible();
    const extra = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
    expect(extra, `the home scrolls sideways by ${extra}px`).toBeLessThanOrEqual(0);
  });
});
