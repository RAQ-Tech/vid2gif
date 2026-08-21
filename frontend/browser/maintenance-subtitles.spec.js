import { test, expect } from '@playwright/test';
import AxeBuilder from '@axe-core/playwright';
import { waitForOpaque } from './helpers.js';

// The subtitles tab can quarantine and permanently delete files, and had no
// browser coverage at all. What matters most here is not that the happy path
// works but that the things which must never become cleanup targets -- videos,
// and "this subtitle is missing" findings -- offer nothing to select.

const scan = {
  id: 'subtitle-scan-1',
  mode: 'coverage',
  path: '/library/XXX',
  status: 'success',
  active: false,
  progress_percent: 100,
  progress_label: 'Complete',
  error: '',
  results_page_size: 25,
  large_result: false,
  scan_mode: 'incremental',
  review_count: 3,
  missing_count: 1,
  incomplete_count: 1,
  coverage_review_count: 1,
  ok_count: 0,
  language_review_count: 0,
  unknown_count: 0,
  reused_count: 4,
  analyzed_count: 2,
  // Three videos are on screen, but only one subtitle among them was measured
  // and found short. This is the number the selection counts against.
  actionable_file_count: 1,
  settings: { expected_languages: ['eng', 'nno'] },
  emby_mapping: { status: 'not_configured' },
  emby_streams: { status: 'unavailable', message: 'Emby is not configured.' },
  freshness: { status: 'unchanged' },
};

// One of each kind that matters:
//  - a video whose subtitle is simply absent (nothing to delete)
//  - a subtitle that is likely incomplete (deletable)
//  - a subtitle whose coverage is uncertain (review only, not deletable)
const items = [
  {
    id: 'missing-1',
    name: 'No Subs.mkv',
    relative_path: 'Studio/No Subs.mkv',
    path: '/library/XXX/Studio/No Subs.mkv',
    status: 'missing',
    size_label: '2.0 GB',
    detail: 'No matching SRT beside the video',
    srt_files: [],
    language_codes: [],
    emby_subtitle_streams: [],
  },
  {
    id: 'incomplete-1',
    name: 'Short Subs.mkv',
    relative_path: 'Studio/Short Subs.mkv',
    path: '/library/XXX/Studio/Short Subs.mkv',
    status: 'incomplete',
    size_label: '3.0 GB',
    detail: 'Subtitle ends long before the video does',
    language_codes: ['eng'],
    emby_subtitle_streams: [],
    srt_files: [
      {
        id: 'srt-incomplete',
        name: 'Short Subs.eng.srt',
        relative_path: 'Studio/Short Subs.eng.srt',
        path: '/library/XXX/Studio/Short Subs.eng.srt',
        language_code: 'eng',
        size_label: '12 KB',
        actionable: true,
        subtitle_quality: {
          status: 'likely_incomplete',
          coverage_percent: 31,
          last_timestamp_label: '00:38:12',
          video_duration_label: '02:03:44',
          cue_count: 412,
        },
      },
    ],
  },
  {
    id: 'uncertain-1',
    name: 'Maybe Fine.mkv',
    relative_path: 'Studio/Maybe Fine.mkv',
    path: '/library/XXX/Studio/Maybe Fine.mkv',
    status: 'coverage_review',
    size_label: '1.5 GB',
    detail: 'Coverage could not be established with confidence',
    language_codes: ['nno'],
    emby_subtitle_streams: [],
    srt_files: [
      {
        id: 'srt-uncertain',
        name: 'Maybe Fine.nno.srt',
        relative_path: 'Studio/Maybe Fine.nno.srt',
        path: '/library/XXX/Studio/Maybe Fine.nno.srt',
        language_code: 'nno',
        size_label: '30 KB',
        actionable: false,
        action_reason: 'video duration unknown',
        subtitle_quality: { status: 'unknown', label: 'Coverage unavailable' },
      },
    ],
  },
];

function itemsBody(overrides = {}) {
  return JSON.stringify({
    scan,
    status: 'review',
    q: '',
    sort: 'video',
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

async function stubSubtitleScan(page, { itemsHandler } = {}) {
  await page.route('**/api/maintenance/subtitles/status*', route => route.fulfill({
    status: 200, contentType: 'application/json', body: JSON.stringify({ scan }),
  }));
  await page.route('**/api/maintenance/subtitles/apply/status*', route => route.fulfill({
    status: 200, contentType: 'application/json', body: JSON.stringify({ apply: null }),
  }));
  await page.route('**/api/maintenance/subtitles/items*', itemsHandler || (route => route.fulfill({
    status: 200, contentType: 'application/json', body: itemsBody(),
  })));
}

test('a video with no subtitle offers nothing to delete', async ({ page }) => {
  await stubSubtitleScan(page);
  await page.goto('/maintenance#subtitles');

  const table = page.locator('#subtitleItems');
  await expect(table).toContainText('No Subs.mkv');

  // The whole guarantee in one assertion: the row for a missing subtitle has
  // no selectable file, so no amount of clicking can queue the video itself
  // for quarantine or deletion.
  const missingRow = page.locator('#subtitleItems tbody tr', { hasText: 'No Subs.mkv' });
  await expect(missingRow).toContainText('No matching SRT');
  await expect(missingRow.locator('[data-subtitle-file]')).toHaveCount(0);

  // Nothing on the page offers the video path as a checkbox either.
  await expect(page.locator('[data-subtitle-file="missing-1"]')).toHaveCount(0);
});

test('uncertain coverage is shown but cannot be actioned', async ({ page }) => {
  await stubSubtitleScan(page);
  await page.goto('/maintenance#subtitles');

  const uncertainRow = page.locator('#subtitleItems tbody tr', { hasText: 'Maybe Fine.mkv' });
  await expect(uncertainRow).toContainText('Coverage review');
  await expect(uncertainRow).toContainText('video duration unknown');

  // Visible for review, but not selectable: an unmeasurable subtitle must not
  // be swept up by a bulk action.
  await expect(uncertainRow.locator('[data-subtitle-file]')).toHaveCount(0);
  await expect(page.locator('[data-subtitle-file="srt-uncertain"]')).toHaveCount(0);

  // The one that was measured and found short is selectable.
  await expect(page.locator('[data-subtitle-file="srt-incomplete"]')).toHaveCount(1);
});

test('select-visible only picks up the flagged subtitle, and the plan says so', async ({ page }) => {
  let planBody = null;
  await stubSubtitleScan(page);
  await page.route('**/api/maintenance/subtitles/plan', async route => {
    planBody = route.request().postDataJSON();
    return route.fulfill({
      status: 200, contentType: 'application/json',
      body: JSON.stringify({
        plan: {
          id: 'plan-1',
          scan_id: scan.id,
          action: 'quarantine',
          file_count: 1,
          files: [{ id: 'srt-incomplete', relative_path: 'Studio/Short Subs.eng.srt' }],
        },
      }),
    });
  });

  await page.goto('/maintenance#subtitles');
  await expect(page.locator('#subtitleItems')).toContainText('Short Subs.mkv');

  // Three videos on screen, one actionable subtitle among them.
  await expect(page.locator('#subtitleSelectionSummary')).toContainText('1 selected');

  // Clearing the only actionable file leaves nothing to review.
  await page.locator('[data-subtitle-file="srt-incomplete"]').uncheck();
  await expect(page.locator('#subtitleSelectionSummary')).toContainText('0 selected');
  await expect(page.locator('#subtitlePlanButton')).toBeDisabled();

  await page.locator('#subtitleSelectAllButton').click();
  await expect(page.locator('#subtitleSelectionSummary')).toContainText('1 selected');
  await expect(page.locator('#subtitlePlanButton')).toBeEnabled();

  await page.locator('#subtitlePlanButton').click();
  await expect.poll(() => planBody).not.toBeNull();
  expect(planBody.scan_id).toBe(scan.id);
  // The selection covers every eligible file with nothing held back -- and the
  // unmeasurable subtitle is not eligible, so it cannot ride along.
  expect(planBody.selection).toEqual({ mode: 'all_eligible', excluded_file_ids: [] });
  expect(JSON.stringify(planBody)).not.toContain('srt-uncertain');
});

test('the subtitles tab is accessible with real results on screen', async ({ page }) => {
  await stubSubtitleScan(page);
  await page.goto('/maintenance#subtitles');
  await expect(page.locator('#subtitleItems')).toContainText('Short Subs.mkv');
  await waitForOpaque(page, '#pane-subtitles');

  const accessibility = await new AxeBuilder({ page })
    .include('#pane-subtitles')
    .withTags(['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa'])
    .analyze();

  expect(accessibility.violations).toEqual([]);
});

test('an empty result set explains itself instead of showing a bare table', async ({ page }) => {
  await stubSubtitleScan(page, {
    itemsHandler: route => route.fulfill({
      status: 200, contentType: 'application/json',
      body: itemsBody({ items: [], total: 0, count: 0 }),
    }),
  });

  await page.goto('/maintenance#subtitles');
  await expect(page.locator('#subtitleItems')).toContainText('No videos in this view');
});

test('the master checkbox mirrors and drives the selection', async ({ page }) => {
  await stubSubtitleScan(page);
  await page.goto('/maintenance#subtitles');
  await expect(page.locator('#subtitleItems')).toContainText('Short Subs.mkv');

  // One actionable file, selected by default, so the master shows fully checked.
  const master = page.locator('#subtitleSelectAllCheckbox');
  await expect(master).toBeEnabled();
  await expect(master).toBeChecked();

  await master.uncheck();
  await expect(page.locator('#subtitleSelectionSummary')).toContainText('0 selected');
  await expect(page.locator('#subtitlePlanButton')).toBeDisabled();

  await master.check();
  await expect(page.locator('#subtitleSelectionSummary')).toContainText('1 selected');
});
