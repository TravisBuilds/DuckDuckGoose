import { request as apiRequest, type Route } from '@playwright/test';
import { test, expect } from './e2e-cleanup';
import path from 'node:path';
import zlib from 'node:zlib';

/**
 * Props and places against the real backend (playwright.e2e.config.ts, DRY_RUN): start a dry run,
 * add a place with a photo (its rights answer first) and a prop, then its photo; while the run waits
 * for the shot plan approval, pick both on a shot and edit another shot's prompt; the run checks the
 * saved plan again; approve it and the scene pictures are made; the plan is locked from then on.
 */

const E2E_USER = 'e2e@test.local';
const E2E_PASSWORD = 'e2e-test-password';
const FIXTURES = path.join(__dirname, 'fixtures', 'e2e');
const API = 'http://localhost:8000';
const EPISODE = 'ep76';
const NEW_PROMPT = 'The red ball comes to rest beside a blue cup on a wooden table, warm evening light';

function crc32(data: Buffer): number {
  let c = ~0;
  for (const byte of data) {
    c ^= byte;
    for (let k = 0; k < 8; k++) c = (c >>> 1) ^ (0xedb88320 & -(c & 1));
  }
  return ~c >>> 0;
}

function pngChunk(type: string, data: Buffer): Buffer {
  const length = Buffer.alloc(4);
  length.writeUInt32BE(data.length);
  const body = Buffer.concat([Buffer.from(type, 'ascii'), data]);
  const crc = Buffer.alloc(4);
  crc.writeUInt32BE(crc32(body));
  return Buffer.concat([length, body, crc]);
}

/** A generated test photo: a plain coloured block on a light background. */
function testPhoto(width: number, height: number, colour: [number, number, number]): Buffer {
  const header = Buffer.alloc(13);
  header.writeUInt32BE(width, 0);
  header.writeUInt32BE(height, 4);
  header[8] = 8; // bit depth
  header[9] = 2; // RGB
  const stride = width * 3 + 1;
  const raw = Buffer.alloc(stride * height);
  for (let y = 0; y < height; y++) {
    for (let x = 0; x < width; x++) {
      const block = x > width / 4 && x < (width * 3) / 4 && y > height / 3;
      raw.set(block ? colour : [235, 240, 245], y * stride + 1 + x * 3);
    }
  }
  return Buffer.concat([
    Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]),
    pngChunk('IHDR', header),
    pngChunk('IDAT', zlib.deflateSync(raw)),
    pngChunk('IEND', Buffer.alloc(0)),
  ]);
}

test('props and places: a place with a photo and a prop picked on a shot, a prompt edit, then approve', async ({ page }) => {
  page.on('dialog', (dialog) => dialog.accept());

  await page.goto('/login?from=/studio/episodes/new');
  await page.locator('#email').fill(E2E_USER);
  await page.locator('#password').fill(E2E_PASSWORD);
  await page.getByRole('button', { name: 'Sign In', exact: true }).first().click();
  await expect(page).toHaveURL(/\/studio\/episodes\/new$/);

  await page.getByTestId('episode-id').fill(EPISODE);
  await page.getByTestId('beatmap-file').setInputFiles(path.join(FIXTURES, 'BEATMAP.md'));
  await page.getByTestId('shots-file').setInputFiles(path.join(FIXTURES, 'shots.json'));
  await page.getByTestId('refs-file').setInputFiles(path.join(FIXTURES, 'refs.json'));
  await page.getByTestId('prompt_kit-file').setInputFiles(path.join(FIXTURES, 'prompt_kit.json'));
  await page.getByTestId('validate-button').click();
  await expect(page.getByTestId('preflight-verdict')).toContainText('all shots pass');
  await expect(page.getByTestId('dry-run')).toBeChecked();
  await page.getByTestId('start-button').click();
  await expect(page).toHaveURL(new RegExp(`/studio/episodes/${EPISODE}$`));
  await expect(page.getByTestId('current-gate')).toHaveText('G1_SHOT_PLAN');

  // --- an empty library teaches the feature; the shot plan says what to do first ---
  const message = page.getByTestId('props-message');
  await expect(page.getByTestId('props-empty')).toContainText('No props or places yet.');
  await expect(page.getByTestId('props-add')).toHaveAttribute('data-first', 'true');
  await expect(page.getByTestId('props-add-title')).toHaveText('Add your first prop or place');
  await expect(page.getByTestId('props-add-with-photo')).toBeDisabled();
  const editor = page.getByTestId('shot-plan-editor');
  await expect(editor).toHaveAttribute('data-phase', 'g1');
  await expect(page.getByTestId('plan-no-props')).toBeVisible();
  await expect(page.getByTestId('plan-refs-S01')).toHaveText('No reference photos for this shot.');

  // --- a place with a photo: the rights answer comes first ---
  await page.getByTestId('props-add-kind-place').check();
  await page.getByTestId('props-add-file').setInputFiles({
    name: 'cabin.png', mimeType: 'image/png', buffer: testPhoto(800, 600, [40, 60, 200]) });
  await page.getByTestId('props-add-name').fill('Harbour cabin');
  await page.getByTestId('props-add-description').fill('a small blue wooden cabin with a red door and a round window');
  await expect(page.getByTestId('props-add-problem')).toContainText('whose picture this is');
  await expect(page.getByTestId('props-add-with-photo')).toBeDisabled();
  await page.getByTestId('props-add-rights-original').check();
  await page.getByTestId('props-add-with-photo').click();
  await expect(message).toHaveText('Harbour cabin added with its photo. Pick it on a shot in the shot plan.');
  const cabin = page.getByTestId('prop-harbour-cabin');
  await expect(cabin).toHaveAttribute('data-kind', 'place');
  const cabinPhoto = cabin.getByRole('img', { name: 'Harbour cabin: main photo' });
  await expect(cabinPhoto).toBeVisible();
  await expect.poll(() => cabinPhoto.evaluate((img: HTMLImageElement) => img.naturalWidth)).toBeGreaterThan(0);
  await expect(page.getByTestId('props-add-title')).toHaveText('Add a prop or place');

  // The photo is private: the API refuses it without a signed-in session.
  const library = await (await page.request.get(`${API}/api/episodes/${EPISODE}/props`)).json();
  const photo = library.items[0].photos[0];
  const anonymous = await apiRequest.newContext();
  expect((await anonymous.get(`${API}${photo.thumb_url}`)).status()).toBe(401);
  expect((await anonymous.get(`${API}${photo.url}`)).status()).toBe(401);
  await anonymous.dispose();

  // --- a prop without a photo, then its photo ---
  // The panel's reload after the add is slow here (the real answer, held back): what is typed into the
  // add form meanwhile stays, because the form is cleared with the message, not when the reload lands.
  const slowReload = async (route: Route) => {
    if (route.request().method() === 'GET') await new Promise((done) => setTimeout(done, 1500));
    await route.continue();
  };
  await page.route(`**/api/episodes/${EPISODE}/props`, slowReload);
  await page.getByTestId('props-add-kind-prop').check();
  await page.getByTestId('props-add-name').fill('Red umbrella');
  await page.getByTestId('props-add-description').fill('a bright red umbrella with a curved wooden handle');
  await page.getByTestId('props-add-button').click();
  await expect(message).toHaveText('Red umbrella added. Add a photo so the studio knows what it looks like.');
  await page.getByTestId('props-add-kind-place').check();
  await page.getByTestId('props-add-name').fill('Lighthouse');
  const umbrella = page.getByTestId('prop-red-umbrella');
  await expect(umbrella).toBeVisible();
  await expect(page.getByTestId('props-add-name')).toHaveValue('Lighthouse');
  await expect(page.getByTestId('props-add-kind-place')).toBeChecked();
  await page.unroute(`**/api/episodes/${EPISODE}/props`, slowReload);
  await page.getByTestId('props-add-name').fill('');
  await expect(umbrella).toContainText('No photo yet');
  await page.getByTestId('prop-photo-file-red-umbrella').setInputFiles({
    name: 'small.png', mimeType: 'image/png', buffer: testPhoto(200, 200, [220, 30, 30]) });
  await page.getByTestId('prop-rights-original-red-umbrella').check();
  await page.getByTestId('prop-photo-send-red-umbrella').click();
  await expect(message).toContainText('at least 512 px');
  await page.getByTestId('prop-photo-file-red-umbrella').setInputFiles({
    name: 'umbrella.png', mimeType: 'image/png', buffer: testPhoto(640, 640, [220, 30, 30]) });
  await page.getByTestId('prop-photo-send-red-umbrella').click();
  await expect(message).toHaveText('Photo of Red umbrella added.');
  await expect(umbrella.getByRole('img', { name: 'Red umbrella: main photo' })).toBeVisible();

  // --- pick them on S01 and edit S02's prompt; approval waits for the save ---
  await expect(page.getByTestId('plan-no-props')).toHaveCount(0);
  await page.getByTestId('plan-place-S01-harbour-cabin').click();
  await expect(page.getByTestId('plan-place-S01-harbour-cabin')).toHaveAttribute('aria-pressed', 'true');
  await page.getByTestId('plan-prop-S01-red-umbrella').click();
  await expect(page.getByTestId('plan-prop-S01-red-umbrella')).toHaveAttribute('aria-pressed', 'true');
  await expect(page.getByTestId('plan-changed-S01')).toHaveText('Not saved yet');
  await expect(page.getByTestId('approve-g1')).toBeDisabled();
  await expect(page.getByTestId('approve-g1-unsaved')).toHaveText('Save or undo your shot changes first.');

  await page.getByTestId('plan-edit-S02').click();
  await page.getByTestId('plan-prompt-S02').fill('   ');
  await expect(page.getByTestId('plan-problem')).toHaveText('Shot S02 needs a picture prompt.');
  await expect(page.getByTestId('plan-save')).toBeDisabled();
  await page.getByTestId('plan-prompt-S02').fill(NEW_PROMPT);
  await page.getByTestId('plan-edit-done-S02').click();
  await page.getByTestId('plan-note').fill('warmer light on the last shot');
  await expect(page.getByTestId('plan-save')).toHaveText('Save 2 shots');
  // While the save is on its way (held back here), nothing can change: the save clears what it sent.
  const slowSave = async (route: Route) => {
    if (route.request().method() === 'POST') await new Promise((done) => setTimeout(done, 1500));
    await route.continue();
  };
  await page.route(`**/api/episodes/${EPISODE}/shot-plan`, slowSave);
  await page.getByTestId('plan-save').click();
  await expect(page.getByTestId('plan-save')).toHaveText('Saving…');
  await expect(page.getByTestId('plan-prop-S01-red-umbrella')).toBeDisabled();
  await expect(page.getByTestId('plan-place-S01-harbour-cabin')).toBeDisabled();
  await expect(page.getByTestId('plan-note')).toBeDisabled();
  await expect(page.getByTestId('plan-message')).toHaveText(
    'Saved 2 shots and checked the plan again. Approve it when you are ready.');
  await page.unroute(`**/api/episodes/${EPISODE}/shot-plan`, slowSave);
  await expect(page.getByTestId('plan-prop-S01-red-umbrella')).toBeEnabled();
  await expect(page.getByTestId('plan-refs-S01')).toHaveText('Made with: Harbour cabin photo · Red umbrella photo');
  await expect(page.getByTestId('plan-refs-S02')).toHaveText('No reference photos for this shot.');
  await expect(page.getByTestId('plan-prompt-text-S02')).toHaveText(NEW_PROMPT);
  await expect(page.getByTestId('plan-changed-S01')).toHaveCount(0);

  // the history keeps who changed what, with the note
  await page.getByTestId('plan-history-S02').locator('summary').click();
  const s02History = page.getByTestId('plan-history-entry-S02');
  await expect(s02History).toHaveCount(1);
  await expect(s02History).toContainText('changed the picture prompt');
  await expect(s02History).toContainText('“warmer light on the last shot”');
  await expect(s02History).toContainText(`After: ${NEW_PROMPT}`);
  await expect(page.getByTestId('plan-history-entry-S01')).toHaveCount(2);

  type PlanShot = { shot_id: string; props: string[]; place: string | null; prompt: string;
    history: { field: string; run_id: string | null }[] };
  const plan = await (await page.request.get(`${API}/api/episodes/${EPISODE}/shot-plan`)).json();
  const s01: PlanShot = plan.shots.find((s: PlanShot) => s.shot_id === 'S01');
  expect(s01.place).toBe('harbour-cabin');
  expect(s01.props).toEqual(['red-umbrella']);
  expect(s01.history.map((h) => h.field).sort()).toEqual(['place', 'props']);
  expect(s01.history.every((h) => h.run_id)).toBe(true);
  expect(plan.phase.state).toBe('g1');

  // --- approve the shot plan: the scene pictures are made, the plan is locked ---
  await expect(page.getByTestId('approve-g1')).toBeEnabled();
  await page.getByTestId('approve-g1').click();
  await expect(page.getByTestId('run-message')).toHaveText('Shot plan approved.');
  await expect(page.getByTestId('current-gate')).toHaveText('G2_SCENE_STILLS');
  await expect(page.getByTestId('shot-S01-still')).toHaveText('done');
  await expect(page.getByTestId('shot-S02-still')).toHaveText('done');
  await expect(page.getByTestId('shot-plan-editor')).toHaveCount(0);
  await expect(page.getByTestId('g1-approved')).toBeAttached();

  const late = await page.request.post(`${API}/api/episodes/${EPISODE}/shot-plan`,
    { data: { shots: { S02: { prompt: 'A different prompt after the approval' } } } });
  expect(late.status()).toBe(409);
  expect((await late.json()).detail).toContain('approved');
  // the run made its pictures with the library: its props and places stay while it runs
  const removing = await page.request.post(`${API}/api/episodes/${EPISODE}/props/${library.items[0].prop_id}/delete`);
  expect(removing.status()).toBe(409);
});
