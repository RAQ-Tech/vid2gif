import { test, expect } from '@playwright/test';
import AxeBuilder from '@axe-core/playwright';
import { waitForOpaque } from './helpers.js';

// The tagline-titles panel writes to Emby, so what matters in the browser is
// the same thing as everywhere else in this app: review before apply, only
// eligible rows selectable, and the undo within reach.

const scan = {
  id: 'tagline-scan-1',
  status: 'success',
  active: false,
  progress_label: '2 ready, 1 already done',
  item_total: 4,
  counts: { ready: 2, done: 1, unusable: 1 },
  lock_items: true,
  error: '',
  created_at: '2026-08-18T10:00:00+00:00',
  finished_at: '2026-08-18T10:00:05+00:00',
};

const items = [
  {
    id: 'full-edit',
    name: 'Show S01E02',
    status: 'ready',
    current_tagline: '',
    proposed_tagline: 'Show',
    original_title: '',
    title_differs: true,
    writes_title: true,
    original_backup: 'will-copy',
    detail: 'Title and tagline will be written',
  },
  {
    id: 'tagline-only',
    name: 'Amelie S01E02',
    status: 'ready',
    current_tagline: '',
    proposed_tagline: 'Amelie',
    original_title: 'Le Fabuleux Destin',
    title_differs: true,
    writes_title: false,
    original_backup: 'occupied',
    detail: 'Tagline only: the original-title field is in use',
  },
  {
    id: 'done-item',
    name: 'Finished',
    status: 'done',
    current_tagline: 'Finished',
    proposed_tagline: 'Finished',
    title_differs: false,
    writes_title: false,
    detail: 'Tagline already matches',
  },
  {
    id: 'marker-only',
    name: 'S05E05',
    status: 'unusable',
    current_tagline: '',
    proposed_tagline: '',
    title_differs: false,
    writes_title: false,
    detail: 'Nothing would be left after removing the markers',
  },
];

function page_payload(filter) {
  const filtered = filter === 'all' ? items : items.filter(item => item.status === filter);
  return {
    scan,
    status: filter,
    offset: 0,
    limit: 25,
    total: filtered.length,
    count: filtered.length,
    has_previous: false,
    has_next: false,
    next_offset: null,
    previous_offset: null,
    items: filtered,
  };
}

async function stubTaglines(page) {
  await page.route('**/api/settings', route => route.fulfill({
    status: 200, contentType: 'application/json',
    body: JSON.stringify({ settings: { emby_tagline_lock_items: true } }),
  }));
  await page.route('**/api/maintenance/emby-taglines/status*', route => route.fulfill({
    status: 200, contentType: 'application/json', body: JSON.stringify({ scan }),
  }));
  await page.route('**/api/maintenance/emby-taglines/items*', route => {
    const url = new URL(route.request().url());
    return route.fulfill({
      status: 200, contentType: 'application/json',
      body: JSON.stringify(page_payload(url.searchParams.get('status') || 'ready')),
    });
  });
  await page.route('**/api/maintenance/emby-taglines/logs', route => route.fulfill({
    status: 200, contentType: 'application/json',
    body: JSON.stringify({
      logs: [
        { id: 'log-1.jsonl', kind: 'apply', created_at: '2026-08-18T10:05:00+00:00', applied_count: 2, failed_count: 0 },
      ],
    }),
  }));
}

test('ready rows are selectable and the rest are not', async ({ page }) => {
  await stubTaglines(page);
  await page.goto('/maintenance#emby-operations');

  await expect(page.locator('#taglineItems')).toContainText('Show S01E02');
  await expect(page.locator('[data-tagline-select="full-edit"]')).toBeChecked();
  await expect(page.locator('#taglineSelectionSummary')).toContainText('2 selected');

  // The occupied-original row still gets its tagline, and says why the title
  // stays put.
  await expect(page.locator('#taglineItems')).toContainText('original-title field is in use');

  await page.locator('#taglineStatusFilter').selectOption('all');
  await expect(page.locator('#taglineItems')).toContainText('Finished');
  const disabled = page.locator('#taglineItems input[type="checkbox"]:disabled');
  await expect(disabled).toHaveCount(2);
});

test('review builds the plan and apply sends exactly that plan', async ({ page }) => {
  let planBody = null;
  let applyBody = null;
  await stubTaglines(page);
  await page.route('**/api/maintenance/emby-taglines/plan', async route => {
    planBody = route.request().postDataJSON();
    return route.fulfill({
      status: 200, contentType: 'application/json',
      body: JSON.stringify({
        plan: {
          id: 'plan-1', scan_id: scan.id, item_count: 1, lock_items: true,
          preview: [{ name: 'Show S01E02', proposed_tagline: 'Show' }],
        },
      }),
    });
  });
  await page.route('**/api/maintenance/emby-taglines/apply', async route => {
    applyBody = route.request().postDataJSON();
    return route.fulfill({
      status: 200, contentType: 'application/json',
      body: JSON.stringify({ run: { id: 'run-1', kind: 'apply', status: 'queued', active: true, item_count: 1 } }),
    });
  });
  await page.route('**/api/maintenance/emby-taglines/run/status*', route => route.fulfill({
    status: 200, contentType: 'application/json',
    body: JSON.stringify({
      run: {
        id: 'run-1', kind: 'apply', status: 'success', active: false,
        progress_label: '1 updated, 0 refused, 0 failed',
        item_count: 1, processed_count: 1, applied_count: 1, refused_count: 0, failed_count: 0,
      },
    }),
  }));

  await page.goto('/maintenance#emby-operations');
  await expect(page.locator('#taglineItems')).toContainText('Show S01E02');

  // Deselect one of the two, then review.
  await page.locator('[data-tagline-select="tagline-only"]').uncheck();
  await expect(page.locator('#taglineSelectionSummary')).toContainText('1 selected');
  await expect(page.locator('#taglineApplyButton')).toBeDisabled();

  await page.locator('#taglinePlanButton').click();
  await expect.poll(() => planBody).not.toBeNull();
  expect(planBody.selection).toEqual({ mode: 'all_eligible', excluded_item_ids: ['tagline-only'] });
  await expect(page.locator('#taglinePlanSummary')).toContainText('1 item will be updated in Emby and locked');

  await page.locator('#taglineApplyButton').click();
  await expect.poll(() => applyBody).not.toBeNull();
  expect(applyBody.plan_id).toBe('plan-1');
  await expect(page.locator('#taglineRunStatus')).toContainText('1 updated');
});

test('an applied run offers undo and undo names the log', async ({ page }) => {
  let undoBody = null;
  await stubTaglines(page);
  await page.route('**/api/maintenance/emby-taglines/undo', async route => {
    undoBody = route.request().postDataJSON();
    return route.fulfill({
      status: 200, contentType: 'application/json',
      body: JSON.stringify({ run: { id: 'run-2', kind: 'undo', status: 'queued', active: true, item_count: 2 } }),
    });
  });
  await page.route('**/api/maintenance/emby-taglines/run/status*', route => route.fulfill({
    status: 200, contentType: 'application/json',
    body: JSON.stringify({
      run: {
        id: 'run-2', kind: 'undo', status: 'success', active: false,
        progress_label: '2 restored, 0 failed',
        item_count: 2, processed_count: 2, applied_count: 2, refused_count: 0, failed_count: 0,
      },
    }),
  }));

  await page.goto('/maintenance#emby-operations');
  await expect(page.locator('#taglineLogList')).toContainText('2 applied');

  await page.locator('[data-tagline-undo="log-1.jsonl"]').click();
  await expect.poll(() => undoBody).not.toBeNull();
  expect(undoBody.log_id).toBe('log-1.jsonl');
  await expect(page.locator('#taglineRunStatus')).toContainText('2 restored');
});

test('the panel is accessible with populated results', async ({ page }) => {
  await stubTaglines(page);
  await page.goto('/maintenance#emby-operations');
  await expect(page.locator('#taglineItems')).toContainText('Show S01E02');
  await waitForOpaque(page, '#pane-emby-operations');

  const accessibility = await new AxeBuilder({ page })
    .include('#pane-emby-operations')
    .withTags(['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa'])
    .analyze();

  expect(accessibility.violations).toEqual([]);
});

test('the master checkbox selects everything or hands over to explicit picking', async ({ page }) => {
  let planBody = null;
  await stubTaglines(page);
  await page.route('**/api/maintenance/emby-taglines/plan', async route => {
    planBody = route.request().postDataJSON();
    return route.fulfill({
      status: 200, contentType: 'application/json',
      body: JSON.stringify({ plan: { id: 'plan-2', scan_id: scan.id, item_count: 1, lock_items: true, preview: [] } }),
    });
  });

  await page.goto('/maintenance#emby-operations');
  await expect(page.locator('#taglineItems')).toContainText('Show S01E02');

  const master = page.locator('#taglineSelectAllCheckbox');
  await expect(master).toBeChecked();

  // Unticking the master clears the selection entirely.
  await master.uncheck();
  await expect(page.locator('#taglineSelectionSummary')).toContainText('0 selected');
  await expect(page.locator('#taglinePlanButton')).toBeDisabled();

  // Ticking one row back in builds an explicit list, and the plan carries it.
  await page.locator('[data-tagline-select="full-edit"]').check();
  await expect(page.locator('#taglineSelectionSummary')).toContainText('1 selected');
  await page.locator('#taglinePlanButton').click();
  await expect.poll(() => planBody).not.toBeNull();
  expect(planBody.selection).toEqual({ item_ids: ['full-edit'] });
});
