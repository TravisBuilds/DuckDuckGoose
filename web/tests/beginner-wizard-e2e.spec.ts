import { test, expect } from '@playwright/test';

/**
 * Beginner video wizard E2E tests with REAL backend (no API mocks).
 * Tests draft generation, ID validation, and episode start.
 */

const E2E_USER = 'e2e@test.local';
const E2E_PASSWORD = 'e2e-test-password';

test.describe('Beginner Video Creation Wizard E2E', () => {
  test.beforeEach(async ({ page }) => {
    // Authenticate with test user
    await page.goto('/login');
    await page.locator('#email').fill(E2E_USER);
    await page.locator('#password').fill(E2E_PASSWORD);
    await page.getByRole('button', { name: 'Sign In', exact: true }).first().click();
    await expect(page).toHaveURL(/\/studio$/);
  });

  test('complete flow: draft idea -> review screen loaded', async ({ page }) => {
    // Navigate to wizard
    await page.goto('/studio/create');
    
    // Should show wizard
    await expect(page.getByText('Make Your Own Video')).toBeVisible();
    
    // Skip archetype selection
    await expect(page.getByText('Step 1: Choose a Style')).toBeVisible();
    await page.getByText('Skip - I\'ll describe my style').click();
    
    // Enter idea
    await expect(page.getByText('Step 2: Describe Your Video')).toBeVisible();
    await page.getByTestId('idea-input').fill(
      'A curious robot explores an abandoned library. It discovers a glowing book with magical creatures.'
    );
    
    // Generate draft (uses DRAFT_LLM_FAKE=1 in CI, returns canned data)
    await page.getByTestId('generate-button').click();
    
    // Should show loading
    await expect(page.getByText('Creating your video...')).toBeVisible();
    
    // Wait for review screen (fast with fake LLM)
    await expect(page.getByText('Step 3: Review & Start')).toBeVisible({ timeout: 60000 });
    
    // Should show episode details
    const episodeText = await page.locator('text=/Episode: ep[0-9]{2}[a-z]{3}/').textContent();
    expect(episodeText).toMatch(/Episode: ep[0-9]{2}[a-z]{3}/);
    
    // Extract episode ID
    const episodeId = episodeText!.replace('Episode: ', '');
    
    // Verify ID format matches server pattern: ep{2 digits}{3 lowercase letters}
    expect(episodeId).toMatch(/^ep[0-9]{2}[a-z]{3}$/);
    
    // Should show cost estimate
    await expect(page.getByTestId('estimated-cost')).toBeVisible();
    
    // Should show characters
    await expect(page.getByText('Characters')).toBeVisible();
    
    // Should show shot list
    await expect(page.getByText('Shot List')).toBeVisible();
    
    // Verify preview button exists (don't click it to avoid run lock conflicts with other tests)
    await expect(page.getByTestId('preview-button')).toBeVisible();
  });

  test('shows content policy reminder', async ({ page }) => {
    await page.goto('/studio/create');
    
    // Skip to idea step
    await page.getByText('Skip - I\'ll describe my style').click();
    
    await expect(page.getByText('✓ Original Content Only')).toBeVisible();
    await expect(page.getByText(/No copyrighted characters/i)).toBeVisible();
  });

  test('shows link to advanced upload', async ({ page }) => {
    await page.goto('/studio/create');

    await expect(page.getByText('Advanced: Upload files manually →')).toBeVisible();
  });

  test('link from studio home navigates to wizard', async ({ page }) => {
    await page.goto('/studio');

    // Wait for page to be fully loaded
    await expect(page.getByTestId('make-video-link')).toBeVisible();

    // Click "Make Your Own Video" and wait for navigation
    await Promise.all([
      page.waitForURL('/studio/create', { timeout: 10000 }),
      page.getByTestId('make-video-link').click(),
    ]);

    // Verify we're on the wizard page
    await expect(page.getByText('Make Your Own Video')).toBeVisible({ timeout: 10000 });
  });

  test('live mode button visibility based on server status', async ({ page }) => {
    // Generate a draft first (uses fake LLM in CI)
    await page.goto('/studio/create');
    await page.getByText('Skip - I\'ll describe my style').click();
    
    await page.getByTestId('idea-input').fill('Test idea for live mode check');
    await page.getByTestId('generate-button').click();
    
    await expect(page.getByText('Step 3: Review & Start')).toBeVisible({ timeout: 60000 });
    
    // Check if server is in dry run mode (always true in e2e tests with DRAFT_LLM_FAKE)
    // In e2e, server is always in DRY_RUN=true mode, so live should not be available
    const notAvailableMessage = page.getByText('Live mode not available');
    await expect(notAvailableMessage).toBeVisible();
    await expect(page.getByText('Server is in dry-run mode')).toBeVisible();
    
    // Preview button should still be visible
    await expect(page.getByTestId('preview-button')).toBeVisible();
  });

  test('duration picker defaults to 60 seconds', async ({ page }) => {
    await page.goto('/studio/create');
    await page.getByText('Skip - I\'ll describe my style').click();
    
    // Check that duration picker defaults to 60
    const durationSelect = page.getByTestId('duration-select');
    await expect(durationSelect).toBeVisible();
    await expect(durationSelect).toHaveValue('60');
  });

  test('regenerate button is present on review screen', async ({ page }) => {
    await page.goto('/studio/create');
    await page.getByText('Skip - I\'ll describe my style').click();
    
    await page.getByTestId('idea-input').fill('Test idea for regenerate check');
    await page.getByTestId('generate-button').click();
    
    await expect(page.getByText('Step 3: Review & Start')).toBeVisible({ timeout: 60000 });
    
    // Verify regenerate button exists (coverage for the UI element)
    const regenerateButton = page.getByTestId('regenerate-button');
    await expect(regenerateButton).toBeVisible();
    await expect(regenerateButton).toBeEnabled();
    
    // Verify warnings are shown when fake LLM is used
    // Note: The fake LLM returns a test warning about using canned data
    const warningText = await page.textContent('body');
    expect(warningText).toContain('Test mode');
  });

  // The tests below never start an episode: this file runs before full-episode-e2e, which needs the
  // account's run lock. wizard-member-e2e (runs last) starts the wizard's episode.

  test('step 1 shows all 24 cards grouped by template with length, aspect and sound', async ({ page }) => {
    await page.goto('/studio/create');
    await expect(page.getByText('Step 1: Choose a Style')).toBeVisible();
    await expect(page.locator('[data-testid^="preset-card-"]')).toHaveCount(24);
    await expect(page.locator('[data-testid^="preset-group-"]')).toHaveCount(7);
    await expect(page.getByTestId('preset-group-mood_loop')).toContainText('Mood loops');

    await expect(page.getByTestId('card-length-cozy-hangout')).toHaveText('10–15s');
    await expect(page.getByTestId('card-aspect-cozy-hangout')).toHaveText('9:16');
    await expect(page.getByTestId('card-sound-cozy-hangout')).toHaveText('no voice-over · lo-fi');
    await expect(page.getByTestId('card-length-creature-legend')).toHaveText('60–180s');
    await expect(page.getByTestId('card-aspect-creature-legend')).toHaveText('4:3');
    await expect(page.getByTestId('card-sound-creature-legend')).toHaveText('narrator voice · score');
    await expect(page.getByTestId('card-aspect-fullscreen-action-anime')).toHaveText('16:9 rotated to fill 9:16');
    await expect(page.getByTestId('card-sound-time-travel-vlog')).toHaveText('a voice per speaker · light score');

    await page.getByTestId('template-filter-dialogue_episode').click();
    await expect(page.locator('[data-testid^="preset-group-"]')).toHaveCount(1);
    await expect(page.locator('[data-testid^="preset-card-"]')).toHaveCount(4);
    await expect(page.getByTestId('preset-card-nostalgia-time-travel')).toBeVisible();
    await page.getByTestId('template-filter-all').click();
    await expect(page.locator('[data-testid^="preset-card-"]')).toHaveCount(24);
  });

  test('a card opens step 2 with its brief prompts and length range; each pick resets the length', async ({ page }) => {
    const durationValues = () =>
      page.getByTestId('duration-select').locator('option').evaluateAll(opts => opts.map(o => (o as HTMLOptionElement).value));
    await page.goto('/studio/create');

    await page.getByTestId('preset-card-nostalgia-time-travel').click();
    await expect(page.getByText('Step 2: Describe Your Video')).toBeVisible();
    await expect(page.getByTestId('selected-style')).toContainText('Style: Back to the 2000s');
    await expect(page.getByTestId('duration-select')).toHaveValue('30');
    expect(await durationValues()).toEqual(['25', '30', '35', '40']);
    for (const field of ['era', 'place', 'cast_roles', 'memory_moment']) {
      await expect(page.getByTestId(`brief-${field}`)).toBeVisible();
    }
    await page.getByTestId('duration-select').selectOption('40');

    await page.getByTestId('change-style').click();
    await expect(page.getByText('Step 1: Choose a Style')).toBeVisible();
    await page.getByTestId('preset-card-creature-legend').click();
    await expect(page.getByTestId('duration-select')).toHaveValue('120');
    expect(await durationValues()).toEqual(['60', '90', '120', '150', '180']);

    await page.getByTestId('change-style').click();
    await page.getByText('Skip - I\'ll describe my style').click();
    await expect(page.getByTestId('selected-style')).toContainText('Style: Freeform');
    await expect(page.getByTestId('duration-select')).toHaveValue('60');
    expect(await durationValues()).toEqual(['30', '60', '90', '120']);
    await expect(page.getByTestId('brief-fields')).toHaveCount(0);

    await page.getByTestId('change-style').click();
    await page.getByTestId('preset-card-nostalgia-time-travel').click();
    await expect(page.getByTestId('duration-select')).toHaveValue('30');
  });

  test('a card picked in the gallery opens step 2 at its default length', async ({ page }) => {
    await page.goto('/studio/create?archetype=cozy-hangout');
    await expect(page.getByText('Step 2: Describe Your Video')).toBeVisible();
    await expect(page.getByTestId('selected-style')).toContainText('Style: Cozy hangout loop');
    await expect(page.getByTestId('duration-select')).toHaveValue('12');

    await page.goto('/studio/create?archetype=no-such-card');
    await expect(page.getByText('Step 1: Choose a Style')).toBeVisible();
  });

  test('the draft follows the card: brief answers and length reach the API', async ({ page }) => {
    await page.goto('/studio/create');
    await page.getByTestId('preset-card-cozy-hangout').click();
    await page.getByTestId('idea-input').fill('A sleepy cat watches rain on a window');
    await page.getByTestId('brief-place').fill('attic reading nook');
    await page.getByTestId('duration-select').selectOption('15');

    const draftRequest = page.waitForRequest(r => r.url().endsWith('/api/episodes/draft-from-idea'));
    await page.getByTestId('generate-button').click();
    const sent = (await draftRequest).postDataJSON();
    expect(sent.archetype_id).toBe('cozy-hangout');
    expect(sent.target_duration).toBe(15);
    expect(sent.idea).toBe('A sleepy cat watches rain on a window\n\nPlace: attic reading nook');

    await expect(page.getByText('Step 3: Review & Start')).toBeVisible({ timeout: 60000 });
    await expect(page.getByTestId('review-video-type')).toHaveText('Video type: Cozy hangout loop');
    await expect(page.getByTestId('review-length')).toHaveText('Length: 15 seconds');
    await expect(page.getByTestId('voices-section')).toHaveCount(0);
    await expect(page.getByTestId('voice-block')).toHaveCount(0);
    await expect(page.getByTestId('preview-button')).toBeEnabled();
  });

  test('narration card: describe the voice, play it, try others until every voice was heard', async ({ page }) => {
    await page.goto('/studio/create');
    await page.getByTestId('preset-card-creature-legend').click();
    await page.getByTestId('idea-input').fill('Lantern moths guide lost travellers home through a foggy valley');
    await page.getByTestId('generate-button').click();
    await expect(page.getByText('Step 3: Review & Start')).toBeVisible({ timeout: 60000 });

    await expect(page.locator('[data-testid^="voice-slot-"]')).toHaveCount(1);
    const description = page.getByTestId('voice-description-narrator');
    await expect(description).toHaveAttribute('placeholder', 'e.g. calm, warm storyteller voice');
    await expect(page.getByTestId('voice-block')).toHaveText(
      'Choose a voice before starting: Narrator. Describe each voice above and press "Find a voice".');
    await expect(page.getByTestId('preview-button')).toBeDisabled();
    await expect(page.getByTestId('voice-find-narrator')).toBeDisabled();

    await description.fill('warm calm storyteller');
    await page.getByTestId('voice-find-narrator').click();
    const choice = page.getByTestId('voice-choice-narrator');
    await expect(choice).toHaveAttribute('data-voice-id', /^dryrun-voice-/);
    await expect(page.getByTestId('voice-block')).toHaveCount(0);
    await expect(page.getByTestId('preview-button')).toBeEnabled();

    const audio = page.getByTestId('voice-audio-narrator');
    await expect(audio).toHaveAttribute('src', /^data:audio\/wav;base64,/);
    await page.getByTestId('voice-play-narrator').click();
    await expect.poll(() => audio.evaluate(a => (a as HTMLAudioElement).played.length)).toBeGreaterThan(0);

    const heard = [await choice.getAttribute('data-voice-id')];
    const names = [await page.getByTestId('voice-name-narrator').textContent()];
    for (let i = 0; i < 7; i++) {
      await page.getByTestId('voice-retry-narrator').click();
      await expect(choice).not.toHaveAttribute('data-voice-id', heard[heard.length - 1]!);
      heard.push(await choice.getAttribute('data-voice-id'));
      names.push(await page.getByTestId('voice-name-narrator').textContent());
    }
    expect(new Set(heard).size).toBe(8);
    expect(new Set(names).size).toBe(8);

    await page.getByTestId('voice-retry-narrator').click();
    await expect(page.getByTestId('voice-error-narrator')).toContainText('You have heard every available voice');
    await expect(choice).toHaveAttribute('data-voice-id', heard[7]!);

    await page.getByTestId('voice-find-narrator').click();
    await expect(page.getByTestId('voice-error-narrator')).toHaveCount(0);
    await expect(choice).toHaveAttribute('data-voice-id', heard[0]!);
  });
});
