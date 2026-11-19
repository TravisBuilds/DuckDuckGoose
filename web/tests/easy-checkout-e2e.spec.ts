import { request as apiRequest, type Page, type Route } from '@playwright/test';
import { test, expect } from './e2e-cleanup';
import { readFileSync } from 'node:fs';
import path from 'node:path';

/**
 * Easy checkout (docs/EASY-CHECKOUT.md) against the real dry-run backend: a paid thing shows one price,
 * in plain words, next to one button; the details are behind "See the breakdown"; one press approves
 * it, with no confirm dialog after it; and when the price goes up before it is made, the screen shows
 * the new price and asks again, so the old price is never sent twice. The estimates override is never
 * shown or sent, and no model name is on screen. Nothing here is generated or charged: a live price
 * needs paid providers, so the only mocks are the live flags (the server's live switch, a run's
 * dry_run, live mode and whether its estimates came back), the wizard's start request (answered here,
 * so no run starts) and the live mood board's estimate and make routes.
 */

const API = 'http://localhost:8000';
const OWNER = { name: 'Easy Checkout Owner', email: 'e2e-easy-checkout@test.local', password: 'e2e-easy-checkout-password' };
const ADMIN = { email: 'e2e@test.local', password: 'e2e-test-password' };
const EPISODE = 'ep95ckg';
const FIXTURES = path.join(__dirname, 'fixtures', 'e2e');
const MODEL_NAMES = /kling|seedance|\bveo\b|\bsora\b|grok|\bxai\b|\bflux\b|nano.banana|imagen|higgsfield|elevenlabs/i;
const OVERRIDE = 'override_unavailable_estimates';
const BOARD_STARTED = 'The studio is making the mood board now. The pictures appear here when they are made.';

type OfflineEstimate = {
  total_usd_micros: number;
  per_shot: Record<string, { stills_usd_micros: number; clip_usd_micros: number | null }>;
  unpriced?: string[];
};

/** Dollars as the checkout says them: "$7.44", and "$9" for whole dollars. */
function dollars(micros: number): string {
  const cents = Math.floor((Math.round(micros) + 5_000) / 10_000);
  const text = `$${Math.floor(cents / 100).toLocaleString('en-US')}.${String(cents % 100).padStart(2, '0')}`;
  return text.endsWith('.00') ? text.slice(0, -3) : text;
}

/** The whole video's price and its breakdown, from the same offline estimate the page reads. */
function videoPrice(estimate: OfflineEstimate) {
  const shots = Object.values(estimate.per_shot);
  const stills = shots.reduce((sum, s) => sum + s.stills_usd_micros, 0);
  const clips = shots.reduce((sum, s) => sum + (s.clip_usd_micros ?? 0), 0);
  const unpriced = (estimate.unpriced ?? []).length;
  return {
    words: `${unpriced > 0 ? 'at least' : 'about'} ${dollars(estimate.total_usd_micros)}`,
    labels: ['Scene pictures', 'Clips', ...(unpriced > 0 ? ['Not priced yet'] : [])],
    values: [dollars(stills), dollars(clips), ...(unpriced > 0 ? [`${unpriced} clip${unpriced === 1 ? '' : 's'}`] : [])],
  };
}

function cors(route: Route): Record<string, string> {
  return {
    'Access-Control-Allow-Origin': route.request().headers().origin ?? 'http://localhost:3000',
    'Access-Control-Allow-Credentials': 'true',
    'Access-Control-Allow-Methods': 'GET, POST, OPTIONS',
    'Access-Control-Allow-Headers': 'content-type, authorization',
  };
}

async function signIn(page: Page, who: { email: string; password: string }) {
  await page.goto('/login');
  await page.locator('#email').fill(who.email);
  await page.locator('#password').fill(who.password);
  await page.getByRole('button', { name: 'Sign In', exact: true }).first().click();
  await expect(page).toHaveURL(/\/studio$/);
}

/** Every dialog the page opens, and every request that carries the estimates override. */
function watch(page: Page) {
  const seen = { dialogs: [] as string[], overrides: [] as string[] };
  page.on('dialog', (dialog) => {
    seen.dialogs.push(dialog.message());
    void dialog.dismiss();
  });
  page.on('request', (request) => {
    if ((request.postData() ?? '').includes(OVERRIDE) || request.url().includes(OVERRIDE)) seen.overrides.push(request.url());
  });
  return seen;
}

/** No confirm dialog opened and the override was never sent; the mocks stop (a poll still in flight is dropped). */
async function expectNoDialogOrOverride(page: Page, seen: ReturnType<typeof watch>) {
  expect(seen.dialogs).toEqual([]);
  expect(seen.overrides).toEqual([]);
  await page.unrouteAll({ behavior: 'ignoreErrors' });
}

async function openBreakdown(page: Page, testId: string) {
  const breakdown = page.getByTestId(testId);
  await expect(breakdown.locator('dt').first()).toBeHidden();
  await breakdown.locator('summary').click();
  await expect(breakdown.locator('dt').first()).toBeVisible();
  return breakdown;
}

test.describe.serial('Easy checkout', () => {
  test.use({ viewport: { width: 1440, height: 900 } });

  test.beforeAll(async ({}, testInfo) => {
    const admin = await apiRequest.newContext({
      extraHTTPHeaders: { Authorization: `Bearer ${testInfo.config.metadata.adminSecret}` },
    });
    const res = await admin.post(`${API}/api/users`, { data: { ...OWNER, role: 'member' } });
    expect(res.status(), await res.text()).toBe(200);
    await admin.dispose();
  });

  test('a video: one price, the breakdown behind it, and one press makes it', async ({ page }) => {
    const seen = watch(page);
    await signIn(page, OWNER);
    // the server's live switch on (the e2e backend is a dry run): the wizard offers a live video
    await page.route(`${API}/api/health`, async (route) => {
      const response = await route.fetch();
      return route.fulfill({ response, json: { ...(await response.json()), live_available: true } });
    });
    await page.goto('/studio/create');
    await page.getByTestId('preset-card-cozy-hangout').click();
    await page.getByTestId('idea-input').fill('A sleepy fox naps by a frosty window while snow falls on the hill');
    const drafted = page.waitForResponse((res) => res.url() === `${API}/api/episodes/draft-from-idea`
      && res.request().method() === 'POST');
    await page.getByTestId('generate-button').click();
    const draft = await (await drafted).json() as { episode_id: string; cost_estimate: OfflineEstimate };
    await expect(page.getByText('Step 3: Review & Start')).toBeVisible({ timeout: 60_000 });
    await expect(page.getByTestId('voice-block')).toHaveCount(0);
    const price = videoPrice(draft.cost_estimate);

    // see the price: once, in plain words, next to one button
    await expect(page.getByTestId('estimated-cost')).toHaveText(`This video: ${price.words}`);
    await expect(page.getByText(/Estimated cost/)).toHaveCount(0);
    await expect(page.getByTestId('make-video-button')).toHaveText('Make it');
    await expect(page.getByTestId('make-video-button')).toBeEnabled();
    await expect(page.getByTestId('wizard-checkout-line')).toHaveText(
      "Starting costs pennies. Nothing big is made until you approve the video's price.");
    await expect(page.getByTestId('preview-button')).toHaveText('Make a free preview first');
    const checkout = page.getByTestId('wizard-checkout');
    await expect(checkout).not.toContainText(/credits|live mode|providers|budget line|reserve/i);
    const shotList = page.locator('div', { has: page.getByRole('heading', { name: 'Shot List', exact: true }) }).last();
    await expect(shotList).toBeVisible();
    await expect(shotList).not.toContainText(MODEL_NAMES);

    // open the breakdown: scene pictures and clips, at list price
    const breakdown = await openBreakdown(page, 'wizard-checkout-breakdown');
    await expect(breakdown.locator('dt')).toHaveText(price.labels);
    await expect(breakdown.locator('dd')).toHaveText(price.values);
    await expect(breakdown).toContainText('The list price for one try of each picture and clip.');

    // make it: one press starts it, with no confirm dialog (the start request is answered here, so
    // nothing runs; its pictures and clips are approved at its shot plan, with its price shown again)
    const starts: Record<string, unknown>[] = [];
    await page.route(`${API}/api/episodes`, async (route) => {
      if (route.request().method() === 'OPTIONS') return route.fulfill({ status: 204, headers: cors(route) });
      if (route.request().method() !== 'POST') return route.fallback();
      starts.push(route.request().postDataJSON());
      return route.fulfill({ json: { success: true, episode_id: draft.episode_id }, headers: cors(route) });
    });
    await page.getByTestId('make-video-button').click();
    await expect(page.getByRole('button', { name: /Confirm/ })).toHaveCount(0);
    await page.waitForURL(new RegExp(`/studio/episodes/${draft.episode_id}$`), { timeout: 60_000 });
    expect(starts).toHaveLength(1);
    expect(starts[0]).toMatchObject({ episode_id: draft.episode_id, dry_run: false });
    expect(Object.keys(starts[0])).not.toContain(OVERRIDE);
    await expectNoDialogOrOverride(page, seen);
  });

  test('a live shot plan: the price on the approval, no approval without prices, approved in one press', async ({ page }) => {
    test.setTimeout(240_000);
    const seen = watch(page);
    await signIn(page, ADMIN);
    const file = (name: string, mimeType: string) => ({ name, mimeType, buffer: readFileSync(path.join(FIXTURES, name)) });
    const uploads: [string, Record<string, ReturnType<typeof file> | string>][] = [
      ['/api/episodes/upload', { episode_id: EPISODE, beatmap_file: file('BEATMAP.md', 'text/markdown') }],
      [`/api/episodes/${EPISODE}/upload-shots`,
        { shots_file: file('shots.json', 'application/json'), refs_file: file('refs.json', 'application/json') }],
      [`/api/episodes/${EPISODE}/upload-prompt-kit`, { prompt_kit_file: file('prompt_kit.json', 'application/json') }],
    ];
    for (const [url, multipart] of uploads) {
      const sent = await page.request.post(`${API}${url}`, { multipart });
      expect(sent.status(), await sent.text()).toBe(200);
    }
    const preflight = await page.request.post(`${API}/api/episodes/${EPISODE}/preflight`);
    expect(preflight.status(), await preflight.text()).toBe(200);
    const price = videoPrice((await preflight.json()).cost_estimate as OfflineEstimate);
    const started = await page.request.post(`${API}/api/episodes`, {
      data: { episode_id: EPISODE, dry_run: true, enable_story_shaping: false } });
    expect(started.status(), await started.text()).toBe(200);

    // the dry run's record made a live one's (a live run needs paid providers), first without prices
    let estimates = false;
    await page.route(`${API}/api/episodes/${EPISODE}`, async (route) => {
      if (route.request().method() !== 'GET') return route.fallback();
      const response = await route.fetch();
      const body = await response.json();
      return route.fulfill({ response, json: { ...body, run: { ...body.run, dry_run: false }, estimates_available: estimates } });
    });
    await page.route(`${API}/api/episodes/${EPISODE}/gates`, async (route) => {
      const response = await route.fetch();
      return route.fulfill({ response, json: { ...(await response.json()), live_mode: true } });
    });
    await page.goto(`/studio/episodes/${EPISODE}`);
    await expect(page.getByTestId('current-gate')).toHaveText('G1_SHOT_PLAN', { timeout: 60_000 });

    // with no prices the shot plan shows none and cannot be approved, and the override is never offered
    const plan = page.getByTestId('g1-card');
    await expect(page.getByTestId('g1-checkout-price')).toHaveText('This video');
    await expect(page.getByTestId('g1-checkout-breakdown')).toHaveCount(0);
    await expect(page.getByTestId('approve-g1')).toBeDisabled();
    await expect(page.getByTestId('approve-g1-why')).toHaveText(
      "We couldn't get today's prices for this video, so it can't be approved yet. Try again in a few minutes.");
    await expect(plan).not.toContainText(/override/i);
    await expect(plan).not.toContainText(MODEL_NAMES);

    // today's prices are back, but the video's own price is still on its way and then cannot be read:
    // the approval stays off and says why until a price is on screen, and "Try again" fetches it
    estimates = true;
    let answerPrice: () => void = () => undefined;
    const priceAsked = new Promise<void>((resolve) => {
      void page.route(`${API}/api/episodes/${EPISODE}/preflight`, async (route) => {
        if (route.request().method() === 'OPTIONS') return route.fulfill({ status: 204, headers: cors(route) });
        resolve();
        await new Promise<void>((answer) => { answerPrice = answer; });
        return route.fulfill({ status: 503, headers: cors(route), json: { detail: 'Preflight unavailable' } });
      });
    });
    await page.reload();
    await priceAsked;
    await expect(page.getByTestId('current-gate')).toHaveText('G1_SHOT_PLAN', { timeout: 60_000 });
    await expect(page.getByTestId('g1-checkout-price')).toHaveText('This video');
    await expect(page.getByTestId('approve-g1')).toBeDisabled();
    await expect(page.getByTestId('approve-g1-why')).toHaveText('Turns on once the price is on screen.');
    await expect(page.getByTestId('g1-checkout-retry')).toHaveCount(0);
    answerPrice();
    await expect(page.getByTestId('approve-g1-why')).toHaveText("We couldn't get this video's price, so it can't be approved yet.");
    await expect(page.getByTestId('approve-g1')).toBeDisabled();
    await expect(page.getByTestId('g1-checkout-price')).toHaveText('This video');
    await expect(page.getByTestId('g1-checkout-breakdown')).toHaveCount(0);
    await expect(plan).not.toContainText(/override/i);
    await page.unroute(`${API}/api/episodes/${EPISODE}/preflight`);
    await page.getByTestId('g1-checkout-retry').click();

    // with the price on screen: the price on the approval that pays for the video, one calm line, the
    // breakdown behind a click
    await expect(page.getByTestId('approve-g1')).toBeEnabled({ timeout: 60_000 });
    await expect(page.getByTestId('approve-g1-why')).toHaveCount(0);
    await expect(page.getByTestId('g1-checkout-retry')).toHaveCount(0);
    await expect(page.getByTestId('approve-g1')).toHaveText('Approve the shot plan');
    await expect(page.getByTestId('g1-checkout-price')).toHaveText(`This video: ${price.words}`);
    await expect(page.getByTestId('g1-checkout-line')).toHaveText('This starts the scene pictures. The clips wait for your OK on them.');
    const breakdown = await openBreakdown(page, 'g1-checkout-breakdown');
    await expect(breakdown.locator('dt')).toHaveText(price.labels);
    await expect(breakdown.locator('dd')).toHaveText(price.values);
    await expect(plan).not.toContainText(MODEL_NAMES);

    // on a phone the next step is a sheet pinned over the bottom of the screen: the approve button,
    // reached by keyboard while the sheet covers its lower half, comes up above it
    const desk = page.viewportSize()!;
    await page.setViewportSize({ width: 390, height: 844 });
    const approve = page.getByTestId('approve-g1');
    const sheetTop = () => page.getByTestId('gate-panel').evaluate((el) => el.getBoundingClientRect().top);
    await expect.poll(sheetTop).toBeLessThan(844);
    await approve.evaluate((el, top) => window.scrollBy(0, el.getBoundingClientRect().bottom - top - 30), await sheetTop());
    expect(await approve.evaluate((el) => el.getBoundingClientRect().bottom), 'the sheet starts over the button')
      .toBeGreaterThan(await sheetTop());
    await approve.focus();
    await expect.poll(async () => await approve.evaluate((el) => el.getBoundingClientRect().bottom) <= await sheetTop(),
      { message: 'the sheet covers the focused approve button' }).toBe(true);
    await page.setViewportSize(desk);

    // approve once: one press, one approval, no dialog and no override
    const approvals: Record<string, unknown>[] = [];
    page.on('request', (request) => {
      if (request.method() === 'POST' && request.url() === `${API}/api/episodes/${EPISODE}/approve`) {
        approvals.push(request.postDataJSON());
      }
    });
    await page.getByTestId('approve-g1').click();
    await expect(page.getByTestId('run-message')).toHaveText('Shot plan approved.');
    expect(approvals).toEqual([{ gate_id: 'g1_shot_plan' }]);
    await expectNoDialogOrOverride(page, seen);
  });

  test('a live mood board: the price going up asks again before anything is made', async ({ page }) => {
    const seen = watch(page);
    await signIn(page, OWNER);
    const made = await page.request.post(`${API}/api/worlds`, {
      data: { name: 'Gull Point', logline: 'A foggy fishing village where the gulls run the post office', preset_id: 'cartoon-made-real' } });
    expect(made.status(), await made.text()).toBe(200);
    const sid = (await made.json()).world.series_id as string;
    const note = await page.request.post(`${API}/api/worlds/${sid}/notes`, {
      data: { topic: 'tone', body: 'Soft grey mornings, warm lamp light, salt on every window' } });
    expect(note.status(), await note.text()).toBe(200);

    // the live board's routes: the price is $1.56, then $1.80 by the time Make is pressed
    const estimate = (each: number) => ({
      version: 1, count: 4, per_image_usd_micros: [each, each, each, each], total_usd_micros: 4 * each, dry_run: false,
      redo: false, references_per_picture: 0,
      budget: { line: 'U1_mood_board', cap_usd_micros: 4_000_000, stop_usd_micros: 3_200_000, spent_usd_micros: 0,
        reserved_usd_micros: 0, available_usd_micros: 3_200_000 },
    });
    let price = 390_000;
    const sent: Record<string, unknown>[] = [];
    let boardRequest: object | null = null;
    await page.route(`${API}/api/worlds/${sid}`, async (route) => {
      if (route.request().method() !== 'GET') return route.fallback();
      const response = await route.fetch();
      const view = await response.json();
      view.creation.board = { ...view.creation.board, dry_run: false, available: true, unavailable: null };
      view.creation.board_request = boardRequest;
      view.creation.boards = [];
      return route.fulfill({ response, json: view });
    });
    await page.route(`${API}/api/worlds/${sid}/board/estimate`, async (route) => {
      if (route.request().method() === 'OPTIONS') return route.fulfill({ status: 204, headers: cors(route) });
      return route.fulfill({ json: estimate(price), headers: cors(route) });
    });
    await page.route(`${API}/api/worlds/${sid}/board`, async (route) => {
      if (route.request().method() === 'OPTIONS') return route.fulfill({ status: 204, headers: cors(route) });
      const body = route.request().postDataJSON() as { approved_total_usd_micros: number };
      sent.push(body);
      if (body.approved_total_usd_micros < 4 * price) {
        return route.fulfill({ status: 409, headers: cors(route), json: {
          detail: `The estimate is now ${dollars(4 * price)} (you approved ${dollars(body.approved_total_usd_micros)}); review it and approve again` } });
      }
      boardRequest = {
        request_id: 'mr_0123456789ab', series_id: sid, status: 'making', answers_feedback: '', replaces_board_id: null, image_count: 4,
        estimate_usd_micros: 4 * price, approved_total_usd_micros: body.approved_total_usd_micros,
        workflow_id: 'universe-board-mr_0123456789ab', board_id: null, error: null, requested_by: 'owner',
        created_at: '2026-11-19T10:00:00+00:00', updated_at: '2026-11-19T10:00:00+00:00',
        jobs: [0, 1, 2, 3].map((position) => ({ position, status: 'queued' })), made: 0,
      };
      return route.fulfill({ json: { board: null, request: boardRequest, estimate: estimate(price) }, headers: cors(route) });
    });
    await page.goto(`/studio/universes/${sid}/create?step=moodboard`);

    // see the price, and what it is made of behind the breakdown
    await page.getByTestId('board-check-price').click();
    await expect(page.getByTestId('board-estimate-price')).toHaveText('Your mood board: about $1.56');
    await expect(page.getByTestId('board-make')).toHaveText('Make my mood board');
    const breakdown = await openBreakdown(page, 'board-estimate-breakdown');
    await expect(breakdown.locator('dd')).toHaveText(['$0.39 each', 'your notes', '$3.20']);
    await expect(page.getByTestId('board-estimate')).not.toContainText(MODEL_NAMES);

    // the price went up meanwhile: nothing is made, the old price cannot be pressed again, and the
    // new price is one press away
    price = 450_000;
    await page.getByTestId('board-make').click();
    await expect(page.getByTestId('board-estimate-problem')).toContainText(
      'The price went up to $1.80 since you looked. Nothing was charged.');
    await expect(page.getByTestId('board-make')).toHaveCount(0);
    await expect(page.getByTestId('board-estimate-price')).toHaveText('Your mood board');
    await expect(page.getByTestId('board-estimate-breakdown')).toHaveCount(0);
    expect(sent).toEqual([{ feedback: '', approved_total_usd_micros: 1_560_000 }]);
    await page.getByTestId('board-estimate-problem-action').click();
    await expect(page.getByTestId('board-estimate-price')).toHaveText('Your mood board: about $1.80');
    await expect((await openBreakdown(page, 'board-estimate-breakdown')).locator('dd'))
      .toHaveText(['$0.45 each', 'your notes', '$3.20']);
    expect(sent).toHaveLength(1);

    // make it at the new price: that price is the approved total
    await page.getByTestId('board-make').click();
    await expect(page.getByTestId('creation-message-board')).toHaveText(BOARD_STARTED);
    expect(sent).toEqual([
      { feedback: '', approved_total_usd_micros: 1_560_000 },
      { feedback: '', approved_total_usd_micros: 1_800_000 },
    ]);
    await expectNoDialogOrOverride(page, seen);
  });
});
