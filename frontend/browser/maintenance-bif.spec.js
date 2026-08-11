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
      retryable: false,
      attempt_count: 1,
      tactics_tried: ['strict', 'tolerant', 'reduced'],
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
  // A failure the decoder owns is labelled as needing a retry, and offers one.
  await expect(page.getByText('Needs retry')).toBeVisible();
  await expect(page.locator('[data-preview-retry]')).toBeVisible();
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

test('a permanently failed video can be released for another attempt', async ({ page }) => {
  let clearBody = null;
  let cleared = false;

  await page.route('**/api/maintenance/video-previews/status*', route => route.fulfill({
    status: 200, contentType: 'application/json', body: JSON.stringify({ scan }),
  }));
  await page.route('**/api/maintenance/video-previews/generation/status*', route => route.fulfill({
    status: 200, contentType: 'application/json', body: JSON.stringify({ run: null }),
  }));
  await page.route('**/api/maintenance/video-previews/generation/issues/clear', async route => {
    clearBody = route.request().postDataJSON();
    cleared = true;
    return route.fulfill({
      status: 200, contentType: 'application/json', body: JSON.stringify({cleared_count: 1}),
    });
  });
  await page.route('**/api/maintenance/video-previews/items*', route => {
    // Once cleared, the failure is gone and the video is a normal candidate.
    const item = {
      id: 'stuck', name: 'Movie 999.mkv', relative_path: 'Movie 999.mkv',
      path: '/library/Movie 999.mkv', status: 'missing', size_label: '1 GB',
      detail: 'No BIF present', bifs: [],
      ...(cleared ? {} : {
        generation_held: true,
        previous_generation_issue: {
          status: 'refused', reason: 'Invalid data found when processing input',
          run_id: 'r1', retryable: false, attempt_count: 3,
          tactics_tried: ['strict', 'tolerant', 'reduced'],
        },
      }),
    };
    return route.fulfill({
      status: 200, contentType: 'application/json',
      body: JSON.stringify({
        scan, offset: 0, limit: 25, total: 1, count: 1,
        has_previous: false, has_next: false, next_offset: null, previous_offset: null,
        sort: 'video', direction: 'asc', missing_total: 1, items: [item],
      }),
    });
  });

  await page.goto('/maintenance#video-previews');
  // Every tactic that was tried is reported, so the reason is not a mystery.
  await expect(page.locator('#previewItems')).toContainText('strict, tolerant, reduced');
  await expect(page.locator('#previewItems')).toContainText('3 attempts');

  await page.locator('[data-preview-retry="stuck"]').click();

  await expect.poll(() => clearBody).toEqual({item_ids: ['stuck']});
  // The dead end is gone: no held badge, no retry button, ready to generate.
  await expect(page.locator('[data-preview-retry]')).toHaveCount(0);
  await expect(page.getByText('Needs retry')).toHaveCount(0);
});

test('the header checkbox selects and clears every video on the page', async ({ page }) => {
  await page.route('**/api/maintenance/video-previews/status*', route => route.fulfill({
    status: 200, contentType: 'application/json', body: JSON.stringify({ scan }),
  }));
  await page.route('**/api/maintenance/video-previews/generation/status*', route => route.fulfill({
    status: 200, contentType: 'application/json', body: JSON.stringify({ run: null }),
  }));
  await page.route('**/api/maintenance/video-previews/items*', route => {
    const pageItems = items.slice(0, 5).map(item => ({...item, generation_held: false}));
    return route.fulfill({
      status: 200, contentType: 'application/json',
      body: JSON.stringify({
        scan, offset: 0, limit: 25, total: 5, count: 5,
        has_previous: false, has_next: false, next_offset: null, previous_offset: null,
        sort: 'video', direction: 'asc', missing_total: 5,
        selection: {missing_total: 5, held_count: 0, default_selected_count: 5},
        items: pageItems,
      }),
    });
  });

  await page.goto('/maintenance#video-previews');
  const master = page.locator('#previewSelectVisible');
  const boxes = page.locator('[data-preview-generate]');
  await expect(boxes).toHaveCount(5);

  // Everything starts selected, so the header reflects that rather than lying.
  await expect(master).toBeChecked();

  await master.uncheck();
  for (let index = 0; index < 5; index += 1) {
    await expect(boxes.nth(index)).not.toBeChecked();
  }

  await master.check();
  for (let index = 0; index < 5; index += 1) {
    await expect(boxes.nth(index)).toBeChecked();
  }

  // Clearing one row drops the header into the partial state.
  await boxes.nth(2).uncheck();
  await expect(master).not.toBeChecked();
  await expect(master).toHaveJSProperty('indeterminate', true);
});

test('the retry control stays on screen next to a very long decoder error', async ({ page }) => {
  // The real error text that made the button unreachable.
  const longError = '[vf#0:0 @ 0x55b13c1a3680] Task finished with error code: -1094995529 '
    + '(Invalid data found when processing input) | [vf#0:0 @ 0x55b13c1a3680] Terminating '
    + 'thread with return code -1094995529 (Invalid data found when processing input) | '
    + '[vost#0:0/mjpeg @ 0x55b13c59d9c0] Could not open encoder before EOF | '
    + '[vost#0:0/mjpeg @ 0x55b13c59d9c0] Task finished with error code: -22 (Invalid argument) | '
    + '[out#0/image2 @ 0x55b13c59d880] Nothing was written into output file, because at least '
    + 'one of its streams received no packets.';

  await page.route('**/api/maintenance/video-previews/status*', route => route.fulfill({
    status: 200, contentType: 'application/json', body: JSON.stringify({ scan }),
  }));
  await page.route('**/api/maintenance/video-previews/generation/status*', route => route.fulfill({
    status: 200, contentType: 'application/json', body: JSON.stringify({ run: null }),
  }));
  await page.route('**/api/maintenance/video-previews/items*', route => route.fulfill({
    status: 200, contentType: 'application/json',
    body: JSON.stringify({
      scan, offset: 0, limit: 25, total: 1, count: 1,
      has_previous: false, has_next: false, next_offset: null, previous_offset: null,
      sort: 'video', direction: 'asc', missing_total: 1,
      selection: {missing_total: 1, held_count: 1, default_selected_count: 0},
      items: [{
        id: 'long', name: 'Movie.mp4', relative_path: 'Studio/Movie.mp4',
        path: '/library/Studio/Movie.mp4', status: 'missing', size_label: '4 GB',
        detail: 'No matching BIF file found beside the video', bifs: [],
        generation_held: true,
        previous_generation_issue: {
          status: 'refused', reason: longError, run_id: 'r1',
          retryable: false, attempt_count: 1,
          tactics_tried: ['strict', 'tolerant', 'reduced'],
        },
      }],
    }),
  }));

  await page.goto('/maintenance#video-previews');
  const retry = page.locator('[data-preview-retry="long"]');
  await expect(retry).toBeVisible();

  // The table scrolls horizontally, so page coordinates say nothing about
  // reachability. What matters is whether the control sits inside the
  // container's visible region without scrolling sideways to hunt for it.
  const reach = await page.evaluate(() => {
    const button = document.querySelector('[data-preview-retry="long"]');
    const wrap = button.closest('.workspace-table-wrap') || button.closest('.table-responsive');
    const b = button.getBoundingClientRect();
    const w = wrap.getBoundingClientRect();
    return {
      buttonRight: b.right,
      visibleRight: w.right,
      hiddenBehindScroll: wrap.scrollWidth - wrap.clientWidth,
    };
  });
  expect(reach.buttonRight).toBeLessThanOrEqual(reach.visibleRight);

  // The full error is still available, just not stretching the row.
  await expect(page.locator('.preview-detail-text')).toHaveAttribute('title', new RegExp('Invalid data found'));
});
