import { test, expect } from '@playwright/test';

test.describe('Live Mode Confirmation Fix', () => {
  test.beforeEach(async ({ page }) => {
    // Mock all required API endpoints
    await page.route('**/api/episodes/ep04', async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          episode_id: 'ep04',
          stage: 'G1.08',
          approvals: { g108: true },
          shots_count: 31,
        }),
      });
    });

    await page.route('**/api/episodes/ep04/budget', async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          episode_id: 'ep04',
          lines: [],
          higgsfield_total: 0,
          elevenlabs_total: 0,
        }),
      });
    });

    await page.route('**/api/episodes/ep04/shots', async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ shots: [] }),
      });
    });

    await page.route('**/api/episodes/ep04/audit', async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ entries: [] }),
      });
    });
  });

  test('should send confirmation field when enabling live mode', async ({ page }) => {
    let receivedRequestBody: any = null;

    await page.route('**/api/episodes/ep04/gates', async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          live_mode: false,
          g108_approved: true,
        }),
      });
    });

    // Mock set-live endpoint and capture request body
    await page.route('**/api/episodes/ep04/set-live', async (route) => {
      const request = route.request();
      const postData = request.postData();
      if (postData) {
        receivedRequestBody = JSON.parse(postData);
      }
      
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          success: true,
          episode_id: 'ep04',
          live_mode: true,
        }),
      });
    });

    await page.goto('/studio/episodes/ep04');

    // Click Enable Live Mode button
    await page.click('text=Enable Live Mode');

    // Should show confirmation dialog
    await expect(page.locator('text=Type here...')).toBeVisible();

    // Type the confirmation text
    await page.fill('input[placeholder="Type here..."]', 'ENABLE LIVE MODE');

    // Click Confirm button
    await page.click('button:has-text("Confirm")');

    // Wait for the request to be sent
    await page.waitForTimeout(500);

    // Verify the request body contains the correct confirmation field
    expect(receivedRequestBody).not.toBeNull();
    expect(receivedRequestBody).toHaveProperty('confirmation');
    expect(receivedRequestBody.confirmation).toBe('ENABLE_LIVE_MODE');
  });

  test('should NOT send request when canceling live mode', async ({ page }) => {
    let setLiveRequestMade = false;

    await page.route('**/api/episodes/ep04/gates', async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          live_mode: false,
          g108_approved: true,
        }),
      });
    });

    // Mock set-live endpoint to track if it's called
    await page.route('**/api/episodes/ep04/set-live', async (route) => {
      setLiveRequestMade = true;
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          success: true,
          episode_id: 'ep04',
          live_mode: true,
        }),
      });
    });

    await page.goto('/studio/episodes/ep04');

    // Click Enable Live Mode button
    await page.click('text=Enable Live Mode');

    // Should show confirmation dialog
    await expect(page.locator('text=Type here...')).toBeVisible();

    // Type something (or nothing)
    await page.fill('input[placeholder="Type here..."]', 'wrong text');

    // Click Cancel button
    await page.click('button:has-text("Cancel")');

    // Wait a bit
    await page.waitForTimeout(500);

    // Verify the request was NOT sent
    expect(setLiveRequestMade).toBe(false);

    // Dialog should be hidden
    await expect(page.locator('text=Type here...')).not.toBeVisible();
  });

  test('should display warning about real money', async ({ page }) => {
    await page.route('**/api/episodes/ep04/gates', async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          live_mode: false,
          g108_approved: true,
        }),
      });
    });

    await page.goto('/studio/episodes/ep04');

    // Wait for the Live Mode section to load
    await page.waitForSelector('text=DRY RUN - Fake providers', { timeout: 10000 });
    await page.waitForSelector('text=Enable Live Mode', { timeout: 10000 });

    // Click Enable Live Mode button
    await page.click('text=Enable Live Mode');

    // Should show warning about real money (USD)
    await expect(page.locator('text=Live mode will charge real money')).toBeVisible();
    
    // Should show the confirmation instruction
    await expect(page.locator('text=Type "ENABLE LIVE MODE" to confirm')).toBeVisible();
  });

  test('should keep dry mode button working', async ({ page }) => {
    let setDryRequestMade = false;

    await page.route('**/api/episodes/ep04/gates', async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          live_mode: true,  // Already in live mode
          g108_approved: true,
        }),
      });
    });

    // Mock set-dry endpoint
    await page.route('**/api/episodes/ep04/set-dry', async (route) => {
      setDryRequestMade = true;
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          success: true,
          episode_id: 'ep04',
          live_mode: false,
        }),
      });
    });

    await page.goto('/studio/episodes/ep04');

    // Wait for the page to load
    await page.waitForSelector('text=Live Mode', { timeout: 10000 });

    // Should show live mode indicator
    await expect(page.locator('text=LIVE - Real providers active')).toBeVisible();

    // Click Switch to Dry Mode button
    page.on('dialog', dialog => dialog.accept());
    await page.click('text=Switch to Dry Mode');

    // Wait for the request
    await page.waitForTimeout(500);

    // Verify the dry mode request was sent
    expect(setDryRequestMade).toBe(true);
  });
});
