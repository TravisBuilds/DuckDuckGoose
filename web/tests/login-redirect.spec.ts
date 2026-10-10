import { test, expect } from '@playwright/test';

test.describe('Login Redirect Fix', () => {
  test('should redirect to /studio after successful login', async ({ page, context }) => {
    // Mock the API login endpoint
    await page.route('**/api/login', async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ success: true }),
        headers: {
          'Set-Cookie': 'studio_admin_token=test-token; HttpOnly; Path=/; SameSite=Lax',
        },
      });
    });

    // Mock the API health endpoint (used by /studio)
    await page.route('**/api/health', async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ status: 'ok', temporal: true }),
      });
    });

    // Navigate to login page with from parameter
    await page.goto('/login?from=/studio');

    // Fill in the secret and submit
    await page.fill('input[type="password"]', 'test-secret-that-is-at-least-32-chars-long');
    await page.click('button[type="submit"]');

    // Wait for navigation and verify we're on /studio
    await page.waitForURL('/studio', { timeout: 5000 });
    
    // Verify we actually landed on /studio page
    expect(page.url()).toContain('/studio');
    expect(page.url()).not.toContain('/login');
    
    // Verify the Studio Console heading is visible
    await expect(page.locator('text=Studio Console')).toBeVisible();
  });

  test('should redirect to original from parameter after login', async ({ page }) => {
    // Mock API endpoints
    await page.route('**/api/login', async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ success: true }),
      });
    });

    await page.route('**/api/episodes/ep04', async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          episode_id: 'ep04',
          stage: 'G1.01',
          approvals: {},
          shots_count: 0,
        }),
      });
    });

    await page.route('**/api/episodes/ep04/**', async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ lines: [], higgsfield_total: 0, elevenlabs_total: 0 }),
      });
    });

    // Navigate to login with custom from parameter
    await page.goto('/login?from=/studio/episodes/ep04');

    // Submit login
    await page.fill('input[type="password"]', 'test-secret-that-is-at-least-32-chars-long');
    await page.click('button[type="submit"]');

    // Should redirect to the original destination
    await page.waitForURL('/studio/episodes/ep04', { timeout: 5000 });
    expect(page.url()).toContain('/studio/episodes/ep04');
  });
});
