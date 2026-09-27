import { test, expect } from '@playwright/test';

for (const width of [1280, 375]) {
  test(`queue timing explains uncertainty and pause at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 900 });
    let seconds = 90;
    let confidence = 'learning';
    await page.route('**/api/queue/status*', route => route.fulfill({
      json: {
        running: [], queued: [], paused: confidence === 'paused',
        total_active_items: 3, completed_active_items: 1,
        queue_eta_seconds: seconds, queue_eta_confidence: confidence,
        queue_progress_percent: 33, queue_progress_label: '1 of 3 complete',
      },
    }));
    await page.goto('/gifs#queue');
    await expect(page.locator('#queue-eta')).toHaveText('Early estimate: about 1m 30s remaining');
    confidence = 'recalculating';
    seconds = null;
    await expect(page.locator('#queue-eta')).toHaveText('Recalculating remaining time');
    confidence = 'paused';
    await expect(page.locator('#queue-eta')).toHaveText('Queue paused');
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth > innerWidth);
    expect(overflow).toBe(false);
  });
}
