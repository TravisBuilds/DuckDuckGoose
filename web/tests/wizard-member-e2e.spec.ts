import { readFileSync } from 'node:fs';
import { test, expect, type Response } from '@playwright/test';

/**
 * Create wizard as a MEMBER against the REAL dry-run backend (scripts/e2e_backend.py seeds the member;
 * no API mocks): card -> idea + brief answer -> draft -> a voice per dialogue speaker (described,
 * picked, retried) -> Preview Only. Every upload and the start must succeed for a member, the uploaded
 * audio plan must carry each speaker's chosen voice id, and the workflow must get the card and length.
 * This file starts an episode, so it sorts after full-episode-e2e (files run in name order, 1 worker).
 */

const MEMBER = 'e2e-member@test.local';
const MEMBER_PASSWORD = 'e2e-member-password';
const API = 'http://localhost:8000';
const CARD = 'nostalgia-time-travel';
const IDEA = 'Two friends find an old mixtape and replay the summer they recorded it';

function postTo(path: string) {
  return (r: Response) => r.request().method() === 'POST' && new URL(r.url()).pathname === path;
}

test('signed-out visitors of /dashboard and /studio/create are sent to the login page', async ({ page }) => {
  await page.goto('/dashboard');
  await page.waitForURL(/\/login\?from=%2Fdashboard/);
  await page.goto('/studio/create');
  await page.waitForURL(/\/login\?from=%2Fstudio%2Fcreate/);
});

test('a member makes a dialogue video: a voice per speaker, then Preview Only', async ({ page }) => {
  await page.goto('/login');
  await page.locator('#email').fill(MEMBER);
  await page.locator('#password').fill(MEMBER_PASSWORD);
  await page.getByRole('button', { name: 'Sign In', exact: true }).first().click();
  await expect(page).toHaveURL(/\/studio$/);

  await page.goto('/studio/create');
  await page.getByTestId(`preset-card-${CARD}`).click();
  await expect(page.getByTestId('duration-select')).toHaveValue('30');
  await page.getByTestId('idea-input').fill(IDEA);
  await page.getByTestId('brief-cast_roles').fill('two old friends');
  await page.getByTestId('duration-select').selectOption('35');
  await page.getByTestId('generate-button').click();
  await expect(page.getByText('Step 3: Review & Start')).toBeVisible({ timeout: 60000 });

  // one "describe the voice" box per speaker of the draft; nothing chosen yet, so Start is blocked
  await expect(page.locator('[data-testid^="voice-slot-"]')).toHaveCount(2);
  await expect(page.getByTestId('voice-description-speaker-lead')).toHaveAttribute('placeholder', 'e.g. bright, curious voice');
  await expect(page.getByTestId('voice-description-speaker-friend')).toHaveAttribute('placeholder', 'e.g. relaxed, friendly voice');
  await expect(page.getByTestId('voice-block')).toHaveText(
    'Choose a voice before starting: Lead, Friend. Describe each voice above and press "Find a voice".');
  await expect(page.getByTestId('preview-button')).toBeDisabled();

  await page.getByTestId('voice-description-speaker-lead').fill('bright curious young woman');
  await page.getByTestId('voice-find-speaker-lead').click();
  const lead = page.getByTestId('voice-choice-speaker-lead');
  await expect(lead).toHaveAttribute('data-voice-id', /^dryrun-voice-/);
  await expect(page.getByTestId('voice-audio-speaker-lead')).toHaveAttribute('src', /^data:audio\/wav;base64,/);
  await expect(page.getByTestId('voice-block')).toHaveText(/^Choose a voice before starting: Friend\./);

  await page.getByTestId('voice-description-speaker-friend').fill('relaxed older man, deep and friendly');
  await page.getByTestId('voice-find-speaker-friend').click();
  const friend = page.getByTestId('voice-choice-speaker-friend');
  await expect(friend).toHaveAttribute('data-voice-id', /^dryrun-voice-/);
  const firstFriend = (await friend.getAttribute('data-voice-id'))!;
  const firstFriendName = (await page.getByTestId('voice-name-speaker-friend').textContent())!;
  await page.getByTestId('voice-retry-speaker-friend').click();
  await expect(friend).not.toHaveAttribute('data-voice-id', firstFriend);
  await expect(page.getByTestId('voice-name-speaker-friend')).not.toHaveText(firstFriendName);

  const leadVoice = await lead.getAttribute('data-voice-id');
  const friendVoice = await friend.getAttribute('data-voice-id');
  await expect(page.getByTestId('voice-block')).toHaveCount(0);
  await expect(page.getByTestId('preview-button')).toBeEnabled();

  const episodeId = (await page.locator('text=/Episode: ep[0-9]{2}[a-z]{3}/').textContent())!.replace('Episode: ', '');
  const responses: Record<string, Promise<Response>> = {
    beatmap: page.waitForResponse(postTo('/api/episodes/upload')),
    shots: page.waitForResponse(postTo(`/api/episodes/${episodeId}/upload-shots`)),
    promptKit: page.waitForResponse(postTo(`/api/episodes/${episodeId}/upload-prompt-kit`)),
    audioPlan: page.waitForResponse(postTo(`/api/episodes/${episodeId}/upload-audio-plan`)),
    start: page.waitForResponse(postTo('/api/episodes')),
  };
  await page.getByTestId('preview-button').click();
  for (const [name, pending] of Object.entries(responses)) {
    const res = await pending;
    expect(res.status(), `${name}: ${await res.text()}`).toBe(200);
  }

  // the plan the API wrote (the backend runs on this machine; Chromium hides multipart file bodies)
  const saved = await (await responses.audioPlan).json();
  expect(saved.audio_plan_path).toMatch(new RegExp(`/${episodeId}/audio/audio_plan\\.json$`));
  const audioPlan = JSON.parse(readFileSync(saved.audio_plan_path, 'utf-8'));
  expect(audioPlan.vo.speakers).toEqual({ Lead: leadVoice, Friend: friendVoice });
  expect(audioPlan.vo.lines.length).toBeGreaterThan(0);
  expect(audioPlan.vo.lines.every((line: any) => ['Lead', 'Friend'].includes(line.speaker))).toBe(true);
  expect(saved.vo_lines).toBe(audioPlan.vo.lines.length);
  expect(saved.lines_missing_voice).toEqual([]);

  const started = (await responses.start).request().postDataJSON();
  expect(started).toEqual({
    episode_id: episodeId,
    dry_run: true,
    enable_story_shaping: true,
    archetype_id: CARD,
    idea: `${IDEA}\n\nCast roles: two old friends`,
    target_duration: 35,
  });

  await page.waitForURL(`/studio/episodes/${episodeId}`);

  // the workflow got the card and the length (its get_state query, through GET /api/episodes/{id})
  await expect.poll(async () => {
    const res = await page.request.get(`${API}/api/episodes/${episodeId}`);
    if (!res.ok()) return `HTTP ${res.status()}`;
    const state = await res.json();
    return `${state.archetype_id} ${state.target_duration_s}`;
  }, { timeout: 60_000 }).toBe(`${CARD} 35`);

  // the member edits the story outline and saves it on the open run (the PUT body is the outline itself)
  await expect(page.getByTestId('run-ended')).toHaveCount(0);
  await page.getByTestId('story-edit').click();
  await page.getByTestId('story-logline-input').fill('Two old friends replay the summer of their mixtape');
  await page.getByTestId('story-save').click();
  await expect(page.getByTestId('story-logline')).toHaveText('Two old friends replay the summer of their mixtape');
  const story = await (await page.request.get(`${API}/api/episodes/${episodeId}/story-shape`)).json();
  expect(story.story_shape.logline).toBe('Two old friends replay the summer of their mixtape');
  expect(story.log.at(-1).action).toBe('updated');
});
