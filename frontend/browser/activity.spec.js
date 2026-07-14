import { test, expect } from '@playwright/test';
import AxeBuilder from '@axe-core/playwright';

const activityPayload = {
  active: true,
  waiting_count: 1,
  current: {
    id: 'gif:job-1',
    label: 'Generate GIF',
    kind: 'conversion',
    status: 'running',
    progress_percent: 42,
    progress_label: 'Rendering segment 3 of 8',
    path: '/library/Studio/Movie/video.mp4',
    href: '/gifs#logs',
    cancel_url: '/api/jobs/job-1/cancel',
  },
  waiting: [{
    id: 'scan:scan-1',
    label: 'Scan video preview quality',
    kind: 'scan',
    status: 'waiting',
    progress_percent: 0,
    progress_label: 'Waiting for the current library operation',
    path: '/library/VR',
    href: '/maintenance',
    cancel_url: '',
  }],
  recent: [{
    id: 'scan:scan-0',
    label: 'Scan missing previews',
    kind: 'scan',
    status: 'success',
    progress_percent: 100,
    progress_label: '1,490 preview files checked',
    path: '/library/XXX',
    href: '/maintenance',
    cancel_url: '',
  }],
};

let currentActivityPayload;

test.beforeEach(async ({ page }) => {
  currentActivityPayload = structuredClone(activityPayload);
  await page.route('**/api/activity', route => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify(currentActivityPayload),
  }));
});

test('global activity is understandable, cancellable, and accessible', async ({ page }) => {
  let cancelRequests = 0;
  await page.route('**/api/jobs/job-1/cancel', route => {
    cancelRequests += 1;
    return route.fulfill({ status: 200, contentType: 'application/json', body: '{"job":{"status":"cancelling"}}' });
  });

  await page.goto('/gifs');
  const activity = page.getByRole('region', { name: 'Library activity' });
  await expect(activity).toBeVisible();
  await expect(page.locator('#globalActivityLabel')).toHaveText('Rendering segment 3 of 8');
  await expect(activity.getByText('1 waiting')).toBeVisible();
  await expect(activity.getByRole('progressbar')).toHaveAttribute('aria-valuenow', '42');

  const toggle = activity.getByRole('button', { name: /Generate GIF/ });
  await toggle.focus();
  await page.keyboard.press('Enter');
  await expect(toggle).toHaveAttribute('aria-expanded', 'true');
  await expect(activity.getByRole('heading', { name: 'Current' })).toBeVisible();
  await expect(activity.getByText('Scan video preview quality')).toBeVisible();

  await activity.getByRole('button', { name: 'Cancel' }).click();
  await expect.poll(() => cancelRequests).toBe(1);

  const accessibility = await new AxeBuilder({ page })
    .include('#globalActivity')
    .withTags(['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa'])
    .analyze();
  expect(accessibility.violations).toEqual([]);
});

test('activity remains usable in dark mode and on a phone-sized viewport', async ({ page }) => {
  currentActivityPayload.current.progress_percent = null;
  await page.setViewportSize({ width: 375, height: 812 });
  await page.goto('/gifs');

  await page.getByRole('button', { name: 'Toggle navigation' }).click();
  const themeToggle = page.getByRole('button', { name: 'Switch to dark theme' });
  await themeToggle.click();
  await expect(page.locator('html')).toHaveAttribute('data-bs-theme', 'dark');
  await expect(page.locator('#themeToggle')).toHaveAttribute('aria-label', 'Switch to light theme');

  const activity = page.getByRole('region', { name: 'Library activity' });
  await expect(activity.getByRole('progressbar')).not.toHaveAttribute('aria-valuenow');
  await expect(activity.getByRole('progressbar')).toHaveAttribute('aria-valuetext', 'Rendering segment 3 of 8');
  await activity.getByRole('button', { name: /Generate GIF/ }).click();
  await expect(activity.getByText('Scan missing previews')).toBeVisible();

  const overflow = await page.evaluate(() => ({
    documentWidth: document.documentElement.scrollWidth,
    viewportWidth: document.documentElement.clientWidth,
  }));
  expect(overflow.documentWidth).toBeLessThanOrEqual(overflow.viewportWidth);
});
