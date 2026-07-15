import { test, expect } from '@playwright/test';

const scan = {
  id: 'scan-cross-page',
  path: '/library',
  status: 'success',
  progress_percent: 100,
  progress_indeterminate: false,
  progress_label: '30 missing, 0 present',
  progress_detail: '',
  current_stage: 'Complete',
  active: false,
  missing_count: 30,
  present_count: 0,
  scanned_video_count: 30,
  configured_profile: { width: 320, interval_seconds: 10 },
  recommended_profile: null,
  profile_mismatch: false,
  freshness: { status: 'unchanged' },
  emby_mapping: { status: 'not_configured', total_count: 30, matched_count: 0 },
};

const items = Array.from({ length: 30 }, (_value, index) => ({
  id: `item-${index}`,
  path: `/library/Studio/Movie ${String(index).padStart(3, '0')}.mkv`,
  relative_path: `Studio/Movie ${String(index).padStart(3, '0')}.mkv`,
  name: `Movie ${String(index).padStart(3, '0')}.mkv`,
  status: 'missing',
  size_bytes: 1000 + index,
  size_label: '1.0 KB',
  detail: 'No matching BIF file found beside the video',
  bifs: [],
  ...(index === 27 ? {
    generation_held: true,
    previous_generation_issue: {
      status: 'refused',
      reason: 'decoder rejected this video',
      run_id: 'previous-run',
    },
  } : {}),
}));

test('missing BIF selection persists across pages and holds prior failures', async ({ page }) => {
  let planRequest = null;
  await page.route('**/api/maintenance/video-previews/status*', route => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({ scan }),
  }));
  await page.route('**/api/maintenance/video-previews/generation/status*', route => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({ run: null }),
  }));
  await page.route('**/api/maintenance/video-previews/items*', route => {
    const url = new URL(route.request().url());
    const offset = Number(url.searchParams.get('offset') || 0);
    const limit = Number(url.searchParams.get('limit') || 25);
    const pageItems = items.slice(offset, offset + limit);
    return route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        scan,
        status: 'missing',
        sort: 'video',
        direction: 'asc',
        offset,
        limit,
        total: items.length,
        count: pageItems.length,
        has_previous: offset > 0,
        has_next: offset + limit < items.length,
        next_offset: offset + limit < items.length ? offset + limit : null,
        previous_offset: offset > 0 ? Math.max(0, offset - limit) : null,
        large_result: false,
        selection: { missing_total: 30, held_count: 1, default_selected_count: 29 },
        items: pageItems,
      }),
    });
  });
  await page.route('**/api/maintenance/video-previews/generation/plan', async route => {
    planRequest = route.request().postDataJSON();
    return route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        plan: {
          id: 'plan-cross-page',
          scan_id: scan.id,
          file_count: 29,
          width: 320,
          interval_seconds: 10,
          selection_mode: 'all_eligible',
          held_override_count: 1,
          files: [],
        },
      }),
    });
  });

  await page.goto('/maintenance#video-previews');
  await expect(page.locator('#previewSelectionSummary')).toContainText('29 selected across all result pages');

  const firstItem = page.getByRole('checkbox', { name: 'Generate BIF for Movie 000.mkv' });
  await expect(firstItem).toBeChecked();
  await firstItem.uncheck();
  await expect(page.locator('#previewSelectionSummary')).toContainText('28 selected across all result pages');

  await page.locator('#previewItems [data-preview-page="next"]').first().click();
  await page.locator('#previewItems [data-preview-page="next"]').first().click();
  const heldItem = page.getByRole('checkbox', { name: 'Generate BIF for Movie 027.mkv' });
  await expect(heldItem).not.toBeChecked();
  await expect(page.getByText('Previous issue')).toBeVisible();
  await heldItem.check();
  await expect(page.locator('#previewSelectionSummary')).toContainText('29 selected across all result pages');

  await page.locator('#previewItems [data-preview-page="prev"]').first().click();
  await page.locator('#previewItems [data-preview-page="prev"]').first().click();
  await expect(firstItem).not.toBeChecked();
  await page.locator('#previewGenerationPlanButton').click();

  await expect.poll(() => planRequest).not.toBeNull();
  expect(planRequest.selection).toEqual({
    mode: 'all_eligible',
    excluded_item_ids: ['item-0'],
    include_held_item_ids: ['item-27'],
  });
  await expect(page.getByText('Across all pages', { exact: true })).toBeVisible();
});
