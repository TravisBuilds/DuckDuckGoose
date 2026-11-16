import { test as base, expect } from '@playwright/test';

/**
 * The e2e specs' `test`: after every test, pass or fail, it ends the runs the backend still has open
 * (POST /api/e2e/end-open-runs, scripts/e2e_backend.py only). The e2e users share one account, which
 * runs one episode at a time, so a test that stops half-way would otherwise hold the run lock and
 * fail every later spec that starts a run.
 */
export const test = base.extend<{ endOpenRuns: void }>({
  endOpenRuns: [async ({ request }, use, testInfo) => {
    await use();
    const res = await request.post('http://localhost:8000/api/e2e/end-open-runs', {
      headers: { Authorization: `Bearer ${testInfo.config.metadata.adminSecret}` },
    });
    expect(res.status(), `ending the open runs after "${testInfo.title}": ${await res.text()}`).toBe(200);
    const { ended } = (await res.json()) as { ended: string[] };
    if (ended.length > 0) console.log(`[e2e cleanup] "${testInfo.title}" left runs open, ended: ${ended.join(', ')}`);
  }, { auto: true }],
});

export { expect };
