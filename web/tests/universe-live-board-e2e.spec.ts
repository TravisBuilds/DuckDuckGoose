import { request as apiRequest, type Page, type Route } from '@playwright/test';
import { test, expect } from './e2e-cleanup';

/**
 * A live mood board on the Universe page (docs/WORLDS.md). Before anything is spent the owner sees
 * the provider's price of each picture, what the pictures are made with and what the Universe's mood
 * board budget has left; Make sends the approved total; the page follows the board while the worker
 * makes it, then shows its pictures. A board that stopped early keeps the pictures that were paid for
 * and says why; one that made nothing says why and offers the price again; an older worker gets a
 * plain message instead of a price. A live board needs a paid provider, so this is the one e2e spec
 * that mocks API routes, and only the live board's (the estimate, Make, the board fields of the
 * Universe view and their pictures): the session, the Universe and its references are the real
 * dry-run backend's. The worker's side is tests/test_universe_board.py (faked provider, Temporal
 * test server); the API's is tests/test_world_api.py.
 */

const API = 'http://localhost:8000';
const OWNER = { name: 'Live Board Owner', email: 'e2e-live-board@test.local', password: 'e2e-live-board-password' };
const PRICE = 390_000;
const MODEL = 'xai/grok-imagine-image-2.0';
const STOPPED = 'The provider could not make a picture, so the studio stopped there. Try again in a while.';
const OLD_WORKER = "Mood boards are made by the studio's worker, and the worker running now cannot make them yet. "
  + 'Nothing was charged. Ask an admin to update the worker, then try again.';
// a generated 1x1 picture, never a real person or place
const PNG = Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==', 'base64');

type Live = { request: object | null; boards: object[]; unavailable: string | null };

const ESTIMATE = {
  version: 1, count: 4, per_image_usd_micros: [PRICE, PRICE, PRICE, PRICE], total_usd_micros: 4 * PRICE, dry_run: false,
  redo: false, model_path: MODEL, references_per_picture: 0,
  budget: { line: 'U1_mood_board', cap_usd_micros: 4_000_000, stop_usd_micros: 3_200_000, spent_usd_micros: 0,
    reserved_usd_micros: 0, available_usd_micros: 3_200_000 },
  summary: "4 pictures, about $1.56, charged to this Universe's mood board budget ($3.20 left)",
};

function boardRequest(sid: string, status: 'making' | 'done' | 'failed', made: number, error: string | null = null) {
  return {
    request_id: 'mr_0123456789ab', series_id: sid, status, answers_feedback: '', replaces_board_id: null, image_count: 4,
    model_path: MODEL, estimate_usd_micros: 4 * PRICE, approved_total_usd_micros: 4 * PRICE,
    workflow_id: 'universe-board-mr_0123456789ab', board_id: status === 'done' ? 'mb_0123456789ab' : null, error,
    requested_by: 'owner', created_at: '2026-11-17T10:00:00+00:00', updated_at: '2026-11-17T10:01:00+00:00',
    jobs: [0, 1, 2, 3].map((position) => ({
      position, status: position < made ? 'completed' : status === 'making' ? 'queued' : 'skipped' })),
    made,
  };
}

function liveBoard(sid: string, pictures: number) {
  return {
    board_id: 'mb_0123456789ab', series_id: sid, version: 1, status: 'ready', answers_feedback: '', redo_feedback: null,
    image_count: pictures, estimate_usd_micros: 4 * PRICE, approved_total_usd_micros: 4 * PRICE, dry_run: false,
    created_at: '2026-11-17T10:01:00+00:00',
    images: Array.from({ length: pictures }, (_, position) => ({
      image_id: `bi_01234567890${position}`, position, width: 1024, height: 1024, placeholder: false,
      url: `/api/worlds/images/bi_01234567890${position}`, thumb_url: `/api/worlds/images/bi_01234567890${position}/thumb`,
    })),
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

async function mocked(route: Route, json: () => object) {
  if (route.request().method() === 'OPTIONS') return route.fulfill({ status: 204, headers: cors(route) });
  return route.fulfill({ json: json(), headers: cors(route) });
}

/** The live board's routes: the rest of the Universe view stays the backend's. `sent`: the Make bodies. */
async function liveBoardRoutes(page: Page, sid: string, live: Live, sent: unknown[]) {
  await page.route(`${API}/api/worlds/${sid}`, async (route) => {
    if (route.request().method() !== 'GET') return route.fallback();
    const response = await route.fetch();
    const view = await response.json();
    view.creation.board = { ...view.creation.board, dry_run: false, available: live.unavailable === null,
      unavailable: live.unavailable };
    view.creation.board_request = live.request;
    view.creation.boards = live.boards;
    return route.fulfill({ response, json: view });
  });
  await page.route(`${API}/api/worlds/${sid}/board/estimate`, (route) => mocked(route, () => ESTIMATE));
  await page.route(`${API}/api/worlds/${sid}/board`, (route) => mocked(route, () => {
    sent.push(route.request().postDataJSON());
    live.request = boardRequest(sid, 'making', 0);
    return { board: null, request: live.request, estimate: ESTIMATE };
  }));
  await page.route(`${API}/api/worlds/images/bi_**`, (route) => route.fulfill({ contentType: 'image/png', body: PNG }));
}

async function signIn(page: Page, who: { email: string; password: string }) {
  await page.goto('/login');
  await page.locator('#email').fill(who.email);
  await page.locator('#password').fill(who.password);
  await page.getByRole('button', { name: 'Sign In', exact: true }).first().click();
  await expect(page).toHaveURL(/\/studio$/);
}

async function expectNoSidewaysScroll(page: Page) {
  const extra = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
  expect(extra, `${page.url()} scrolls sideways by ${extra}px`).toBeLessThanOrEqual(0);
}

/** The Universe's creation steps only (the rest of the page is the real backend's and covered elsewhere). */
async function screenshot(page: Page, name: string) {
  await page.getByTestId('universe-creation').screenshot({ path: test.info().outputPath(`${name}.png`) });
}

test.describe('A live mood board', () => {
  test.use({ viewport: { width: 1440, height: 900 } });

  test.beforeAll(async ({}, testInfo) => {
    const admin = await apiRequest.newContext({
      extraHTTPHeaders: { Authorization: `Bearer ${testInfo.config.metadata.adminSecret}` },
    });
    const res = await admin.post(`${API}/api/users`, { data: { ...OWNER, role: 'member' } });
    expect(res.status(), await res.text()).toBe(200);
    await admin.dispose();
  });

  test('the price comes first, the page follows the worker, and a board that stopped says why', async ({ page }) => {
    await signIn(page, OWNER);
    await page.getByTestId('universe-name').fill('Kelp Hollow');
    await page.getByTestId('universe-create').click();
    await page.waitForURL(/\/studio\/universes\/sr_[0-9a-f]+\?created=1$/);
    const sid = new URL(page.url()).pathname.split('/').pop()!;
    await page.getByTestId('creation-topic').selectOption('premise');
    await page.getByTestId('creation-note-text').fill('A village in a kelp forest where the tide decides the day.');
    await page.getByTestId('creation-note-add').click();
    await expect(page.getByTestId('creation-note-text')).toHaveValue('');

    const live: Live = { request: null, boards: [], unavailable: null };
    const sent: unknown[] = [];
    await liveBoardRoutes(page, sid, live, sent);
    await page.reload();

    // the price first: each picture's provider price, what it is made with, the budget left
    await page.getByTestId('board-check-price').click();
    const estimate = page.getByTestId('board-estimate');
    await expect(estimate).toContainText(ESTIMATE.summary);
    await expect(page.getByTestId('board-estimate-lines').locator('li')).toHaveText([
      "4 pictures at $0.39 each (the provider's price)",
      `Made from your notes, with ${MODEL}`,
      "This Universe's mood board budget has $3.20 left (it stops at $3.20 spent of $4.00)",
    ]);
    await expect(estimate).toContainText('Nothing is charged until you press Make.');
    expect(sent).toEqual([]);
    await screenshot(page, 'live-board-estimate-1440');

    // Make sends the approved total; the page follows the pictures as the worker makes them
    await page.getByTestId('board-make').click();
    await expect.poll(() => sent).toEqual([{ feedback: '', approved_total_usd_micros: 4 * PRICE }]);
    await expect(page.getByTestId('creation-message-board')).toHaveText(
      'The studio is making the mood board now. The pictures appear here when they are made.');
    await expect(page.getByTestId('board-making')).toContainText('Making the mood board: 0 of 4 pictures made.');
    await expect(page.getByTestId('board-check-price')).toHaveCount(0);
    live.request = boardRequest(sid, 'making', 2);
    await expect(page.getByTestId('board-making')).toContainText('2 of 4 pictures made');
    await expect(page.getByTestId('board-making').locator('progress')).toHaveAttribute('value', '2');
    await screenshot(page, 'live-board-making-1440');
    await page.setViewportSize({ width: 390, height: 844 });
    await expectNoSidewaysScroll(page);
    await screenshot(page, 'live-board-making-390');
    await page.setViewportSize({ width: 1440, height: 900 });

    // it stopped after three pictures: the three that were paid for are the board, with the reason
    live.request = boardRequest(sid, 'done', 3, STOPPED);
    live.boards = [liveBoard(sid, 3)];
    const pictures = page.getByTestId('board-images-1').locator('img');
    await expect(pictures).toHaveCount(3);
    await expect.poll(() => pictures.evaluateAll((imgs) => imgs.every((img) => (img as HTMLImageElement).naturalWidth > 0)))
      .toBe(true);
    await expect(page.getByTestId('board-short')).toHaveText(`This mood board has 3 of the 4 pictures. ${STOPPED}`);
    await expect(page.getByTestId('board-making')).toHaveCount(0);
    await expect(page.getByTestId('creation-message-board')).toHaveCount(0);
    await expect(page.getByTestId('board-approve')).toBeVisible();
    await expect(page.getByTestId('board-redo-price')).toBeVisible();
    await screenshot(page, 'live-board-short-1440');

    // a board that made nothing says why, and the price can be asked again
    live.request = boardRequest(sid, 'failed', 0, STOPPED);
    live.boards = [];
    await page.reload();
    await expect(page.getByTestId('board-failed')).toHaveText(`The mood board was not made. ${STOPPED}`);
    await expect(page.getByTestId('board-check-price')).toBeVisible();

    // an older worker: a plain message, and no price to ask for
    live.request = null;
    live.unavailable = OLD_WORKER;
    await page.reload();
    await expect(page.getByTestId('board-unavailable')).toHaveText(OLD_WORKER);
    await expect(page.getByTestId('board-check-price')).toHaveCount(0);
    expect(sent).toHaveLength(1);
  });
});
