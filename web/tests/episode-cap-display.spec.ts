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

  const budgetBody = (lines: object[], higgsfieldTotal: number) =>
    JSON.stringify({
      episode_id: 'ep04',
      unit: 'usd_micros',
      lines,
      higgsfield_total: higgsfieldTotal,
      elevenlabs_total: 0,
      episode_total: higgsfieldTotal,
      episode_cap: 60_000_000,
      episode_stop: 48_000_000,
      gc01_headroom: 5_000_000,
      balance: {
        manual_balance_usd_micros: 82_400_000,
        set_at: '2026-10-10T10:00:00+00:00',
        balance_remaining_usd_micros: 82_400_000 - higgsfieldTotal,
      },
    });

  const lines = [
    {
      line_name: 'L1_stills',
      provider: 'higgsfield',
      spent: 5_000_000,
      reserved: 1_000_000,
      total: 6_000_000,
      cap: 12_000_000,
      stop: 9_600_000,
      at_stop: false,
      unit: 'usd_micros',
    },
    {
      line_name: 'L4_clips',
      provider: 'higgsfield',
      spent: 10_000_000,
      reserved: 2_000_000,
      total: 12_000_000,
      cap: 30_000_000,
      stop: 24_000_000,
      at_stop: false,
      unit: 'usd_micros',
    },
  ];

  test('should display the $60.00 episode cap prominently', async ({ page }) => {
    await page.route('**/api/episodes/ep04/budget', async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: budgetBody(lines, 18_000_000),
      });
    });

    await page.goto('/studio/episodes/ep04');
    await page.waitForSelector('text=Budget Status', { timeout: 10000 });

    // Episode cap in dollars (not credits)
    await expect(page.locator('text=/\\$60\\.00 episode cap/')).toBeVisible();
    // Actual spend (integer micro-dollars rendered as dollars)
    await expect(page.locator('text=$18.00').first()).toBeVisible();
    // Episode stop at 80%
    await expect(page.locator('text=/Episode stop \\(80%\\): \\$48\\.00/')).toBeVisible();
  });

  test('should show dollars, never credits or cents', async ({ page }) => {
    await page.route('**/api/episodes/ep04/budget', async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: budgetBody(
          [{ ...lines[0], spent: 25_500_000, reserved: 5_000_000, total: 30_500_000 }],
          30_500_000
        ),
      });
    });

    await page.goto('/studio/episodes/ep04');
    await page.waitForSelector('text=G1.08 Approved', { timeout: 10000 });

    await expect(page.locator('text=$30.50').first()).toBeVisible();
    const pageContent = await page.content();
    expect(pageContent).not.toContain('¢');
    expect(pageContent).not.toContain('API credits');
  });

  test('should show per-line caps separately from episode cap', async ({ page }) => {
    await page.route('**/api/episodes/ep04/budget', async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: budgetBody(lines, 18_000_000),
      });
    });

    await page.goto('/studio/episodes/ep04');
    await page.waitForSelector('text=Budget Status', { timeout: 10000 });

    await expect(page.locator('text=/\\$60\\.00 episode cap/')).toBeVisible();
    await expect(page.locator('text=(cap $12.00)')).toBeVisible();
    await expect(page.locator('text=(cap $30.00)')).toBeVisible();
    await expect(page.locator('text=Line caps total: $42.00')).toBeVisible();
  });

  test('should show the GC.01 manual balance', async ({ page }) => {
    await page.route('**/api/episodes/ep04/budget', async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: budgetBody(lines, 18_000_000),
      });
    });

    await page.goto('/studio/episodes/ep04');
    await page.waitForSelector('text=Budget Status', { timeout: 10000 });

    await expect(page.locator('text=/Manual balance: \\$82\\.40/')).toBeVisible();
    await expect(page.locator('text=/GC\\.01 headroom \\$5\\.00/')).toBeVisible();
  });
});
