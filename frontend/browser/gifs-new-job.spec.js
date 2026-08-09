import { test, expect } from '@playwright/test';

function estimateBody({ count, isDir }) {
  return JSON.stringify({
    status: 'ready',
    scan_status: 'complete',
    is_dir: isDir,
    compatible_count: count,
    estimated_seconds: 60,
    estimated_size_bytes: 1024,
    time_label: '1m',
    size_label: '1 KB',
    confidence: 'high',
    low_confidence: false,
    detail: '',
    message: `${count} compatible videos`,
  });
}

/**
 * Serve a controlled scan estimate and capture any POST to /api/add so a test
 * can assert whether the job batch was actually submitted.
 */
async function setupNewJob(page, { count, isDir }) {
  const submitted = [];
  await page.route('**/api/scan-estimate*', route => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: estimateBody({ count, isDir }),
  }));
  await page.route('**/api/add', async route => {
    submitted.push(route.request().postData() || '');
    return route.fulfill({
      status: 200,
      contentType: 'text/html',
      body: '<html><body>queued</body></html>',
    });
  });
  await page.route('**/api/media-browser*', route => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({ path: '/library', parent: '/', folders: [] }),
  }));
  return submitted;
}

async function fillPath(page, value) {
  await page.goto('/gifs#new');
  const video = page.locator('#video');
  await expect(video).toBeVisible();
  await video.fill(value);
  // Let the debounced estimate settle so the form state is realistic.
  await page.waitForTimeout(500);
}

test('queueing a large folder asks for confirmation with the exact count', async ({ page }) => {
  const submitted = await setupNewJob(page, { count: 1247, isDir: true });
  const messages = [];
  page.on('dialog', dialog => {
    messages.push(dialog.message());
    dialog.accept();
  });

  await fillPath(page, '/library');
  await page.locator('#newJobForm button[type="submit"]').first().click();

  await expect.poll(() => messages.length).toBe(1);
  expect(messages[0]).toContain('1247');
  expect(messages[0]).toContain('/library');
  // Accepting the dialog must still submit the batch.
  await expect.poll(() => submitted.length).toBe(1);
  expect(submitted[0]).toContain('video=');
});

test('dismissing the confirmation does not queue anything', async ({ page }) => {
  const submitted = await setupNewJob(page, { count: 1247, isDir: true });
  let asked = 0;
  page.on('dialog', dialog => {
    asked += 1;
    dialog.dismiss();
  });

  await fillPath(page, '/library');
  await page.locator('#newJobForm button[type="submit"]').first().click();

  await expect.poll(() => asked).toBe(1);
  await page.waitForTimeout(500);
  expect(submitted).toEqual([]);
  // The user stays on the form rather than navigating to the queue.
  await expect(page.locator('#newJobForm')).toBeVisible();
});

test('a small folder submits without interrupting the user', async ({ page }) => {
  const submitted = await setupNewJob(page, { count: 3, isDir: true });
  let asked = 0;
  page.on('dialog', dialog => {
    asked += 1;
    dialog.accept();
  });

  await fillPath(page, '/library/Movies/Example');
  await page.locator('#newJobForm button[type="submit"]').first().click();

  await expect.poll(() => submitted.length).toBe(1);
  expect(asked).toBe(0);
});

test('a single video file submits without interrupting the user', async ({ page }) => {
  const submitted = await setupNewJob(page, { count: 1, isDir: false });
  let asked = 0;
  page.on('dialog', dialog => {
    asked += 1;
    dialog.accept();
  });

  await fillPath(page, '/library/Movies/Example/Example.mkv');
  await page.locator('#newJobForm button[type="submit"]').first().click();

  await expect.poll(() => submitted.length).toBe(1);
  expect(asked).toBe(0);
});

test('a failed estimate falls open and still submits', async ({ page }) => {
  const submitted = [];
  await page.route('**/api/scan-estimate*', route => route.fulfill({ status: 500, body: 'boom' }));
  await page.route('**/api/add', async route => {
    submitted.push(route.request().postData() || '');
    return route.fulfill({ status: 200, contentType: 'text/html', body: 'queued' });
  });
  let asked = 0;
  page.on('dialog', dialog => {
    asked += 1;
    dialog.accept();
  });

  await fillPath(page, '/library');
  await page.locator('#newJobForm button[type="submit"]').first().click();

  // A broken estimate must never block the user from queueing work.
  await expect.poll(() => submitted.length).toBe(1);
  expect(asked).toBe(0);
});
