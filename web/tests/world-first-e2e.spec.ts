import { request as apiRequest, type Page } from '@playwright/test';
import { test, expect } from './e2e-cleanup';
import { readFileSync } from 'node:fs';
import zlib from 'node:zlib';

/**
 * Universe first (docs/WORLDS.md) against the real dry-run backend (playwright.e2e.config.ts, no API
 * mocks, DRAFT_LLM_FAKE drafts and bible). A new member's home opens on creating a Universe; its
 * references (notes and a photo), a mood board (dry run: free placeholder pictures, the price shown
 * first), the approval and the bible give it characters and a place, which get their photos, and a
 * character gets a saved voice: still no episode. A new dialogue episode from the Universe comes with
 * its cast matched by name and the saved voice filled in; it waits on its page until every look is
 * locked, then runs to the end, and the home shows the Universe and plays the finished episode. Another
 * member gets the 404 a missing Universe gets, with no names or pictures. The pages are checked at
 * 1440 px and at 390 px, where nothing scrolls sideways. Screenshots go to the test's output folder.
 */

const API = 'http://localhost:8000';
const OWNER = { name: 'Universe Owner', email: 'e2e-universe@test.local', password: 'e2e-universe-password' };
const OUTSIDER = { name: 'Universe Outsider', email: 'e2e-outsider@test.local', password: 'e2e-outsider-password' };
const UNIVERSE = 'Lantern Bay';
const CARD = 'nostalgia-time-travel';
const CARD_NAME = 'Back to the 2000s';
const IDEA = 'Mira Vale and Oskar Brine fix the old pier lantern before the autumn storm';
const NAMES = [UNIVERSE, 'Mira Vale', 'Oskar Brine', 'Lantern Pier'];
const MISSING = 'sr_000000000000';

let universeId = '';
let episodeId = '';
let savedVoice = '';
let savedVoiceId = '';
let universePictures: string[] = [];

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

/** A generated test picture (a plain block shape on a coloured background), never a real person or place. */
function testPicture(width: number, height: number, background: [number, number, number]): Buffer {
  const header = Buffer.alloc(13);
  header.writeUInt32BE(width, 0);
  header.writeUInt32BE(height, 4);
  header[8] = 8; // bit depth
  header[9] = 2; // RGB
  const stride = width * 3 + 1;
  const raw = Buffer.alloc(stride * height);
  for (let y = 0; y < height; y++) {
    for (let x = 0; x < width; x++) {
      const shape = Math.abs(x - width / 2) < width / 6 && y > height / 6 && y < (height * 5) / 6;
      raw.set(shape ? [200, 150, 40] : background, y * stride + 1 + x * 3);
    }
  }
  return Buffer.concat([
    Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]),
    pngChunk('IHDR', header),
    pngChunk('IDAT', zlib.deflateSync(raw)),
    pngChunk('IEND', Buffer.alloc(0)),
  ]);
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

async function screenshot(page: Page, name: string) {
  await page.screenshot({ path: test.info().outputPath(`${name}.png`), fullPage: true });
}

async function addNote(page: Page, topic: string, text: string) {
  await page.getByTestId('creation-topic').selectOption(topic);
  await page.getByTestId('creation-note-text').fill(text);
  await page.getByTestId('creation-note-add').click();
  await expect(page.getByTestId('creation-note-text')).toHaveValue('');
}

/** Make the character sheet of a look and lock it (dry run: free stand-in pictures). */
async function lockLook(page: Page, slug: string) {
  const sheet = page.getByTestId(`cast-sheet-${slug}`);
  await sheet.scrollIntoViewIfNeeded();
  await expect(sheet).toHaveAttribute('data-step', 'start');
  await page.getByTestId(`cast-check-options-${slug}`).click();
  await expect(page.getByTestId(`cast-estimate-${slug}`)).toContainText('free');
  await page.getByTestId(`cast-generate-${slug}`).click();
  await expect(sheet).toHaveAttribute('data-step', 'pick', { timeout: 120_000 });
  await sheet.getByRole('button', { name: 'Use this look' }).first().click();
  await expect(sheet).toHaveAttribute('data-step', 'turnaround');
  await page.getByTestId(`cast-check-turnaround-${slug}`).click();
  await expect(page.getByTestId(`cast-estimate-${slug}`)).toContainText('free');
  await page.getByTestId(`cast-generate-${slug}`).click();
  await expect(sheet).toHaveAttribute('data-step', 'approve', { timeout: 120_000 });
  await page.getByTestId(`cast-approve-${slug}`).click();
  await expect(sheet).toHaveAttribute('data-step', 'locked');
}

test.describe.serial('Universe first', () => {
  test.use({ viewport: { width: 1440, height: 900 } });

  test.beforeAll(async ({}, testInfo) => {
    const admin = await apiRequest.newContext({
      extraHTTPHeaders: { Authorization: `Bearer ${testInfo.config.metadata.adminSecret}` },
    });
    for (const who of [OWNER, OUTSIDER]) {
      const res = await admin.post(`${API}/api/users`, { data: { ...who, role: 'member' } });
      expect(res.status(), await res.text()).toBe(200);
    }
    await admin.dispose();
  });

  test('a new member creates a Universe: references, mood board, bible, photos and a saved voice', async ({ page }) => {
    page.on('dialog', (dialog) => dialog.accept());
    await signIn(page, OWNER);

    // nothing yet: the home opens on creating a Universe, with a way to just make one video
    await expect(page.getByTestId('first-run')).toBeVisible();
    await expect(page.getByTestId('make-video-link')).toHaveText('Skip, just make one video');
    await expect(page.getByTestId('make-video-link')).toHaveAttribute('href', '/studio/create');
    await expect(page.getByTestId('universe-create-form')).toBeVisible();
    await expectNoSidewaysScroll(page);
    await screenshot(page, 'onboarding-1440');

    await page.getByTestId('universe-name').fill(UNIVERSE);
    await page.getByTestId('universe-logline').fill('A rainy harbour town where two friends keep the old lights burning');
    await page.getByTestId('universe-genre').selectOption(CARD);
    await page.getByTestId('universe-create').click();
    await page.waitForURL(/\/studio\/universes\/sr_[0-9a-f]+\?created=1$/);
    universeId = new URL(page.url()).pathname.split('/').pop()!;
    await expect(page.getByTestId('universe-name')).toHaveText(UNIVERSE);
    await expect(page.getByTestId('universe-genre')).toHaveText(CARD_NAME);
    await expect(page.getByTestId('universe-health')).toHaveText('No characters yet');

    // 1. references: notes on each topic and a photo, with the rights answer first
    await addNote(page, 'premise', 'Two friends keep the harbour lights of a rainy seaside town burning through the autumn storms.');
    await addNote(page, 'characters',
      'Mira Vale: a lighthouse keeper in her sixties with short grey hair and a yellow oilskin coat\n'
      + 'Oskar Brine: a young ferry pilot with a red knitted beanie and a navy jumper');
    await addNote(page, 'places', 'Lantern Pier: a long wooden pier lit by brass lanterns');
    await addNote(page, 'tone', 'Warm and gentle, rainy evenings, amber light');
    await expect(page.locator('[data-testid^="creation-note-un_"]')).toHaveCount(4);
    await page.getByTestId('creation-photo-file').setInputFiles({
      name: 'harbour.png', mimeType: 'image/png', buffer: testPicture(800, 600, [60, 90, 140]) });
    await page.getByTestId('creation-photo-caption').fill('The colours of the harbour at dusk');
    await page.getByTestId('creation-rights-original').check();
    await page.getByTestId('creation-photo-send').click();
    await expect(page.locator('[data-testid^="creation-photo-ui_"]')).toHaveCount(1);

    // 2. the mood board: its price first, then the pictures
    await page.getByTestId('board-check-price').click();
    await expect(page.getByTestId('board-estimate')).toContainText('4 placeholder images, free in dry-run mode');
    await page.getByTestId('board-make').click();
    await expect(page.getByTestId('board-images-1').locator('img')).toHaveCount(4);
    // 3. approved, nothing downstream ran before
    await expect(page.getByTestId('bible-write')).toHaveCount(0);
    await page.getByTestId('board-approve').click();
    await expect(page.getByTestId('board-approved')).toBeVisible();
    // 4. the bible: its characters and place join the Universe
    await page.getByTestId('bible-write').click();
    await expect(page.getByTestId('bible-done')).toBeVisible();
    await expect(page.getByTestId('creation-message-bible')).toHaveText('Added 2 characters, 1 place and 0 props.');
    await expect(page.getByTestId('cast-character-mira-vale')).toBeVisible();
    await expect(page.getByTestId('cast-character-oskar-brine')).toBeVisible();
    await expect(page.getByTestId('prop-lantern-pier')).toBeVisible();

    // photos of a character and of the place
    await page.getByTestId('cast-upload-file-mira-vale').setInputFiles({
      name: 'mira.png', mimeType: 'image/png', buffer: testPicture(640, 800, [235, 240, 245]) });
    await page.getByTestId('cast-rights-original-mira-vale').check();
    await page.getByTestId('cast-upload-send-mira-vale').click();
    await expect(page.getByTestId('cast-message')).toHaveText('Picture of Mira Vale added.');
    await page.getByTestId('prop-photo-file-lantern-pier').setInputFiles({
      name: 'pier.png', mimeType: 'image/png', buffer: testPicture(800, 600, [120, 100, 80]) });
    await page.getByTestId('prop-rights-original-lantern-pier').check();
    await page.getByTestId('prop-photo-send-lantern-pier').click();
    await expect(page.getByTestId('prop-lantern-pier').getByRole('img', { name: 'Lantern Pier: main photo' })).toBeVisible();
    await expect(page.getByTestId('prop-lantern-pier')).toContainText('1 photo');
    await expect(page.getByTestId('universe-item-badge-lantern-pier')).toHaveText('Photo set');

    // a saved voice for Mira Vale: found with the wizard's voice search, saved without generating anything
    await page.getByTestId('voice-description-voice-mira-vale').fill('warm, low, unhurried voice of an older woman');
    await page.getByTestId('voice-find-voice-mira-vale').click();
    await expect(page.getByTestId('voice-choice-voice-mira-vale')).toHaveAttribute('data-voice-id', /^dryrun-voice-/);
    savedVoiceId = (await page.getByTestId('voice-choice-voice-mira-vale').getAttribute('data-voice-id'))!;
    savedVoice = (await page.getByTestId('voice-name-voice-mira-vale').textContent())!.trim();
    await page.getByTestId('saved-voice-save-voice-mira-vale').click();
    await expect(page.getByTestId('saved-voice-name-voice-mira-vale')).toHaveText(savedVoice);

    // no episode yet: every look waits for one, and the film is a placeholder that asks for nothing
    await expect(page.getByTestId('universe-health')).toHaveText('0 of 2 characters locked');
    await expect(page.getByTestId('universe-badge-mira-vale')).toHaveText('Not locked yet');
    await expect(page.getByTestId('universe-used-mira-vale')).toHaveText('Not used in an episode yet');
    await expect(page.getByTestId('universe-lock-mira-vale')).toContainText('Lock this look from an episode in this Universe');
    await expect(page.getByTestId('universe-lock-link-mira-vale')).toHaveCount(0);
    await expect(page.getByTestId('universe-episodes-empty')).toBeVisible();
    await expect(page.getByTestId('universe-film-soon')).toHaveText('Coming soon');
    await expect(page.getByTestId('universe-film-button')).toBeDisabled();
    await expectNoSidewaysScroll(page);
    await screenshot(page, 'universe-1440');

    const view = await (await page.request.get(`${API}/api/worlds/${universeId}`)).json();
    expect(view.world.counts).toMatchObject({ characters: 2, locked_characters: 0, places: 1, episodes: 0 });
    type Picture = { url: string; thumb_url: string };
    universePictures = [
      ...view.characters.flatMap((c: { versions: { images: Picture[] }[] }) => c.versions.flatMap((v) => v.images)),
      ...view.items.flatMap((i: { photos: Picture[] }) => i.photos),
      ...view.creation.photos,
      ...view.creation.boards.flatMap((b: { images: Picture[] }) => b.images),
    ].flatMap((p: Picture) => [p.url, p.thumb_url]);
    expect(universePictures.length).toBeGreaterThanOrEqual(14);
    for (const url of universePictures) expect((await page.request.get(`${API}${url}`)).status(), url).toBe(200);

    // the home now lists it
    await page.goto('/studio');
    await expect(page.getByTestId(`universe-card-${universeId}`)).toContainText(UNIVERSE);
    await expect(page.getByTestId(`universe-health-${universeId}`)).toHaveText('0 of 2 characters locked');
  });

  test('a dialogue episode from the Universe: its cast and saved voice come along, it waits for locked looks, '
    + 'runs to the end and plays on the home', async ({ page }) => {
    test.slow(); // a character sheet for each look, then the whole dry run
    page.on('dialog', (dialog) => dialog.accept());
    await signIn(page, OWNER);
    await page.goto(`/studio/universes/${universeId}`);
    await page.getByTestId('universe-new-episode').click();
    await page.waitForURL(`/studio/create?world=${universeId}`);
    await expect(page.getByTestId('wizard-universe')).toHaveText(`In: ${UNIVERSE}`);
    // the Universe's genre is the card
    await expect(page.getByTestId('selected-style')).toContainText(`Style: ${CARD_NAME}`);
    await page.getByTestId('idea-input').fill(IDEA);
    const draft = page.waitForRequest((req) => req.url() === `${API}/api/episodes/draft-from-idea` && req.method() === 'POST');
    await page.getByTestId('generate-button').click();
    expect((await draft).postDataJSON()).toMatchObject({ archetype_id: CARD, world_id: universeId });
    await expect(page.getByText('Step 3: Review & Start')).toBeVisible({ timeout: 60_000 });
    episodeId = (await page.locator('text=/Episode: ep[0-9]{2}[a-z]{3}/').textContent())!.replace('Episode: ', '');

    // the drafted characters are the Universe's, matched by name; no series to pick
    await expect(page.getByTestId('cast-series-select')).toHaveCount(0);
    await expect(page.getByTestId('cast-match-A')).toHaveText('Same as in the Universe: Mira Vale · Not locked yet');
    await expect(page.getByTestId('cast-match-B')).toHaveText('Same as in the Universe: Oskar Brine · Not locked yet');
    await expect(page.getByTestId('cast-same-A')).toHaveValue('mira-vale');
    // the speakers are the Universe's characters: Mira Vale's saved voice is filled in, Oskar Brine needs one
    await expect(page.getByTestId('voice-saved-name-speaker-mira-vale')).toHaveText(`Saved voice: ${savedVoice}`);
    await expect(page.getByTestId('voice-saved-change-speaker-mira-vale')).toHaveText('Use a different voice for this episode');
    await expect(page.getByTestId('voice-block')).toContainText('Oskar Brine');
    await page.getByTestId('voice-description-speaker-oskar-brine').fill('bright, quick voice of a young man');
    await page.getByTestId('voice-find-speaker-oskar-brine').click();
    await expect(page.getByTestId('voice-choice-speaker-oskar-brine')).toHaveAttribute('data-voice-id', /^dryrun-voice-/);
    const oskarVoiceId = (await page.getByTestId('voice-choice-speaker-oskar-brine').getAttribute('data-voice-id'))!;
    await expect(page.getByTestId('voice-block')).toHaveCount(0);
    await expect(page.getByTestId('universe-lock-note')).toBeVisible();
    await expectNoSidewaysScroll(page);
    await screenshot(page, 'wizard-1440');

    // its looks are not locked yet: the video is set up in the Universe and waits on its page
    const audioPlan = page.waitForResponse((res) => res.url() === `${API}/api/episodes/${episodeId}/upload-audio-plan`
      && res.request().method() === 'POST');
    await page.getByTestId('preview-button').click();
    // the saved voice speaks Mira Vale's lines (read from the plan the API wrote: the backend runs on this
    // machine, and Chromium hides multipart file bodies)
    const saved = await (await audioPlan).json();
    const plan = JSON.parse(readFileSync(saved.audio_plan_path, 'utf-8'));
    expect(plan.vo.speakers).toEqual({ 'Mira Vale': savedVoiceId, 'Oskar Brine': oskarVoiceId });
    expect(saved.lines_missing_voice).toEqual([]);
    await page.waitForURL(new RegExp(`/studio/episodes/${episodeId}#cast$`), { timeout: 60_000 });
    await expect(page.getByTestId('breadcrumb-universe')).toHaveText(UNIVERSE);
    await expect(page.getByTestId('breadcrumb-universe')).toHaveAttribute('href', `/studio/universes/${universeId}`);
    await expect(page.getByTestId('start-lock')).toContainText(
      `Lock the look of Mira Vale and Oskar Brine before this video starts: it is in ${UNIVERSE}`);
    await expect(page.getByTestId('start-mode')).toHaveText('It starts as a free test run: no credits are used.');
    await expect(page.getByTestId('start-episode')).toBeDisabled();
    await expect(page.getByTestId('strip-character-mira-vale-badge')).toHaveText('Not locked yet');
    await expect(page.getByTestId('strip-character-oskar-brine-badge')).toHaveText('Not locked yet');
    // the studio refuses to start it, whoever asks
    const refused = await page.request.post(`${API}/api/episodes`, {
      data: { episode_id: episodeId, dry_run: true, enable_story_shaping: true } });
    expect(refused.status()).toBe(409);
    expect((await refused.json()).detail).toContain('Lock the look of Mira Vale and Oskar Brine before this video starts');
    await screenshot(page, 'episode-waiting-1440');

    // lock both looks on the episode's page: then it may start
    await lockLook(page, 'mira-vale');
    await lockLook(page, 'oskar-brine');
    await expect(page.getByTestId('start-ready')).toBeVisible();
    await expect(page.getByTestId('start-episode')).toBeEnabled();
    await expect(page.getByTestId('strip-character-mira-vale-badge')).toHaveText('Locked v1');
    await expect(page.getByTestId('strip-character-oskar-brine-badge')).toHaveText('Locked v1');
    await page.getByTestId('breadcrumb-universe').scrollIntoViewIfNeeded();
    await screenshot(page, 'episode-breadcrumb-1440');

    // it starts with the request the wizard kept: the card, the idea and the free test run
    const start = page.waitForRequest((req) => req.url() === `${API}/api/episodes` && req.method() === 'POST');
    await page.getByTestId('start-episode').click();
    const sent = (await start).postDataJSON();
    expect(sent).toMatchObject({ episode_id: episodeId, dry_run: true, enable_story_shaping: true, archetype_id: CARD });
    expect(sent.idea).toContain(IDEA);
    await expect(page.getByTestId('start-card')).toHaveCount(0, { timeout: 60_000 });
    expect(await page.evaluate((id) => window.localStorage.getItem(`ddg:start:${id}`), episodeId)).toBeNull();
    await expect(page.getByTestId('next-step-title')).toHaveText('Read and approve your story', { timeout: 120_000 });
    await page.getByTestId('story-approve').click();
    await expect(page.getByTestId('story-approved')).toBeVisible();
    await page.getByTestId('approve-g1').click();
    await expect(page.getByTestId('run-message')).toHaveText('Shot plan approved.');

    await expect(page.getByTestId('current-gate')).toHaveText('G2_SCENE_STILLS', { timeout: 120_000 });
    let shotIds: string[] = [];
    await expect.poll(async () => {
      const res = await page.request.get(`${API}/api/episodes/${episodeId}/shot-states`);
      if (!res.ok()) return `HTTP ${res.status()}`;
      const shots: Record<string, { still_status?: string }> = (await res.json()).shots;
      shotIds = Object.keys(shots).sort();
      return shotIds.length > 0 && shotIds.every((sid) => shots[sid].still_status === 'done') ? 'ready' : 'making';
    }, { timeout: 120_000 }).toBe('ready');
    // each scene picture says what it was made with
    await expect(page.getByTestId(`made-with-${shotIds[0]}`)).toContainText('Mira Vale');
    await expect(page.getByTestId(`made-with-${shotIds[0]}`)).toContainText('Oskar Brine');
    await page.getByTestId('approve-g2').click();
    await expect(page.getByTestId('run-message')).toHaveText(/^Scene pictures approved\./);
    await expect(page.getByTestId('next-step-title')).toHaveText('Watch and approve the final cut', { timeout: 180_000 });
    await expect(page.getByTestId('final-blockers')).toHaveCount(0, { timeout: 120_000 });
    await page.getByTestId('approve-final').click();
    await expect(page.getByTestId('run-finished')).toHaveText('Finished', { timeout: 120_000 });

    // the home: the Universe with its locked looks, and the finished episode playing in place
    await page.goto('/studio');
    await expect(page.getByTestId(`universe-card-${universeId}`)).toContainText(UNIVERSE);
    await expect(page.getByTestId(`universe-health-${universeId}`)).toHaveText('2 of 2 characters locked');
    const video = page.getByTestId(`finished-video-${episodeId}`);
    await expect(video).toHaveAttribute('src', `${API}/api/episodes/${episodeId}/files/phone`);
    await expect(video).toHaveAttribute('controls', '');
    await expect(video).toHaveAttribute('preload', 'none');
    await expect(video).toHaveAttribute('poster', new RegExp(`/api/episodes/${episodeId}/`));
    await expect(page.getByTestId(`finished-download-${episodeId}`)).toHaveAttribute(
      'href', `${API}/api/episodes/${episodeId}/files/phone?download=true`);
    await expect(page.getByTestId(`episode-universe-${episodeId}`)).toHaveText(`In ${UNIVERSE}`);
    await expectNoSidewaysScroll(page);
    await screenshot(page, 'home-1440');

    // the Universe lists it, counts it and offers it to the coming film
    await page.goto(`/studio/universes/${universeId}`);
    await expect(page.getByTestId(`universe-episode-${episodeId}`)).toContainText('Finished');
    await expect(page.getByTestId('universe-used-mira-vale')).toHaveText('Used in 1 episode');
    await expect(page.getByTestId('universe-badge-mira-vale')).toHaveText('Locked v1');
    const film = page.getByTestId('universe-film-episodes');
    await expect(film.getByRole('checkbox')).toHaveCount(1);
    await expect(film.getByRole('checkbox')).toBeDisabled();
    const asked: string[] = [];
    page.on('request', (req) => {
      if (req.url().startsWith(API) && req.method() !== 'GET') asked.push(`${req.method()} ${req.url()}`);
    });
    await page.getByTestId('universe-film-button').click({ force: true });
    await page.waitForTimeout(1_000);
    expect(asked).toEqual([]);
  });

  test.describe('at 390 px', () => {
    test.use({ viewport: { width: 390, height: 844 } });

    test('another member: a first run of their own, and the 404 a missing Universe gets', async ({ page }) => {
      await signIn(page, OUTSIDER);
      await expect(page.getByTestId('first-run')).toBeVisible();
      await expect(page.getByTestId(`universe-card-${universeId}`)).toHaveCount(0);
      await expectNoSidewaysScroll(page);
      await screenshot(page, 'onboarding-390');

      const theirs = await page.request.get(`${API}/api/worlds/${universeId}`);
      const missing = await page.request.get(`${API}/api/worlds/${MISSING}`);
      expect(theirs.status()).toBe(404);
      expect(missing.status()).toBe(404);
      expect((await theirs.json()).detail.replace(universeId, '<id>')).toBe((await missing.json()).detail.replace(MISSING, '<id>'));
      for (const url of universePictures) expect((await page.request.get(`${API}${url}`)).status(), url).toBe(404);
      const write = await page.request.post(`${API}/api/worlds/${universeId}/profile`, { data: { logline: 'Mine now' } });
      expect(write.status()).toBe(404);

      await page.goto(`/studio/universes/${universeId}`);
      await expect(page.getByTestId('universe-not-found')).toBeVisible();
      const shown = await page.locator('main').innerText();
      for (const name of NAMES) expect(shown).not.toContain(name);
      await expect(page.locator('main img')).toHaveCount(0);
      await expectNoSidewaysScroll(page);

      // the episode itself is open to members, read-only: its Universe and what it uses are not shown
      const info = await (await page.request.get(`${API}/api/episodes/${episodeId}/universe`)).json();
      expect(info).toMatchObject({ can_act: false, linked: null, universe: null, strip: null, lock: null });
      await page.goto(`/studio/episodes/${episodeId}`);
      await expect(page.getByTestId('episode-breadcrumb')).toBeVisible();
      await expect(page.getByTestId('breadcrumb-universe')).toHaveCount(0);
      await expect(page.getByTestId('universe-strip')).toHaveCount(0);
      await expect(page.getByText('Waiting for you')).toHaveCount(0);
    });

    test('the owner\'s pages fit a phone', async ({ page }) => {
      await signIn(page, OWNER);
      await expect(page.getByTestId(`universe-card-${universeId}`)).toBeVisible();
      await expect(page.getByTestId(`finished-video-${episodeId}`)).toBeVisible();
      await expectNoSidewaysScroll(page);
      await screenshot(page, 'home-390');

      await page.goto(`/studio/universes/${universeId}`);
      await expect(page.getByTestId('universe-name')).toHaveText(UNIVERSE);
      await expect(page.getByTestId('cast-character-mira-vale')).toBeVisible();
      await expectNoSidewaysScroll(page);
      await screenshot(page, 'universe-390');

      await page.goto(`/studio/create?world=${universeId}`);
      await expect(page.getByTestId('wizard-universe')).toHaveText(`In: ${UNIVERSE}`);
      await expectNoSidewaysScroll(page);

      await page.goto(`/studio/episodes/${episodeId}`);
      await expect(page.getByTestId('breadcrumb-universe')).toHaveText(UNIVERSE);
      await expect(page.getByTestId('universe-strip')).toBeVisible();
      await expect(page.getByTestId('ready-video')).toBeVisible();
      await expectNoSidewaysScroll(page);
      await screenshot(page, 'episode-breadcrumb-390');
    });
  });
});
