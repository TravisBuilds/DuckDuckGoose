import { test, expect } from '@playwright/test';

test.describe('Canary Status Polling Fix', () => {
  test.beforeEach(async ({ page }) => {
    // Mock authentication
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
          lines: [
            {
              line_name: 'L1_stills',
              provider: 'higgsfield',
              spent: 0,
              reserved: 0,
              total: 0,
              cap: 120,
              stop: 96,
              at_stop: false,
              unit: 'credits',
            },
          ],
          higgsfield_total: 0,
          elevenlabs_total: 0,
        }),
      });
    });

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

  test('should poll canary status and show completed', async ({ page }) => {
    const workflowId = 'test-workflow-123';
    let canaryStatusCallCount = 0;

    // Mock canary start
    await page.route('**/api/episodes/ep04/canary', async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          success: true,
          workflow_id: workflowId,
          message: 'Canary started',
        }),
      });
    });

    // Mock canary status polling
    await page.route(`**/api/canary/${workflowId}`, async (route) => {
      canaryStatusCallCount++;
      const status = canaryStatusCallCount === 1 ? 'running' : 'completed';
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          workflow_id: workflowId,
          status: status,
          result: status === 'completed' ? { still_url: 'http://fake.url', clip_url: 'http://fake.url' } : undefined,
        }),
      });
    });

    await page.goto('/studio/episodes/ep04');

    // Click Run Canary button
    page.on('dialog', dialog => dialog.accept());
    await page.click('text=Run Canary');

    // Should show running status
    await expect(page.locator('text=running')).toBeVisible({ timeout: 3000 });

    // Should eventually show completed status
    await expect(page.locator('text=completed')).toBeVisible({ timeout: 5000 });

    // Verify polling happened
    expect(canaryStatusCallCount).toBeGreaterThan(0);
  });

  test('should poll canary status and show failed with error', async ({ page }) => {
    const workflowId = 'test-workflow-456';

    // Mock canary start
    await page.route('**/api/episodes/ep04/canary', async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          success: true,
          workflow_id: workflowId,
          message: 'Canary started',
        }),
      });
    });

    // Mock canary status polling - immediately return failed
    await page.route(`**/api/canary/${workflowId}`, async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          workflow_id: workflowId,
          status: 'failed',
          error: 'HIGGSFIELD_API_KEY not set',
        }),
      });
    });

    await page.goto('/studio/episodes/ep04');

    // Wait for G1.08 to be approved and canary section to appear
    await page.waitForSelector('text=G1.08 Approved', { timeout: 10000 });
    await page.waitForSelector('text=Run Canary', { timeout: 10000 });

    // Click Run Canary button
    page.on('dialog', dialog => dialog.accept());
    await page.click('text=Run Canary');

    // Should show failed status
    await expect(page.locator('text=failed')).toBeVisible({ timeout: 5000 });

    // Should show error message
    await expect(page.locator('text=HIGGSFIELD_API_KEY not set')).toBeVisible();
  });

  test('should NOT show immediate success message', async ({ page }) => {
    const workflowId = 'test-workflow-789';

    // Mock canary start
    await page.route('**/api/episodes/ep04/canary', async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          success: true,
          workflow_id: workflowId,
          message: 'Canary started',
        }),
      });
    });

    // Mock canary status - stays running
    await page.route(`**/api/canary/${workflowId}`, async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          workflow_id: workflowId,
          status: 'running',
        }),
      });
    });

    let alertShown = false;
    page.on('dialog', async dialog => {
      const message = dialog.message();
      if (message.includes('Canary completed successfully')) {
        alertShown = true;
      }
      await dialog.accept();
    });

    await page.goto('/studio/episodes/ep04');

    // Wait for G1.08 to be approved and canary section to appear
    await page.waitForSelector('text=G1.08 Approved', { timeout: 10000 });
    await page.waitForSelector('text=Run Canary', { timeout: 10000 });

    // Click Run Canary button
    await page.click('text=Run Canary');

    // Wait a bit for any potential alert
    await page.waitForTimeout(1000);

    // Should NOT show immediate success alert
    expect(alertShown).toBe(false);

    // Should show running status instead
    await expect(page.locator('text=running')).toBeVisible();
  });
});
