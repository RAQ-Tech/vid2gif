import { test, expect } from '@playwright/test';

const logs = [{
  id: 'cleanup-1.jsonl',
  created_at: '2026-08-11T10:00:00Z',
  action: 'move',
  applied_count: 3,
  refused_count: 0,
  size_label: '4 KB',
  truncated: false,
  reversible: true,
  restore_available: true,
  remaining_restorable_count: 2,
  restored_count: 1,
}];

const items = {
  log_id: 'cleanup-1.jsonl',
  action: 'move',
  created_at: '2026-08-11T10:00:00Z',
  item_count: 3,
  restorable_count: 2,
  restored_count: 1,
  unavailable_count: 0,
  restore_incomplete: false,
  items: [
    {
      file_id: 'f-video', restore_key: 'id:f-video', operation: 'move', kind: 'video',
      current_path: '/library/.vid2gif-duplicates/Movie/Movie.720p.mkv',
      current_name: 'Movie.720p.mkv',
      original_path: '/library/Movie/Movie.720p.mkv', original_name: 'Movie.720p.mkv',
      size_bytes: 100, size_label: '100 B', state: 'restorable', detail: 'Can be put back',
    },
    {
      file_id: 'f-srt', restore_key: 'id:f-srt', operation: 'move', kind: 'accessory',
      current_path: '/library/.vid2gif-duplicates/Movie/Movie.720p.en.srt',
      current_name: 'Movie.720p.en.srt',
      original_path: '/library/Movie/Movie.720p.en.srt', original_name: 'Movie.720p.en.srt',
      size_bytes: 50, size_label: '50 B', state: 'restorable', detail: 'Can be put back',
    },
    {
      file_id: 'f-done', restore_key: 'id:f-done', operation: 'move', kind: 'accessory',
      current_path: '/library/Movie/Movie.480p-poster.jpg',
      current_name: 'Movie.480p-poster.jpg',
      original_path: '/library/Movie/Movie.480p-poster.jpg', original_name: 'Movie.480p-poster.jpg',
      size_bytes: 20, size_label: '20 B', state: 'restored', detail: 'Already put back',
    },
  ],
};

test.beforeEach(async ({ page }) => {
  await page.route('**/api/maintenance/duplicates/status*', route => route.fulfill({
    status: 200, contentType: 'application/json', body: JSON.stringify({scan: null}),
  }));
  await page.route('**/api/maintenance/duplicates/apply/status*', route => route.fulfill({
    status: 200, contentType: 'application/json', body: JSON.stringify({apply: null}),
  }));
  await page.route('**/api/maintenance/duplicates/refresh/status*', route => route.fulfill({
    status: 404, contentType: 'application/json', body: JSON.stringify({error: 'none'}),
  }));
  await page.route('**/api/maintenance/duplicates/logs', route => route.fulfill({
    status: 200, contentType: 'application/json', body: JSON.stringify({logs}),
  }));
  await page.route('**/api/maintenance/duplicates/logs/*/items', route => route.fulfill({
    status: 200, contentType: 'application/json', body: JSON.stringify(items),
  }));
});

async function openFileChooser(page) {
  await page.goto('/maintenance#duplicates');
  await page.locator('#maintenanceRefreshLogsButton').click();
  await page.locator('[data-maint-restore-choose]').click();
  await expect(page.locator('[data-maint-log-items]')).toBeVisible();
}

test('each moved file is listed with whether it can still be put back', async ({ page }) => {
  await openFileChooser(page);

  await expect(page.locator('[data-maint-log-items]')).toContainText('2 of 3 files');
  await expect(page.locator('[data-maint-restore-file="f-video"]')).toBeEnabled();
  await expect(page.locator('[data-maint-restore-file="f-srt"]')).toBeEnabled();
  // An already-restored file cannot be selected again.
  await expect(page.locator('[data-maint-restore-file="f-done"]')).toBeDisabled();
  const doneRow = page.locator('tr', {hasText: 'Movie.480p-poster.jpg'});
  await expect(doneRow).toContainText('Already put back');
});

test('a single file can be selected and previewed for restore', async ({ page }) => {
  let planBody = null;
  await page.route('**/api/maintenance/duplicates/logs/*/restore/plan', async route => {
    planBody = route.request().postDataJSON();
    return route.fulfill({
      status: 200, contentType: 'application/json',
      body: JSON.stringify({plan: {
        id: 'restore-plan-1', log_id: 'cleanup-1.jsonl', file_count: 1,
        unavailable_count: 0, collision_adjusted_count: 0, partial: true,
        files: [{
          file_id: 'f-srt', source_path: '/library/.vid2gif-duplicates/Movie/Movie.720p.en.srt',
          destination_path: '/library/Movie/Movie.720p.en.srt',
          size_label: '50 B', original_operation: 'move', collision_adjusted: false,
        }],
      }}),
    });
  });

  await openFileChooser(page);
  await expect(page.locator('#maintenanceRestoreSelectedButton')).toBeDisabled();

  await page.locator('[data-maint-restore-file="f-srt"]').check();
  await expect(page.locator('#maintenanceRestoreSelectionSummary')).toHaveText('1 file selected');
  await page.locator('#maintenanceRestoreSelectedButton').click();

  // Only the chosen file is sent, not the whole run.
  await expect.poll(() => planBody).toEqual({file_ids: ['f-srt']});
  await expect(page.locator('#maintenanceRestoreSummary')).toContainText('Restore Preview');
});

test('select all picks only the files that can actually be restored', async ({ page }) => {
  await openFileChooser(page);
  await page.locator('[data-maint-restore-select-all]').click();

  await expect(page.locator('#maintenanceRestoreSelectionSummary')).toHaveText('2 files selected');
  await expect(page.locator('[data-maint-restore-file="f-done"]')).not.toBeChecked();

  await page.locator('[data-maint-restore-select-none]').click();
  await expect(page.locator('#maintenanceRestoreSelectionSummary')).toHaveText('No files selected');
  await expect(page.locator('#maintenanceRestoreSelectedButton')).toBeDisabled();
});

test('a truncated log warns that some files can never be put back', async ({ page }) => {
  await page.route('**/api/maintenance/duplicates/logs/*/items', route => route.fulfill({
    status: 200, contentType: 'application/json',
    body: JSON.stringify({...items, restore_incomplete: true}),
  }));

  await openFileChooser(page);
  await expect(page.locator('[data-maint-log-items]')).toContainText('never recorded');
});
