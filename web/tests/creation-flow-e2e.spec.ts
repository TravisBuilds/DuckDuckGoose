import { request as apiRequest, type Locator, type Page } from '@playwright/test';
import { test, expect } from './e2e-cleanup';
import zlib from 'node:zlib';

/**
 * The creation flow fixes (docs/CREATION-FLOW-FIXES.md) against the real dry-run backend
 * (playwright.e2e.config.ts: no API mocks, DRAFT_LLM_FAKE reads the story into its reference list and
 * writes the bible, free stand-in pictures, no provider and no credits).
 *
 * Story first: a new Universe starts from its story; the studio lists the references the story needs,
 * photos upload onto the list (a small one is kept with a plain note, one under the floor is refused
 * before it is sent), the owner adds and removes items, and the progress reads "6 of 6 ready". The
 * mood board and the bible follow as before, and the photos on the list go to the characters, places
 * and props the bible made.
 *
 * First frames: the wizard keeps the start (in the studio, not the browser) while the owner picks
 * the shots' first frames on the episode's page: each shot in plain words, the price first, a round
 * of options, a change in plain words, the pick. A fresh browser (another device) shows the start
 * card and the picked frame and starts the run with the request the wizard kept, and the picked shot
 * films from its picked frame (its scene picture is the pre-approved one). Each new screen is
 * checked at 1440 px and at 390 px, where nothing scrolls sideways; First frames and the start card
 * also at 320 px, where every button and label stays on one line, uncut, and no word breaks.
 * Screenshots go to the test's output folder.
 */

const API = 'http://localhost:8000';
const OWNER = { name: 'Story Owner', email: 'e2e-story@test.local', password: 'e2e-story-password' };
const UNIVERSE = 'Copper Cove';
const CARD = 'nostalgia-time-travel';
const STORY = 'Wren mends sails in the harbour town. One windy night she finds a copper bell in the old lighthouse. '
  + 'She carries the bell to the night market, where Tobin sells kites with a paper moon.';
const IDEA = 'A small paper boat sails across a rain puddle to deliver a note to a snail';
const SMALL_PHONE = { width: 320, height: 720 };
const PHONE = { width: 390, height: 844 };
const DESKTOP = { width: 1440, height: 900 };
// the buttons and labels of First frames and the start card that stay on one line
const ONE_LINE = 'button, summary, .pf-chip, [data-testid="first-frames-summary"], [data-testid^="frames-status-"], '
  + '[data-testid^="frames-option-"] span, [data-testid^="frames-changes-"] > span';

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

/** The whole page, or the part of it given, from the top of the page (the bar stays at the top). */
async function screenshot(page: Page, name: string, part?: Locator) {
  await page.evaluate(() => window.scrollTo(0, 0));
  const box = part ? await part.boundingBox() : null;
  const clip = box ? { x: 0, y: Math.max(0, box.y - 16), width: page.viewportSize()!.width, height: box.height + 32 } : undefined;
  await page.screenshot({ path: test.info().outputPath(`${name}.png`), fullPage: true, clip });
}

/** What one screen shows: from the part given (`gap` px under the top of the screen, below the bar) or from the top. */
async function oneScreen(page: Page, name: string, part?: Locator, gap = 72) {
  if (part) await part.evaluate((el, below) => window.scrollTo(0, el.getBoundingClientRect().top + window.scrollY - below), gap);
  else await page.evaluate(() => window.scrollTo(0, 0));
  await page.screenshot({ path: test.info().outputPath(`${name}.png`) });
}

/** The same screen at 390 px, where nothing scrolls sideways: one phone screen; then back to 1440 px. */
async function onPhone(page: Page, name: string, part?: Locator) {
  await page.setViewportSize(PHONE);
  await expectNoSidewaysScroll(page);
  await oneScreen(page, `${name}-390`, part);
  await page.setViewportSize(DESKTOP);
}

/**
 * What reads badly in a part of the page (run in the browser): a button or label (`oneLine`) that
 * takes more than one line or is cut off, or a word of any text broken across two lines.
 */
function layoutProblems(root: Element, oneLine: string): string[] {
  const problems: string[] = [];
  const lines = (rects: DOMRectList) => {
    const tops = [...rects].filter((r) => r.width > 0.5 && r.height > 0.5).map((r) => r.top).sort((a, b) => a - b);
    return tops.filter((top, i) => i === 0 || top - tops[i - 1] > 4).length;
  };
  const shown = (el: Element) => el.getClientRects().length > 0 && getComputedStyle(el).visibility !== 'hidden';
  const say = (el: Element) => `"${(el.textContent ?? '').trim().replace(/\s+/g, ' ').slice(0, 60)}"`;
  // the screen and every box around the element that cuts off what overflows it
  const edges = (el: Element) => {
    const out: { left: number; right: number }[] = [{ left: 0, right: window.innerWidth }];
    for (let up = el.parentElement; up && up !== document.body; up = up.parentElement) {
      if (getComputedStyle(up).overflowX !== 'visible') out.push(up.getBoundingClientRect());
    }
    return out;
  };
  for (const el of root.querySelectorAll(oneLine)) {
    if (!shown(el) || !(el.textContent ?? '').trim()) continue;
    const range = document.createRange();
    range.selectNodeContents(el);
    if (lines(range.getClientRects()) > 1) problems.push(`${say(el)} takes more than one line`);
    const box = el.getBoundingClientRect();
    const cut = edges(el).some((edge) => box.left < edge.left - 0.5 || box.right > edge.right + 0.5);
    if (cut || el.scrollWidth > el.clientWidth + 1) problems.push(`${say(el)} is cut off`);
  }
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  for (let node = walker.nextNode(); node; node = walker.nextNode()) {
    const text = node.textContent ?? '';
    if (!text.trim() || !node.parentElement || !shown(node.parentElement)) continue;
    for (const word of text.matchAll(/[^\s\-\u2010-\u2015/]+/g)) {
      const range = document.createRange();
      range.setStart(node, word.index!);
      range.setEnd(node, word.index! + word[0].length);
      if (lines(range.getClientRects()) > 1) problems.push(`"${word[0]}" breaks across lines in ${say(node.parentElement)}`);
    }
  }
  return problems;
}

/** The part reads well at this width: nothing scrolls sideways, no button or label wraps or is cut off, no word breaks. */
async function expectTidy(page: Page, part: Locator, what: string) {
  await expectNoSidewaysScroll(page);
  const width = page.viewportSize()!.width;
  expect(await part.evaluate(layoutProblems, ONE_LINE), `${what} at ${width} px`).toEqual([]);
}

/** The id of the reference list item with this name. */
async function itemId(page: Page, name: string): Promise<string> {
  const row = page.locator('[data-testid^="checklist-item-"]').filter({
    has: page.getByRole('heading', { name, exact: true }) });
  await expect(row).toHaveCount(1);
  return (await row.getAttribute('data-testid'))!.replace('checklist-item-', '');
}

/** A photo onto a list item: chosen, its rights answered, sent. */
async function addPhoto(page: Page, id: string, name: string, picture: Buffer, note: string | null) {
  await page.getByTestId(`checklist-add-photo-${id}`).click();
  await page.getByTestId(`checklist-file-${id}`).setInputFiles({ name: 'reference.png', mimeType: 'image/png', buffer: picture });
  if (note) await expect(page.getByTestId(`checklist-size-${id}`)).toHaveText(note);
  else await expect(page.getByTestId(`checklist-size-${id}`)).toHaveCount(0);
  await page.getByTestId(`checklist-rights-original-${id}`).check();
  await page.getByTestId(`checklist-send-${id}`).click();
  await expect(page.getByTestId('creation-message-checklist')).toHaveText(`Photo added to ${name}.`);
}

test.describe.serial('Creation flow fixes', () => {
  test.use({ viewport: DESKTOP });

  test.beforeAll(async ({}, testInfo) => {
    const admin = await apiRequest.newContext({
      extraHTTPHeaders: { Authorization: `Bearer ${testInfo.config.metadata.adminSecret}` },
    });
    const res = await admin.post(`${API}/api/users`, { data: { ...OWNER, role: 'member' } });
    expect(res.status(), await res.text()).toBe(200);
    await admin.dispose();
  });

  test('story first: the story, its reference list, photos onto it, then the mood board and the bible', async ({ page }) => {
    await signIn(page, OWNER);
    await page.getByTestId('universe-name').fill(UNIVERSE);
    await page.getByTestId('universe-logline').fill('A windy harbour town where a sail mender keeps the old bells ringing');
    await page.getByTestId('universe-genre').selectOption(CARD);
    await page.getByTestId('universe-create').click();
    await page.waitForURL(/\/studio\/universes\/sr_[0-9a-f]+\/create\?step=references$/);
    const universeId = new URL(page.url()).pathname.split('/')[3];
    const base = `${API}/api/worlds/${universeId}`;

    // the story comes first: the list waits for it, and so does the mood board
    await expect(page.getByTestId('story-card')).toBeVisible();
    await expect(page.getByTestId('story-saved')).toHaveText('Nothing written yet');
    await expect(page.getByTestId('checklist-make')).toBeDisabled();
    await expect(page.getByTestId('story-make-why')).toBeVisible();
    await expect(page.getByTestId('creation-count-story')).toHaveText('Not yet');
    await expect(page.getByTestId('creation-count-list')).toHaveText('Not made yet');
    await expect(page.getByTestId('creation-next')).toBeDisabled();
    await expectNoSidewaysScroll(page);
    await screenshot(page, 'story-empty-1440');
    await onPhone(page, 'story-empty');

    // the studio reads the story into the references it needs (one language model call, nothing charged)
    await page.getByTestId('story-text').fill(STORY);
    await expect(page.getByTestId('story-saved')).toHaveText('Not saved yet');
    await page.getByTestId('checklist-make').click();
    await expect(page.getByTestId('story-saved')).toHaveText('Saved');
    const rows = page.locator('[data-testid^="checklist-item-"]');
    await expect(rows).toHaveCount(7);
    await expect(page.getByTestId('creation-message-checklist')).toHaveText('Your list is ready: 7 references added.');
    await expect(page.getByTestId('checklist-progress')).toHaveText('0 of 6 ready · 0 of 1 optional');
    await expect(page.getByTestId('creation-count-story')).toHaveText('Written');
    const names = await page.locator('[data-testid^="checklist-name-"]').allTextContents();
    expect(names).toEqual(['Wren', 'Tobin', 'Harbour town', 'Old lighthouse', 'Night market', 'Copper bell', 'Paper moon']);
    const ids: Record<string, string> = {};
    for (const name of names) ids[name] = await itemId(page, name);
    // each item says why the story needs it, how many photos are ideal and the fewest that work
    await expect(page.getByTestId(`checklist-why-${ids.Wren}`)).toContainText('Wren is in the story');
    await expect(page.getByTestId(`checklist-counts-${ids.Wren}`)).toContainText('Ideally 3 photos, at least 1');
    await expect(page.getByTestId(`checklist-status-${ids.Wren}`)).toHaveText('Needs 1 photo');
    await expect(page.getByTestId(`checklist-item-${ids['Paper moon']}`)).toHaveAttribute('data-optional', 'true');
    await expect(page.getByTestId(`checklist-status-${ids['Paper moon']}`)).toHaveText('Optional');
    // the mood board opens once the story is written
    await expect(page.getByTestId('creation-next')).toBeEnabled();

    // photos onto the list: a small picture from the web is kept with a plain note, never refused
    await addPhoto(page, ids.Wren, 'Wren', testPicture(300, 400, [235, 240, 245]), "Small image, we'll sharpen it up.");
    await expect(page.getByTestId(`checklist-status-${ids.Wren}`)).toHaveText('Ready · 2 more photos would help');
    await expect(page.getByTestId(`checklist-photos-${ids.Wren}`)).toContainText("Small image, we'll sharpen it up.");
    await expect(page.getByTestId('checklist-progress')).toHaveText('1 of 6 ready · 0 of 1 optional');
    // one under the floor is refused before it is sent, in plain words
    await page.getByTestId(`checklist-add-photo-${ids.Tobin}`).click();
    await page.getByTestId(`checklist-file-${ids.Tobin}`).setInputFiles({
      name: 'tiny.png', mimeType: 'image/png', buffer: testPicture(100, 100, [90, 120, 60]) });
    await expect(page.getByTestId(`checklist-upload-${ids.Tobin}`)).toContainText(
      'This picture is 100x100 px; pictures need at least 128 px on each side. Choose a bigger one.');
    await page.getByTestId(`checklist-rights-original-${ids.Tobin}`).check();
    await expect(page.getByTestId(`checklist-send-${ids.Tobin}`)).toBeDisabled();
    await page.getByTestId(`checklist-upload-${ids.Tobin}`).getByRole('button', { name: 'Cancel' }).click();
    // a very small one is kept too, with the note that a bigger one looks sharper
    await addPhoto(page, ids.Tobin, 'Tobin', testPicture(160, 200, [90, 120, 60]),
      "Very small image: we'll sharpen it up, but a bigger one will look sharper.");
    const backgrounds: [number, number, number][] = [[60, 90, 140], [120, 100, 80], [140, 60, 90], [70, 130, 120]];
    for (const [k, name] of ['Harbour town', 'Old lighthouse', 'Night market', 'Copper bell'].entries()) {
      await addPhoto(page, ids[name], name, testPicture(800, 600, backgrounds[k]), null);
    }
    await expect(page.getByTestId('checklist-progress')).toHaveText('6 of 6 ready · 0 of 1 optional');

    // the owner adds what the story missed and takes off what it does not need
    await page.getByTestId('checklist-add-kind').selectOption('prop');
    await page.getByTestId('checklist-add-name').fill('Rain cloak');
    await page.getByTestId('checklist-add-why').fill('Wren wears it on the windy night');
    await page.getByTestId('checklist-add-send').click();
    await expect(page.getByTestId('creation-message-checklist')).toHaveText('Rain cloak is on the list.');
    await expect(rows).toHaveCount(8);
    await expect(page.getByTestId('checklist-progress')).toHaveText('6 of 7 ready · 0 of 1 optional');
    ids['Rain cloak'] = await itemId(page, 'Rain cloak');
    await page.getByTestId(`checklist-remove-${ids['Paper moon']}`).click();
    await page.getByTestId(`checklist-remove-${ids['Paper moon']}-confirm`).click();
    await expect(page.getByTestId('creation-message-checklist')).toHaveText('Paper moon is off the list.');
    await expect(rows).toHaveCount(7);
    await expect(page.getByTestId('checklist-progress')).toHaveText('6 of 7 ready');
    await expect(page.getByTestId('creation-count-list')).toHaveText('6 of 7 ready');
    await expectNoSidewaysScroll(page);
    await screenshot(page, 'story-checklist-1440');
    await onPhone(page, 'story-checklist', page.getByTestId('checklist'));

    // then the mood board and the bible, as before; nothing above was charged
    await page.getByTestId('creation-next').click();
    await page.waitForURL(`/studio/universes/${universeId}/create?step=moodboard`);
    await page.getByTestId('board-check-price').click();
    await expect(page.getByTestId('board-estimate')).toContainText('4 placeholder images, free in dry-run mode');
    await page.getByTestId('board-make').click();
    await expect(page.getByTestId('board-images-1').locator('img')).toHaveCount(4);
    await page.getByTestId('board-approve').click();
    await page.waitForURL(`/studio/universes/${universeId}/create?step=bible`);
    await page.getByTestId('bible-write').click();
    await expect(page.getByTestId('bible-done')).toBeVisible();
    await expect(page.getByTestId('creation-message-bible')).toHaveText(
      'Added 2 characters, 3 places and 2 props. The 6 photos from your reference list went with them.');

    // the photos on the list went to what the bible made, under the same names
    const view = await (await page.request.get(base)).json();
    type Picture = { image_id: string };
    const characters: Record<string, number> = Object.fromEntries(view.characters.map(
      (c: { name: string; versions: { images: Picture[] }[] }) => [c.name, c.versions.flatMap((v) => v.images).length]));
    const items: Record<string, number> = Object.fromEntries(view.items.map(
      (i: { name: string; photos: Picture[] }) => [i.name, i.photos.length]));
    expect(characters).toEqual({ Wren: 1, Tobin: 1 });
    expect(items).toEqual({ 'Harbour town': 1, 'Old lighthouse': 1, 'Night market': 1, 'Copper bell': 1, 'Rain cloak': 0 });
    await page.getByTestId('creation-open-universe').click();
    await page.waitForURL(`/studio/universes/${universeId}`);
    await expect(page.getByTestId('cast-character-wren')).toBeVisible();
    await expect(page.getByTestId('prop-copper-bell')).toContainText('1 photo');
  });

  test('first frames: options for a shot, a change in plain words, the pick, then filming starts from it', async ({ page, browser }) => {
    test.slow(); // the dry run goes on to the scene pictures
    await signIn(page, OWNER);
    await page.goto('/studio/create');
    await page.getByText('Skip - I\'ll describe my style').click();
    await page.getByTestId('idea-input').fill(IDEA);
    await page.getByTestId('generate-button').click();
    await expect(page.getByText('Step 3: Review & Start')).toBeVisible({ timeout: 60_000 });
    const episodeId = (await page.locator('text=/Episode: ep[0-9]{2}[a-z]{3}/').textContent())!.replace('Episode: ', '');

    // opting in keeps the start: the video waits on its page while its first frames are picked
    await expect(page.getByTestId('pick-frames-option')).toContainText("Pick each shot's first frame before filming");
    await expect(page.getByTestId('pick-frames-checkbox')).not.toBeChecked();
    await page.getByTestId('pick-frames-checkbox').check();
    await expectNoSidewaysScroll(page);
    await screenshot(page, 'wizard-pick-frames-1440');
    await onPhone(page, 'wizard-pick-frames', page.getByTestId('pick-frames-option'));
    const started: string[] = [];
    page.on('request', (req) => {
      if (req.url() === `${API}/api/episodes` && req.method() === 'POST') started.push(req.url());
    });
    await page.getByTestId('preview-button').click();
    await page.waitForURL(new RegExp(`/studio/episodes/${episodeId}#first-frames$`), { timeout: 60_000 });
    expect(started).toEqual([]);
    await expect(page.getByTestId('start-card')).toBeVisible();
    await expect(page.getByTestId('start-frames')).toContainText('or you can pick them first');

    // every shot of the plan, with what is in it; nothing picked yet
    const section = page.getByTestId('first-frames');
    await expect(section).toBeVisible();
    const shots = section.locator('[data-testid^="frames-shot-"]');
    const count = await shots.count();
    expect(count).toBeGreaterThan(0);
    await expect(page.getByTestId('first-frames-summary')).toHaveText(`Start frames picked: 0 of ${count} shot${count === 1 ? '' : 's'}`);
    const shot = (await shots.first().getAttribute('data-testid'))!.replace('frames-shot-', '');
    await expect(page.getByTestId(`frames-status-${shot}`)).toHaveText('No start frame yet (optional)');
    await expect(page.getByTestId(`frames-in-it-${shot}`)).toBeVisible();
    await expect(section).not.toContainText(/grok|seedream|nano|kling|flux/i);

    // each shot in plain words: what is in it and the moment, never the picture prompt; the
    // reference photos are named once
    const card = page.getByTestId(`frames-shot-${shot}`);
    await expect(page.getByTestId(`frames-in-it-${shot}`)).toHaveText(/, day$/);
    await expect(page.getByTestId(`frames-beat-${shot}`)).toContainText(`The moment: ${IDEA}`);
    await expect(page.getByTestId(`frames-refs-${shot}`)).toHaveText(/^No reference photos of .+ yet: the words guide it\.$/);
    await expect(card.getByText(/reference photo/)).toHaveCount(1);
    await expect(section).not.toContainText(/cinematic|photorealistic|only the description is used/i);

    // the price first (free stand-ins in a test run), then a round of three options
    await page.getByTestId(`frames-start-${shot}`).click();
    const price = page.getByTestId(`frames-price-${shot}`);
    await expect(price).toContainText('Free');
    await expect(price).toContainText('3 options');
    await expect(page.getByTestId(`frames-make-${shot}`)).toHaveText('Make 3 options');
    for (const size of [SMALL_PHONE, PHONE, DESKTOP]) {
      await page.setViewportSize(size);
      await expectTidy(page, card, 'A shot with its price');
    }
    await page.getByTestId(`frames-make-${shot}`).click();
    await expect(page.getByTestId(`frames-message-${shot}`)).toHaveText('3 options ready. Pick the one this shot starts from.');
    const first = page.getByTestId(`frames-round-${shot}-1`);
    await expect(first.locator('[data-testid^="frames-option-"]')).toHaveCount(3);
    await expect(first.locator('img')).toHaveCount(3);
    await expect(page.getByTestId(`frames-status-${shot}`)).toHaveText('Options ready: pick one');

    // not quite right: a change in plain words, priced again, makes the next round
    await page.getByTestId(`frames-change-input-${shot}`).fill('make it night');
    await page.getByTestId(`frames-try-change-${shot}`).click();
    await expect(price).toContainText('With your change: make it night.');
    await page.getByTestId(`frames-make-${shot}`).click();
    const second = page.getByTestId(`frames-round-${shot}-2`);
    await expect(second.locator('[data-testid^="frames-option-"]')).toHaveCount(3);
    await expect(page.getByTestId(`frames-changes-${shot}`)).toHaveText('Changes so far: make it night');
    await expect(page.getByTestId(`frames-earlier-${shot}`)).toContainText('Earlier tries (1)');
    await expect(page.getByTestId(`frames-in-it-${shot}`)).toHaveText(/, night$/);

    // the pick: it becomes the shot's start frame for filming
    const option = (await second.locator('[data-testid^="frames-option-"]').nth(1).getAttribute('data-testid'))!
      .replace('frames-option-', '');
    await page.getByTestId(`frames-use-${option}`).click();
    await expect(page.getByTestId(`frames-message-${shot}`)).toHaveText(`Option 2 is the start frame of shot ${shot}.`);
    await expect(page.getByTestId(`frames-status-${shot}`)).toHaveText('Start frame locked');
    await expect(page.getByTestId(`frames-option-${option}`)).toHaveAttribute('data-picked', 'true');
    await expect(page.getByTestId(`frames-picked-${shot}`)).toBeVisible();
    await expect(page.getByTestId('first-frames-summary')).toHaveText(`Start frames picked: 1 of ${count} shot${count === 1 ? '' : 's'}`);
    await expect(page.getByTestId('start-frames')).toContainText(
      count === 1 ? 'Every shot starts from a frame you picked.' : `1 of ${count} shots start from a frame you picked.`);
    const plan = await (await page.request.get(`${API}/api/episodes/${episodeId}/concept-frames`)).json();
    const picked = plan.shots.find((s: { shot_id: string }) => s.shot_id === shot);
    expect(picked.pick).toMatchObject({ source: 'here', option_id: option, stale: false });
    await expectTidy(page, section, 'First frames');
    await expectTidy(page, page.getByTestId('start-card'), 'The start card');
    await screenshot(page, 'first-frames-1440', section);
    await oneScreen(page, 'first-frames-options-1440', page.getByTestId(`frames-shot-${shot}`), 120);
    await screenshot(page, 'episode-start-1440', page.getByTestId('start-card'));
    for (const size of [SMALL_PHONE, PHONE]) {
      await page.setViewportSize(size);
      await expectTidy(page, section, 'First frames');
      await oneScreen(page, `first-frames-${size.width}`, section);
      await oneScreen(page, `first-frames-options-${size.width}`, second);
      // on a phone the start card is in the bottom sheet
      await page.getByTestId('gate-sheet-toggle').click();
      await expect(page.getByTestId('start-frames')).toBeVisible();
      await expectTidy(page, page.getByTestId('start-card'), 'The start card');
      await page.screenshot({ path: test.info().outputPath(`episode-start-${size.width}.png`) });
      await page.getByTestId('gate-sheet-toggle').click();
      await expect(page.getByTestId('start-frames')).toBeHidden();
    }
    await page.setViewportSize(DESKTOP);

    // the studio keeps the start, not this browser: a fresh browser (another device) shows the start
    // card and the picked frame, and starts the run with the request the wizard kept
    expect(await page.evaluate((id) => window.localStorage.getItem(`ddg:start:${id}`), episodeId)).toBeNull();
    const kept = await (await page.request.get(`${API}/api/episodes/${episodeId}/universe`)).json();
    expect(kept.kept_start).toMatchObject({ episode_id: episodeId, dry_run: true, enable_story_shaping: true, idea: IDEA });
    const device = await browser.newContext({ baseURL: test.info().project.use.baseURL, viewport: DESKTOP });
    const other = await device.newPage();
    await signIn(other, OWNER);
    await other.goto(`/studio/episodes/${episodeId}`);
    expect(await other.evaluate((id) => window.localStorage.getItem(`ddg:start:${id}`), episodeId)).toBeNull();
    await expect(other.getByTestId('start-card')).toBeVisible();
    await expect(other.getByTestId('start-frames')).toContainText(
      count === 1 ? 'Every shot starts from a frame you picked.' : `1 of ${count} shots start from a frame you picked.`);
    await expect(other.getByTestId(`frames-status-${shot}`)).toHaveText('Start frame locked');
    await expect(other.getByTestId(`frames-picked-${shot}`)).toBeVisible();
    await expect(other.getByTestId(`frames-option-${option}`)).toHaveAttribute('data-picked', 'true');
    await screenshot(other, 'episode-start-another-device-1440', other.getByTestId('start-card'));
    const start = other.waitForRequest((req) => req.url() === `${API}/api/episodes` && req.method() === 'POST');
    await other.getByTestId('start-episode').click();
    expect((await start).postDataJSON()).toMatchObject({
      episode_id: episodeId, dry_run: true, enable_story_shaping: true, idea: IDEA });
    await expect(other.getByTestId('start-card')).toHaveCount(0, { timeout: 60_000 });
    await device.close();
    expect((await (await page.request.get(`${API}/api/episodes/${episodeId}/universe`)).json()).kept_start).toBeNull();

    // the picked shot films from its frame
    await page.reload();
    await expect(page.getByTestId('start-card')).toHaveCount(0, { timeout: 60_000 });
    await expect(page.getByTestId('next-step-title')).toHaveText('Read and approve your story', { timeout: 120_000 });
    await page.getByTestId('story-approve').click();
    await expect(page.getByTestId('story-approved')).toBeVisible();
    await page.getByTestId('approve-g1').click();
    await expect(page.getByTestId('run-message')).toHaveText('Shot plan approved.');
    await expect(page.getByTestId('current-gate')).toHaveText('G2_SCENE_STILLS', { timeout: 120_000 });
    let states: Record<string, { still_status?: string; pre_approved?: boolean }> = {};
    await expect.poll(async () => {
      const res = await page.request.get(`${API}/api/episodes/${episodeId}/shot-states`);
      if (!res.ok()) return `HTTP ${res.status()}`;
      states = (await res.json()).shots;
      const all = Object.values(states);
      return all.length > 0 && all.every((s) => s.still_status === 'done') ? 'ready' : 'making';
    }, { timeout: 120_000 }).toBe('ready');
    expect(Object.entries(states).filter(([, s]) => s.pre_approved).map(([sid]) => sid)).toEqual([shot]);
    await expect(page.getByTestId(`still-thumb-${shot}`)).toBeVisible();
    await expect(page.getByTestId(`shot-${shot}-pre-approved`)).toHaveText('pre-approved still');
    // while it films, the first frames are read-only
    await expect(page.getByTestId('first-frames-locked')).toBeVisible();
    await expect(page.getByTestId(`frames-try-again-${shot}`)).toBeDisabled();
  });
});
