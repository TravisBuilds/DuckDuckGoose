import { test, expect } from './e2e-cleanup';

/**
 * A MEMBER makes and reviews their own video against the REAL dry-run backend (scripts/e2e_backend.py,
 * no API mocks): the admins-only page on /dashboard, the wizard with a card that needs no voices
 * (/studio/create?archetype=..., the archetype page's link), the story, the shot plan, one scene
 * picture thumbnail per shot at G2 with the Next step card counting them, then the final cut and the
 * finished video. No admin control is rendered for a member. The card has music, so the dry run mixes
 * it under a black stand-in picture with ffmpeg (the e2e CI job installs it, like test-python) and
 * delivery QC measures the files: without ffmpeg the verdict is NEEDS_REVIEW and the final cut stays
 * blocked. Then the workflow server purges the finished run (POST /api/e2e/purge-history, the launcher's
 * stand-in for Temporal's 24 h retention): the page still shows the finished video from the saved state,
 * and stops polling. If this test stops half-way, e2e-cleanup ends its run so later specs can start theirs.
 */

const MEMBER = 'e2e-member@test.local';
const MEMBER_PASSWORD = 'e2e-member-password';
const API = 'http://localhost:8000';
const CARD = 'cozy-hangout';
const IDEA = 'A sleepy cat watches the rain on a cafe window and dreams of a sunny garden';
const ADMIN_ONLY = ['new-cap', 'raise-cap', 'new-balance', 'record-balance', 'approve-g108', 'g108-card'];

test('a member sees one scene picture per shot at G2, no admin controls, and finishes the video', async ({ page }) => {
  await page.goto('/login');
  await page.locator('#email').fill(MEMBER);
  await page.locator('#password').fill(MEMBER_PASSWORD);
  await page.getByRole('button', { name: 'Sign In', exact: true }).first().click();
  await expect(page).toHaveURL(/\/studio$/);
  await expect(page.getByTestId('me-role')).toHaveText('Member');

  // the cost dashboard is for admins: a member gets a plain page, not a broken one
  await page.goto('/dashboard');
  await expect(page.getByTestId('admins-only')).toContainText('Only admins can open this page');

  // the wizard, with the card picked by the link
  await page.goto(`/studio/create?archetype=${CARD}`);
  await page.getByTestId('idea-input').fill(IDEA);
  await page.getByTestId('generate-button').click();
  await expect(page.getByText('Step 3: Review & Start')).toBeVisible({ timeout: 60_000 });
  const episodeId = (await page.locator('text=/Episode: ep[0-9]{2}[a-z]{3}/').textContent())!.replace('Episode: ', '');
  await page.getByTestId('preview-button').click();
  await page.waitForURL(`/studio/episodes/${episodeId}`, { timeout: 60_000 });

  // the member owns this video, so its cast takes a character with a photo
  await expect(page.getByTestId('cast-add-file')).toBeVisible();
  await expect(page.getByTestId('cast-add-with-photo')).toBeVisible();

  // the story comes first
  await expect(page.getByTestId('next-step-title')).toHaveText('Read and approve your story');
  await expect(page.getByTestId('step-story')).toHaveAttribute('data-status', 'current');
  await expect(page.getByTestId('episode-status')).toHaveText('Story to approve');
  await page.getByTestId('story-approve').click();
  await expect(page.getByTestId('story-approved')).toBeVisible();

  // G1: the shot plan (the cast step before it is advice only)
  await page.getByTestId('approve-g1').click();
  await expect(page.getByTestId('run-message')).toHaveText('Shot plan approved.');

  // G2: one thumbnail per shot, every one loaded, and the Next step counts the pictures to review
  await expect(page.getByTestId('current-gate')).toHaveText('G2_SCENE_STILLS');
  let shotIds: string[] = [];
  await expect.poll(async () => {
    const res = await page.request.get(`${API}/api/episodes/${episodeId}/shot-states`);
    if (!res.ok()) return `HTTP ${res.status()}`;
    const shots: Record<string, { still_status?: string }> = (await res.json()).shots;
    shotIds = Object.keys(shots).sort();
    return shotIds.length > 0 && shotIds.every((sid) => shots[sid].still_status === 'done') ? 'ready' : 'making';
  }, { timeout: 120_000 }).toBe('ready');
  const n = shotIds.length;
  await expect(page.getByTestId('next-step-title')).toHaveText(`Review ${n} scene picture${n === 1 ? '' : 's'}`);
  await expect(page.getByTestId('step-stills')).toHaveAttribute('data-status', 'current');
  await expect(page.locator('[data-testid^="still-card-"]')).toHaveCount(n);
  await expect(page.locator('[data-testid^="still-thumb-"]')).toHaveCount(n);
  for (const sid of shotIds) {
    const thumb = page.getByTestId(`still-thumb-${sid}`);
    await thumb.scrollIntoViewIfNeeded();
    await expect(thumb).toBeVisible();
    await expect.poll(() => thumb.evaluate((img: HTMLImageElement) => img.naturalWidth)).toBeGreaterThan(0);
    await expect(page.getByTestId(`still-chip-${sid}`)).toHaveText('Ready to review');
  }
  await expect(page.getByTestId('stills-error')).toHaveCount(0);
  for (const id of ADMIN_ONLY) await expect(page.getByTestId(id)).toHaveCount(0);

  await page.getByTestId(`approve-shot-${shotIds[0]}`).click();
  await expect(page.getByTestId('run-message')).toHaveText(`Picture ${shotIds[0]} approved.`);
  await expect(page.getByTestId(`still-chip-${shotIds[0]}`)).toHaveText('Approved');
  if (n > 1) await expect(page.getByTestId('next-step-title')).toHaveText(`Review ${n - 1} scene picture${n - 1 === 1 ? '' : 's'}`);
  await page.getByTestId('approve-g2').click();
  await expect(page.getByTestId('run-message')).toHaveText(/^Scene pictures approved\./);

  // the final cut, then the finished video
  await expect(page.getByTestId('next-step-title')).toHaveText('Watch and approve the final cut', { timeout: 120_000 });
  await expect(page.getByTestId('delivery-qc-verdict')).toHaveText('DRY_RUN', { timeout: 120_000 });
  await expect(page.getByTestId('final-blockers')).toHaveCount(0, { timeout: 120_000 });
  await page.getByTestId('approve-final').click();
  await expect(page.getByTestId('run-message')).toHaveText('Final cut approved.');
  await expect(page.getByTestId('run-finished')).toHaveText('Finished', { timeout: 120_000 });
  await expect(page.getByTestId('episode-status')).toHaveText('Finished');
  // the files delivery QC measured are there: the finish card plays the phone version and offers the downloads
  const files: Record<string, { available?: boolean }> =
    (await (await page.request.get(`${API}/api/episodes/${episodeId}/files`)).json()).files;
  const ready = ['phone', 'master_subs', 'master_nosubs'].filter((type) => files[type]?.available);
  expect(ready[0]).toBe('phone');
  await expect(page.getByTestId('run-ended-title')).toHaveText('Your video is ready');
  await expect(page.getByTestId('ready-video')).toBeVisible();
  await expect(page.getByTestId('ready-video')).toHaveAttribute('src', new RegExp(`/api/episodes/${episodeId}/files/phone$`));
  for (const type of ready) {
    await expect(page.getByTestId(`download-${type}`)).toHaveAttribute('href', new RegExp(`/files/${type}\\?download=true$`));
  }
  await expect(page.getByTestId('step-download')).toHaveAttribute('data-status', 'current');
  await expect(page.getByTestId('approve-final')).toHaveCount(0);
  await expect(page.getByTestId('start-again')).toHaveCount(0);
  await expect(page.getByTestId('story-edit')).toHaveCount(0);
  for (const id of ADMIN_ONLY) await expect(page.getByTestId(id)).toHaveCount(0);
  const run = (await (await page.request.get(`${API}/api/episodes/${episodeId}`)).json()).run;
  expect(run).toMatchObject({ ended: true, outcome: 'completed' });

  // a day later the workflow server purges the finished run. The API's saver keeps each episode's
  // state on disk every few minutes; an admin saves it now, then the run's history is deleted.
  const admin = { Authorization: `Bearer ${test.info().config.metadata.adminSecret}` };
  const saved = await page.request.post(`${API}/api/episodes/${episodeId}/snapshot`, { headers: admin });
  expect(saved.status(), await saved.text()).toBe(200);
  const purged = await page.request.post(`${API}/api/e2e/purge-history/${episodeId}`, { headers: admin });
  expect(purged.status(), await purged.text()).toBe(200);
  const archived = (await (await page.request.get(`${API}/api/episodes/${episodeId}`)).json()).run;
  expect(archived).toMatchObject({
    run_id: run.run_id, status: 'ARCHIVED', archived: true, ended: true, outcome: 'completed', can_start_again: false,
  });
  expect(archived.saved_at).toMatch(/^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$/);

  // the page still shows the finished video, its downloads and the steps it took
  await page.reload();
  await expect(page.getByTestId('run-ended-title')).toHaveText('Your video is ready');
  await expect(page.getByText(/hasn't started/)).toHaveCount(0);
  await expect(page.getByTestId('ready-video')).toHaveAttribute('src', new RegExp(`/api/episodes/${episodeId}/files/phone$`));
  for (const type of ready) {
    await expect(page.getByTestId(`download-${type}`)).toHaveAttribute('href', new RegExp(`/files/${type}\\?download=true$`));
  }
  await expect(page.getByTestId('episode-status')).toHaveText('Finished');
  for (const step of ['shot_plan', 'stills', 'final_cut']) {
    await expect(page.getByTestId(`step-${step}`)).toHaveAttribute('data-status', 'done');
  }
  await expect(page.getByTestId('step-download')).toHaveAttribute('data-status', 'current');
  await expect(page.getByTestId('final-approved')).toBeVisible();
  await expect(page.getByTestId('next-step')).toHaveCount(0);
  await expect(page.getByTestId('stills-error')).toHaveCount(0);
  await expect(page.getByTestId('approve-final')).toHaveCount(0);
  await expect(page.getByTestId('start-again')).toHaveCount(0);
  // and stops asking once it has shown it: no more reads of this episode
  await page.waitForLoadState('networkidle');
  const reads: string[] = [];
  page.on('request', (req) => {
    if (['fetch', 'xhr'].includes(req.resourceType()) && req.url().startsWith(`${API}/api/episodes/${episodeId}`)) reads.push(req.url());
  });
  await page.waitForTimeout(7_000);
  expect(reads).toEqual([]);
  const details = page.getByTestId('production-details');
  await details.locator('summary').click();
  await expect(page.getByTestId('run-finished')).toHaveText('Finished');
  await expect(page.getByTestId('run-archived')).toContainText('This is the run as saved on');

  // the studio home lists it as the member's, finished: from the saved state, now that the run is purged
  await page.goto('/studio');
  await expect(page.getByTestId(`episode-owner-${episodeId}`)).toHaveText('By you');
  await expect(page.getByTestId(`episode-status-${episodeId}`)).toHaveText('Finished');

  // the footer's coming-soon links are for the marketing pages, not the studio
  await expect(page.getByTestId('footer-coming-soon')).toHaveCount(0);
  await page.goto('/');
  await expect(page.getByTestId('footer-coming-soon')).toBeVisible();
});
