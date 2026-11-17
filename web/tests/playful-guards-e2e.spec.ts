import { request as apiRequest, type Page } from '@playwright/test';
import { test, expect } from './e2e-cleanup';
import { readFileSync } from 'node:fs';
import path from 'node:path';

/**
 * The guarantees the studio's look must keep (DESIGN-TARGETS-v2 §6.7), against the real dry-run backend
 * (playwright.e2e.config.ts): the mood board's price is on screen before its button turns on; a creation
 * screen is the highlighted stop of its path; at 390 px the mood board decision fits the screen; an
 * episode whose character has no locked look keeps its start button off (and the server refuses it
 * too), and on a phone its start panel is a solid bottom sheet with nothing showing beneath it; a
 * member who may see a Universe without owning it gets no lock or voice controls; at 390 px the
 * studio's bottom bar replaces the top tabs; and a finished episode's trail says Download is done and
 * never calls a paid step "Free" without a recorded cost. The Universe is made through the API: nothing
 * here is generated or charged. The only API mocks turn the finished test run's record into a live
 * one's (a live run needs paid providers): dry_run off, then spend on its budget lines.
 */

const API = 'http://localhost:8000';
const OWNER = { name: 'Pebble Owner', email: 'e2e-pebble-owner@test.local', password: 'e2e-pebble-owner-password' };
const VIEWER = { name: 'Pebble Viewer', email: 'e2e-pebble-viewer@test.local', password: 'e2e-pebble-viewer-password' };
const UNIVERSE = 'Pebble Cove';
const CHARACTER = { name: 'Juniper Reed', slug: 'juniper-reed', description: 'a young mapmaker with a green scarf and round glasses' };
const CARD = 'cartoon-made-real';
const CARD_NAME = 'Cartoon world, made real';
const VIEWER_EPISODE = 'ep93pbv';
const FINISHED_EPISODE = 'ep94pbf';
const ADMIN = { email: 'e2e@test.local', password: 'e2e-test-password' };
const FIXTURES = path.join(__dirname, 'fixtures', 'e2e');
const BEATMAP = readFileSync(path.join(FIXTURES, 'BEATMAP.md'));

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

/** At 390 x 844 the gate's bottom sheet spans the screen to its bottom edge, solid, under the bottom bar. */
async function expectSheetOnTheBottom(page: Page) {
  const sheet = (await page.getByTestId('gate-sheet').boundingBox())!;
  const bar = (await page.getByTestId('studio-bottom-bar').boundingBox())!;
  expect(sheet.x).toBeLessThanOrEqual(0);
  expect(sheet.x + sheet.width).toBeGreaterThanOrEqual(390);
  expect(sheet.y + sheet.height, 'a gap under the sheet shows the page').toBeGreaterThanOrEqual(844 - 1);
  expect(bar.y, 'the bottom bar sits on the sheet').toBeGreaterThan(sheet.y);
  expect(await page.getByTestId('gate-sheet').evaluate((el) => getComputedStyle(el).backgroundColor)).toBe('rgb(255, 253, 248)');
  return sheet;
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

  test('at 390 px the mood board decision and the goose stay on the screen', async ({ page }) => {
    await signIn(page, OWNER);
    await page.goto(`/studio/universes/${universeId}/create?step=moodboard`);
    await page.getByTestId('board-check-price').click();
    await page.getByTestId('board-make').click();
    await expect(page.getByTestId('board-images-1').locator('img')).toHaveCount(4);
    await expect(page.getByTestId('creation-approve')).toBeVisible();

    await page.setViewportSize({ width: 390, height: 844 });
    const extra = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
    expect(extra, `the mood board scrolls sideways by ${extra}px`).toBeLessThanOrEqual(0);
    const board = (await page.getByTestId('creation-board').boundingBox())!;
    for (const id of ['board-love-it', 'board-not-quite', 'board-approve', 'board-feedback', 'board-redo-price']) {
      const box = (await page.getByTestId(id).boundingBox())!;
      expect(box.x, `${id} starts left of its card`).toBeGreaterThanOrEqual(board.x);
      expect(box.x + box.width, `${id} ends past its card`).toBeLessThanOrEqual(board.x + board.width);
    }
    expect(board.x + board.width).toBeLessThanOrEqual(390);
    // the bubble's tail points at the goose beside it, inside the page's margin
    const goose = (await page.getByTestId('creation-goose').locator('svg').first().boundingBox())!;
    const bubble = (await page.getByTestId('creation-bubble').boundingBox())!;
    const tail = await page.getByTestId('creation-bubble').evaluate((el) => parseFloat(getComputedStyle(el, '::after').left));
    expect(goose.x).toBeGreaterThanOrEqual(16);
    expect(bubble.x + tail, 'the bubble tail reaches past the goose').toBeGreaterThanOrEqual(goose.x + goose.width);
    expect(bubble.x + bubble.width).toBeLessThanOrEqual(390 - 16);
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
    // green means locked or approved: the link to go and lock is a plain primary action
    await expect(page.getByTestId('lock-gate-link')).toHaveClass(/\bpf-btn-primary\b/);
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
    await expectSheetOnTheBottom(page);
    await page.getByTestId('gate-sheet-toggle').click();
    await expect(page.getByTestId('start-episode')).toBeVisible();
    await expect(page.getByTestId('start-episode')).toBeDisabled();
    // open, it is still solid and on the bottom edge, its start button clears the bottom bar, and the
    // page behind is dimmed: a tap on it, or Escape, closes the sheet
    const sheet = await expectSheetOnTheBottom(page);
    const bar = (await page.getByTestId('studio-bottom-bar').boundingBox())!;
    const start = (await page.getByTestId('start-episode').boundingBox())!;
    expect(start.y + start.height, 'the start button is under the bottom bar').toBeLessThanOrEqual(bar.y);
    await expect(page.getByTestId('gate-sheet-scrim')).toBeVisible();
    await page.getByTestId('gate-sheet-scrim').click({ position: { x: 20, y: sheet.y - 20 } });
    await expect(page.getByTestId('gate-sheet-toggle')).toHaveAttribute('aria-expanded', 'false');
    await expect(page.getByTestId('gate-sheet-scrim')).toHaveCount(0);
    await page.getByTestId('gate-sheet-toggle').click();
    await expect(page.getByTestId('gate-sheet-toggle')).toHaveAttribute('aria-expanded', 'true');
    await page.keyboard.press('Escape');
    await expect(page.getByTestId('gate-sheet-toggle')).toHaveAttribute('aria-expanded', 'false');
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
    // nothing is locked yet: "look changed since lock" is neutral, not the green of a locked look
    await expect(page.getByTestId('universe-drift')).toHaveText('None');
    await expect(page.getByTestId('universe-drift')).toHaveClass(/\btext-ink-soft\b/);
    await expect(page.getByTestId('universe-new-episode')).toHaveClass(/\bpf-btn-primary\b/);

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

  test('a finished episode: Download is done, and a paid step with no recorded cost never says Free', async ({ page }) => {
    test.setTimeout(240_000);
    await signIn(page, ADMIN);
    const file = (name: string, mimeType: string) => ({ name, mimeType, buffer: readFileSync(path.join(FIXTURES, name)) });
    const uploads: [string, Record<string, ReturnType<typeof file> | string>][] = [
      ['/api/episodes/upload', { episode_id: FINISHED_EPISODE, beatmap_file: file('BEATMAP.md', 'text/markdown') }],
      [`/api/episodes/${FINISHED_EPISODE}/upload-shots`,
        { shots_file: file('shots.json', 'application/json'), refs_file: file('refs.json', 'application/json') }],
      [`/api/episodes/${FINISHED_EPISODE}/upload-prompt-kit`, { prompt_kit_file: file('prompt_kit.json', 'application/json') }],
    ];
    for (const [url, multipart] of uploads) {
      const sent = await page.request.post(`${API}${url}`, { multipart });
      expect(sent.status(), await sent.text()).toBe(200);
    }
    const started = await page.request.post(`${API}/api/episodes`, {
      data: { episode_id: FINISHED_EPISODE, dry_run: true, enable_story_shaping: false } });
    expect(started.status(), await started.text()).toBe(200);

    // a free test run, approved gate by gate
    await page.goto(`/studio/episodes/${FINISHED_EPISODE}`);
    await expect(page.getByTestId('current-gate')).toHaveText('G1_SHOT_PLAN', { timeout: 60_000 });
    await page.getByTestId('approve-g1').click();
    await expect(page.getByTestId('run-message')).toHaveText('Shot plan approved.');
    await expect(page.getByTestId('current-gate')).toHaveText('G2_SCENE_STILLS', { timeout: 60_000 });
    for (const sid of ['S01', 'S02']) {
      await expect(page.getByTestId(`shot-${sid}-still`)).toHaveText('done', { timeout: 60_000 });
      await page.getByTestId(`approve-shot-${sid}`).click();
      await expect(page.getByTestId('run-message')).toHaveText(`Picture ${sid} approved.`);
    }
    await page.getByTestId('approve-g2').click();
    await expect(page.getByTestId('run-message')).toHaveText(/^Scene pictures approved\./);
    await expect(page.getByTestId('current-gate')).toHaveText('G_FINAL_CUT', { timeout: 120_000 });
    await expect(page.getByTestId('final-blockers')).toHaveCount(0, { timeout: 120_000 });
    await page.getByTestId('approve-final').click();
    await expect(page.getByTestId('run-finished')).toHaveText('Finished', { timeout: 120_000 });

    // finished: Download is done, nothing on the trail is "now", and a test run's paid steps were free
    const note = (key: string) => page.getByTestId(`step-${key}`).locator('.pf-stop-note');
    await page.reload();
    await expect(page.getByTestId('step-download')).toHaveAttribute('data-status', 'done');
    await expect(page.getByTestId('stepper').locator('[aria-current="step"]')).toHaveCount(0);
    await expect(note('stills')).toHaveText('Free');
    await expect(note('final_cut')).toHaveText('Free');
    await expect(page.getByTestId('episode-real-cost')).toHaveText('$0.00');

    // the same run as a live one that recorded no spend (a live run needs paid providers, so its
    // record is the dry run's with dry_run off): no paid step says "Free"
    await page.route(`${API}/api/episodes/${FINISHED_EPISODE}`, async (route) => {
      if (route.request().method() !== 'GET') return route.fallback();
      const response = await route.fetch();
      const body = await response.json();
      return route.fulfill({ response, json: { ...body, run: { ...body.run, dry_run: false } } });
    });
    await page.route(`${API}/api/episodes/${FINISHED_EPISODE}/gates`, async (route) => {
      const response = await route.fetch();
      return route.fulfill({ response, json: { ...(await response.json()), live_mode: true } });
    });
    await page.reload();
    await expect(note('stills')).toHaveText('Cost not recorded');
    await expect(note('final_cut')).toHaveText('Cost not recorded');
    await expect(page.getByTestId('stepper')).not.toContainText('Free');
    await expect(page.getByTestId('episode-real-cost')).toHaveText('Not recorded');
    await expect(page.getByTestId('step-download')).toHaveAttribute('data-status', 'done');

    // with spend on record, each paid step shows what it cost
    const spend: Record<string, number> = { L3_final_stills: 1_240_000, L4_video: 3_000_000 };
    await page.route(`${API}/api/episodes/${FINISHED_EPISODE}/budget`, async (route) => {
      const response = await route.fetch();
      const body = await response.json();
      const lines = body.lines.filter((line: { line_name: string }) => !(line.line_name in spend));
      for (const [lineName, spent] of Object.entries(spend)) {
        lines.push({ line_name: lineName, provider: 'higgsfield', spent, reserved: 0, total: spent, cap: 10_000_000,
          stop: 8_000_000, at_stop: false, unit: 'usd_micros' });
      }
      return route.fulfill({ response, json: { ...body, lines } });
    });
    await page.reload();
    await expect(note('stills')).toHaveText('$1.24');
    await expect(note('final_cut')).toHaveText('$3.00');
    await expect(page.getByTestId('episode-real-cost')).toHaveText('$4.24');
  });
});
