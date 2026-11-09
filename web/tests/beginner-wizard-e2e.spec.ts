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

  test('complete flow: draft idea -> start dry run -> episode loads', async ({ page }) => {
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
    
    // Generate draft (real API call, OpenAI will be mocked by backend or use real key)
    await page.getByTestId('generate-button').click();
    
    // Should show loading
    await expect(page.getByText('Creating your video...')).toBeVisible();
    
    // Wait for review screen (may take 10-20s with real OpenAI)
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
    
    // Start in preview mode (dry run) - real API start
    await page.getByTestId('preview-button').click();
    
    // Should redirect to episode page
    await expect(page).toHaveURL(new RegExp(`/studio/episodes/${episodeId}$`), { timeout: 10000 });
    
    // Episode page should load and show status
    await expect(page.getByText(/Episode.*Status/i)).toBeVisible({ timeout: 10000 });
    
    // Should be in dry run mode
    await expect(page.locator('text=/dry.?run/i')).toBeVisible({ timeout: 5000 });
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

    // Click "Make Your Own Video"
    await page.getByTestId('make-video-link').click();

    // Should navigate to wizard
    await expect(page).toHaveURL('/studio/create');
    await expect(page.getByText('Make Your Own Video')).toBeVisible();
  });

  test('live mode button visibility based on server status', async ({ page }) => {
    // Generate a draft first
    await page.goto('/studio/create');
    await page.getByText('Skip - I\'ll describe my style').click();
    
    await page.getByTestId('idea-input').fill('Test idea for live mode check');
    await page.getByTestId('generate-button').click();
    
    await expect(page.getByText('Step 3: Review & Start')).toBeVisible({ timeout: 60000 });
    
    // Check if server is in dry run mode (most likely in e2e tests)
    // Either "Make My Video" button is visible, or the "not available" message is shown
    const liveButton = page.getByTestId('make-video-button');
    const notAvailableMessage = page.getByText('Live mode not available');
    
    const hasLiveButton = await liveButton.isVisible().catch(() => false);
    const hasNotAvailable = await notAvailableMessage.isVisible().catch(() => false);
    
    // One of them must be visible
    expect(hasLiveButton || hasNotAvailable).toBe(true);
    
    // If live not available, should show explanation
    if (hasNotAvailable) {
      await expect(page.getByText('Server is in dry-run mode')).toBeVisible();
    }
  });
});
