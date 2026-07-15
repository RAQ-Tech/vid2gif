import { test, expect } from '@playwright/test';

const scan = {
  id: 'duplicate-scan-cross-page',
  path: '/library',
  status: 'success',
  progress_percent: 100,
  progress_indeterminate: false,
  progress_label: 'Found 30 duplicate groups',
  progress_detail: '',
  active: false,
  duplicate_group_count: 30,
  reclaimable_bytes: 30000,
  reclaimable_label: '29.3 KB',
  freshness: { status: 'unchanged' },
  emby_mapping: { status: 'not_configured', total_count: 60, matched_count: 0 },
};

const groups = Array.from({ length: 30 }, (_value, index) => ({
  id: `group-${index}`,
  folder: `/library/Studio/Movie ${String(index).padStart(3, '0')}`,
  normalized_name: `Movie ${String(index).padStart(3, '0')}`,
  recommended_keep_id: `keep-${index}`,
  recommended_keep_name: `Movie ${String(index).padStart(3, '0')}.1080p.mkv`,
  video_count: 2,
  accessory_count: 0,
  reclaimable_bytes: 1000,
  reclaimable_label: '1000 B',
}));

test('duplicate results render and selection persists across pages', async ({ page }) => {
  let planRequest = null;
  await page.route('**/api/maintenance/duplicates/status*', route => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({ scan }),
  }));
  await page.route('**/api/maintenance/duplicates/apply/status*', route => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({ apply: null }),
  }));
  await page.route('**/api/maintenance/duplicates/groups?*', route => {
    const url = new URL(route.request().url());
    const offset = Number(url.searchParams.get('offset') || 0);
    const limit = Number(url.searchParams.get('limit') || 25);
    const pageGroups = groups.slice(offset, offset + limit);
    return route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        scan,
        offset,
        limit,
        total: groups.length,
        count: pageGroups.length,
        has_previous: offset > 0,
        has_next: offset + limit < groups.length,
        next_offset: offset + limit < groups.length ? offset + limit : null,
        previous_offset: offset > 0 ? Math.max(0, offset - limit) : null,
        large_result: false,
        groups: pageGroups,
      }),
    });
  });
  await page.route('**/api/maintenance/duplicates/plan', async route => {
    planRequest = route.request().postDataJSON();
    return route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        plan: {
          id: 'duplicate-plan-cross-page',
          scan_id: scan.id,
          action: 'move',
          status: 'ready',
          move_root: '/library/.vid2gif-duplicates',
          selection_mode: 'all_eligible',
          selected_group_count: 28,
          total_group_count: 30,
          file_count: 28,
          total_size_label: '27.3 KB',
          skipped_groups: ['group-0', 'group-27'],
          manual_review: [],
          files: [],
        },
      }),
    });
  });

  await page.goto('/maintenance#duplicates');
  await expect(page.locator('#maintenanceGroups')).toContainText('Movie 000');
  await expect(page.locator('#duplicateSelectionSummary')).toContainText('30 selected across all result pages');

  const firstGroup = page.locator('[data-maint-group-enabled="group-0"]');
  await expect(firstGroup).toBeChecked();
  await firstGroup.uncheck();
  await expect(page.locator('#duplicateSelectionSummary')).toContainText('29 selected across all result pages');

  await page.locator('#maintenanceGroups [data-maint-page="next"]').first().click();
  const laterGroup = page.locator('[data-maint-group-enabled="group-27"]');
  await expect(laterGroup).toBeChecked();
  await laterGroup.uncheck();
  await expect(page.locator('#duplicateSelectionSummary')).toContainText('28 selected across all result pages');

  await page.locator('#maintenanceGroups [data-maint-page="prev"]').first().click();
  await expect(firstGroup).not.toBeChecked();
  await page.locator('#maintenancePlanButton').click();

  await expect.poll(() => planRequest).not.toBeNull();
  expect(planRequest.selection).toEqual({
    mode: 'all_eligible',
    excluded_group_ids: ['group-0', 'group-27'],
  });
  expect(planRequest.groups).toEqual([]);
  await expect(page.getByText('Across all pages', { exact: true })).toBeVisible();
});

test('active duplicate quarantine resumes live progress polling', async ({ page }) => {
  let applyStatusRequests = 0;
  await page.route('**/api/maintenance/duplicates/status*', route => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({ scan }),
  }));
  await page.route('**/api/maintenance/duplicates/groups?*', route => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({
      scan,
      offset: 0,
      limit: 25,
      total: groups.length,
      count: 25,
      has_previous: false,
      has_next: true,
      next_offset: 25,
      previous_offset: null,
      large_result: false,
      groups: groups.slice(0, 25),
    }),
  }));
  await page.route('**/api/maintenance/duplicates/apply/status*', route => {
    applyStatusRequests += 1;
    const processed = Math.min(28, applyStatusRequests * 5);
    return route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        apply: {
          id: 'duplicate-apply-running',
          plan_id: 'duplicate-plan-running',
          scan_id: scan.id,
          action: 'move',
          status: 'running',
          progress_percent: Math.round((processed / 28) * 100),
          progress_label: `Processed ${processed} of 28 files`,
          file_count: 28,
          processed_count: processed,
          applied_count: processed,
          missing_count: 0,
          refused_count: 0,
          deferred_count: 0,
          current_path: `/library/Studio/Movie ${processed}.720p.mkv`,
        },
      }),
    });
  });

  await page.goto('/maintenance#duplicates');
  await expect(page.locator('#maintenanceApplyStatus')).toBeVisible();
  await expect(page.locator('#maintenanceApplyCounts')).toContainText('of 28 processed');
  await expect.poll(() => applyStatusRequests, { timeout: 3000 }).toBeGreaterThan(1);
  await expect.poll(async () => {
    const text = await page.locator('#maintenanceApplyCounts').textContent();
    return Number((text || '').split(' ')[0] || 0);
  }).toBeGreaterThanOrEqual(10);
  await expect(page.locator('#maintenanceApplyCurrent')).toContainText('.720p.mkv');
});
