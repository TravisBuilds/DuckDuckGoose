import { test, expect, request as apiRequest } from '@playwright/test';
import path from 'node:path';
import zlib from 'node:zlib';

/**
 * The Cast panel against the real backend (playwright.e2e.config.ts, DRY_RUN): add a character,
 * upload a generated picture with the rights answers, make the character sheet with the CastSheetWorkflow
 * (free stand-in pictures in preview mode), pick a look, make the turnaround, approve and lock it, put the
 * character in a shot, and start a new outfit from the locked look. Nothing here starts an episode run.
 */

const E2E_USER = 'e2e@test.local';
const E2E_PASSWORD = 'e2e-test-password';
const FIXTURES = path.join(__dirname, 'fixtures', 'e2e');
const API = 'http://localhost:8000';
const EPISODE = 'ep72';

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

/** A generated test picture (a plain block figure on a light background), never a real person. */
function testPicture(width: number, height: number): Buffer {
  const header = Buffer.alloc(13);
  header.writeUInt32BE(width, 0);
  header.writeUInt32BE(height, 4);
  header[8] = 8; // bit depth
  header[9] = 2; // RGB
  const stride = width * 3 + 1;
  const raw = Buffer.alloc(stride * height);
  for (let y = 0; y < height; y++) {
    for (let x = 0; x < width; x++) {
      const figure = Math.abs(x - width / 2) < width / 6 && y > height / 6 && y < (height * 5) / 6;
      raw.set(figure ? [200, 150, 40] : [235, 240, 245], y * stride + 1 + x * 3);
    }
  }
  return Buffer.concat([
    Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]),
    pngChunk('IHDR', header),
    pngChunk('IDAT', zlib.deflateSync(raw)),
    pngChunk('IEND', Buffer.alloc(0)),
  ]);
}

test('cast: a character with a picture, a locked character sheet, in a shot, then a new outfit', async ({ page }) => {
  page.on('dialog', (dialog) => dialog.accept());

  await page.goto('/login?from=/studio/episodes/new');
  await page.locator('#email').fill(E2E_USER);
  await page.locator('#password').fill(E2E_PASSWORD);
  await page.getByRole('button', { name: 'Sign In', exact: true }).first().click();
  await expect(page).toHaveURL(/\/studio\/episodes\/new$/);

  // Validate uploads the episode files; the run is never started here.
  await page.getByTestId('episode-id').fill(EPISODE);
  await page.getByTestId('beatmap-file').setInputFiles(path.join(FIXTURES, 'BEATMAP.md'));
  await page.getByTestId('shots-file').setInputFiles(path.join(FIXTURES, 'shots.json'));
  await page.getByTestId('refs-file').setInputFiles(path.join(FIXTURES, 'refs.json'));
  await page.getByTestId('prompt_kit-file').setInputFiles(path.join(FIXTURES, 'prompt_kit.json'));
  await page.getByTestId('validate-button').click();
  await expect(page.getByTestId('preflight-verdict')).toContainText('all shots pass');
  await expect(page.getByTestId('preflight-cast-errors')).toHaveCount(0);

  await page.goto(`/studio/episodes/${EPISODE}`);
  const panel = page.getByTestId('cast-panel');
  const message = page.getByTestId('cast-message');
  await expect(panel).toContainText('No one in the cast yet.');

  // --- add a character ---
  await page.getByTestId('cast-add-name').fill('Mara');
  await page.getByTestId('cast-add-description').fill(
    'a tall woman in her thirties with short silver hair, a round face, a mustard raincoat and green boots');
  await page.getByTestId('cast-add-button').click();
  await expect(message).toHaveText('Mara joined the cast.');
  const mara = page.getByTestId('cast-character-mara');
  await expect(mara).toContainText('Look not locked yet');
  await expect(page.getByTestId('cast-source-text-mara')).toBeChecked();
  await expect(page.getByTestId('cast-source-upload-mara')).toBeDisabled();

  // --- pictures: the rights answer is required, a small picture is refused with the reason ---
  await page.getByTestId('cast-upload-file-mara').setInputFiles({
    name: 'small.png', mimeType: 'image/png', buffer: testPicture(200, 200) });
  await expect(page.getByTestId('cast-upload-send-mara')).toBeDisabled();
  await page.getByTestId('cast-rights-original-mara').check();
  await page.getByTestId('cast-upload-send-mara').click();
  await expect(message).toContainText('at least 512 px');

  await page.getByTestId('cast-upload-file-mara').setInputFiles({
    name: 'mara.png', mimeType: 'image/png', buffer: testPicture(640, 800) });
  await page.getByTestId('cast-upload-send-mara').click();
  await expect(message).toHaveText('Picture of Mara added.');
  const mainPicture = mara.getByRole('img', { name: 'Main picture' });
  await expect(mainPicture).toBeVisible();
  await expect.poll(() => mainPicture.evaluate((img: HTMLImageElement) => img.naturalWidth)).toBeGreaterThan(0);

  // The picture is private: the API refuses it without a signed-in session.
  const cast = await (await page.request.get(`${API}/api/episodes/${EPISODE}/cast`)).json();
  const upload = cast.characters[0].versions[0].images[0];
  const anonymous = await apiRequest.newContext();
  expect((await anonymous.get(`${API}${upload.thumb_url}`)).status()).toBe(401);
  expect((await anonymous.get(`${API}${upload.url}`)).status()).toBe(401);
  await anonymous.dispose();

  // --- character sheet: options from the picture, pick one, turnaround, approve and lock ---
  const sheet = page.getByTestId('cast-sheet-mara');
  await expect(sheet).toHaveAttribute('data-step', 'start');
  await expect(page.getByTestId('cast-source-upload-mara')).toBeChecked();
  await page.getByTestId('cast-source-text-mara').check();
  await page.getByTestId('cast-check-options-mara').click();
  await expect(page.getByTestId('cast-estimate-mara')).toContainText('3 pictures, free');
  await page.getByTestId('cast-source-upload-mara').check();
  await expect(page.getByTestId('cast-estimate-mara')).toHaveCount(0);
  await page.getByTestId('cast-check-options-mara').click();
  await expect(page.getByTestId('cast-estimate-mara')).toContainText('2 pictures, free');
  await page.getByTestId('cast-generate-mara').click();
  await expect(sheet).toHaveAttribute('data-step', 'pick', { timeout: 120_000 });
  await expect(sheet.getByRole('button', { name: 'Use this look' })).toHaveCount(2);
  await sheet.getByRole('button', { name: 'Use this look' }).first().click();
  await expect(message).toHaveText('Main look picked.');
  await expect(sheet).toHaveAttribute('data-step', 'turnaround');

  await page.getByTestId('cast-check-turnaround-mara').click();
  await expect(page.getByTestId('cast-estimate-mara')).toContainText('pictures, free');
  await page.getByTestId('cast-generate-mara').click();
  await expect(sheet).toHaveAttribute('data-step', 'approve', { timeout: 120_000 });
  for (const view of ['Main look', 'Front', 'Three-quarter', 'Side', 'Back', 'Head close-ups']) {
    await expect(sheet.getByRole('img', { name: view, exact: true })).toBeVisible();
  }

  await page.getByTestId('cast-approve-mara').click();
  await expect(message).toHaveText("Mara's look is locked.");
  await expect(page.getByTestId('cast-locked-mara')).toHaveText('Look locked');
  await expect(sheet).toHaveAttribute('data-step', 'locked');
  await expect(page.getByTestId('cast-upload-mara')).toHaveCount(0);

  const locked = (await (await page.request.get(`${API}/api/episodes/${EPISODE}/cast`)).json()).characters[0].versions[0];
  expect(locked.locked).toBe(true);
  expect(locked.lock_hash).toMatch(/^[0-9a-f]{64}$/);
  const refused = await page.request.post(
    `${API}/api/episodes/${EPISODE}/cast/${cast.characters[0].character_id}/versions/1/images`,
    { multipart: {
      image_file: { name: 'late.png', mimeType: 'image/png', buffer: testPicture(600, 600) },
      rights: 'original', depicts_real_person: 'false', likeness_consent: 'false',
    } },
  );
  expect(refused.status()).toBe(409);

  // --- who is in each shot: the locked sheet becomes the shot's reference pictures ---
  await page.getByTestId('cast-shot-S01-mara').click();
  await expect(page.getByTestId('cast-shot-S01-mara')).toHaveAttribute('aria-pressed', 'true');
  await page.getByTestId('cast-shots-save').click();
  await expect(message).toHaveText('Saved who is in each shot.');
  await expect(page.getByTestId('cast-shot-S01')).toContainText('Reference pictures for this shot: Mara');
  await expect(page.getByTestId('cast-plan-errors')).toHaveCount(0);

  type ShotView = { shot_id: string; cast: { character: string }[]; refs: { key: string }[] };
  const shots: ShotView[] = (await (await page.request.get(`${API}/api/episodes/${EPISODE}/cast`)).json()).shots.shots;
  const s01 = shots.find((s) => s.shot_id === 'S01');
  expect(s01?.cast.map((c) => c.character)).toEqual(['mara']);
  expect(s01?.refs.length).toBeGreaterThan(0);
  expect(shots.find((s) => s.shot_id === 'S02')?.cast).toEqual([]);

  // --- a new outfit starts from the locked look ---
  await mara.getByPlaceholder('Name, e.g. Winter coat').fill('Winter coat');
  await mara.getByPlaceholder('The outfit, e.g. a long green wool coat and red boots').fill('a long green wool coat and red boots');
  await page.getByTestId('cast-new-outfit-mara').click();
  await expect(message).toHaveText('New outfit added. Make its sheet below.');
  await expect(mara.getByRole('button', { name: 'Winter coat' })).toHaveAttribute('aria-pressed', 'true');
  await expect(sheet).toHaveAttribute('data-step', 'start');
  await expect(sheet).toContainText('keep the locked look of version 1');
});
