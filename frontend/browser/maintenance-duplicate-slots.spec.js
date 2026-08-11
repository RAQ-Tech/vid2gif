import { test, expect } from '@playwright/test';

const scan = {
  id: 'slot-scan',
  path: '/library',
  status: 'success',
  progress_percent: 100,
  progress_indeterminate: false,
  progress_label: 'Found 1 duplicate group',
  active: false,
  duplicate_group_count: 1,
  reclaimable_bytes: 1000,
  reclaimable_label: '1000 B',
  default_action_counts: { keep: 2, cleanup: 3, rename: 2 },
  review_group_count: 1,
  freshness: { status: 'unchanged' },
  emby_mapping: { status: 'not_configured', total_count: 2, matched_count: 0 },
};

const summaryGroup = {
  id: 'group-0',
  folder: '/library/Studio/Shared Title',
  normalized_name: 'Shared Title',
  recommended_keep_id: 'keep-0',
  recommended_keep_name: 'Shared Title [WEBDL-2160p].mkv',
  keeper_options: [
    {id: 'keep-0', name: 'Shared Title [WEBDL-2160p].mkv', metadata_label: '3840x2160'},
    {id: 'remove-0', name: 'Shared Title [BluRay-1080p].mkv', metadata_label: '1920x1080'},
  ],
  video_count: 2,
  accessory_count: 5,
  reclaimable_bytes: 1000,
  reclaimable_label: '1000 B',
  default_action_counts: { keep: 2, cleanup: 3, rename: 2 },
  needs_review: true,
  review_flags: [],
  slot_summary: { slot_count: 4, borrowed_count: 2, review_count: 1, identical_count: 1 },
};

function accessory(id, name, role, suffix) {
  return {
    id, name, role, suffix,
    kind: 'accessory',
    path: `/library/Studio/Shared Title/${name}`,
    size_label: '1 KB',
    equivalence_key: `${role}:${suffix.toLowerCase()}`,
  };
}

const detailGroup = {
  ...summaryGroup,
  videos: [
    {
      id: 'keep-0',
      name: 'Shared Title [WEBDL-2160p].mkv',
      kind: 'video',
      path: '/library/Studio/Shared Title/Shared Title [WEBDL-2160p].mkv',
      metadata_label: '3840x2160',
      size_label: '8 GB',
      accessories: [
        accessory('sub-keep', 'Shared Title [WEBDL-2160p].eng.srt', 'subtitle', '.eng.srt'),
        accessory('poster-keep', 'Shared Title [WEBDL-2160p]-poster.jpg', 'poster', '-poster.jpg'),
        accessory('nfo-keep', 'Shared Title [WEBDL-2160p].nfo', 'nfo', '.nfo'),
      ],
    },
    {
      id: 'remove-0',
      name: 'Shared Title [BluRay-1080p].mkv',
      kind: 'video',
      path: '/library/Studio/Shared Title/Shared Title [BluRay-1080p].mkv',
      metadata_label: '1920x1080',
      size_label: '2 GB',
      accessories: [
        accessory('sub-other', 'Shared Title [BluRay-1080p].eng.srt', 'subtitle', '.eng.srt'),
        accessory('bg-other', 'Shared Title [BluRay-1080p]-background.jpg', 'background', '-background.jpg'),
        accessory('nfo-other', 'Shared Title [BluRay-1080p].nfo', 'nfo', '.nfo'),
      ],
    },
  ],
  folder_files: [],
  slots: [
    {
      slot_key: 'subtitle:.eng.srt', role: 'subtitle', suffix: '.eng.srt',
      label: 'Subtitle (.eng.srt)', candidate_count: 2,
      candidate_file_ids: ['sub-keep', 'sub-other'],
      winner_file_id: 'sub-other', winner_video_id: 'remove-0',
      destination_path: '/library/Studio/Shared Title/Shared Title [WEBDL-2160p].eng.srt',
      loser_file_ids: ['sub-keep'],
      reason: '98% coverage of the keeper’s runtime',
      flags: [{kind: 'runtime_mismatch', label: 'Subtitle runs past the keeper’s runtime'}],
      needs_review: true, identical: false, borrowed: true,
    },
    {
      slot_key: 'background:-background.jpg', role: 'background', suffix: '-background.jpg',
      label: 'Background (-background.jpg)', candidate_count: 1,
      candidate_file_ids: ['bg-other'],
      winner_file_id: 'bg-other', winner_video_id: 'remove-0',
      destination_path: '/library/Studio/Shared Title/Shared Title [WEBDL-2160p]-background.jpg',
      loser_file_ids: [],
      reason: 'Only copy in the set',
      flags: [], needs_review: false, identical: false, borrowed: true,
    },
    {
      slot_key: 'poster:-poster.jpg', role: 'poster', suffix: '-poster.jpg',
      label: 'Poster (-poster.jpg)', candidate_count: 1,
      candidate_file_ids: ['poster-keep'],
      winner_file_id: 'poster-keep', winner_video_id: 'keep-0',
      destination_path: '', loser_file_ids: [],
      reason: 'Only copy in the set',
      flags: [], needs_review: false, identical: false, borrowed: false,
    },
    {
      slot_key: 'nfo:.nfo', role: 'nfo', suffix: '.nfo',
      label: 'NFO (.nfo)', candidate_count: 2,
      candidate_file_ids: ['nfo-keep', 'nfo-other'],
      winner_file_id: 'nfo-keep', winner_video_id: 'keep-0',
      destination_path: '', loser_file_ids: ['nfo-other'],
      reason: 'Identical in every copy',
      flags: [], needs_review: false, identical: true, borrowed: false,
    },
  ],
};

test.beforeEach(async ({ page }) => {
  await page.route('**/api/maintenance/duplicates/review-draft*', route => route.fulfill({
    status: 200, contentType: 'application/json',
    body: JSON.stringify({review_draft: {
      scan_id: scan.id, saved: false,
      selection: {mode: 'all_eligible', excluded_group_ids: [], group_ids: []},
      groups: {}, saved_group_count: 0, review_required_count: 0,
    }}),
  }));
  await page.route('**/api/maintenance/duplicates/refresh/status*', route => route.fulfill({
    status: 404, contentType: 'application/json', body: JSON.stringify({error: 'none'}),
  }));
  await page.route('**/api/maintenance/duplicates/status*', route => route.fulfill({
    status: 200, contentType: 'application/json', body: JSON.stringify({scan}),
  }));
  await page.route('**/api/maintenance/duplicates/apply/status*', route => route.fulfill({
    status: 200, contentType: 'application/json', body: JSON.stringify({apply: null}),
  }));
  await page.route('**/api/maintenance/duplicates/groups?*', route => route.fulfill({
    status: 200, contentType: 'application/json',
    body: JSON.stringify({
      scan, offset: 0, limit: 25, total: 1, count: 1,
      has_previous: false, has_next: false, next_offset: null, previous_offset: null,
      large_result: false, groups: [summaryGroup],
    }),
  }));
  await page.route('**/api/maintenance/duplicates/groups/group-0?*', route => route.fulfill({
    status: 200, contentType: 'application/json', body: JSON.stringify({group: detailGroup}),
  }));
});

async function openGroup(page) {
  await page.goto('/maintenance#duplicates');
  await page.locator('[data-maint-expand="group-0"]').click();
  await expect(page.locator('[data-duplicate-slots="group-0"]')).toBeVisible();
}

test('the slot table names the winning copy of each file and where it came from', async ({ page }) => {
  await openGroup(page);
  const table = page.locator('.duplicate-slot-table');

  const subtitleRow = table.locator('tr', {hasText: 'Subtitle (.eng.srt)'});
  // The subtitle is taken from the copy being removed, not the keeper.
  await expect(subtitleRow.locator('code')).toContainText('[BluRay-1080p].eng.srt');
  await expect(subtitleRow).toContainText('98% coverage');

  const posterRow = table.locator('tr', {hasText: 'Poster (-poster.jpg)'});
  await expect(posterRow.locator('code')).toContainText('[WEBDL-2160p]-poster.jpg');
});

test('a flagged slot is called out with its reason', async ({ page }) => {
  await openGroup(page);
  const subtitleRow = page.locator('.duplicate-slot-table tr', {hasText: 'Subtitle (.eng.srt)'});

  await expect(subtitleRow).toHaveClass(/is-flagged/);
  await expect(subtitleRow.locator('.duplicate-slot-flag.is-critical'))
    .toContainText('runs past the keeper');
});

test('flagged slots sort above settled ones', async ({ page }) => {
  await openGroup(page);
  const firstRow = page.locator('.duplicate-slot-table tbody tr').first();
  await expect(firstRow).toContainText('Subtitle (.eng.srt)');
});

test('identical files collapse out of the comparison', async ({ page }) => {
  await openGroup(page);
  // The NFO matches in both copies, so it is not a row in the main table.
  await expect(page.locator('.duplicate-slot-table tbody')).not.toContainText('NFO (.nfo)');
  const collapsed = page.locator('.duplicate-slot-identical');
  await expect(collapsed).toContainText('1 file identical in every copy');
  await collapsed.locator('summary').click();
  await expect(collapsed).toContainText('NFO (.nfo)');
});

test('the headline counts files borrowed from other copies', async ({ page }) => {
  await openGroup(page);
  await expect(page.locator('.duplicate-slot-heading')).toContainText(
    '2 files will be taken from another copy'
  );
});

test('the full per-file view is available but collapsed by default', async ({ page }) => {
  await openGroup(page);
  const detail = page.locator('details.duplicate-file-detail');

  await expect(detail).toBeVisible();
  await expect(detail).not.toHaveAttribute('open', '');
  await detail.locator('summary').click();
  // Opening it still exposes the original per-file action controls.
  await expect(detail.locator('.duplicate-compare-pairs')).toBeVisible();
});

test('the per-file view stays open while actions are changed', async ({ page }) => {
  // Changing an action re-renders the group; the disclosure must not snap shut
  // underneath the user mid-edit.
  await openGroup(page);
  const detail = page.locator('details.duplicate-file-detail');
  await detail.locator('summary').click();
  await expect(detail).toHaveAttribute('open', '');

  await page.locator('[data-maint-operation="sub-other"]').selectOption('keep');
  await expect(detail).toHaveAttribute('open', '');
  await expect(detail.locator('.duplicate-compare-pairs')).toBeVisible();

  await page.locator('[data-maint-operation="sub-other"]').selectOption('cleanup');
  await expect(detail).toHaveAttribute('open', '');
});
