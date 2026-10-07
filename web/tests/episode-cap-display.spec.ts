import { test, expect } from '@playwright/test';

test.describe('Episode Cap Display Fix', () => {
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

  test('should display 1,250 credits episode cap prominently', async ({ page }) => {
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
              spent: 50,
              reserved: 10,
              total: 60,
              cap: 120,
              stop: 96,
              at_stop: false,
              unit: 'credits',
            },
            {
              line_name: 'L4_clips',
              provider: 'higgsfield',
              spent: 100,
              reserved: 20,
              total: 120,
              cap: 300,
              stop: 240,
              at_stop: false,
              unit: 'credits',
            },
          ],
          higgsfield_total: 180,
          elevenlabs_total: 0,
        }),
      });
    });

    await page.goto('/studio/episodes/ep04');

    // Wait for the budget section to load
    await page.waitForSelector('text=Budget Status', { timeout: 10000 });

    // Should show 1250 credits episode cap in the budget display
    await expect(page.locator('text=/1250 credits episode cap/')).toBeVisible();

    // Should show the actual spend
    await expect(page.locator('text=180.0 credits')).toBeVisible();

    // Should NOT display the old sum of line caps as the main cap
    // The old code showed: budget?.lines.reduce((sum, l) => l.provider === 'higgsfield' ? sum + l.cap : sum, 0)
    // which would be 420 (120 + 300), but we should show 1250 as the episode cap
  });

  test('should show credits not cents for budget values', async ({ page }) => {
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
              spent: 25.5,
              reserved: 5.0,
              total: 30.5,
              cap: 120,
              stop: 96,
              at_stop: false,
              unit: 'credits',
            },
          ],
          higgsfield_total: 30.5,
          elevenlabs_total: 0,
        }),
      });
    });

    await page.goto('/studio/episodes/ep04');

    // Wait for the G1.08 section to load
    await page.waitForSelector('text=G1.08 Approved', { timeout: 10000 });

    // Should show credits in canary test description
    await expect(page.locator('text=/34\\.5 credits/')).toBeVisible();

    // Should NOT show cents symbol (¢) or dollar signs ($)
    const pageContent = await page.content();
    expect(pageContent).not.toContain('¢');
    expect(pageContent).not.toContain('6.5¢');
    expect(pageContent).not.toContain('28¢');
  });

  test('should show per-line caps separately from episode cap', async ({ page }) => {
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
              spent: 50,
              reserved: 10,
              total: 60,
              cap: 120,
              stop: 96,
              at_stop: false,
              unit: 'credits',
            },
            {
              line_name: 'L4_clips',
              provider: 'higgsfield',
              spent: 100,
              reserved: 20,
              total: 120,
              cap: 300,
              stop: 240,
              at_stop: false,
              unit: 'credits',
            },
          ],
          higgsfield_total: 180,
          elevenlabs_total: 0,
        }),
      });
    });

    await page.goto('/studio/episodes/ep04');

    // Wait for the budget section to load
    await page.waitForSelector('text=Budget Status', { timeout: 10000 });

    // Should show episode cap prominently
    await expect(page.locator('text=/1250 credits episode cap/')).toBeVisible();

    // Should still show per-line caps in each line's display
    await expect(page.locator('text=cap 120 credits')).toBeVisible();
    await expect(page.locator('text=cap 300 credits')).toBeVisible();

    // Should show line caps total separately (as a secondary detail)
    await expect(page.locator('text=Line caps total: 420.0 credits')).toBeVisible();
  });
});
