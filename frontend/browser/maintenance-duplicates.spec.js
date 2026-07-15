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
  default_action_counts: { keep: 30, cleanup: 30, rename: 0 },
  review_group_count: 0,
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
  default_action_counts: { keep: 1, cleanup: 1, rename: 0 },
  needs_review: false,
  review_flags: [],
}));

test('duplicate scan source folder browser opens and collapses like the BIF browser', async ({ page }) => {
  await page.route('**/api/media-browser?*', route => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({
      path: '/library',
      parent: null,
      folders: [{name: 'Studio', path: '/library/Studio'}],
    }),
  }));

  await page.goto('/maintenance#duplicates');
  const button = page.locator('#maintenanceBrowseButton');
  const browser = page.locator('#maintenanceBrowserCollapse');

  await expect(button).toHaveAttribute('aria-expanded', 'false');
  await button.click();
  await expect(button).toHaveAttribute('aria-expanded', 'true');
  await expect(button).toContainText('Hide folders');
  await expect(browser).toHaveClass(/show/);
  await expect(browser).toContainText('Studio');

  await button.click();
  await expect(button).toHaveAttribute('aria-expanded', 'false');
  await expect(button).toContainText('Choose folder');
  await expect(browser).not.toHaveClass(/show/);
});

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
  await page.locator('#maintenanceGroups [data-maint-page="next"]').first().click();
  const laterGroup = page.locator('[data-maint-group-enabled="group-27"]');
  await expect(laterGroup).toBeChecked();
  await laterGroup.uncheck();
  await expect(page.locator('#duplicateSelectionSummary')).toContainText('28 selected across all result pages');

  await page.locator('#maintenanceGroups [data-maint-page="prev"]').first().click();
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

test('quick review can quarantine the keeper and duplicate sidecars with one clear action each', async ({ page }) => {
  let planRequest = null;
  const quickScan = {
    ...scan,
    id: 'duplicate-scan-quick-review',
    duplicate_group_count: 1,
    reclaimable_bytes: 1200,
    reclaimable_label: '1.2 KB',
    default_action_counts: { keep: 2, cleanup: 2, rename: 0 },
    review_group_count: 1,
    protected_distinct_set_count: 1,
    protected_distinct_video_count: 3,
  };
  const summary = {
    id: 'group-review',
    folder: '/library/Movie',
    normalized_name: 'Movie',
    recommended_keep_id: 'video-2160',
    recommended_keep_name: 'Movie.Extended.Release.2160p.Remux.mkv',
    recommended_keep_reason: 'Higher media quality outweighed the copy-number filename',
    keeper_options: [
      {id: 'video-2160', name: 'Movie.Extended.Release.2160p.Remux.mkv', metadata_label: '3840x2160', size_label: '2 KB'},
      {id: 'video-1080', name: 'Movie.Extended.Release.1080p.WEB-DL.mkv', metadata_label: '1920x1080', size_label: '1 KB'},
    ],
    video_count: 2,
    accessory_count: 2,
    reclaimable_bytes: 1200,
    reclaimable_label: '1.2 KB',
    default_action_counts: { keep: 2, cleanup: 2, rename: 0 },
    needs_review: true,
    review_flags: [{
      kind: 'different_size_accessories',
      role: 'subtitle',
      file_count: 2,
      label: '2 matching subtitle files differ in size',
    }],
  };
  const detail = {
    ...summary,
    videos: [
      {
        id: 'video-2160',
        kind: 'video',
        path: '/library/Movie/Movie.Extended.Release.2160p.Remux.mkv',
        name: 'Movie.Extended.Release.2160p.Remux.mkv',
        size_bytes: 2000,
        size_label: '2 KB',
        metadata_label: '3840x2160',
        default_operation: 'keep',
        default_selected: false,
        accessories: [{
          id: 'srt-2160',
          kind: 'accessory',
          path: '/library/Movie/Movie.Extended.Release.2160p.Remux.en.srt',
          name: 'Movie.Extended.Release.2160p.Remux.en.srt',
          size_bytes: 100,
          size_label: '100 B',
          parent_video_id: 'video-2160',
          role: 'subtitle',
          suffix: '.en.srt',
          equivalence_key: 'subtitle:.en.srt',
          renameable: true,
          default_operation: 'keep',
          default_selected: false,
        }],
      },
      {
        id: 'video-1080',
        kind: 'video',
        path: '/library/Movie/Movie.Extended.Release.1080p.WEB-DL.mkv',
        name: 'Movie.Extended.Release.1080p.WEB-DL.mkv',
        size_bytes: 1000,
        size_label: '1 KB',
        metadata_label: '1920x1080',
        default_operation: 'move',
        default_selected: true,
        accessories: [{
          id: 'srt-1080',
          kind: 'accessory',
          path: '/library/Movie/Movie.Extended.Release.1080p.WEB-DL.en.srt',
          name: 'Movie.Extended.Release.1080p.WEB-DL.en.srt',
          size_bytes: 200,
          size_label: '200 B',
          parent_video_id: 'video-1080',
          role: 'subtitle',
          suffix: '.en.srt',
          equivalence_key: 'subtitle:.en.srt',
          renameable: true,
          default_operation: 'move',
          default_selected: true,
        }],
      },
    ],
  };

  await page.route('**/api/maintenance/duplicates/status*', route => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({ scan: quickScan }),
  }));
  await page.route('**/api/maintenance/duplicates/apply/status*', route => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({ apply: null }),
  }));
  await page.route('**/api/maintenance/duplicates/groups?*', route => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({
      scan: quickScan,
      offset: 0,
      limit: 25,
      total: 1,
      count: 1,
      has_previous: false,
      has_next: false,
      next_offset: null,
      previous_offset: null,
      large_result: false,
      review: 'all',
      groups: [summary],
    }),
  }));
  await page.route('**/api/maintenance/duplicates/groups/group-review?*', route => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({ group: detail }),
  }));
  await page.route('**/api/maintenance/duplicates/plan', route => {
    planRequest = route.request().postDataJSON();
    return route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        plan: {
          id: 'duplicate-plan-quick-review',
          scan_id: quickScan.id,
          action: 'move',
          status: 'ready',
          move_root: '/library/.vid2gif-duplicates',
          selection_mode: 'all_eligible',
          selected_group_count: 1,
          total_group_count: 1,
          file_count: 3,
          total_size_label: '1.3 KB',
          skipped_groups: [],
          manual_review: [],
          files: [],
        },
      }),
    });
  });

  await page.goto('/maintenance#duplicates');
  await expect(page.locator('#duplicateReviewSummary')).toContainText('Groups flagged');
  await expect(page.locator('#duplicateReviewSummary .duplicate-review-protected strong')).toHaveText('1');
  await expect(page.locator('[data-maint-group-card="group-review"]')).toContainText('2 matching subtitle files differ in size');
  await expect(page.locator('[data-maint-group-card="group-review"] .duplicate-summary-keeper')).toContainText('Movie.Extended.Release.2160p.Remux.mkv');
  await expect(page.locator('[data-maint-group-card="group-review"] [data-maint-keep]')).toBeVisible();
  await expect(page.locator('[data-maint-group-card="group-review"] [data-maint-keep] option')).toHaveCount(2);

  await page.locator('[data-maint-expand="group-review"]').click();
  await expect(page.locator('[data-maint-operation="srt-2160"]')).toHaveValue('keep');
  await expect(page.locator('[data-maint-file]')).toHaveCount(0);
  await expect(page.locator('.duplicate-file-name')).toContainText([
    'Movie.Extended.Release.2160p.Remux.mkv',
    'Movie.Extended.Release.1080p.WEB-DL.mkv',
    'Movie.Extended.Release.2160p.Remux.en.srt',
    'Movie.Extended.Release.1080p.WEB-DL.en.srt',
  ]);
  expect(await page.locator('[data-duplicate-file-row]').evaluateAll(rows => rows.map(row => row.getAttribute('data-comparison-depth')))).toEqual(['0', '1', '0', '1']);
  await expect(page.locator('[data-duplicate-file-row="video-1080"]')).toHaveAttribute('data-comparison-anchor', 'video-2160');
  await expect(page.locator('[data-duplicate-file-row="srt-1080"]')).toHaveAttribute('data-comparison-anchor', 'srt-2160');
  await expect(page.locator('[data-duplicate-file-row="video-2160"] .duplicate-action-badge')).toHaveText('Keep selected video');
  await expect(page.locator('[data-duplicate-file-row="video-1080"] .duplicate-action-badge')).toHaveText('Quarantine');
  await expect(page.locator('.duplicate-review-table')).toHaveAttribute('data-sort-mode', 'none');
  await expect(page.locator('.duplicate-review-table thead button')).toHaveCount(0);

  await page.locator('[data-maint-operation="srt-1080"]').selectOption('keep');
  await expect(page.locator('[data-duplicate-file-row="srt-1080"]')).toHaveAttribute('data-comparison-depth', '0');
  await expect(page.locator('[data-duplicate-file-row="srt-1080"] .duplicate-action-badge')).toHaveText('Keep');
  await page.locator('[data-maint-operation="srt-1080"]').selectOption('cleanup');
  await expect(page.locator('[data-duplicate-file-row="srt-1080"]')).toHaveAttribute('data-comparison-depth', '1');

  await page.locator('[data-maint-group-sidecars="cleanup"]').click();
  await expect(page.locator('[data-maint-operation="srt-2160"]')).toHaveValue('cleanup');
  await expect(page.locator('[data-maint-operation="srt-1080"]')).toHaveValue('cleanup');
  await expect(page.locator('[data-maint-group-card="group-review"]')).toContainText('Quarantine 3');
  await expect(page.locator('[data-maint-group-card="group-review"]')).toContainText('Keep 1');

  await page.locator('#maintenancePlanButton').click();
  await expect.poll(() => planRequest).not.toBeNull();
  expect(planRequest.groups).toHaveLength(1);
  expect(planRequest.groups[0].include_file_ids.sort()).toEqual(['srt-1080', 'srt-2160', 'video-1080']);
  expect(planRequest.groups[0].file_operations).toEqual(expect.arrayContaining([
    { file_id: 'srt-2160', operation: 'cleanup' },
    { file_id: 'srt-1080', operation: 'cleanup' },
  ]));
});

test('subtitle coverage recommendation is visible and a resolved group leaves the current list', async ({ page }) => {
  let applied = false;
  const qualityScan = {
    ...scan,
    id: 'duplicate-scan-subtitle-quality',
    duplicate_group_count: 1,
    default_action_counts: {keep: 1, cleanup: 2, rename: 1},
    review_group_count: 0,
  };
  const resolvedScan = {
    ...qualityScan,
    duplicate_group_count: 0,
    reclaimable_bytes: 0,
    reclaimable_label: '0 B',
    default_action_counts: {keep: 0, cleanup: 0, rename: 0},
  };
  const summary = {
    id: 'group-subtitle-quality',
    folder: '/library/Movie',
    normalized_name: 'Movie',
    recommended_keep_id: 'video-2160',
    recommended_keep_name: 'Movie.2160p.mkv',
    video_count: 2,
    accessory_count: 2,
    reclaimable_bytes: 1200,
    reclaimable_label: '1.2 KB',
    default_action_counts: {keep: 1, cleanup: 2, rename: 1},
    needs_review: false,
    review_flags: [],
    subtitle_signals: [{
      kind: 'subtitle_quality_choice',
      severity: 'success',
      label: 'Best SRT: Movie.1080p.eng.srt · 99.6% coverage; 1 likely incomplete replacement',
    }],
  };
  const detail = {
    ...summary,
    videos: [
      {
        id: 'video-2160', kind: 'video', path: '/library/Movie/Movie.2160p.mkv', name: 'Movie.2160p.mkv',
        size_bytes: 2000, size_label: '2 KB', metadata_label: '3840x2160', default_operation: 'keep', default_selected: false,
        accessories: [{
          id: 'srt-2160', kind: 'accessory', path: '/library/Movie/Movie.2160p.eng.srt', name: 'Movie.2160p.eng.srt',
          size_bytes: 100, size_label: '100 B', parent_video_id: 'video-2160', role: 'subtitle', renameable: true,
          suffix: '.eng.srt', equivalence_key: 'subtitle:.eng.srt',
          default_operation: 'move', default_selected: true,
          subtitle_quality: {status: 'likely_incomplete', coverage_percent: 65.6, last_timestamp_label: '27:15', video_duration_label: '41:30', cue_count: 373},
        }],
      },
      {
        id: 'video-1080', kind: 'video', path: '/library/Movie/Movie.1080p.mkv', name: 'Movie.1080p.mkv',
        size_bytes: 1000, size_label: '1 KB', metadata_label: '1920x1080', default_operation: 'move', default_selected: true,
        accessories: [{
          id: 'srt-1080', kind: 'accessory', path: '/library/Movie/Movie.1080p.eng.srt', name: 'Movie.1080p.eng.srt',
          size_bytes: 200, size_label: '200 B', parent_video_id: 'video-1080', role: 'subtitle', renameable: true,
          suffix: '.eng.srt', equivalence_key: 'subtitle:.eng.srt',
          default_operation: 'rename', default_selected: true, default_destination_path: '/library/Movie/Movie.2160p.eng.srt',
          subtitle_quality: {status: 'complete', coverage_percent: 99.6, last_timestamp_label: '41:21', video_duration_label: '41:30', cue_count: 593},
        }],
      },
    ],
  };
  const applyResult = {
    applied_count: 3,
    missing_count: 0,
    refused_count: 0,
    deferred_count: 0,
    total_applied_label: '1.1 KB',
    resolved_group_ids: [summary.id],
    resolved_group_count: 1,
    scan_reconciled: true,
    scan: resolvedScan,
  };

  await page.route('**/api/maintenance/duplicates/status*', route => route.fulfill({
    status: 200, contentType: 'application/json', body: JSON.stringify({scan: qualityScan}),
  }));
  await page.route('**/api/maintenance/duplicates/groups?*', route => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({
      scan: applied ? resolvedScan : qualityScan,
      offset: 0, limit: 25, total: applied ? 0 : 1, count: applied ? 0 : 1,
      has_previous: false, has_next: false, next_offset: null, previous_offset: null,
      large_result: false, review: 'all', groups: applied ? [] : [summary],
    }),
  }));
  await page.route('**/api/maintenance/duplicates/groups/group-subtitle-quality?*', route => route.fulfill({
    status: 200, contentType: 'application/json', body: JSON.stringify({group: detail}),
  }));
  await page.route('**/api/maintenance/duplicates/plan', route => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({plan: {
      id: 'plan-subtitle-quality', scan_id: qualityScan.id, action: 'move', status: 'ready',
      move_root: '/library/.vid2gif-duplicates', selection_mode: 'all_eligible', selected_group_count: 1,
      total_group_count: 1, file_count: 3, total_size_label: '1.1 KB', skipped_groups: [], manual_review: [], files: [],
    }}),
  }));
  await page.route('**/api/maintenance/duplicates/apply', route => {
    applied = true;
    return route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({apply: {
        id: 'apply-subtitle-quality', scan_id: qualityScan.id, action: 'move', status: 'success',
        file_count: 3, processed_count: 3, applied_count: 3, result: applyResult,
      }}),
    });
  });
  await page.route('**/api/maintenance/duplicates/apply/status*', route => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({apply: applied ? {
      id: 'apply-subtitle-quality', scan_id: qualityScan.id, action: 'move', status: 'success',
      file_count: 3, processed_count: 3, applied_count: 3, result: applyResult,
    } : null}),
  }));

  page.on('dialog', dialog => dialog.accept());
  await page.goto('/maintenance#duplicates');
  const card = page.locator('[data-maint-group-card="group-subtitle-quality"]');
  await expect(card).toContainText('99.6% coverage');
  await card.locator('[data-maint-expand]').click();
  await expect(page.locator('[data-maint-operation="srt-1080"]')).toHaveValue('rename');
  await expect(page.locator('[data-maint-operation="srt-1080"]')).toContainText('Keep with selected video (rename)');
  await expect(card).toContainText('65.6% · ends 27:15 of 41:30');
  await expect(card).toContainText('99.6% · ends 41:21 of 41:30');
  expect(await page.locator('[data-duplicate-file-row]').evaluateAll(rows => rows.map(row => row.getAttribute('data-comparison-depth')))).toEqual(['0', '1', '0', '1']);
  expect(await page.locator('[data-duplicate-file-row]').evaluateAll(rows => rows.map(row => row.getAttribute('data-duplicate-file-row')))).toEqual([
    'video-2160',
    'video-1080',
    'srt-1080',
    'srt-2160',
  ]);
  await expect(page.locator('[data-duplicate-file-row="srt-1080"] .duplicate-action-badge')).toHaveText('Keep + rename');
  await expect(page.locator('[data-duplicate-file-row="srt-2160"] .duplicate-action-badge')).toHaveText('Quarantine');
  await expect(page.locator('[data-duplicate-file-row="srt-2160"]')).toHaveClass(/duplicate-file-match-child/);
  const actionColors = await page.locator('[data-duplicate-file-row]').evaluateAll(rows => rows.map(row =>
    getComputedStyle(row.cells[0]).borderLeftColor
  ));
  expect(actionColors[0]).not.toBe(actionColors[1]);
  expect(actionColors[2]).not.toBe(actionColors[3]);

  await page.locator('#maintenancePlanButton').click();
  await page.locator('#maintenanceApplyButton').click();

  await expect(page.locator('[data-maint-group-card="group-subtitle-quality"]')).toHaveCount(0);
  await expect(page.locator('#maintenanceGroupCount')).toHaveText('0');
  await expect(page.locator('#maintenanceGroups')).toContainText('No duplicate groups found');
});
