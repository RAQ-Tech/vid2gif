import { test, expect } from '@playwright/test';
import AxeBuilder from '@axe-core/playwright';
import { waitForOpaque } from './helpers.js';

// Actor image import is the one maintenance workstream that writes to Emby
// rather than the library, and it had no browser coverage. The property worth
// locking down is that only an unambiguous match can be imported: an actor with
// several candidate images, or none, must not be swept up by a bulk select.

const scan = {
  id: 'actor-scan-1',
  path: '/library/XXX',
  status: 'success',
  active: false,
  progress_percent: 100,
  progress_label: 'Complete',
  error: '',
  results_page_size: 25,
  large_result: false,
  missing_count: 4,
  ready_count: 1,
  ambiguous_count: 1,
  no_candidate_count: 1,
  imported_count: 0,
  failed_count: 0,
  blocked_count: 1,
  emby: { configured: true, status: 'ok' },
  freshness: { status: 'unchanged' },
};

const items = [
  {
    id: 'ready-1',
    person_id: '1001',
    name: 'Ada Lovelace',
    status: 'ready',
    candidates: [{
      relative_path: 'Studio/Ada Lovelace.jpg',
      path: '/library/XXX/Studio/Ada Lovelace.jpg',
      size_label: '240 KB',
      match_name: 'Ada Lovelace',
    }],
    videos: [{ relative_path: 'Studio/Some Film.mkv' }],
    exception: null,
  },
  {
    id: 'ambiguous-1',
    person_id: '1002',
    name: 'Grace Hopper',
    status: 'ambiguous',
    candidates: [
      { relative_path: 'Studio/Grace Hopper.jpg', path: '/library/XXX/Studio/Grace Hopper.jpg', size_label: '210 KB', match_name: 'Grace Hopper' },
      { relative_path: 'Studio/Grace Hopper.png', path: '/library/XXX/Studio/Grace Hopper.png', size_label: '310 KB', match_name: 'Grace Hopper' },
    ],
    videos: [{ relative_path: 'Studio/Another Film.mkv' }],
    exception: null,
  },
  {
    id: 'none-1',
    person_id: '1003',
    name: 'Katherine Johnson',
    status: 'no_candidate',
    candidates: [],
    videos: [{ relative_path: 'Studio/Third Film.mkv' }],
    exception: null,
  },
  {
    id: 'blocked-1',
    person_id: '1004',
    name: 'Radia Perlman',
    status: 'blocked',
    candidates: [{ relative_path: 'Studio/Radia Perlman.jpg', path: '/library/XXX/Studio/Radia Perlman.jpg', size_label: '180 KB', match_name: 'Radia Perlman' }],
    videos: [{ relative_path: 'Studio/Fourth Film.mkv' }],
    exception: { status: 'blocked', note: 'wrong person', candidate_path: '', updated_at: null },
  },
];

function itemsBody(overrides = {}) {
  return JSON.stringify({
    scan,
    status: 'all',
    sort: 'actor',
    direction: 'asc',
    offset: 0,
    limit: 25,
    total: items.length,
    count: items.length,
    has_previous: false,
    has_next: false,
    next_offset: null,
    previous_offset: null,
    large_result: false,
    items,
    ...overrides,
  });
}

async function stubActorScan(page, { itemsHandler } = {}) {
  await page.route('**/api/maintenance/actor-images/status*', route => route.fulfill({
    status: 200, contentType: 'application/json', body: JSON.stringify({ scan }),
  }));
  await page.route('**/api/maintenance/actor-images/apply/status*', route => route.fulfill({
    status: 200, contentType: 'application/json', body: JSON.stringify({ apply: null }),
  }));
  await page.route('**/api/maintenance/actor-images/items*', itemsHandler || (route => route.fulfill({
    status: 200, contentType: 'application/json', body: itemsBody(),
  })));
}

test('only an unambiguous match can be imported', async ({ page }) => {
  await stubActorScan(page);
  await page.goto('/maintenance#actor-images');

  await expect(page.locator('#actorItems')).toContainText('Ada Lovelace');

  // The one clean match is selectable.
  await expect(page.locator('[data-actor-select="ready-1"]')).toBeEnabled();

  // Two plausible images, no image at all, and an actor the user blocked: none
  // of these may be imported, so none of them offers a working checkbox.
  await expect(page.locator('[data-actor-select="ambiguous-1"]')).toBeDisabled();
  await expect(page.locator('[data-actor-select="none-1"]')).toBeDisabled();
  await expect(page.locator('[data-actor-select="blocked-1"]')).toBeDisabled();
});

test('an ambiguous actor shows both candidates rather than picking one', async ({ page }) => {
  await stubActorScan(page);
  await page.goto('/maintenance#actor-images');

  const row = page.locator('#actorItems tbody tr', { hasText: 'Grace Hopper' });
  await expect(row).toContainText('Grace Hopper.jpg');
  await expect(row).toContainText('Grace Hopper.png');
});

test('the bulk selection counts only ready actors and the plan agrees', async ({ page }) => {
  let planBody = null;
  await stubActorScan(page);
  await page.route('**/api/maintenance/actor-images/plan', async route => {
    planBody = route.request().postDataJSON();
    return route.fulfill({
      status: 200, contentType: 'application/json',
      body: JSON.stringify({
        plan: { id: 'actor-plan-1', scan_id: scan.id, file_count: 1, files: [] },
      }),
    });
  });

  await page.goto('/maintenance#actor-images');
  await expect(page.locator('#actorItems')).toContainText('Ada Lovelace');

  // Four actors listed, one importable.
  await expect(page.locator('#actorSelectionSummary')).toContainText('1');

  await page.locator('#actorPlanButton').click();
  await expect.poll(() => planBody).not.toBeNull();
  expect(planBody.scan_id).toBe(scan.id);
  expect(planBody.selection).toEqual({ mode: 'all_eligible', excluded_item_ids: [] });
});

test('deselecting the only ready actor disables the review action', async ({ page }) => {
  await stubActorScan(page);
  await page.goto('/maintenance#actor-images');
  await expect(page.locator('#actorItems')).toContainText('Ada Lovelace');

  await page.locator('[data-actor-select="ready-1"]').uncheck();

  await expect(page.locator('#actorSelectionSummary')).toContainText('0');
  await expect(page.locator('#actorPlanButton')).toBeDisabled();
});

test('an actor can be marked so it stops being offered', async ({ page }) => {
  let exceptionBody = null;
  await stubActorScan(page);
  await page.route('**/api/maintenance/actor-images/exceptions', async route => {
    exceptionBody = route.request().postDataJSON();
    return route.fulfill({
      status: 200, contentType: 'application/json', body: JSON.stringify({ exception: { status: 'ignored' } }),
    });
  });

  await page.goto('/maintenance#actor-images');
  await expect(page.locator('#actorItems')).toContainText('Katherine Johnson');

  await page.locator('[data-actor-exception="ignored"][data-actor-id="none-1"]').click();

  await expect.poll(() => exceptionBody).not.toBeNull();
  expect(exceptionBody.status).toBe('ignored');
});

test('the actor images tab is accessible with real results on screen', async ({ page }) => {
  await stubActorScan(page);
  await page.goto('/maintenance#actor-images');
  await expect(page.locator('#actorItems')).toContainText('Ada Lovelace');
  await waitForOpaque(page, '#pane-actor-images');

  const accessibility = await new AxeBuilder({ page })
    .include('#pane-actor-images')
    .withTags(['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa'])
    .analyze();

  expect(accessibility.violations).toEqual([]);
});

test('an empty result set explains itself instead of showing a bare table', async ({ page }) => {
  await stubActorScan(page, {
    itemsHandler: route => route.fulfill({
      status: 200, contentType: 'application/json',
      body: itemsBody({ items: [], total: 0, count: 0 }),
    }),
  });

  await page.goto('/maintenance#actor-images');
  await expect(page.locator('#actorItems')).toContainText('No actors in this view');
});
