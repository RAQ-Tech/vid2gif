import { test, expect } from '@playwright/test';
import AxeBuilder from '@axe-core/playwright';

const completedScan = {
  id: 'poster-scan-complete',
  path: '/library/XXX',
  status: 'success',
  active: false,
  progress_percent: 100,
  progress_indeterminate: false,
  progress_label: '1 poster update ready',
  eligible_count: 1,
  already_landscape_count: 1,
  missing_count: 1,
  ambiguous_count: 1,
  unreadable_count: 1,
  unsafe_count: 0,
  freshness: {status: 'unchanged'},
  emby_mapping: {status: 'not_configured', total_count: 5, matched_count: 0},
};

const posterItems = [
  {
    id: 'ready',
    status: 'eligible',
    eligible: true,
    source: 'Studio/Ready-background.jpg',
    poster: 'Studio/Ready-poster.jpg',
    backup: 'Studio/Ready-poster-backup.jpg',
    message: 'Ready: rename portrait to Ready-poster-backup.jpg, then install the landscape background',
  },
  {
    id: 'missing', status: 'missing', eligible: false,
    source: 'Studio/Missing-background.jpg', poster: 'Studio/Missing-poster.jpg',
    backup: 'Studio/Missing-poster-backup.jpg', message: 'Matching poster does not exist',
  },
  {
    id: 'ambiguous', status: 'ambiguous', eligible: false,
    source: 'Studio/Ambiguous-background.jpg', poster: 'Studio/Ambiguous-poster.jpg',
    backup: 'Studio/Ambiguous-poster-backup.jpg', message: 'Multiple poster candidates are ambiguous',
  },
  {
    id: 'unreadable', status: 'unreadable', eligible: false,
    source: 'Studio/Unreadable-background.jpg', poster: 'Studio/Unreadable-poster.jpg',
    backup: 'Studio/Unreadable-poster-backup.jpg', message: 'Background image is unreadable',
  },
  {
    id: 'landscape', status: 'already_landscape', eligible: false,
    source: 'Studio/Landscape-background.jpg', poster: 'Studio/Landscape-poster.jpg',
    backup: 'Studio/Landscape-poster-backup.jpg', message: 'Poster is already landscape',
  },
];

function statusPayload(scan = completedScan) {
  return {
    settings: {
      enabled: false,
      scan_interval_seconds: 900,
      full_scan_interval_seconds: 86400,
    },
    current_run: null,
    last_run: null,
    scheduler: {next_run_at: null},
    emby_status: {configured: false, last_test: null, last_refresh: null},
    analysis_scan: scan,
    analysis_apply: null,
  };
}

test('landscape poster review exposes the shared source, filters, backup plan, and manual apply', async ({ page }) => {
  let lastItemsQuery = null;
  let planRequest = null;
  let applyRequest = null;
  await page.addInitScript(() => localStorage.setItem('vid2gif_maintenance_scan_source', '/library/XXX'));
  await page.route('**/api/maintenance/landscape-posters/status', route => route.fulfill({
    status: 200, contentType: 'application/json', body: JSON.stringify(statusPayload()),
  }));
  await page.route('**/api/maintenance/landscape-posters/items?*', route => {
    const url = new URL(route.request().url());
    lastItemsQuery = Object.fromEntries(url.searchParams.entries());
    const status = url.searchParams.get('status') || 'all';
    const search = (url.searchParams.get('search') || '').toLowerCase();
    const filtered = posterItems.filter(item =>
      (status === 'all' || item.status === status)
      && (!search || `${item.source} ${item.poster} ${item.backup} ${item.message}`.toLowerCase().includes(search))
    );
    return route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        scan: completedScan, offset: 0, limit: 10, total: filtered.length,
        count: filtered.length, has_previous: false, has_next: false,
        sort: 'background', direction: 'asc', items: filtered,
      }),
    });
  });
  await page.route('**/api/media-browser?*', route => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({
      path: '/library/XXX', parent: '/library',
      folders: [{name: 'Studio', path: '/library/XXX/Studio'}],
    }),
  }));
  await page.route('**/api/maintenance/landscape-posters/plan', async route => {
    planRequest = route.request().postDataJSON();
    return route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({plan: {id: 'poster-plan', scan_id: completedScan.id, file_count: 1}}),
    });
  });
  await page.route('**/api/maintenance/landscape-posters/apply', async route => {
    applyRequest = route.request().postDataJSON();
    return route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({apply: {id: 'poster-apply', status: 'queued', progress_percent: 0}}),
    });
  });
  await page.route('**/api/maintenance/landscape-posters/apply/status?*', route => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({apply: {
      id: 'poster-apply', status: 'success', progress_percent: 100,
      progress_label: '1 posters updated', updated_count: 1, failed_count: 0,
    }}),
  }));

  await page.goto('/maintenance#posters');
  await expect(page.locator('#posterPath')).toHaveValue('/library/XXX');
  await expect(page.locator('#posterBackupNote')).toContainText('not quarantined or deleted');
  await expect(page.locator('#posterSelectionSummary')).toContainText('1 of 1');
  await expect(page.getByRole('button', {name: 'Preview Selected Changes'})).toBeEnabled();
  const accessibility = await new AxeBuilder({page})
    .include('#pane-posters')
    .withTags(['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa'])
    .analyze();
  expect(accessibility.violations).toEqual([]);

  await page.locator('#posterBrowseButton').click();
  await expect(page.locator('#posterBrowserCollapse')).toHaveClass(/show/);
  await expect(page.locator('#posterBrowser')).toContainText('Studio');
  await page.locator('#posterBrowseButton').click();
  await expect(page.locator('#posterBrowserCollapse')).not.toHaveClass(/show/);

  await page.locator('#posterItemStatus').selectOption('missing');
  await expect.poll(() => lastItemsQuery?.status).toBe('missing');
  await expect(page.locator('#posterRecentItems')).toContainText('Missing-poster.jpg');
  await expect(page.locator('#posterRecentItems')).not.toContainText('Ready-poster.jpg');

  await page.locator('#posterItemStatus').selectOption('eligible');
  await expect(page.locator('#posterRecentItems')).toContainText('Ready-poster-backup.jpg');
  await page.getByRole('button', {name: 'Preview Selected Changes'}).click();
  await expect.poll(() => planRequest).not.toBeNull();
  expect(planRequest.selection).toEqual({mode: 'all_eligible', excluded_item_ids: []});
  await expect(page.locator('#posterPlanSummary')).toContainText('portrait will be renamed');

  page.on('dialog', dialog => dialog.accept());
  await page.getByRole('button', {name: 'Apply Selected Changes'}).click();
  await expect.poll(() => applyRequest).toEqual({plan_id: 'poster-plan'});
  await expect(page.locator('#posterMessageTitle')).toContainText('updated');
});

test('an active landscape poster scan can be cancelled', async ({ page }) => {
  let cancelRequest = null;
  const running = {
    ...completedScan,
    id: 'poster-scan-running', status: 'running', active: true,
    progress_percent: 37, progress_label: 'Analyzed 500 folders',
    eligible_count: 0, missing_count: 0, ambiguous_count: 0, unreadable_count: 0,
  };
  await page.route('**/api/maintenance/landscape-posters/status', route => route.fulfill({
    status: 200, contentType: 'application/json', body: JSON.stringify(statusPayload(running)),
  }));
  await page.route('**/api/maintenance/landscape-posters/scan/cancel', async route => {
    cancelRequest = route.request().postDataJSON();
    return route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({scan: {...running, status: 'cancelling', progress_label: 'Cancelling poster analysis'}}),
    });
  });

  await page.goto('/maintenance#posters');
  await expect(page.locator('#posterProgressPercent')).toHaveText('37%');
  await expect(page.getByRole('button', {name: 'Cancel'})).toBeEnabled();
  await page.getByRole('button', {name: 'Cancel'}).click();
  await expect.poll(() => cancelRequest).toEqual({scan_id: 'poster-scan-running'});
  await expect(page.locator('#posterScanState')).toHaveText('cancelling');
});
