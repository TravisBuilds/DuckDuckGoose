import { test, expect } from '@playwright/test';

test.describe('Beginner Video Creation Wizard', () => {
  test.beforeEach(async ({ page }) => {
    // Mock API endpoints
    await page.route('**/api/health', async route => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ status: 'ok', temporal: true }),
      });
    });
  });

  test('displays archetype selection on initial load', async ({ page }) => {
    await page.goto('/studio/create');

    // Should show step 1: archetype selection
    await expect(page.getByText('Step 1: Choose a Style')).toBeVisible();
    
    // Should show at least some archetype cards
    await expect(page.getByText('Cozy hangout loop')).toBeVisible();
  });

  test('skips to idea step when archetype is preselected', async ({ page }) => {
    await page.goto('/studio/create?archetype=cozy-hangout');

    // Should skip to step 2
    await expect(page.getByText('Step 2: Describe Your Video')).toBeVisible();
    await expect(page.getByText('Style: Cozy hangout loop')).toBeVisible();
  });

  test('shows validation error for empty idea', async ({ page }) => {
    await page.goto('/studio/create?archetype=cozy-hangout');

    // Try to generate without entering an idea
    await page.getByTestId('generate-button').click();

    // Should show error
    await expect(page.getByText('Please describe your video idea')).toBeVisible();
  });

  test('generates draft and shows review screen', async ({ page }) => {
    // Mock the draft generation API
    await page.route('**/api/episodes/draft-from-idea', async route => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          episode_id: 'ep42',
          beatmap: '# Episode ep42 Beatmap\n\n| # | ID | Screen (gen) | Room | TOD | Chars | Action | Purpose | In | Out | Model |\n|---|---|---|---|---|---|---|---|---|---|---|\n| 1 | S01 | 5.0 (5.0) | Library | Day | A | Robot enters | Open | - | - | K |',
          shots_json: {
            shots: [
              {
                id: 'S01',
                prompt: 'A robot enters an abandoned library',
                count: 1,
                characters: ['A'],
                wet: false,
                model: 'kling',
                duration: 5,
                end_frame: false,
                ref_keys: []
              },
              {
                id: 'S02',
                prompt: 'The robot discovers a glowing book',
                count: 1,
                characters: ['A'],
                wet: false,
                model: 'kling',
                duration: 6,
                end_frame: false,
                ref_keys: []
              }
            ]
          },
          refs_json: {
            per_shot: {
              S01: [],
              S02: []
            }
          },
          prompt_kit_json: {
            style: 'photorealistic, cinematic',
            aspect_ratio: '9:16',
            characters: {
              A: { description: 'a curious robot' }
            },
            sets: {
              Library: 'an abandoned library'
            }
          },
          cost_estimate: {
            total_usd_micros: 5000000,
            per_shot: {}
          },
          warnings: []
        }),
      });
    });

    await page.goto('/studio/create?archetype=cozy-hangout');

    // Enter an idea
    await page.getByTestId('idea-input').fill(
      'A curious robot explores an abandoned library and discovers glowing books'
    );

    // Generate draft
    await page.getByTestId('generate-button').click();

    // Should show loading state
    await expect(page.getByText('Creating your video...')).toBeVisible();

    // Wait for review screen
    await expect(page.getByText('Step 3: Review & Start')).toBeVisible({ timeout: 10000 });

    // Should show episode details
    await expect(page.getByText('Episode: ep42')).toBeVisible();
    await expect(page.getByText('Shots: 2')).toBeVisible();

    // Should show cost estimate
    await expect(page.getByTestId('estimated-cost')).toContainText('$5.00');

    // Should show characters
    await expect(page.getByText('a curious robot')).toBeVisible();

    // Should show shot list
    await expect(page.getByText('A robot enters an abandoned library')).toBeVisible();
  });

  test('displays IP steering warning when provided', async ({ page }) => {
    await page.route('**/api/episodes/draft-from-idea', async route => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          episode_id: 'ep43',
          beatmap: '# Test',
          shots_json: { shots: [{ id: 'S01', prompt: 'test', count: 1, characters: [], wet: false, model: 'kling', duration: 5, end_frame: false, ref_keys: [] }] },
          refs_json: { per_shot: { S01: [] } },
          prompt_kit_json: { style: 'test', aspect_ratio: '9:16', characters: {}, sets: {} },
          warnings: ['Content policy note: Changed copyrighted character to original alternative']
        }),
      });
    });

    await page.goto('/studio/create?archetype=cozy-hangout');
    await page.getByTestId('idea-input').fill('Test idea with copyrighted content');
    await page.getByTestId('generate-button').click();

    await expect(page.getByText('Content policy note:')).toBeVisible({ timeout: 10000 });
  });

  test('shows clear error when API key is missing', async ({ page }) => {
    await page.route('**/api/episodes/draft-from-idea', async route => {
      await route.fulfill({
        status: 503,
        contentType: 'application/json',
        body: JSON.stringify({
          detail: 'Story writing assistant not configured. Set OPENAI_API_KEY environment variable.'
        }),
      });
    });

    await page.goto('/studio/create?archetype=cozy-hangout');
    await page.getByTestId('idea-input').fill('Test idea');
    await page.getByTestId('generate-button').click();

    await expect(page.getByText(/not configured/i)).toBeVisible({ timeout: 10000 });
  });

  test('allows regenerating draft', async ({ page }) => {
    let callCount = 0;
    
    await page.route('**/api/episodes/draft-from-idea', async route => {
      callCount++;
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          episode_id: `ep${40 + callCount}`,
          beatmap: '# Test',
          shots_json: { shots: [{ id: 'S01', prompt: `Generated ${callCount}`, count: 1, characters: [], wet: false, model: 'kling', duration: 5, end_frame: false, ref_keys: [] }] },
          refs_json: { per_shot: { S01: [] } },
          prompt_kit_json: { style: 'test', aspect_ratio: '9:16', characters: {}, sets: {} },
          warnings: []
        }),
      });
    });

    await page.goto('/studio/create?archetype=cozy-hangout');
    await page.getByTestId('idea-input').fill('Test idea for regeneration');
    await page.getByTestId('generate-button').click();

    // Wait for first draft
    await expect(page.getByText('Step 3: Review & Start')).toBeVisible({ timeout: 10000 });
    await expect(page.getByText('Episode: ep41')).toBeVisible();

    // Regenerate
    await page.getByTestId('regenerate-button').click();
    await expect(page.getByText('Creating your video...')).toBeVisible();

    // Should show new draft
    await expect(page.getByText('Episode: ep42')).toBeVisible({ timeout: 10000 });
  });

  test('shows content policy reminder', async ({ page }) => {
    await page.goto('/studio/create?archetype=cozy-hangout');

    await expect(page.getByText('Original Content Only')).toBeVisible();
    await expect(page.getByText(/No copyrighted characters/i)).toBeVisible();
  });

  test('link from studio home navigates to wizard', async ({ page }) => {
    await page.goto('/studio');

    // Click "Make Your Own Video"
    await page.getByTestId('make-video-link').click();

    // Should navigate to wizard
    await expect(page).toHaveURL('/studio/create');
    await expect(page.getByText('Make Your Own Video')).toBeVisible();
  });

  test('shows link to advanced upload', async ({ page }) => {
    await page.goto('/studio/create');

    await expect(page.getByText(/Upload files manually/i)).toBeVisible();
  });
});
