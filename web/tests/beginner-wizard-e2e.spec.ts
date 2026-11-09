import { test, expect } from '@playwright/test';

/**
 * Beginner video wizard E2E tests with REAL backend (no API mocks).
 * Tests draft generation, ID validation, and episode start.
 */

const ADMIN_SECRET = process.env.E2E_ADMIN_SECRET || 'e2e-only-admin-secret-not-used-anywhere-else';

test.describe('Beginner Video Creation Wizard E2E', () => {
  test.beforeEach(async ({ page }) => {
    // Authenticate
    await page.goto('/login');
    await page.locator('#secret').fill(ADMIN_SECRET);
    await page.getByRole('button', { name: 'Access Console' }).click();
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
});
