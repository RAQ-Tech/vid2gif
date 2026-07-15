(function () {
  const config = window.vid2gifMaintenanceConfig || {};
  const maintenanceTabHashes = ['overview', 'emby-operations', 'posters', 'duplicates', 'video-previews', 'subtitles', 'actor-images'];
  const PAGE_SIZE_OPTIONS = [5, 10, 25, 50, 100];
  const PAGE_SIZE_DEFAULT = 10;
  let maintenanceFolderPicker = null;
  let previewFolderPicker = null;
  let subtitleFolderPicker = null;
  let actorFolderPicker = null;
  let posterFolderPicker = null;
  const overviewExpandedFolders = new Set();
  let overviewFolderPage = null;
  let overviewPageOffset = 0;
  let overviewPageLimit = PAGE_SIZE_DEFAULT;
  let overviewSearchTimer = null;
  let overviewPollTimer = null;
  const groupState = new Map();
  const groupSummaries = new Map();
  let currentScan = null;
  let currentPlan = null;
  let currentApply = null;
  let currentRestorePlan = null;
  let currentGroupsPage = null;
  let groupPageOffset = 0;
  let groupPageLimit = PAGE_SIZE_DEFAULT;
  let duplicateReviewFilter = 'all';
  let pollTimer = null;
  let applyPollTimer = null;
  const DUPLICATE_SELECTION_STORAGE_KEY = 'vid2gif_duplicate_cleanup_selection_v1';
  const DUPLICATE_PAGE_SIZE_STORAGE_KEY = 'vid2gif_duplicate_page_size';
  let duplicateSelection = {
    scanId: '',
    mode: 'all_eligible',
    excluded: new Set(),
    selected: new Set(),
    total: 0,
    reclaimableById: new Map(),
    actionCountsById: new Map()
  };
  let previewScan = null;
  let previewPollTimer = null;
  let previewItemsPage = null;
  let previewPageOffset = 0;
  let previewPageLimit = PAGE_SIZE_DEFAULT;
  let previewSort = {column: 'video', direction: 'asc'};
  let previewLastPath = config.previewScanPath || config.libRoot || '/library';
  const PREVIEW_SELECTION_STORAGE_KEY = 'vid2gif_preview_generation_selection_v1';
  const PREVIEW_PAGE_SIZE_STORAGE_KEY = 'vid2gif_preview_page_size';
  const OVERVIEW_PAGE_SIZE_STORAGE_KEY = 'vid2gif_overview_page_size';
  const QUALITY_PAGE_SIZE_STORAGE_KEY = 'vid2gif_quality_page_size';
  const SUBTITLE_PAGE_SIZE_STORAGE_KEY = 'vid2gif_subtitle_page_size';
  const ACTOR_PAGE_SIZE_STORAGE_KEY = 'vid2gif_actor_page_size';
  const POSTER_PAGE_SIZE_STORAGE_KEY = 'vid2gif_poster_page_size';
  let previewSelection = {
    scanId: '',
    mode: 'all_eligible',
    excluded: new Set(),
    includedHeld: new Set(),
    selected: new Set(),
    missingTotal: 0,
    heldTotal: 0
  };
  let previewGenerationPlan = null;
  let previewGenerationRun = null;
  let previewGenerationPollTimer = null;
  let qualityScan = null;
  let qualityPollTimer = null;
  let qualityItemsPage = null;
  let qualityPageOffset = 0;
  let qualityPageLimit = PAGE_SIZE_DEFAULT;
  let qualitySort = {column: 'bif', direction: 'asc'};
  let qualityPlan = null;
  let qualityApply = null;
  let qualityApplyPollTimer = null;
  const qualitySelectedStatuses = new Set(['bad', 'warning']);
  const qualityExcludedItems = new Set();
  const qualityIncludedItems = new Set();
  let subtitleScan = null;
  let subtitlePollTimer = null;
  let subtitleItemsPage = null;
  let subtitlePageOffset = 0;
  let subtitlePageLimit = PAGE_SIZE_DEFAULT;
  let subtitleSort = {column: 'video', direction: 'asc'};
  let subtitleSearchTimer = null;
  const SUBTITLE_SELECTION_STORAGE_KEY = 'vid2gif_subtitle_selection_v1';
  let subtitleSelection = {
    scanId: '', mode: 'all_eligible', excluded: new Set(), selected: new Set(), total: 0,
  };
  let subtitlePlan = null;
  let subtitleApply = null;
  let subtitleApplyPollTimer = null;
  let actorScan = null;
  let actorPollTimer = null;
  let actorItemsPage = null;
  let actorPageOffset = 0;
  let actorPageLimit = PAGE_SIZE_DEFAULT;
  let actorSort = {column: 'actor', direction: 'asc'};
  let actorPlan = null;
  let actorApply = null;
  let actorApplyPollTimer = null;
  const ACTOR_SELECTION_STORAGE_KEY = 'vid2gif_actor_selection_v1';
  let actorSelection = {
    scanId: '', mode: 'all_eligible', excluded: new Set(), selected: new Set(), total: 0,
  };
  let posterPollTimer = null;
  let posterSettingsLoaded = false;
  let posterSettingsPending = 0;
  const posterSettingsFailures = new Set();
  let posterSettingsSaveChain = Promise.resolve();
  const posterSettingsDirty = new Set();
  const posterSettingGenerations = new Map();
  const posterSettingInputTimers = new Map();
  let posterScan = null;
  let posterItemsPage = null;
  let posterPageOffset = 0;
  let posterPageLimit = PAGE_SIZE_DEFAULT;
  let posterSearchTimer = null;
  let posterSort = {column: 'background', direction: 'asc'};
  let posterPlan = null;
  let posterApply = null;
  const POSTER_SELECTION_STORAGE_KEY = 'vid2gif_poster_selection_v1';
  let posterSelection = {
    scanId: '', mode: 'all_eligible', excluded: new Set(), selected: new Set(), total: 0,
  };
  const groupDetailGenerations = new Map();
  let maintenanceFreshnessTimer = null;
  let embyOperationsTimer = null;
  let embyOperationsRunning = 0;

  function byId(id) {
    return document.getElementById(id);
  }

  function rememberScanSource(path, pageKey = '') {
    try {
      localStorage.setItem('vid2gif_maintenance_scan_source', path);
      if (pageKey) localStorage.setItem(pageKey, path);
    } catch (_e) {}
  }

  function escapeHtml(value) {
    return String(value ?? '').replace(/[&<>"']/g, ch => ({
      '&': '&amp;',
      '<': '&lt;',
      '>': '&gt;',
      '"': '&quot;',
      "'": '&#39;'
    }[ch]));
  }

  function escapeSelector(value) {
    if (window.CSS && typeof window.CSS.escape === 'function') {
      return window.CSS.escape(String(value));
    }
    return String(value).replace(/["\\]/g, '\\$&');
  }

  function formatSize(bytes) {
    let value = Number(bytes) || 0;
    const units = ['B', 'KB', 'MB', 'GB', 'TB'];
    for (let i = 0; i < units.length; i += 1) {
      if (value < 1024 || i === units.length - 1) {
        return i === 0 ? `${Math.round(value)} ${units[i]}` : `${value.toFixed(1)} ${units[i]}`;
      }
      value /= 1024;
    }
    return '0 B';
  }

  const CHANGE_PREVIEW_LIMIT = 50;

  function operationCounts(files) {
    return (files || []).reduce((counts, file) => {
      const operation = String(file.operation || 'change').toLowerCase();
      counts[operation] = (counts[operation] || 0) + 1;
      return counts;
    }, {});
  }

  function operationSummary(files) {
    const counts = operationCounts(files);
    return Object.entries(counts)
      .map(([operation, count]) => `${count} ${operation}`)
      .join(', ') || 'No changes';
  }

  function renderChangePreview(options) {
    const files = options.files || [];
    const visible = files.slice(0, CHANGE_PREVIEW_LIMIT);
    const metrics = (options.metrics || []).map(metric =>
      `<div class="change-preview-metric">` +
        `<span class="metric-label">${escapeHtml(metric.label)}</span>` +
        `<strong>${escapeHtml(metric.value)}</strong>` +
        `${metric.detail ? `<span class="text-muted small">${escapeHtml(metric.detail)}</span>` : ''}` +
      `</div>`
    ).join('');
    const rows = visible.map(file => {
      const change = options.changeForFile(file);
      const operation = String(change.operation || 'change').toLowerCase();
      const operationClass = ['delete', 'move', 'rename', 'import'].includes(operation) ? operation : '';
      const sourceHtml = escapeHtml(change.source || '');
      return `<div class="change-preview-row">` +
        `<div><span class="change-preview-operation ${operationClass}">${escapeHtml(change.operationLabel || operation)}</span></div>` +
        `<div class="change-preview-paths">` +
          `<code title="${escapeHtml(change.source || '')}">${sourceHtml}</code>` +
          `${change.target ? `<div class="change-preview-target"><i class="bi bi-arrow-right" aria-hidden="true"></i> <code title="${escapeHtml(change.target)}">${escapeHtml(change.target)}</code></div>` : ''}` +
          `${change.detail ? `<div class="text-muted small">${escapeHtml(change.detail)}</div>` : ''}` +
        `</div>` +
      `</div>`;
    }).join('');
    const omitted = Math.max(0, files.length - visible.length);
    const list = rows
      ? `<details class="change-preview-details"${files.length <= 8 ? ' open' : ''}>` +
          `<summary>File changes (${escapeHtml(files.length)})</summary>` +
          `<div class="change-preview-list">${rows}</div>` +
          `${omitted ? `<div class="text-muted small mt-2">${escapeHtml(omitted)} additional changes are included in this plan.</div>` : ''}` +
        `</details>`
      : '<div class="text-muted mt-2">No files selected.</div>';
    return `<div class="maintenance-change-preview">` +
      `<div class="panel-subheading"><i class="bi bi-list-check" aria-hidden="true"></i><span>${escapeHtml(options.title)}</span></div>` +
      `<div class="change-preview-metrics">${metrics}</div>` +
      `${options.note ? `<div class="scan-estimate mt-3"><i class="bi bi-info-circle" aria-hidden="true"></i><div>${escapeHtml(options.note)}</div></div>` : ''}` +
      `${list}` +
    `</div>`;
  }

  function clampPercent(value) {
    const number = Number(value);
    if (!Number.isFinite(number)) return 0;
    return Math.max(0, Math.min(100, Math.round(number)));
  }

  function coveragePercent(count, videoCount) {
    const videos = Number(videoCount || 0);
    if (!videos) return 0;
    return clampPercent((Number(count || 0) / videos) * 100);
  }

  function overviewStateLabel(scan) {
    if (scan?.active || ['queued', 'running', 'cancelling'].includes(scan?.status || '')) return 'Running';
    if (scan?.status === 'cached') return 'Cached';
    if (scan?.status === 'success') return 'Complete';
    if (scan?.status === 'failed') return 'Failed';
    return 'Not scanned';
  }

  function embyCoverageDetail(scan) {
    const mapping = scan?.emby_mapping;
    if (!mapping) return '';
    if (['not_configured', 'unavailable', 'stale', 'not_checked'].includes(mapping.status)) {
      return mapping.message || 'Emby item IDs are unavailable.';
    }
    return `Emby IDs: ${mapping.matched_count || 0} mapped, ${mapping.unmatched_count || 0} unmatched, ${mapping.ambiguous_count || 0} ambiguous.`;
  }

  function withEmbyCoverage(detail, scan) {
    return [detail, embyCoverageDetail(scan)].filter(Boolean).join(' ');
  }

  function embySyncFrom(value) {
    return value?.emby_sync || value?.result?.emby_sync || value?.result?.emby || null;
  }

  function embySyncText(sync) {
    if (!sync) return '';
    if (sync.status === 'disabled') return 'Automatic Emby synchronization is disabled.';
    if (sync.status === 'not_configured') return 'Emby is not configured; targeted synchronization was skipped.';
    if (sync.status === 'success') return `${sync.succeeded_count || 0} targeted Emby change(s) accepted.`;
    return `${sync.succeeded_count || 0} Emby change(s) accepted; ${(sync.failed_count || 0) + (sync.unresolved_count || 0)} need attention.`;
  }

  async function retryEmbySync(syncId, notice) {
    const button = notice.querySelector('button');
    if (button) button.disabled = true;
    try {
      const response = await fetch(`/api/emby/sync/${encodeURIComponent(syncId)}/retry`, {method: 'POST'});
      const data = await readJsonResponse(response);
      if (!response.ok) throw new Error(data.error || 'Retry could not start');
      notice.firstChild.textContent = 'Retrying targeted Emby synchronization… ';
      const poll = async () => {
        const statusResponse = await fetch(`/api/emby/sync/${encodeURIComponent(syncId)}`);
        const statusData = await readJsonResponse(statusResponse);
        if (!statusResponse.ok) throw new Error(statusData.error || 'Retry status unavailable');
        const sync = statusData.emby_sync || {};
        if (['queued', 'running'].includes(sync.status)) {
          setTimeout(() => poll().catch(error => { notice.firstChild.textContent = error.message; }), 750);
          return;
        }
        notice.firstChild.textContent = `${embySyncText(sync)} `;
        if (button) {
          button.disabled = !sync.retryable;
          button.classList.toggle('d-none', !sync.retryable);
        }
      };
      setTimeout(() => poll().catch(error => { notice.firstChild.textContent = error.message; }), 500);
    } catch (error) {
      notice.firstChild.textContent = `${error.message || 'Retry failed'} `;
      if (button) button.disabled = false;
    }
  }

  function appendEmbySyncNotice(detailId, sync) {
    const target = byId(detailId);
    if (!target || !sync) return;
    const existing = target.querySelector('.emby-sync-notice');
    if (existing) existing.remove();
    const notice = document.createElement('div');
    notice.className = `emby-sync-notice mt-2 ${['partial', 'failed', 'not_configured'].includes(sync.status) ? 'text-warning' : ''}`;
    notice.append(document.createTextNode(`${embySyncText(sync)} `));
    if (sync.retryable && sync.id) {
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'btn btn-outline-secondary btn-sm ms-1';
      button.textContent = 'Retry Emby sync';
      button.addEventListener('click', () => retryEmbySync(sync.id, notice));
      notice.append(button);
    }
    target.append(notice);
  }

  function notificationFrom(run) {
    return run?.emby_notification || run?.result?.emby_notification || null;
  }

  function appendEmbyNotificationNotice(detailId, notification) {
    const target = byId(detailId);
    if (!target || !notification || ['skipped', 'disabled'].includes(notification.status)) return;
    target.querySelector('.emby-notification-notice')?.remove();
    const notice = document.createElement('div');
    notice.className = `emby-notification-notice mt-2 ${['failed', 'not_configured'].includes(notification.status) ? 'text-warning' : ''}`;
    notice.textContent = notification.message || 'Emby administrator notification status is unavailable.';
    target.append(notice);
  }

  function playbackGuardText(playback) {
    if (!playback) return '';
    if (playback.status === 'disabled') return 'Playback protection is disabled.';
    if (playback.status === 'not_configured') return 'Playback protection is inactive because Emby is not configured.';
    if (playback.status === 'unavailable') return `${playback.unverified_count || playback.target_count || 0} target(s) could not be verified and will be deferred.`;
    if (playback.active_count) return `${playback.active_count} playback target(s) are active and will be deferred.`;
    return 'No selected targets are actively playing in Emby.';
  }

  function setOverviewProgress(scan) {
    const pct = clampPercent(scan?.progress_percent || 0);
    const state = byId('overviewScanState');
    const label = byId('overviewProgressLabel');
    const percent = byId('overviewProgressPercent');
    const bar = byId('overviewProgressBar');
    const videos = byId('overviewVideoCount');
    const folders = byId('overviewFolderCount');
    const refresh = byId('overviewRefreshButton');
    if (state) state.textContent = overviewStateLabel(scan);
    if (label) label.textContent = scan?.progress_label || 'Run a library stat refresh';
    if (percent) percent.textContent = window.vid2gifProgress.valueLabel(scan);
    window.vid2gifProgress.apply(bar, scan || {progress_percent: pct});
    if (videos) videos.textContent = String(scan?.video_count || 0);
    if (folders) folders.textContent = String(scan?.folder_count || 0);
    if (refresh) refresh.disabled = Boolean(scan?.active);
  }

  function overviewLibraryBar(label, value) {
    const pct = clampPercent(value);
    return `
      <div class="dashboard-library-bar">
        <span>${escapeHtml(label)}</span>
        <div class="progress progress-thin" role="progressbar" aria-label="${escapeHtml(label)} coverage" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${pct}">
          <div class="progress-bar" style="width: ${pct}%"></div>
        </div>
        <strong>${pct}%</strong>
      </div>
    `;
  }

  function renderOverviewRoot(scan) {
    const root = scan?.root || {};
    const videoCount = root.video_count || scan?.video_count || 0;
    const name = root.name || 'Library';
    if (byId('overviewRootName')) byId('overviewRootName').textContent = name;
    if (byId('overviewRootSummary')) byId('overviewRootSummary').textContent = `${videoCount} videos, ${root.video_size_label || scan?.video_size_label || '0 B'}`;
    if (byId('overviewLastScan')) byId('overviewLastScan').textContent = scan?.finished_at ? `Last scan: ${scan.finished_at}` : 'Last scan: never';
    if (byId('overviewRootPath')) byId('overviewRootPath').textContent = root.path || config.libRoot || '/library';
    const container = byId('overviewRootDetails');
    if (!container) return;
    if (!root.path && !scan?.finished_at && !scan?.active) {
      container.innerHTML = '<div class="text-muted text-center py-4">Run a library stat refresh to populate this view.</div>';
      return;
    }
    container.innerHTML = `
      <article class="dashboard-library-row">
        <div class="dashboard-library-main">
          <div>
            <h3>${escapeHtml(name)}</h3>
            <code>${escapeHtml(root.path || config.libRoot || '/library')}</code>
          </div>
          <strong>${escapeHtml(videoCount)} videos</strong>
        </div>
        <div class="dashboard-library-metrics">
          <span>${escapeHtml(root.video_size_label || scan?.video_size_label || '0 B')}</span>
          <span>${escapeHtml(scan?.folder_count || 0)} direct folders</span>
          <span>${escapeHtml(root.file_count || 0)} files</span>
          <span>${escapeHtml(root.nfo_count || 0)} NFO</span>
          <span>${escapeHtml(root.bif_count || 0)} BIF</span>
          <span>${escapeHtml(root.poster_count || 0)} posters</span>
          <span>${escapeHtml(root.background_count || 0)} backgrounds</span>
        </div>
        <div class="dashboard-library-bars">
          ${overviewLibraryBar('Subtitles', coveragePercent(root.subtitle_count, videoCount))}
          ${overviewLibraryBar('Posters', coveragePercent(root.poster_count, videoCount))}
          ${overviewLibraryBar('Previews', coveragePercent(root.bif_count, videoCount))}
          ${overviewLibraryBar('Actor images', coveragePercent(root.actor_image_count, videoCount))}
        </div>
      </article>
    `;
  }

  function overviewRangeText(page) {
    const total = Number(page?.total || 0);
    if (!total) return '0 of 0';
    const start = Number(page.offset || 0) + 1;
    const end = Math.min(total, Number(page.offset || 0) + Number(page.count || 0));
    return `${start}-${end} of ${total}`;
  }

  function renderOverviewFolders(page) {
    overviewFolderPage = page || overviewFolderPage;
    const container = byId('overviewFolders');
    if (!container) return;
    const folders = overviewFolderPage?.folders || [];
    const pager = `
      <div class="maintenance-pager">
        <div class="text-muted small">${escapeHtml(overviewRangeText(overviewFolderPage))}</div>
        <div class="toolbar-row mb-0">
          <button class="btn btn-outline-secondary btn-sm" type="button" data-overview-page="prev"${overviewFolderPage?.has_previous ? '' : ' disabled'}>
            <i class="bi bi-chevron-left" aria-hidden="true"></i>
            <span>Previous</span>
          </button>
          <button class="btn btn-outline-secondary btn-sm" type="button" data-overview-page="next"${overviewFolderPage?.has_next ? '' : ' disabled'}>
            <span>Next</span>
            <i class="bi bi-chevron-right" aria-hidden="true"></i>
          </button>
        </div>
      </div>
    `;
    if (!folders.length) {
      container.innerHTML = `${pager}<div class="text-muted text-center py-4">No direct subfolders match this view.</div>`;
      return;
    }
    const rows = folders.map(item => {
      const rowId = item.path || item.name || '';
      const expanded = overviewExpandedFolders.has(rowId);
      const videoCount = item.video_count || 0;
      const detail = expanded ? `
        <div class="dashboard-library-bars mt-3">
          ${overviewLibraryBar('Subtitles', coveragePercent(item.subtitle_count, videoCount))}
          ${overviewLibraryBar('Posters', coveragePercent(item.poster_count, videoCount))}
          ${overviewLibraryBar('Previews', coveragePercent(item.bif_count, videoCount))}
          ${overviewLibraryBar('Actor images', coveragePercent(item.actor_image_count, videoCount))}
        </div>
        <div class="dashboard-library-metrics mt-3">
          <span>${escapeHtml(item.file_count || 0)} files</span>
          <span>${escapeHtml(item.subtitle_count || 0)} subtitles</span>
          <span>${escapeHtml(item.nfo_count || 0)} NFO</span>
          <span>${escapeHtml(item.bif_count || 0)} BIF</span>
          <span>${escapeHtml(item.poster_count || 0)} posters</span>
          <span>${escapeHtml(item.background_count || 0)} backgrounds</span>
          <span>${escapeHtml(item.actor_image_count || 0)} actor images</span>
          <span>${escapeHtml(item.other_sidecar_count || 0)} other sidecars</span>
        </div>
      ` : '';
      return `
        <article class="dashboard-library-row">
          <div class="dashboard-library-main">
            <div>
              <h3>${escapeHtml(item.name || 'Folder')}</h3>
              <code>${escapeHtml(item.path || '')}</code>
            </div>
            <div class="toolbar-row mb-0">
              <strong>${escapeHtml(videoCount)} videos</strong>
              <button class="btn btn-outline-secondary btn-sm" type="button" data-overview-folder-toggle="${escapeHtml(rowId)}" aria-expanded="${expanded ? 'true' : 'false'}">
                <i class="bi ${expanded ? 'bi-chevron-up' : 'bi-chevron-down'}" aria-hidden="true"></i>
                <span>${expanded ? 'Hide Details' : 'Show Details'}</span>
              </button>
            </div>
          </div>
          <div class="dashboard-library-metrics">
            <span>${escapeHtml(item.video_size_label || '0 B')}</span>
            <span>${escapeHtml(item.file_count || 0)} files</span>
            <span>${escapeHtml(item.bif_count || 0)} BIF</span>
            <span>${escapeHtml(item.poster_count || 0)} posters</span>
          </div>
          ${detail}
        </article>
      `;
    }).join('');
    container.innerHTML = `${pager}<div class="dashboard-libraries">${rows}</div>${pager}`;
  }

  async function refreshOverviewStatus() {
    try {
      const res = await fetch('/api/dashboard/library-scan/status');
      const data = await readJsonResponse(res);
      setOverviewProgress(data.scan || {});
      renderOverviewRoot(data.scan || {});
      if (data.scan?.active) {
        clearTimeout(overviewPollTimer);
        overviewPollTimer = setTimeout(refreshOverviewStatus, 1200);
      } else if (overviewFolderPage && byId('overviewFolderInventory')?.classList.contains('show')) {
        loadOverviewFolders(overviewPageOffset);
      }
    } catch (_e) {
      setOverviewProgress({status: 'failed', progress_label: 'Library inventory status could not load'});
    }
  }

  async function loadOverviewFolders(offset) {
    overviewPageOffset = Math.max(0, Number(offset || 0));
    const params = new URLSearchParams({
      offset: String(overviewPageOffset),
      limit: String(overviewPageLimit),
      q: byId('overviewSearch')?.value || '',
      sort: byId('overviewSort')?.value || 'name',
      direction: byId('overviewDirection')?.value || 'asc'
    });
    try {
      const res = await fetch(`/api/dashboard/library-scan/folders?${params.toString()}`);
      const data = await readJsonResponse(res);
      setOverviewProgress(data.scan || {});
      renderOverviewRoot(data.scan || {});
      renderOverviewFolders(data);
    } catch (_e) {
      const container = byId('overviewFolders');
      if (container) container.innerHTML = '<div class="text-danger text-center py-4">Folder inventory could not load.</div>';
    }
  }

  async function startOverviewScan() {
    const button = byId('overviewRefreshButton');
    if (button) button.disabled = true;
    try {
      const res = await fetch('/api/dashboard/library-scan', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({path: config.libRoot || '/library'})
      });
      const data = await readJsonResponse(res);
      if (!res.ok) {
        setOverviewProgress({status: 'failed', progress_label: data.error || 'Library stat refresh could not start'});
        return;
      }
      setOverviewProgress(data.scan || {});
      renderOverviewRoot(data.scan || {});
      clearTimeout(overviewPollTimer);
      overviewPollTimer = setTimeout(refreshOverviewStatus, 600);
    } catch (_e) {
      setOverviewProgress({status: 'failed', progress_label: 'Library stat refresh could not start'});
    } finally {
      if (button) button.disabled = false;
    }
  }

  function setMessage(title, detail) {
    const titleEl = byId('maintenanceMessageTitle');
    const detailEl = byId('maintenanceMessageDetail');
    if (titleEl) titleEl.textContent = title || '';
    if (detailEl) detailEl.textContent = detail || '';
  }

  async function readJsonResponse(res) {
    const text = await res.text();
    if (!text) return {};
    try {
      return JSON.parse(text);
    } catch (e) {
      const status = res?.status ? ` ${res.status}` : '';
      const label = res?.statusText ? ` ${res.statusText}` : '';
      throw new Error(`Server returned${status}${label}`.trim());
    }
  }

  function setProgress(scan) {
    const state = byId('maintenanceScanState');
    const label = byId('maintenanceProgressLabel');
    const percent = byId('maintenanceProgressPercent');
    const bar = byId('maintenanceProgressBar');
    const groups = byId('maintenanceGroupCount');
    const pct = Math.max(0, Math.min(100, Math.round(Number(scan?.progress_percent || 0))));
    if (state) state.textContent = scan?.status ? scan.status : 'Idle';
    if (label) label.textContent = scan?.progress_label || 'Choose a folder';
    if (percent) percent.textContent = window.vid2gifProgress.valueLabel(scan);
    window.vid2gifProgress.apply(bar, scan || {progress_percent: pct});
    if (groups) groups.textContent = String(scan?.duplicate_group_count || 0);
    const active = Boolean(scan?.active || ['queued', 'running', 'cancelling'].includes(scan?.status || ''));
    const scanButton = byId('maintenanceScanButton');
    const cancelButton = byId('maintenanceCancelScanButton');
    if (scanButton) scanButton.disabled = active;
    if (cancelButton) cancelButton.disabled = !active || scan?.status === 'cancelling';
  }

  function invalidatePlan() {
    currentPlan = null;
    const apply = byId('maintenanceApplyButton');
    if (apply) apply.disabled = true;
    const summary = byId('maintenancePlanSummary');
    if (summary) summary.innerHTML = '';
  }

  function fileParentVideo(group, file) {
    if (file.kind === 'video') return file;
    return (group.videos || []).find(video => video.id === file.parent_video_id) || null;
  }

  function fileDetails(group, file, state) {
    if (file.kind === 'video') {
      return file.metadata_label || 'Video';
    }
    const parent = fileParentVideo(group, file);
    const parts = [file.role ? `${file.role[0].toUpperCase()}${file.role.slice(1)} sidecar` : 'Sidecar'];
    if (parent?.name) parts.push(`for ${parent.name}`);
    if (parent?.id === state.keepId) parts.push('attached to keeper');
    return parts.join(' - ');
  }

  function defaultSelected(file) {
    return file.default_selected !== false && file.default_operation !== 'keep';
  }

  function defaultFileAction(file) {
    if (!defaultSelected(file)) return 'keep';
    return file.default_operation === 'rename' ? 'rename' : 'cleanup';
  }

  function effectiveFileAction(file, state) {
    const override = state.fileOperations?.get(file.id);
    if (override === 'keep') return 'keep';
    if (override === 'rename') return 'rename';
    if (['cleanup', 'move', 'delete'].includes(override || '')) return 'cleanup';
    return state.includedFileIds.has(file.id) ? defaultFileAction(file) : 'keep';
  }

  function cleanupActionLabel() {
    return byId('maintenanceAction')?.value === 'delete' ? 'Delete permanently' : 'Move to quarantine';
  }

  function setFileAction(state, fileId, action) {
    if (!state || !fileId) return;
    if (action === 'keep') {
      state.includedFileIds.delete(fileId);
      state.fileOperations.set(fileId, 'keep');
    } else {
      state.includedFileIds.add(fileId);
      state.fileOperations.set(fileId, action === 'rename' ? 'rename' : 'cleanup');
    }
  }

  function fileActionOptions(group, file, state, locked = false) {
    if (locked) return '';
    const selected = effectiveFileAction(file, state);
    const parent = fileParentVideo(group, file);
    const canPreserveWithKeeper = file.kind !== 'video' && file.renameable && parent?.id !== state.keepId;
    const options = canPreserveWithKeeper
      ? [
          ['rename', 'Keep with selected video (rename)'],
          ['cleanup', cleanupActionLabel()],
          ['keep', 'Leave unchanged']
        ]
      : [
          ['keep', file.kind === 'video' ? 'Keep in library' : 'Keep with selected video'],
          ['cleanup', cleanupActionLabel()]
        ];
    return `<select class="form-select form-select-sm duplicate-action-select duplicate-action-select-${escapeHtml(selected)}" data-maint-operation="${escapeHtml(file.id)}" data-maint-group="${escapeHtml(group.id)}" aria-label="Planned action for ${escapeHtml(file.name)}"${state.enabled ? '' : ' disabled'}>` +
      options.map(([value, label]) => `<option value="${escapeHtml(value)}"${selected === value ? ' selected' : ''}>${escapeHtml(label)}</option>`).join('') +
      `</select>`;
  }

  function fileActionIndicator(action, locked = false) {
    if (locked) {
      return '<span class="badge duplicate-action-badge duplicate-action-indicator-keep">Keep selected video</span>';
    }
    if (action === 'rename') {
      return '<span class="badge duplicate-action-badge duplicate-action-indicator-rename">Keep + rename</span>';
    }
    if (action === 'cleanup') {
      const deleting = byId('maintenanceAction')?.value === 'delete';
      return `<span class="badge duplicate-action-badge ${deleting ? 'duplicate-action-indicator-delete' : 'duplicate-action-indicator-cleanup'}">${deleting ? 'Delete' : 'Quarantine'}</span>`;
    }
    return '<span class="badge duplicate-action-badge duplicate-action-indicator-keep">Keep</span>';
  }

  function fileActionHelp(file, state, locked = false) {
    if (locked) return 'This is the selected keeper video.';
    const action = effectiveFileAction(file, state);
    if (action === 'rename') return 'Preserves this sidecar and renames it to the selected video stem.';
    if (action === 'cleanup') {
      return byId('maintenanceAction')?.value === 'delete'
        ? 'This file will be permanently deleted.'
        : 'This file will be moved to the configured quarantine folder.';
    }
    return file.kind !== 'video' && file.renameable
      ? 'This sidecar stays under its current filename and may become orphaned.'
      : 'This file stays where it is.';
  }

  function groupCandidateFiles(group, state) {
    const keepId = state.keepId || group.recommended_keep_id;
    const files = [];
    (group.videos || []).forEach(video => {
      if (video.id !== keepId) files.push(video);
      (video.accessories || []).forEach(accessory => files.push(accessory));
    });
    return files;
  }

  function groupDisplayFiles(group) {
    const files = [];
    (group.videos || []).forEach(video => {
      files.push(video);
      (video.accessories || []).forEach(accessory => files.push(accessory));
    });
    return files;
  }

  function accessoryComparisonKey(file) {
    const equivalenceKey = String(file.equivalence_key || '').trim().toLowerCase();
    if (equivalenceKey) return equivalenceKey;
    const suffix = String(file.suffix || '').trim().toLowerCase();
    if (suffix) return `${file.role || 'accessory'}:${suffix}`;
    return `unmatched:${file.id}`;
  }

  function comparisonRow(file, depth = 0, anchor = null, relation = '') {
    return {file, depth, anchor, relation};
  }

  function groupComparisonRows(group, state) {
    const videos = group.videos || [];
    const keepId = state.keepId || group.recommended_keep_id || '';
    const keeper = videos.find(video => video.id === keepId) || null;
    const rows = [];

    if (keeper) rows.push(comparisonRow(keeper, 0, null, 'keeper'));
    videos
      .filter(video => video.id !== keepId && effectiveFileAction(video, state) === 'cleanup')
      .forEach(video => rows.push(comparisonRow(video, keeper ? 1 : 0, keeper, keeper ? 'matching_cleanup' : 'standalone_cleanup')));
    videos
      .filter(video => video.id !== keepId && effectiveFileAction(video, state) !== 'cleanup')
      .forEach(video => rows.push(comparisonRow(video, 0, null, 'preserved')));

    const accessoryBuckets = new Map();
    const orderedVideos = keeper
      ? [keeper, ...videos.filter(video => video.id !== keepId)]
      : videos;
    orderedVideos.forEach(video => {
      (video.accessories || []).forEach(accessory => {
        const key = accessoryComparisonKey(accessory);
        if (!accessoryBuckets.has(key)) accessoryBuckets.set(key, []);
        accessoryBuckets.get(key).push(accessory);
      });
    });

    accessoryBuckets.forEach(accessories => {
      const preserved = accessories.filter(file => effectiveFileAction(file, state) !== 'cleanup');
      const cleanup = accessories.filter(file => effectiveFileAction(file, state) === 'cleanup');
      const anchor = preserved.find(file => effectiveFileAction(file, state) === 'rename') ||
        preserved.find(file => file.parent_video_id === keepId) ||
        preserved[0] || null;

      if (!anchor) {
        cleanup.forEach(file => rows.push(comparisonRow(file, 0, null, 'standalone_cleanup')));
        return;
      }
      rows.push(comparisonRow(anchor, 0, null, effectiveFileAction(anchor, state) === 'rename' ? 'preserved_rename' : 'preserved'));
      cleanup.forEach(file => rows.push(comparisonRow(file, 1, anchor, 'matching_cleanup')));
      preserved
        .filter(file => file.id !== anchor.id)
        .forEach(file => rows.push(comparisonRow(file, 0, null, 'preserved')));
    });

    return rows;
  }

  function fileIsKeeperVideo(file, state) {
    return file.kind === 'video' && file.id === (state.keepId || '');
  }

  function duplicateSelectionFromStorage(scanId, total) {
    let saved = null;
    try {
      saved = JSON.parse(localStorage.getItem(DUPLICATE_SELECTION_STORAGE_KEY) || 'null');
    } catch (_e) {
      saved = null;
    }
    const sameScan = saved && saved.scanId === scanId;
    const mode = sameScan && saved.mode === 'explicit' ? 'explicit' : 'all_eligible';
    return {
      scanId,
      mode,
      excluded: new Set(sameScan && Array.isArray(saved.excluded) ? saved.excluded : []),
      selected: new Set(sameScan && Array.isArray(saved.selected) ? saved.selected : []),
      total: Number(total || 0),
      reclaimableById: new Map(sameScan && Array.isArray(saved.reclaimableById) ? saved.reclaimableById : []),
      actionCountsById: new Map(sameScan && Array.isArray(saved.actionCountsById) ? saved.actionCountsById : [])
    };
  }

  function ensureDuplicateSelection(scanId, total) {
    if (duplicateSelection.scanId !== scanId) {
      duplicateSelection = duplicateSelectionFromStorage(scanId, total);
      groupState.clear();
      groupSummaries.clear();
      currentGroupsPage = null;
      groupPageOffset = 0;
      invalidatePlan();
    } else {
      duplicateSelection.total = Number(total || duplicateSelection.total || 0);
    }
  }

  function saveDuplicateSelection() {
    try {
      localStorage.setItem(DUPLICATE_SELECTION_STORAGE_KEY, JSON.stringify({
        scanId: duplicateSelection.scanId,
        mode: duplicateSelection.mode,
        excluded: Array.from(duplicateSelection.excluded),
        selected: Array.from(duplicateSelection.selected),
        reclaimableById: Array.from(duplicateSelection.reclaimableById.entries()),
        actionCountsById: Array.from(duplicateSelection.actionCountsById.entries())
      }));
    } catch (_e) {
      // Selection still remains stable for this page session.
    }
  }

  function duplicateGroupIsSelected(groupId) {
    if (!groupId) return false;
    if (duplicateSelection.mode === 'explicit') return duplicateSelection.selected.has(groupId);
    return !duplicateSelection.excluded.has(groupId);
  }

  function duplicateSelectedCount() {
    if (duplicateSelection.mode === 'explicit') return duplicateSelection.selected.size;
    return Math.max(0, duplicateSelection.total - duplicateSelection.excluded.size);
  }

  function duplicateSelectionPayload() {
    if (duplicateSelection.mode === 'explicit') {
      return {mode: 'explicit', group_ids: Array.from(duplicateSelection.selected)};
    }
    return {mode: 'all_eligible', excluded_group_ids: Array.from(duplicateSelection.excluded)};
  }

  function setDuplicateGroupSelected(groupId, selected) {
    if (!groupId) return;
    if (duplicateSelection.mode === 'explicit') {
      if (selected) duplicateSelection.selected.add(groupId);
      else duplicateSelection.selected.delete(groupId);
    } else if (selected) {
      duplicateSelection.excluded.delete(groupId);
    } else {
      duplicateSelection.excluded.add(groupId);
    }
    const state = groupState.get(groupId);
    if (state) state.enabled = selected;
  }

  function renderDuplicateSelectionSummary() {
    const target = byId('duplicateSelectionSummary');
    if (!target) return;
    const selected = duplicateSelectedCount();
    const mode = duplicateSelection.mode === 'all_eligible' ? 'across all result pages' : 'chosen individually';
    target.innerHTML = `<strong>${escapeHtml(selected)} selected</strong> ${escapeHtml(mode)}` +
      '<div class="small text-muted mt-1">Page navigation does not change this selection.</div>';
  }

  function updateDuplicateControls() {
    const selected = duplicateSelectedCount();
    const stale = currentScan?.freshness?.status === 'changed';
    const applyActive = ['queued', 'running'].includes(currentApply?.status || '');
    const planButton = byId('maintenancePlanButton');
    if (planButton) {
      planButton.disabled = !currentScan || currentScan.status !== 'success' || !selected || stale || applyActive;
      const label = planButton.querySelector('span');
      if (label) label.textContent = currentPlan ? 'Refresh plan preview' : 'Preview cleanup plan';
    }
    const master = byId('duplicateSelectAllCheckbox');
    if (master) {
      master.disabled = !duplicateSelection.total || applyActive;
      master.checked = Boolean(duplicateSelection.total && selected === duplicateSelection.total);
      master.indeterminate = selected > 0 && selected < duplicateSelection.total;
    }
    const toggle = byId('duplicateTogglePageButton');
    if (toggle) {
      const visible = currentGroupsPage?.groups || [];
      const allExpanded = Boolean(visible.length && visible.every(group => ensureGroupState(group).expanded));
      toggle.disabled = !visible.length || applyActive;
      const label = toggle.querySelector('span');
      const icon = toggle.querySelector('i');
      if (label) label.textContent = allExpanded ? 'Collapse page' : 'Expand page';
      if (icon) icon.className = `bi ${allExpanded ? 'bi-arrows-collapse' : 'bi-arrows-expand'}`;
    }
    renderDuplicateSelectionSummary();
    renderDuplicateReviewSummary();
  }

  function duplicateSelectionChanged() {
    invalidatePlan();
    saveDuplicateSelection();
    updateDuplicateControls();
    updateSelectedSize();
  }

  function ensureGroupState(group) {
    if (!group?.id) {
      return {
        enabled: duplicateGroupIsSelected(group?.id),
        keepId: '',
        includedFileIds: new Set(),
        fileOperations: new Map(),
        candidateSignature: '',
        dirty: false,
        expanded: false,
        loading: false,
        projectionPending: false,
        appliedKeepId: ''
      };
    }
    if (!groupState.has(group.id)) {
      groupState.set(group.id, {
        enabled: duplicateGroupIsSelected(group.id),
        keepId: group.recommended_keep_id,
        includedFileIds: new Set(),
        fileOperations: new Map(),
        candidateSignature: '',
        dirty: false,
        expanded: false,
        loading: false,
        projectionPending: false,
        appliedKeepId: group.recommended_keep_id
      });
    }
    const state = groupState.get(group.id);
    if (!state.keepId && group.recommended_keep_id) {
      state.keepId = group.recommended_keep_id;
    }
    if (!state.appliedKeepId && group.recommended_keep_id) {
      state.appliedKeepId = group.recommended_keep_id;
    }
    if (!(group.videos || []).length) {
      return state;
    }
    if (state.projectionPending) return state;
    const candidates = groupCandidateFiles(group, state);
    const signature = candidates.map(file => file.id).join('|');
    if (state.candidateSignature !== signature) {
      state.candidateSignature = signature;
      state.includedFileIds = new Set(candidates.filter(defaultSelected).map(file => file.id));
      state.fileOperations = new Map();
    }
    return state;
  }

  function emptyActionCounts() {
    return {keep: 0, cleanup: 0, rename: 0};
  }

  function normalizedActionCounts(counts) {
    return {
      keep: Number(counts?.keep || 0),
      cleanup: Number(counts?.cleanup || 0),
      rename: Number(counts?.rename || 0)
    };
  }

  function addActionCounts(target, source, multiplier = 1) {
    ['keep', 'cleanup', 'rename'].forEach(key => {
      target[key] = Number(target[key] || 0) + (Number(source?.[key] || 0) * multiplier);
    });
    return target;
  }

  function groupReviewStats(group, state) {
    if (!(group.videos || []).length) {
      return {
        counts: normalizedActionCounts(group.default_action_counts),
        cleanupBytes: Number(group.reclaimable_bytes || 0)
      };
    }
    const counts = emptyActionCounts();
    let cleanupBytes = 0;
    groupDisplayFiles(group).forEach(file => {
      const action = fileIsKeeperVideo(file, state) ? 'keep' : effectiveFileAction(file, state);
      counts[action] += 1;
      if (action === 'cleanup') cleanupBytes += Number(file.size_bytes || 0);
    });
    return {counts, cleanupBytes};
  }

  function duplicateReviewTotals() {
    const totals = emptyActionCounts();
    if (duplicateSelection.mode === 'all_eligible') {
      addActionCounts(totals, currentScan?.default_action_counts);
      duplicateSelection.excluded.forEach(groupId => {
        addActionCounts(totals, duplicateSelection.actionCountsById.get(groupId), -1);
      });
    } else {
      duplicateSelection.selected.forEach(groupId => {
        addActionCounts(totals, duplicateSelection.actionCountsById.get(groupId));
      });
    }
    groupState.forEach((state, groupId) => {
      if (!duplicateGroupIsSelected(groupId)) return;
      const group = groupSummaries.get(groupId);
      if (!group || !(group.videos || []).length) return;
      addActionCounts(totals, duplicateSelection.actionCountsById.get(groupId), -1);
      addActionCounts(totals, groupReviewStats(group, state).counts);
    });
    Object.keys(totals).forEach(key => { totals[key] = Math.max(0, totals[key]); });
    return totals;
  }

  function renderDuplicateReviewSummary() {
    const target = byId('duplicateReviewSummary');
    if (!target) return;
    if (!currentScan || currentScan.status !== 'success') {
      target.innerHTML = '<div class="small text-muted">Run a scan to see the proposed keep and cleanup totals.</div>';
      return;
    }
    const totals = duplicateReviewTotals();
    const cleanupLabel = byId('maintenanceAction')?.value === 'delete' ? 'Delete' : 'Quarantine';
    target.innerHTML = `<div class="duplicate-review-stats">` +
      `<span class="duplicate-review-stat"><strong>${escapeHtml(duplicateSelectedCount())}</strong><small>Groups selected</small></span>` +
      `<span class="duplicate-review-stat duplicate-review-keep"><strong>${escapeHtml(totals.keep)}</strong><small>Keep</small></span>` +
      `<span class="duplicate-review-stat duplicate-review-cleanup"><strong>${escapeHtml(totals.cleanup)}</strong><small>${escapeHtml(cleanupLabel)}</small></span>` +
      `<span class="duplicate-review-stat duplicate-review-rename"><strong>${escapeHtml(totals.rename)}</strong><small>Rename sidecars</small></span>` +
      `<span class="duplicate-review-stat duplicate-review-attention"><strong>${escapeHtml(currentScan.review_group_count || 0)}</strong><small>Groups flagged</small></span>` +
      `<span class="duplicate-review-stat duplicate-review-protected" title="Same-title videos with different release evidence or runtimes are never added to cleanup"><strong>${escapeHtml(currentScan.protected_distinct_set_count || 0)}</strong><small>Distinct sets protected</small></span>` +
      `</div><div class="small text-muted mt-2">These are the current choices. You only need to expand groups that are flagged or that you want to override.</div>`;
  }

  function updateSelectedSize() {
    let total = 0;
    if (duplicateSelection.mode === 'all_eligible') {
      total = Number(currentScan?.reclaimable_bytes || 0);
      duplicateSelection.excluded.forEach(groupId => {
        total -= Number(duplicateSelection.reclaimableById.get(groupId) || 0);
      });
    } else {
      duplicateSelection.selected.forEach(groupId => {
        total += Number(duplicateSelection.reclaimableById.get(groupId) || 0);
      });
    }
    groupState.forEach((state, groupId) => {
      if (!duplicateGroupIsSelected(groupId)) return;
      const group = groupSummaries.get(groupId);
      if (!group || !(group.videos || []).length) return;
      total -= Number(duplicateSelection.reclaimableById.get(groupId) || 0);
      total += groupReviewStats(group, state).cleanupBytes;
    });
    total = Math.max(0, total);
    const selected = byId('maintenanceSelectedSize');
    if (selected) {
      selected.textContent = currentPlan?.total_size_label || (duplicateSelectedCount() ? `about ${formatSize(total)}` : '0 B');
      selected.title = currentPlan ? 'Reviewed cleanup size' : 'Estimated from the scan defaults; Review Selection shows the exact plan.';
    }
    renderDuplicateReviewSummary();
  }

  function markGroupDirty(groupId) {
    const state = groupState.get(groupId);
    if (state) state.dirty = true;
    invalidatePlan();
    updateSelectedSize();
  }

  function mergeGroupDetail(group) {
    if (!group?.id) return;
    const existing = groupSummaries.get(group.id) || {};
    groupSummaries.set(group.id, {...existing, ...group});
    if (currentGroupsPage?.groups) {
      currentGroupsPage.groups = currentGroupsPage.groups.map(item =>
        item.id === group.id ? {...item, ...group} : item
      );
    }
    ensureGroupState(groupSummaries.get(group.id));
  }

  function currentPageGroups() {
    return (currentGroupsPage?.groups || []).map(group => groupSummaries.get(group.id) || group);
  }

  function pageRangeText(page) {
    const total = Number(page?.total || 0);
    if (!total) return '0 of 0';
    const start = Number(page.offset || 0) + 1;
    const end = Math.min(total, Number(page.offset || 0) + Number(page.count || 0));
    return `${start}-${end} of ${total}`;
  }

  function renderPager(page) {
    if (!page) return '';
    return `<div class="maintenance-pager">` +
      `<div class="text-muted small">${escapeHtml(pageRangeText(page))}${page.large_result ? ' - large result set' : ''}</div>` +
      `<div class="toolbar-row mb-0">` +
      `<button class="btn btn-outline-secondary btn-sm" type="button" data-maint-page="prev"${page.has_previous ? '' : ' disabled'}>Previous</button>` +
      `<button class="btn btn-outline-secondary btn-sm" type="button" data-maint-page="next"${page.has_next ? '' : ' disabled'}>Next</button>` +
      `</div>` +
      `</div>`;
  }

  function groupOption(video, recommendedId) {
    const label = `${video.name}${video.metadata_label ? ` - ${video.metadata_label}` : ''}${video.size_label ? ` - ${video.size_label}` : ''}${video.copy_marked ? ' - copy-marked name' : ''}`;
    return `<option value="${escapeHtml(video.id)}"${video.id === recommendedId ? ' selected' : ''}>${escapeHtml(label)}</option>`;
  }

  function fileRow(group, row, state) {
    const file = row.file || row;
    const comparisonDepth = Number(row.depth || 0);
    const comparisonAnchor = row.anchor || null;
    const locked = fileIsKeeperVideo(file, state);
    const parent = fileParentVideo(group, file);
    const kind = file.kind === 'video'
      ? 'Video'
      : `${file.role ? `${file.role[0].toUpperCase()}${file.role.slice(1)}` : 'Sidecar'}`;
    const association = file.kind === 'video'
      ? (file.metadata_label || 'Video candidate')
      : `Attached to ${parent?.name || 'video'}`;
    const associationBadge = file.kind !== 'video' && parent?.id === state.keepId
      ? '<span class="badge text-bg-info ms-1">Keeper sidecar</span>'
      : '';
    const action = locked ? 'keep' : effectiveFileAction(file, state);
    const comparisonDetail = comparisonDepth && comparisonAnchor
      ? `<div class="duplicate-match-context" title="Matches ${escapeHtml(comparisonAnchor.name)}">Matches kept item above</div>`
      : '';
    const matchMarker = comparisonDepth
      ? '<span class="duplicate-match-arrow" aria-hidden="true">&#8627;</span>'
      : '';
    const quality = file.subtitle_quality || {};
    const qualityClass = quality.status === 'complete'
      ? 'text-bg-success'
      : (quality.status === 'likely_incomplete' ? 'text-bg-danger' : 'text-bg-warning');
    const qualityDetail = file.role === 'subtitle' && quality.status
      ? `<div class="mt-1"><span class="badge ${qualityClass}">${escapeHtml(quality.status === 'likely_incomplete' ? 'Likely incomplete' : (quality.status === 'complete' ? 'Coverage good' : 'Coverage review'))}</span>` +
        `<span class="small text-muted ms-1">${escapeHtml(quality.coverage_percent != null ? `${quality.coverage_percent}% · ends ${quality.last_timestamp_label || '?'} of ${quality.video_duration_label || '?'} · ${quality.cue_count || 0} cues` : (quality.label || 'Runtime unavailable'))}</span></div>`
      : '';
    return `<tr class="duplicate-file-row duplicate-action-${escapeHtml(action)}${comparisonDepth ? ' duplicate-file-match-child' : ''}" data-duplicate-file-row="${escapeHtml(file.id)}" data-comparison-depth="${comparisonDepth}"${comparisonAnchor ? ` data-comparison-anchor="${escapeHtml(comparisonAnchor.id)}"` : ''}>` +
      `<td data-sort-value="${escapeHtml(kind)}"><span class="fw-semibold">${escapeHtml(kind)}</span>${locked ? '<span class="badge text-bg-success ms-1">Keeper</span>' : ''}</td>` +
      `<td class="duplicate-file-cell" data-sort-value="${escapeHtml(file.name)}">${matchMarker}<code class="duplicate-file-name" title="${escapeHtml(file.path)}">${escapeHtml(file.name)}</code></td>` +
      `<td class="duplicate-file-context" data-sort-value="${escapeHtml(fileDetails(group, file, state))}">${comparisonDetail}${escapeHtml(association)}${associationBadge}${qualityDetail}</td>` +
      `<td data-sort-value="${Number(file.size_bytes || 0)}">${escapeHtml(file.size_label || formatSize(file.size_bytes))}</td>` +
      `<td class="duplicate-file-action" data-sort-value="${escapeHtml(action)}"><div class="duplicate-action-control">${fileActionIndicator(action, locked)}${fileActionOptions(group, file, state, locked)}</div><div class="small text-muted mt-1">${escapeHtml(fileActionHelp(file, state, locked))}</div></td>` +
      `</tr>`;
  }

  async function loadGroupDetails(groupId, keepVideoId = '') {
    if (!currentScan?.id || !groupId) return;
    const state = groupState.get(groupId);
    const generation = (groupDetailGenerations.get(groupId) || 0) + 1;
    groupDetailGenerations.set(groupId, generation);
    if (state) state.loading = true;
    renderGroups();
    try {
      const params = new URLSearchParams({scan_id: currentScan.id});
      if (keepVideoId) params.set('keep_video_id', keepVideoId);
      const res = await fetch(`/api/maintenance/duplicates/groups/${encodeURIComponent(groupId)}?${params.toString()}`);
      const data = await readJsonResponse(res);
      if (!res.ok) {
        if (groupDetailGenerations.get(groupId) === generation) {
          const latest = groupState.get(groupId);
          if (latest?.projectionPending) {
            latest.keepId = latest.appliedKeepId || latest.keepId;
            latest.projectionPending = false;
          }
        }
        setMessage(data.error || 'Group details unavailable', '');
        return;
      }
      if (groupDetailGenerations.get(groupId) !== generation) return;
      const latest = groupState.get(groupId);
      if (latest) {
        latest.projectionPending = false;
        latest.appliedKeepId = keepVideoId || latest.keepId;
        latest.candidateSignature = '';
      }
      mergeGroupDetail(data.group);
    } catch (e) {
      if (groupDetailGenerations.get(groupId) !== generation) return;
      const latest = groupState.get(groupId);
      if (latest?.projectionPending) {
        latest.keepId = latest.appliedKeepId || latest.keepId;
        latest.projectionPending = false;
      }
      setMessage('Group details unavailable', e.message || '');
    } finally {
      if (groupDetailGenerations.get(groupId) !== generation) return;
      const latest = groupState.get(groupId);
      if (latest) latest.loading = false;
      renderGroups();
      updateSelectedSize();
    }
  }

  async function loadGroupsPage(offset = groupPageOffset) {
    if (!currentScan?.id || currentScan.status !== 'success') return;
    const target = byId('maintenanceGroups');
    if (target) target.innerHTML = '<div class="text-muted text-center py-4">Loading duplicate groups...</div>';
    try {
      const res = await fetch(`/api/maintenance/duplicates/groups?scan_id=${encodeURIComponent(currentScan.id)}&offset=${encodeURIComponent(offset)}&limit=${encodeURIComponent(groupPageLimit)}&review=${encodeURIComponent(duplicateReviewFilter)}`);
      const data = await readJsonResponse(res);
      if (!res.ok) {
        setMessage(data.error || 'Duplicate groups unavailable', '');
        return;
      }
      currentGroupsPage = data;
      groupPageOffset = Number(data.offset || 0);
      groupPageLimit = Number(data.limit || groupPageLimit);
      (data.groups || []).forEach(group => {
        const existing = groupSummaries.get(group.id) || {};
        groupSummaries.set(group.id, {...existing, ...group});
        duplicateSelection.reclaimableById.set(group.id, Number(group.reclaimable_bytes || 0));
        duplicateSelection.actionCountsById.set(group.id, normalizedActionCounts(group.default_action_counts));
      });
      saveDuplicateSelection();
      renderGroups();
      updateDuplicateControls();
      updateSelectedSize();
      if (data.large_result) {
        setMessage(
          `${data.total || 0} duplicate groups found`,
          `Large result set loaded ${data.limit || groupPageLimit} groups at a time.`
        );
      }
    } catch (e) {
      setMessage('Duplicate groups unavailable', e.message || '');
    }
  }

  function renderGroupReviewStats(group, state) {
    if (!state.enabled) {
      return '<div class="duplicate-group-review"><span class="badge text-bg-secondary">Excluded from this cleanup run</span></div>';
    }
    const stats = groupReviewStats(group, state).counts;
    const cleanupLabel = byId('maintenanceAction')?.value === 'delete' ? 'Delete' : 'Quarantine';
    return `<div class="duplicate-group-review">` +
      `<span class="badge duplicate-review-keep">Keep ${escapeHtml(stats.keep)}</span>` +
      `<span class="badge duplicate-review-cleanup">${escapeHtml(cleanupLabel)} ${escapeHtml(stats.cleanup)}</span>` +
      `<span class="badge duplicate-review-rename">Rename ${escapeHtml(stats.rename)}</span>` +
      `</div>`;
  }

  function renderGroupReviewFlags(group) {
    const flags = group.review_flags || [];
    if (!flags.length) return '<span class="badge text-bg-success">No quick-review flags</span>';
    return flags.map(flag =>
      `<span class="badge text-bg-warning" title="Review this group before applying">${escapeHtml(flag.label || 'Review recommended')}</span>`
    ).join('');
  }

  function renderGroupSubtitleSignals(group) {
    return (group.subtitle_signals || []).map(signal => {
      const klass = signal.severity === 'success' ? 'text-bg-success' : 'text-bg-warning';
      return `<span class="badge ${klass}" title="Subtitle timestamps were compared with video runtime">${escapeHtml(signal.label || 'Subtitle coverage checked')}</span>`;
    }).join('');
  }

  function renderGroup(group) {
    const state = ensureGroupState(group);
    const expanded = Boolean(state.expanded);
    const hasDetails = Boolean((group.videos || []).length);
    const loading = Boolean(state.loading);
    const displayFiles = hasDetails ? groupComparisonRows(group, state) : [];
    const rows = displayFiles.length
      ? displayFiles.map(row => fileRow(group, row, state)).join('')
      : '<tr><td colspan="5" class="text-muted text-center py-3">No files are available in this group.</td></tr>';
    const keeper = hasDetails
      ? (group.videos || []).find(video => video.id === state.keepId)
      : null;
    const keeperOptions = (group.keeper_options || group.videos || []);
    const selectedKeeper = keeper || keeperOptions.find(video => video.id === state.keepId) || {};
    const recommended = selectedKeeper.name || group.recommended_keep_name || '';
    const detail = expanded
      ? (loading
        ? '<div class="text-muted small mt-3">Loading group details...</div>'
        : (hasDetails
          ? `<div class="maintenance-group-detail">` +
            `<div class="duplicate-group-tools">` +
            `<div class="toolbar-row mb-0">` +
            `<button class="btn btn-outline-secondary btn-sm" type="button" data-maint-group-defaults="${escapeHtml(group.id)}"${state.enabled ? '' : ' disabled'}>Reset suggested actions</button>` +
            `<button class="btn btn-outline-warning btn-sm" type="button" data-maint-group-sidecars="cleanup" data-maint-group="${escapeHtml(group.id)}"${state.enabled ? '' : ' disabled'}>${escapeHtml(cleanupActionLabel())} all sidecars</button>` +
            `<button class="btn btn-outline-secondary btn-sm" type="button" data-maint-group-sidecars="keep" data-maint-group="${escapeHtml(group.id)}"${state.enabled ? '' : ' disabled'}>Keep all sidecars</button>` +
            `</div></div>` +
            `<div class="small text-muted mt-2"><strong>Action meanings:</strong> Keep leaves a file untouched. ${escapeHtml(cleanupActionLabel())} removes it from the library${byId('maintenanceAction')?.value === 'delete' ? ' permanently' : ' and stores it in quarantine'}. Rename preserves a sidecar by changing only its filename to match the kept video.</div>` +
            `<div class="small text-muted mt-1"><strong>Comparison order:</strong> each kept file is followed by its matching cleanup files, shown slightly indented.</div>` +
            `<div class="table-responsive workspace-table-wrap mt-2">` +
            `<table class="table table-hover align-middle workspace-table maintenance-table duplicate-review-table" data-table-id="maintenance-duplicate-files" data-sort-mode="none">` +
            `<thead><tr><th data-column-id="kind">Type</th><th data-column-id="file">Full filename</th><th data-column-id="details">Belongs to / details</th><th data-column-id="size">Size</th><th data-column-id="operation">Planned action</th></tr></thead>` +
            `<tbody>${rows}</tbody>` +
            `</table></div></div>`
          : '<div class="text-muted small mt-3">Open this group to load file details.</div>'))
      : '';
    return `<section class="maintenance-group" data-maint-group-card="${escapeHtml(group.id)}">` +
      `<div class="maintenance-group-heading">` +
      `<div class="form-check">` +
      `<input class="form-check-input" type="checkbox" data-maint-group-enabled="${escapeHtml(group.id)}" id="enabled-${escapeHtml(group.id)}"${state.enabled ? ' checked' : ''}>` +
      `<label class="form-check-label" for="enabled-${escapeHtml(group.id)}">Include group</label>` +
      `</div>` +
      `<div class="maintenance-group-title">` +
      `<div class="fw-semibold">${escapeHtml(group.normalized_name || 'Duplicate group')}</div>` +
      `<div class="text-muted small path-cell"><code title="${escapeHtml(group.folder)}">${escapeHtml(group.folder)}</code></div>` +
      `</div>` +
      `<button class="btn btn-outline-secondary btn-sm" type="button" data-maint-expand="${escapeHtml(group.id)}">` +
      `<i class="bi ${expanded ? 'bi-chevron-up' : 'bi-chevron-down'}" aria-hidden="true"></i>` +
      `<span>${expanded ? 'Collapse' : 'Expand'}</span></button>` +
      `</div>` +
      `<div class="maintenance-group-summary">` +
      `<span>${escapeHtml(group.video_count ?? (group.videos || []).length)} videos</span>` +
      `<span>${escapeHtml(group.accessory_count || 0)} sidecar files</span>` +
      `<span>Default reclaimable: ${escapeHtml(group.reclaimable_label || '')}</span>` +
      `</div>` +
      `<label class="form-label duplicate-keeper-control duplicate-collapsed-keeper duplicate-summary-keeper mt-2 mb-0">Video to keep` +
      `<select class="form-select form-select-sm" data-maint-keep="${escapeHtml(group.id)}"${state.enabled ? '' : ' disabled'}>` +
      `${keeperOptions.map(video => groupOption(video, state.keepId)).join('')}` +
      `</select><code class="duplicate-keeper-name" title="${escapeHtml(recommended)}">${escapeHtml(recommended)}</code>` +
      `<span class="small text-muted">${escapeHtml(state.keepId === group.recommended_keep_id ? (group.recommended_keep_reason || 'Automatic recommendation') : 'Manual keeper selection')}</span></label>` +
      `${renderGroupReviewStats(group, state)}` +
      `<div class="duplicate-review-flags">${renderGroupSubtitleSignals(group)}${renderGroupReviewFlags(group)}</div>` +
      `${detail}` +
      `</section>`;
  }

  function renderGroups() {
    const target = byId('maintenanceGroups');
    if (!target) return;
    if (!currentScan || currentScan.status !== 'success') {
      target.innerHTML = '<div class="text-muted text-center py-4">Duplicate groups will appear here after a scan.</div>';
      updateSelectedSize();
      return;
    }
    if (!currentGroupsPage || !(currentGroupsPage.groups || []).length) {
      const emptyLabel = duplicateReviewFilter === 'attention'
        ? 'No groups are flagged for closer review.'
        : (duplicateReviewFilter === 'ready'
          ? 'Every group currently has a quick-review flag.'
          : 'No duplicate groups on this page.');
      target.innerHTML = currentScan.duplicate_group_count
        ? `${currentGroupsPage ? renderPager(currentGroupsPage) : ''}<div class="text-muted text-center py-4">${emptyLabel}</div>`
        : '<div class="text-muted text-center py-4">No duplicate groups found.</div>';
      updateSelectedSize();
      return;
    }
    const groups = currentPageGroups();
    target.innerHTML = `${renderPager(currentGroupsPage)}${groups.map(renderGroup).join('')}${renderPager(currentGroupsPage)}`;
    updateSelectedSize();
  }

  function setCurrentPageGroupSelection(selected) {
    currentPageGroups().forEach(group => {
      setDuplicateGroupSelected(group.id, selected);
    });
    duplicateSelectionChanged();
    renderGroups();
  }

  function setCurrentPageExpanded(expanded) {
    currentPageGroups().forEach(group => { ensureGroupState(group).expanded = expanded; });
    renderGroups();
    if (expanded) {
      Promise.allSettled(currentPageGroups()
        .filter(group => !(group.videos || []).length)
        .map(group => loadGroupDetails(group.id)));
    }
  }

  function resetGroupSuggestedActions(groupId) {
    const group = groupSummaries.get(groupId);
    const state = groupState.get(groupId);
    if (!group || !state) return;
    const candidates = groupCandidateFiles(group, state);
    state.includedFileIds = new Set(candidates.filter(defaultSelected).map(file => file.id));
    state.fileOperations = new Map();
    state.dirty = true;
    markGroupDirty(groupId);
    renderGroups();
  }

  function setGroupSidecarActions(groupId, action) {
    const group = groupSummaries.get(groupId);
    const state = groupState.get(groupId);
    if (!group || !state) return;
    groupCandidateFiles(group, state)
      .filter(file => file.kind !== 'video')
      .forEach(file => setFileAction(state, file.id, action));
    state.dirty = true;
    markGroupDirty(groupId);
    renderGroups();
  }

  async function openBrowser(path, show = true) {
    if (show) return maintenanceFolderPicker?.load(path || config.libRoot || '/library');
  }

  function setMaintenanceBrowserOpen(open) {
    if (!open && maintenanceFolderPicker?.isOpen()) maintenanceFolderPicker.toggle();
  }

  function maintenanceBrowserIsOpen() {
    return byId('maintenanceBrowserCollapse')?.classList.contains('show') || false;
  }

  function setPreviewBrowserOpen(open) {
    if (!open && previewFolderPicker?.isOpen()) previewFolderPicker.toggle();
  }

  function previewBrowserIsOpen() {
    return byId('previewBrowserCollapse')?.classList.contains('show') || false;
  }

  async function persistPreviewPath(path) {
    const res = await fetch('/api/maintenance/video-previews/scan-path', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({path})
    });
    const data = await readJsonResponse(res);
    if (!res.ok) throw new Error(data.error || 'Scan source could not be saved');
    const saved = data.scan_source?.path || path;
    previewLastPath = saved;
    config.previewScanPath = saved;
    if (byId('previewPath')) byId('previewPath').value = saved;
    return saved;
  }

  async function openPreviewBrowser(path) {
    return previewFolderPicker?.load(path || config.previewScanPath || config.libRoot || '/library');
  }

  async function openSubtitleBrowser(path) {
    return subtitleFolderPicker?.load(path || config.libRoot || '/library');
  }

  async function openActorBrowser(path) {
    return actorFolderPicker?.load(path || config.libRoot || '/library');
  }

  function initFolderPickers() {
    const create = window.vid2gifFolderPicker?.create;
    if (!create) return;
    maintenanceFolderPicker = create({
      inputId: 'maintenancePath', buttonId: 'maintenanceBrowseButton',
      panelId: 'maintenanceBrowserCollapse', containerId: 'maintenanceBrowser',
      defaultPath: config.libRoot || '/library', storageKey: 'vid2gif_duplicate_scan_source',
      bindButton: false,
    });
    previewFolderPicker = create({
      inputId: 'previewPath', buttonId: 'previewBrowseButton',
      panelId: 'previewBrowserCollapse', containerId: 'previewBrowser',
      defaultPath: config.previewScanPath || config.libRoot || '/library',
      storageKey: 'vid2gif_preview_scan_source', preserveInitialValue: true,
      bindButton: false, onChoose: persistPreviewPath,
    });
    subtitleFolderPicker = create({
      inputId: 'subtitlePath', buttonId: 'subtitleBrowseButton',
      panelId: 'subtitleBrowserCollapse', containerId: 'subtitleBrowser',
      defaultPath: config.libRoot || '/library', storageKey: 'vid2gif_subtitle_scan_source',
      bindButton: false,
    });
    actorFolderPicker = create({
      inputId: 'actorPath', buttonId: 'actorBrowseButton',
      panelId: 'actorBrowserCollapse', containerId: 'actorBrowser',
      defaultPath: config.libRoot || '/library', storageKey: 'vid2gif_actor_scan_source',
      bindButton: false,
    });
    posterFolderPicker = create({
      inputId: 'posterPath', buttonId: 'posterBrowseButton',
      panelId: 'posterBrowserCollapse', containerId: 'posterBrowser',
      defaultPath: config.libRoot || '/library', storageKey: 'vid2gif_poster_scan_source',
      bindButton: false,
    });
  }

  function stopPolling() {
    if (pollTimer) {
      clearInterval(pollTimer);
      pollTimer = null;
    }
  }

  function stopApplyPolling() {
    if (applyPollTimer) {
      clearInterval(applyPollTimer);
      applyPollTimer = null;
    }
  }

  function stopPreviewPolling() {
    if (previewPollTimer) {
      clearInterval(previewPollTimer);
      previewPollTimer = null;
    }
  }

  function stopQualityPolling() {
    if (qualityPollTimer) {
      clearInterval(qualityPollTimer);
      qualityPollTimer = null;
    }
  }

  function stopQualityApplyPolling() {
    if (qualityApplyPollTimer) {
      clearInterval(qualityApplyPollTimer);
      qualityApplyPollTimer = null;
    }
  }

  function stopSubtitlePolling() {
    if (subtitlePollTimer) {
      clearInterval(subtitlePollTimer);
      subtitlePollTimer = null;
    }
  }

  function stopActorPolling() {
    if (actorPollTimer) {
      clearInterval(actorPollTimer);
      actorPollTimer = null;
    }
  }

  function stopActorApplyPolling() {
    if (actorApplyPollTimer) {
      clearInterval(actorApplyPollTimer);
      actorApplyPollTimer = null;
    }
  }

  function handleScan(scan) {
    currentScan = scan;
    if (scan?.id) ensureDuplicateSelection(scan.id, scan.duplicate_group_count || 0);
    setProgress(scan);
    renderGroups();
    const stale = scan?.freshness?.status === 'changed';
    updateDuplicateControls();
    if (!scan) {
      setMessage('No scan results yet.', '');
    } else if (scan.status === 'success') {
      const protectedDetail = scan.protected_distinct_set_count
        ? `${scan.protected_distinct_set_count} same-title set${scan.protected_distinct_set_count === 1 ? '' : 's'} (${scan.protected_distinct_video_count || 0} videos) recognized as distinct and excluded from cleanup.`
        : '';
      const resultDetail = scan.large_result
        ? `Large result set. Loading ${groupPageLimit} groups at a time.`
        : (scan.reclaimable_label ? `Default reclaimable size: ${scan.reclaimable_label}.` : '');
      setMessage(
        `${scan.duplicate_group_count || 0} duplicate groups found`,
        withEmbyCoverage([resultDetail, protectedDetail].filter(Boolean).join(' '), scan)
      );
      appendEmbySyncNotice('maintenanceMessageDetail', embySyncFrom(currentApply));
      appendEmbyNotificationNotice('maintenanceMessageDetail', notificationFrom(currentApply));
      if (scan.duplicate_group_count && currentGroupsPage?.scan?.id !== scan.id) {
        loadGroupsPage(0);
      }
      if (stale) setMessage('Duplicate results are out of date', 'Library files changed after this scan. Rescan before creating a cleanup plan.');
    } else if (scan.status === 'failed') {
      setMessage('Scan failed', scan.error || '');
    } else if (scan.status === 'cancelled') {
      setMessage('Scan cancelled', '');
    } else {
      setMessage(
        scan.progress_label || 'Scanning',
        scan.progress_detail || (scan.scanned_video_count ? `${scan.scanned_video_count} videos checked` : 'Starting scan')
      );
    }
    if (scan && ['success', 'failed', 'cancelled'].includes(scan.status)) {
      stopPolling();
    }
  }

  async function pollScan(scanId) {
    if (!scanId) return;
    try {
      const res = await fetch(`/api/maintenance/duplicates/status?scan_id=${encodeURIComponent(scanId)}`);
      const data = await readJsonResponse(res);
      if (!res.ok) {
        setMessage(data.error || 'Scan unavailable', '');
        stopPolling();
        return;
      }
      handleScan(data.scan);
    } catch (e) {
      setMessage('Scan unavailable', e.message || '');
      stopPolling();
    }
  }

  async function refreshDuplicateStatus() {
    try {
      const res = await fetch('/api/maintenance/duplicates/status');
      const data = await readJsonResponse(res);
      if (res.ok) {
        handleScan(data.scan);
        if (data.scan?.active && !pollTimer) pollTimer = setInterval(() => pollScan(data.scan.id), 1000);
      }
    } catch (_e) {
      // Latest-result hydration is best effort.
    }
  }

  async function startScan() {
    const path = byId('maintenancePath')?.value.trim() || '';
    if (!path) {
      setMessage('Choose a folder under the library', '');
      return;
    }
    rememberScanSource(path, 'vid2gif_duplicate_scan_source');
    stopPolling();
    groupState.clear();
    groupSummaries.clear();
    currentGroupsPage = null;
    groupPageOffset = 0;
    currentPlan = null;
    currentApply = null;
    stopApplyPolling();
    invalidatePlan();
    setMessage('Starting scan', '');
    setProgress({status: 'queued', progress_percent: 0, progress_label: 'Queued', duplicate_group_count: 0});
    const button = byId('maintenanceScanButton');
    if (button) button.disabled = true;
    try {
      const res = await fetch('/api/maintenance/duplicates/scan', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({path})
      });
      const data = await readJsonResponse(res);
      if (!res.ok) {
        setMessage(data.error || 'Scan could not start', '');
        return;
      }
      handleScan(data.scan);
      if (data.scan?.active) pollTimer = setInterval(() => pollScan(data.scan.id), 1000);
    } catch (e) {
      setMessage('Scan could not start', e.message || '');
    } finally {
      if (button) button.disabled = Boolean(currentScan?.active);
    }
  }

  async function cancelScan() {
    if (!currentScan?.id) return;
    const cancelButton = byId('maintenanceCancelScanButton');
    if (cancelButton) cancelButton.disabled = true;
    setMessage('Cancelling scan', 'The current scan will stop after the active folder or file check finishes.');
    try {
      const res = await fetch('/api/maintenance/duplicates/cancel', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({scan_id: currentScan.id})
      });
      const data = await readJsonResponse(res);
      if (!res.ok) {
        setMessage(data.error || 'Scan could not be cancelled', '');
        return;
      }
      handleScan(data.scan);
    } catch (e) {
      setMessage('Scan could not be cancelled', e.message || '');
    }
  }

  function collectOverrides() {
    const overrides = [];
    groupState.forEach((state, groupId) => {
      if (!state.dirty) return;
      const group = groupSummaries.get(groupId) || {id: groupId, videos: [], recommended_keep_id: state.keepId};
      const keepId = state.keepId || group.recommended_keep_id;
      const candidates = groupCandidateFiles(group, state);
      const fileOperations = candidates
        .map(file => ({
          file_id: file.id,
          operation: state.fileOperations?.get(file.id) || 'default'
        }))
        .filter(item => item.operation !== 'default');
      const override = {
        id: group.id,
        enabled: state.enabled
      };
      if ((group.videos || []).length) {
        override.keep_video_id = keepId;
        override.remove_video_ids = (group.videos || []).filter(video => video.id !== keepId).map(video => video.id);
        override.include_file_ids = candidates.filter(file => state.includedFileIds.has(file.id)).map(file => file.id);
        override.file_operations = fileOperations;
      }
      overrides.push(override);
    });
    return overrides;
  }

  function renderPlan(plan) {
    const summary = byId('maintenancePlanSummary');
    if (!summary) return;
    const action = plan.action === 'delete' ? 'Delete' : 'Move';
    const unchangedCount = (plan.manual_review || []).length + (plan.skipped_groups || []).length;
    summary.innerHTML = renderChangePreview({
      title: 'Cleanup Plan',
      files: plan.files || [],
      metrics: [
        {label: 'Selection', value: plan.selection_mode === 'all_eligible' ? 'Across all pages' : 'Individual groups', detail: `${plan.selected_group_count || 0} of ${plan.total_group_count || 0} groups`},
        {label: 'Files affected', value: plan.file_count || 0},
        {label: 'Disk data', value: plan.total_size_label || '0 B'},
        {label: 'Operations', value: operationSummary(plan.files)},
        {label: 'Left unchanged', value: unchangedCount, detail: `${(plan.manual_review || []).length} manual review, ${(plan.skipped_groups || []).length} skipped groups`},
        {label: 'Playback deferred', value: plan.emby_playback?.deferred_count || 0}
      ],
      note: plan.action === 'delete'
        ? `Delete operations are permanent. Rename operations shown below remain inside the library. ${playbackGuardText(plan.emby_playback)}`
        : `Files marked move will be quarantined under ${plan.move_root || 'the configured move destination'}. ${playbackGuardText(plan.emby_playback)}`,
      changeForFile: file => ({
        operation: file.operation || plan.action,
        operationLabel: file.operation || action,
        source: file.relative_path || file.source_path,
        target: file.destination_path || '',
        detail: `${file.kind || 'file'}${file.size_label ? `, ${file.size_label}` : ''}`
      })
    });
    const selected = byId('maintenanceSelectedSize');
    if (selected) selected.textContent = plan.total_size_label || '0 B';
  }

  async function reviewPlan() {
    if (!currentScan || currentScan.status !== 'success') {
      setMessage('Run a scan first', '');
      return;
    }
    const action = byId('maintenanceAction')?.value || 'move';
    setMessage('Building cleanup plan', '');
    try {
      const res = await fetch('/api/maintenance/duplicates/plan', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({
          scan_id: currentScan.id,
          action,
          groups: collectOverrides(),
          selection: duplicateSelectionPayload()
        })
      });
      const data = await readJsonResponse(res);
      if (!res.ok) {
        setMessage(data.error || 'Plan could not be built', '');
        return;
      }
      currentPlan = data.plan;
      renderPlan(currentPlan);
      const summary = byId('maintenancePlanSummary');
      summary?.classList.add('maintenance-plan-ready');
      summary?.scrollIntoView({behavior: 'smooth', block: 'start'});
      summary?.focus({preventScroll: true});
      updateDuplicateControls();
      byId('maintenanceApplyButton').disabled = !currentPlan.file_count;
      setMessage(
        'Cleanup plan preview is ready below',
        `${currentPlan.selected_group_count || 0} groups selected across the scan results. ` + (Number(currentPlan.file_count || 0) >= 100
          ? `${currentPlan.file_count} files selected. This can take a while and will continue in the background.`
          : (currentPlan.total_size_label || ''))
      );
    } catch (e) {
      setMessage('Plan could not be built', e.message || '');
    }
  }

  function duplicateApplyBadgeClass(status) {
    if (status === 'success') return 'text-bg-success';
    if (status === 'failed') return 'text-bg-danger';
    if (['queued', 'running'].includes(status || '')) return 'text-bg-primary';
    return 'text-bg-secondary';
  }

  function renderDuplicateApplyStatus(apply) {
    const panel = byId('maintenanceApplyStatus');
    if (!panel) return;
    if (!apply) {
      panel.classList.add('d-none');
      return;
    }
    panel.classList.remove('d-none');
    const title = byId('maintenanceApplyTitle');
    const label = byId('maintenanceApplyProgressLabel');
    const badge = byId('maintenanceApplyBadge');
    const bar = byId('maintenanceApplyProgressBar');
    const counts = byId('maintenanceApplyCounts');
    const percent = byId('maintenanceApplyProgressPercent');
    const current = byId('maintenanceApplyCurrent');
    const results = byId('maintenanceApplyResults');
    if (title) title.textContent = apply.action === 'delete' ? 'Duplicate deletion' : 'Duplicate quarantine';
    if (label) label.textContent = apply.progress_label || apply.status || 'Waiting to start';
    if (badge) {
      badge.className = `badge ${duplicateApplyBadgeClass(apply.status)}`;
      badge.textContent = apply.status || 'Idle';
    }
    window.vid2gifProgress.apply(bar, apply);
    if (counts) counts.textContent = `${apply.processed_count || 0} of ${apply.file_count || 0} processed`;
    if (percent) percent.textContent = window.vid2gifProgress.valueLabel(apply);
    if (current) current.textContent = apply.current_path || apply.current_name || 'Nothing is running.';
    if (results) {
      const result = apply.result || {};
      results.textContent = apply.status === 'success'
        ? `${result.applied_count || apply.applied_count || 0} applied, ${result.missing_count || apply.missing_count || 0} missing, ${result.refused_count || apply.refused_count || 0} refused, ${result.deferred_count || apply.deferred_count || 0} deferred`
        : (apply.status === 'failed' ? (apply.error || 'Cleanup failed') : 'Progress updates automatically while cleanup runs.');
    }
  }

  function reconcileDuplicateResults(result) {
    if (!result?.scan_reconciled || !result.scan) return false;
    const resolved = new Set(result.resolved_group_ids || []);
    resolved.forEach(groupId => {
      groupState.delete(groupId);
      groupSummaries.delete(groupId);
      groupDetailGenerations.delete(groupId);
      duplicateSelection.excluded.delete(groupId);
      duplicateSelection.selected.delete(groupId);
      duplicateSelection.reclaimableById.delete(groupId);
      duplicateSelection.actionCountsById.delete(groupId);
    });
    currentScan = result.scan;
    duplicateSelection.total = Number(currentScan.duplicate_group_count || 0);
    saveDuplicateSelection();
    const oldPageTotal = Number(currentGroupsPage?.total || 0);
    const remainingPageTotal = Math.max(0, oldPageTotal - resolved.size);
    const refreshOffset = remainingPageTotal
      ? Math.min(groupPageOffset, Math.floor((remainingPageTotal - 1) / groupPageLimit) * groupPageLimit)
      : 0;
    currentGroupsPage = null;
    setProgress(currentScan);
    renderGroups();
    updateDuplicateControls();
    updateSelectedSize();
    if (currentScan.duplicate_group_count) loadGroupsPage(refreshOffset);
    return true;
  }

  function handleApply(apply) {
    currentApply = apply;
    renderDuplicateApplyStatus(apply);
    const button = byId('maintenanceApplyButton');
    const running = apply && ['queued', 'running'].includes(apply.status || '');
    if (button) button.disabled = running || !currentPlan;
    updateDuplicateControls();
    if (!apply) return;
    if (running) {
      const counts = `${apply.processed_count || 0} of ${apply.file_count || 0} files`;
      const detail = `${counts}, ${apply.applied_count || 0} applied, ${apply.missing_count || 0} missing, ${apply.refused_count || 0} refused, ${apply.deferred_count || 0} deferred`;
      setMessage(apply.progress_label || 'Cleanup running', apply.large_operation ? `${detail}. Large cleanup is running in the background.` : detail);
      return;
    }
    if (apply.status === 'success') {
      stopApplyPolling();
      const result = apply.result || {};
      setMessage(
        `${result.applied_count || apply.applied_count || 0} files processed`,
        `${result.total_applied_label || '0 B'} cleaned, ${result.missing_count || apply.missing_count || 0} missing, ${result.refused_count || apply.refused_count || 0} refused, ${result.deferred_count || apply.deferred_count || 0} deferred`
      );
      refreshMaintenanceLogs();
      currentPlan = null;
      const reconciled = reconcileDuplicateResults(result);
      if (!reconciled && currentScan?.id && currentScan.id === apply.scan_id && Number(result.applied_count || apply.applied_count || 0) > 0) {
        currentScan.freshness = {status: 'changed'};
      }
      updateDuplicateControls();
      if (button) button.disabled = true;
      if (!reconciled) checkMaintenanceFreshness();
      return;
    }
    if (apply.status === 'failed') {
      stopApplyPolling();
      setMessage('Cleanup failed', apply.error || '');
    }
  }

  async function pollApply(applyId) {
    if (!applyId) return;
    try {
      const res = await fetch(`/api/maintenance/duplicates/apply/status?apply_id=${encodeURIComponent(applyId)}`);
      const data = await readJsonResponse(res);
      if (!res.ok) {
        setMessage(data.error || 'Cleanup status unavailable', '');
        stopApplyPolling();
        return;
      }
      handleApply(data.apply);
    } catch (e) {
      setMessage('Cleanup status unavailable', e.message || '');
      stopApplyPolling();
    }
  }

  async function refreshDuplicateApplyStatus() {
    try {
      const res = await fetch('/api/maintenance/duplicates/apply/status');
      const data = await readJsonResponse(res);
      if (!res.ok || !data.apply) return;
      currentApply = data.apply;
      renderDuplicateApplyStatus(data.apply);
      if (['queued', 'running'].includes(data.apply.status || '')) {
        handleApply(data.apply);
        stopApplyPolling();
        applyPollTimer = setInterval(() => pollApply(data.apply.id), 1000);
      }
    } catch (_e) {
      // Latest cleanup hydration is best effort.
    }
  }

  async function applyPlan() {
    if (!currentPlan) {
      setMessage('Review a cleanup plan first', '');
      return;
    }
    const counts = operationCounts(currentPlan.files);
    const confirmation = currentPlan.action === 'delete'
      ? `Permanently delete ${counts.delete || 0} file(s) and apply ${Number(currentPlan.file_count || 0) - Number(counts.delete || 0)} other change(s), totaling ${currentPlan.total_size_label || '0 B'}?\n\nThis cannot be undone by vid2gif.`
      : `Apply ${currentPlan.file_count} file change(s), totaling ${currentPlan.total_size_label || '0 B'}?\n\nMoved files will go to:\n${currentPlan.move_root || 'the configured quarantine'}`;
    if (!window.confirm(confirmation)) {
      return;
    }
    const button = byId('maintenanceApplyButton');
    if (button) button.disabled = true;
    setMessage('Applying cleanup plan', '');
    try {
      const res = await fetch('/api/maintenance/duplicates/apply', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({plan_id: currentPlan.id})
      });
      const data = await readJsonResponse(res);
      if (!res.ok) {
        setMessage(data.error || 'Cleanup failed', '');
        return;
      }
      handleApply(data.apply);
      stopApplyPolling();
      if (data.apply?.id && ['queued', 'running'].includes(data.apply.status || '')) {
        applyPollTimer = setInterval(() => pollApply(data.apply.id), 1000);
      } else if (data.apply?.id) {
        pollApply(data.apply.id);
      }
    } catch (e) {
      setMessage('Cleanup failed', e.message || '');
    }
  }

  function renderMaintenanceLogs(logs) {
    const list = byId('maintenanceLogList');
    if (!list) return;
    if (!(logs || []).length) {
      list.innerHTML = '<div class="text-muted text-center py-4">No duplicate cleanup logs yet.</div>';
      return;
    }
    const rows = logs.map(log =>
      `<tr>` +
      `<td><div class="toolbar-row mb-0"><button class="btn btn-outline-secondary btn-sm" type="button" data-maint-log="${escapeHtml(log.id)}">Open</button>` +
      (log.restore_available ? `<button class="btn btn-outline-primary btn-sm" type="button" data-maint-restore-preview="${escapeHtml(log.id)}">Preview restore</button>` : '') +
      (log.restored_at ? `<span class="badge text-bg-success">Restored ${escapeHtml(log.restored_count || 0)}</span>` : '') +
      `</div></td>` +
      `<td>${escapeHtml(log.created_at || '')}</td>` +
      `<td>${escapeHtml(log.action || '')}</td>` +
      `<td>${escapeHtml(log.applied_count || 0)} applied, ${escapeHtml(log.refused_count || 0)} refused</td>` +
      `<td>${escapeHtml(log.size_label || '')}${log.truncated ? ' truncated' : ''}</td>` +
      `</tr>`
    ).join('');
    list.innerHTML =
      `<div class="table-responsive workspace-table-wrap">` +
      `<table class="table table-hover align-middle workspace-table" data-table-id="maintenance-duplicate-logs" data-sort-mode="client">` +
      `<thead><tr><th data-column-id="open" data-resizable="false"></th><th data-column-id="time" data-sortable="true">Time</th><th data-column-id="action" data-sortable="true">Action</th><th data-column-id="result" data-sortable="true">Result</th><th data-column-id="size" data-sortable="true">Log size</th></tr></thead>` +
      `<tbody>${rows}</tbody></table></div>`;
  }

  function renderDuplicateRestorePlan(plan) {
    const target = byId('maintenanceRestoreSummary');
    if (!target) return;
    target.innerHTML = renderChangePreview({
      title: 'Restore Preview',
      files: plan.files || [],
      metrics: [
        {label: 'Files restorable', value: plan.file_count || 0},
        {label: 'Unavailable', value: plan.unavailable_count || 0},
        {label: 'Collision names adjusted', value: plan.collision_adjusted_count || 0},
      ],
      note: 'Changes run in reverse order. Existing files are never overwritten; a restored-number suffix is added when a destination is occupied.',
      changeForFile: file => ({
        operation: 'restore',
        operationLabel: file.original_operation === 'rename' ? 'Undo rename' : 'Restore',
        source: file.source_path,
        target: file.destination_path,
        detail: `${file.size_label || ''}${file.collision_adjusted ? ' · collision-adjusted destination' : ''}`,
      }),
    }) + `<div class="toolbar-row mt-3"><button class="btn btn-primary btn-sm" type="button" data-maint-restore-apply="${escapeHtml(plan.id)}"><i class="bi bi-arrow-counterclockwise" aria-hidden="true"></i><span>Apply restore</span></button></div>`;
    target.scrollIntoView({behavior: 'smooth', block: 'start'});
  }

  async function previewDuplicateRestore(logId) {
    setMessage('Building restore preview', 'No files will be changed yet.');
    try {
      const res = await fetch(`/api/maintenance/duplicates/logs/${encodeURIComponent(logId)}/restore/plan`, {
        method: 'POST', headers: {'Content-Type': 'application/json'}, body: '{}',
      });
      const data = await readJsonResponse(res);
      if (!res.ok) throw new Error(data.error || 'Restore preview could not be built');
      currentRestorePlan = data.plan;
      renderDuplicateRestorePlan(currentRestorePlan);
      setMessage('Restore preview is ready', `${currentRestorePlan.file_count || 0} file(s) can be restored.`);
    } catch (e) {
      setMessage('Restore preview could not be built', e.message || '');
    }
  }

  async function applyDuplicateRestore(planId) {
    if (!currentRestorePlan || currentRestorePlan.id !== planId) return;
    if (!window.confirm(`Restore ${currentRestorePlan.file_count || 0} file(s)? Existing files will not be overwritten.`)) return;
    try {
      const res = await fetch('/api/maintenance/duplicates/restore', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({plan_id: planId}),
      });
      const data = await readJsonResponse(res);
      if (!res.ok) throw new Error(data.error || 'Restore could not be applied');
      currentRestorePlan = null;
      if (byId('maintenanceRestoreSummary')) byId('maintenanceRestoreSummary').innerHTML = '';
      setMessage('Restore complete', `${data.result?.applied_count || 0} restored, ${data.result?.refused_count || 0} refused, ${data.result?.collision_adjusted_count || 0} collision names adjusted.`);
      refreshMaintenanceLogs();
      checkMaintenanceFreshness();
    } catch (e) {
      setMessage('Restore could not be applied', e.message || '');
    }
  }

  async function refreshMaintenanceLogs() {
    try {
      const res = await fetch('/api/maintenance/duplicates/logs');
      const data = await readJsonResponse(res);
      if (!res.ok) return;
      renderMaintenanceLogs(data.logs || []);
    } catch (e) {
      const list = byId('maintenanceLogList');
      if (list) list.innerHTML = `<div class="small text-danger">${escapeHtml(e.message || 'Logs unavailable')}</div>`;
    }
  }

  async function openMaintenanceLog(logId) {
    const output = byId('maintenanceLogContent');
    if (!output) return;
    output.classList.remove('d-none');
    output.textContent = 'Loading log...';
    try {
      const res = await fetch(`/api/maintenance/duplicates/logs/${encodeURIComponent(logId)}`);
      const data = await readJsonResponse(res);
      if (!res.ok) {
        output.textContent = data.error || 'Log unavailable';
        return;
      }
      output.textContent = data.log?.content || '';
    } catch (e) {
      output.textContent = e.message || 'Log unavailable';
    }
  }

  function setPreviewMessage(title, detail) {
    const titleEl = byId('previewMessageTitle');
    const detailEl = byId('previewMessageDetail');
    if (titleEl) titleEl.textContent = title || '';
    if (detailEl) detailEl.textContent = detail || '';
  }

  function previewSelectionFromStorage(scanId, metadata) {
    let saved = null;
    try {
      saved = JSON.parse(localStorage.getItem(PREVIEW_SELECTION_STORAGE_KEY) || 'null');
    } catch (_e) {
      saved = null;
    }
    const sameScan = saved && saved.scanId === scanId;
    const mode = sameScan && saved.mode === 'explicit' ? 'explicit' : 'all_eligible';
    return {
      scanId,
      mode,
      excluded: new Set(sameScan && Array.isArray(saved.excluded) ? saved.excluded : []),
      includedHeld: new Set(sameScan && Array.isArray(saved.includedHeld) ? saved.includedHeld : []),
      selected: new Set(sameScan && Array.isArray(saved.selected) ? saved.selected : []),
      missingTotal: Number(metadata?.missing_total || 0),
      heldTotal: Number(metadata?.held_count || 0)
    };
  }

  function ensurePreviewSelection(scanId, metadata) {
    if (previewSelection.scanId !== scanId) {
      previewSelection = previewSelectionFromStorage(scanId, metadata);
      previewGenerationPlan = null;
      const planSummary = byId('previewGenerationSummary');
      if (planSummary) planSummary.innerHTML = '';
      const startButton = byId('previewGenerationStartButton');
      if (startButton) startButton.disabled = true;
    } else {
      previewSelection.missingTotal = Number(metadata?.missing_total || previewSelection.missingTotal || 0);
      previewSelection.heldTotal = Number(metadata?.held_count || 0);
    }
  }

  function savePreviewSelection() {
    try {
      localStorage.setItem(PREVIEW_SELECTION_STORAGE_KEY, JSON.stringify({
        scanId: previewSelection.scanId,
        mode: previewSelection.mode,
        excluded: Array.from(previewSelection.excluded),
        includedHeld: Array.from(previewSelection.includedHeld),
        selected: Array.from(previewSelection.selected)
      }));
    } catch (_e) {
      // Selection still remains stable for this page session.
    }
  }

  function previewItemIsSelected(item) {
    if (!item || item.status !== 'missing') return false;
    if (previewSelection.mode === 'explicit') return previewSelection.selected.has(item.id);
    if (item.generation_held) return previewSelection.includedHeld.has(item.id);
    return !previewSelection.excluded.has(item.id);
  }

  function previewSelectedCount() {
    if (previewSelection.mode === 'explicit') return previewSelection.selected.size;
    return Math.max(0, Math.min(
      previewSelection.missingTotal,
      previewSelection.missingTotal
        - previewSelection.heldTotal
        - previewSelection.excluded.size
        + previewSelection.includedHeld.size
    ));
  }

  function previewSelectionPayload() {
    if (previewSelection.mode === 'explicit') {
      return {mode: 'explicit', item_ids: Array.from(previewSelection.selected)};
    }
    return {
      mode: 'all_eligible',
      excluded_item_ids: Array.from(previewSelection.excluded),
      include_held_item_ids: Array.from(previewSelection.includedHeld)
    };
  }

  function renderPreviewSelectionSummary() {
    const target = byId('previewSelectionSummary');
    if (!target) return;
    const selected = previewSelectedCount();
    const held = Math.max(0, previewSelection.heldTotal - previewSelection.includedHeld.size);
    const mode = previewSelection.mode === 'all_eligible' ? 'across all result pages' : 'chosen individually';
    target.innerHTML = `<strong>${escapeHtml(selected)} selected</strong> ${escapeHtml(mode)}` +
      (held ? ` <span class="text-warning-emphasis">- ${escapeHtml(held)} held back after previous generation issues</span>` : '') +
      `<div class="small text-muted mt-1">Page navigation does not change this selection.</div>`;
  }

  function updatePreviewGenerationControls() {
    const selected = previewSelectedCount();
    const generationActive = ['queued', 'running', 'cancelling'].includes(previewGenerationRun?.status || '');
    const generationMadeScanStale = previewGenerationRun?.status === 'success'
      && previewGenerationRun?.scan_id === previewScan?.id
      && Number(previewGenerationRun?.generated_count || 0) > 0;
    const blocked = previewScan?.freshness?.status === 'changed' || generationActive || generationMadeScanStale;
    const planButton = byId('previewGenerationPlanButton');
    if (planButton) {
      planButton.disabled = !selected || blocked;
      planButton.textContent = selected ? `Review ${selected} Selected` : 'Review Selection';
    }
    const selectButton = byId('previewSelectMissingButton');
    if (selectButton) selectButton.disabled = !previewSelection.missingTotal || generationActive;
    const clearButton = byId('previewDeselectMissingButton');
    if (clearButton) clearButton.disabled = !selected || generationActive;
    renderPreviewSelectionSummary();
  }

  function previewSelectionChanged() {
    previewGenerationPlan = null;
    savePreviewSelection();
    const summary = byId('previewGenerationSummary');
    if (summary) summary.innerHTML = '';
    const startButton = byId('previewGenerationStartButton');
    if (startButton) startButton.disabled = true;
    updatePreviewGenerationControls();
  }

  function setPreviewProgress(scan) {
    const state = byId('previewScanState');
    const label = byId('previewProgressLabel');
    const percent = byId('previewProgressPercent');
    const bar = byId('previewProgressBar');
    const missing = byId('previewMissingCount');
    const present = byId('previewPresentCount');
    const pct = Math.max(0, Math.min(100, Math.round(Number(scan?.progress_percent || 0))));
    if (state) state.textContent = scan?.status || 'Idle';
    if (label) label.textContent = scan?.progress_label || 'Choose a folder';
    if (percent) percent.textContent = window.vid2gifProgress.valueLabel(scan);
    window.vid2gifProgress.apply(bar, scan || {progress_percent: pct});
    if (missing) missing.textContent = String(scan?.missing_count || 0);
    if (present) present.textContent = String(scan?.present_count || 0);
    const active = Boolean(scan?.active || ['queued', 'running', 'cancelling'].includes(scan?.status || ''));
    const scanButton = byId('previewScanButton');
    const cancelButton = byId('previewCancelScanButton');
    const verifyButton = byId('previewVerifyButton');
    if (scanButton) scanButton.disabled = active;
    if (cancelButton) cancelButton.disabled = !active || scan?.status === 'cancelling';
    if (verifyButton) verifyButton.disabled = active || !previewLastPath;
    const planButton = byId('previewGenerationPlanButton');
    if (planButton && scan?.freshness?.status === 'changed') planButton.disabled = true;
  }

  function previewPageRangeText(page) {
    const total = Number(page?.total || 0);
    if (!total) return '0 of 0';
    const start = Number(page.offset || 0) + 1;
    const end = Math.min(total, Number(page.offset || 0) + Number(page.count || 0));
    return `${start}-${end} of ${total}`;
  }

  function previewPager(page) {
    if (!page) return '';
    return `<div class="maintenance-pager">` +
      `<div class="text-muted small">${escapeHtml(previewPageRangeText(page))}${page.large_result ? ' - large result set' : ''}</div>` +
      `<div class="toolbar-row mb-0">` +
      `<button class="btn btn-outline-secondary btn-sm" type="button" data-preview-page="prev"${page.has_previous ? '' : ' disabled'}>Previous</button>` +
      `<button class="btn btn-outline-secondary btn-sm" type="button" data-preview-page="next"${page.has_next ? '' : ' disabled'}>Next</button>` +
      `</div>` +
      `</div>`;
  }

  function previewStatusBadge(status) {
    if (status === 'missing') return '<span class="badge text-bg-warning">Missing</span>';
    if (status === 'present') return '<span class="badge text-bg-success">Present</span>';
    return `<span class="badge text-bg-secondary">${escapeHtml(status || 'Unknown')}</span>`;
  }

  function renderPreviewItems(page) {
    const target = byId('previewItems');
    if (!target) return;
    if (!previewScan || previewScan.status !== 'success') {
      target.innerHTML = '<div class="text-muted text-center py-4">Missing video previews will appear here after a scan.</div>';
      return;
    }
    if (!page || !(page.items || []).length) {
      target.innerHTML = `${page ? previewPager(page) : ''}<div class="text-muted text-center py-4">No videos in this view.</div>`;
      return;
    }
    const rows = (page.items || []).map(item => {
      const bifNames = (item.bifs || []).map(bif => bif.interval_seconds
        ? `${bif.name} (${bif.interval_seconds}s)`
        : bif.name
      ).join(', ');
      const issue = item.previous_generation_issue;
      const issueBadge = issue
        ? '<span class="badge text-bg-warning ms-1">Previous issue</span>'
        : '';
      const detail = issue
        ? `${item.detail || ''} Previous generation attempt: ${issue.reason || 'could not complete'}`
        : (item.detail || '');
      return `<tr>` +
        `<td>${item.status === 'missing' ? `<input class="form-check-input" type="checkbox" data-preview-generate="${escapeHtml(item.id)}" aria-label="Generate BIF for ${escapeHtml(item.name)}"${previewItemIsSelected(item) ? ' checked' : ''}>` : ''}</td>` +
        `<td>${previewStatusBadge(item.status)}${issueBadge}</td>` +
        `<td class="path-cell"><code title="${escapeHtml(item.path)}">${escapeHtml(item.relative_path || item.name)}</code></td>` +
        `<td>${escapeHtml(item.size_label || '')}</td>` +
        `<td>${escapeHtml(detail)}</td>` +
        `<td class="path-cell"><code title="${escapeHtml(bifNames)}">${escapeHtml(bifNames || 'none')}</code></td>` +
        `</tr>`;
    }).join('');
    target.innerHTML =
      `${previewPager(page)}` +
      `<div class="table-responsive workspace-table-wrap">` +
      `<table class="table table-hover align-middle workspace-table" data-table-id="maintenance-missing-bifs" data-sort-mode="server" data-current-sort="${escapeHtml(page.sort || previewSort.column)}" data-current-direction="${escapeHtml(page.direction || previewSort.direction)}">` +
      `<thead><tr><th data-column-id="generate" data-resizable="false">Generate</th><th data-column-id="status" data-sortable="true">Status</th><th data-column-id="video" data-sortable="true">Video</th><th data-column-id="size" data-sortable="true" data-sort-type="number">Size</th><th data-column-id="detail" data-sortable="true">Detail</th><th data-column-id="bifs" data-sortable="true" data-sort-type="number">BIF files</th></tr></thead>` +
      `<tbody>${rows}</tbody></table></div>` +
      `${previewPager(page)}`;
  }

  async function loadPreviewItems(offset = previewPageOffset) {
    if (!previewScan?.id || previewScan.status !== 'success') return;
    const status = byId('previewItemStatus')?.value || 'missing';
    const target = byId('previewItems');
    if (target) target.innerHTML = '<div class="text-muted text-center py-4">Loading video preview results...</div>';
    try {
      const res = await fetch(`/api/maintenance/video-previews/items?scan_id=${encodeURIComponent(previewScan.id)}&status=${encodeURIComponent(status)}&offset=${encodeURIComponent(offset)}&limit=${encodeURIComponent(previewPageLimit)}&sort=${encodeURIComponent(previewSort.column)}&direction=${encodeURIComponent(previewSort.direction)}`);
      const data = await readJsonResponse(res);
      if (!res.ok) {
        setPreviewMessage(data.error || 'Video preview results unavailable', '');
        return;
      }
      previewItemsPage = data;
      previewSort = {column: data.sort || previewSort.column, direction: data.direction || previewSort.direction};
      previewPageOffset = Number(data.offset || 0);
      ensurePreviewSelection(data.scan?.id || previewScan.id, data.selection || {});
      updatePreviewGenerationControls();
      renderPreviewItems(data);
    } catch (e) {
      setPreviewMessage('Video preview results unavailable', e.message || '');
    }
  }

  function handlePreviewScan(scan) {
    previewScan = scan;
    setPreviewProgress(scan);
    const terminal = scan && ['success', 'failed', 'cancelled'].includes(scan.status || '');
    if (!scan) {
      setPreviewMessage('No video preview scan yet.', '');
    } else if (scan.status === 'success') {
      const configured = scan.configured_profile || {width: config.bifWidth || 320, interval_seconds: config.bifInterval || 10};
      const recommendation = scan.recommended_profile;
      if (byId('previewBifWidth')) byId('previewBifWidth').value = configured.width;
      if (byId('previewBifInterval')) byId('previewBifInterval').value = configured.interval_seconds;
      const recommendationEl = byId('previewBifRecommendation');
      if (recommendationEl) {
        recommendationEl.textContent = recommendation
          ? `Latest observed Emby profile: ${recommendation.width}px every ${recommendation.interval_seconds}s (${recommendation.source_name || 'BIF'}). ${scan.profile_mismatch ? 'Current settings do not match.' : 'Current settings match.'}`
          : 'No valid externally generated BIF profile was found in this scan.';
      }
      byId('previewUseRecommendationButton').disabled = !recommendation;
      setPreviewMessage(
        `${scan.missing_count || 0} missing video preview${(scan.missing_count || 0) === 1 ? '' : 's'}`,
        withEmbyCoverage(`${scan.present_count || 0} present`, scan)
      );
      if (previewItemsPage?.scan?.id !== scan.id) {
        loadPreviewItems(0);
      }
      if (scan.freshness?.status === 'changed') setPreviewMessage('Video preview results are out of date', 'Library files changed after this scan. Rescan before generating BIF files.');
    } else if (scan.status === 'failed') {
      setPreviewMessage('Video preview scan failed', scan.error || '');
    } else if (scan.status === 'cancelled') {
      setPreviewMessage('Video preview scan cancelled', '');
    } else {
      setPreviewMessage(
        scan.progress_label || 'Scanning video previews',
        scan.progress_detail || (scan.scanned_video_count ? `${scan.scanned_video_count} videos checked` : 'Starting scan')
      );
    }
    if (terminal) {
      stopPreviewPolling();
    }
  }

  async function pollPreviewScan(scanId) {
    if (!scanId) return;
    try {
      const res = await fetch(`/api/maintenance/video-previews/status?scan_id=${encodeURIComponent(scanId)}`);
      const data = await readJsonResponse(res);
      if (!res.ok) {
        setPreviewMessage(data.error || 'Video preview scan unavailable', '');
        stopPreviewPolling();
        return;
      }
      handlePreviewScan(data.scan);
    } catch (e) {
      setPreviewMessage('Video preview scan unavailable', e.message || '');
      stopPreviewPolling();
    }
  }

  async function refreshPreviewStatus() {
    try {
      const res = await fetch('/api/maintenance/video-previews/status');
      const data = await readJsonResponse(res);
      if (res.ok) {
        handlePreviewScan(data.scan);
        if (data.scan?.active && !previewPollTimer) previewPollTimer = setInterval(() => pollPreviewScan(data.scan.id), 1000);
      }
    } catch (_e) {
      // Latest-result hydration is best effort.
    }
  }

  async function startPreviewScan(pathOverride) {
    const path = (pathOverride || byId('previewPath')?.value || '').trim();
    if (!path) {
      setPreviewMessage('Choose a folder under the library', '');
      return;
    }
    rememberScanSource(path, 'vid2gif_preview_scan_source');
    stopPreviewPolling();
    previewItemsPage = null;
    previewPageOffset = 0;
    setPreviewMessage('Starting video preview scan', '');
    setPreviewProgress({status: 'queued', progress_percent: 0, progress_label: 'Queued'});
    try {
      const res = await fetch('/api/maintenance/video-previews/scan', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({path})
      });
      const data = await readJsonResponse(res);
      if (!res.ok) {
        setPreviewMessage(data.error || 'Video preview scan could not start', '');
        return;
      }
      previewLastPath = data.scan?.path || path;
      config.previewScanPath = previewLastPath;
      if (byId('previewPath')) byId('previewPath').value = previewLastPath;
      handlePreviewScan(data.scan);
      previewPollTimer = setInterval(() => pollPreviewScan(data.scan.id), 1000);
    } catch (e) {
      setPreviewMessage('Video preview scan could not start', e.message || '');
    }
  }

  async function cancelPreviewScan() {
    if (!previewScan?.id) return;
    setPreviewMessage('Cancelling video preview scan', '');
    try {
      const res = await fetch('/api/maintenance/video-previews/cancel', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({scan_id: previewScan.id})
      });
      const data = await readJsonResponse(res);
      if (!res.ok) {
        setPreviewMessage(data.error || 'Video preview scan could not be cancelled', '');
        return;
      }
      handlePreviewScan(data.scan);
    } catch (e) {
      setPreviewMessage('Video preview scan could not be cancelled', e.message || '');
    }
  }

  async function saveBifProfile(width, interval) {
    const res = await fetch('/api/maintenance/video-previews/generation/settings', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({width, interval_seconds: interval})
    });
    const data = await readJsonResponse(res);
    if (!res.ok) throw new Error(data.error || 'BIF profile could not be saved');
    config.bifWidth = data.settings.width;
    config.bifInterval = data.settings.interval_seconds;
    return data.settings;
  }

  async function saveCurrentBifProfile() {
    try {
      const settings = await saveBifProfile(byId('previewBifWidth')?.value, byId('previewBifInterval')?.value);
      setPreviewMessage('BIF generation profile saved', `${settings.width}px every ${settings.interval_seconds}s`);
      if (previewScan) handlePreviewScan({...previewScan, configured_profile: settings, profile_mismatch: Boolean(previewScan.recommended_profile && (previewScan.recommended_profile.width !== settings.width || previewScan.recommended_profile.interval_seconds !== settings.interval_seconds))});
    } catch (e) {
      setPreviewMessage('BIF profile could not be saved', e.message || '');
    }
  }

  async function useBifRecommendation() {
    const profile = previewScan?.recommended_profile;
    if (!profile) return;
    if (byId('previewBifWidth')) byId('previewBifWidth').value = profile.width;
    if (byId('previewBifInterval')) byId('previewBifInterval').value = profile.interval_seconds;
    await saveCurrentBifProfile();
  }

  function renderGenerationPlan(plan) {
    const target = byId('previewGenerationSummary');
    if (!target) return;
    target.innerHTML = renderChangePreview({
      title: 'Missing BIF Generation Plan',
      files: plan.files || [],
      metrics: [
        {label: 'Videos', value: plan.file_count || 0},
        {label: 'Width', value: `${plan.width}px`},
        {label: 'Interval', value: `${plan.interval_seconds}s`},
        {label: 'Selection', value: plan.selection_mode === 'all_eligible' ? 'Across all pages' : 'Individual items'},
        {label: 'Previous issues included', value: plan.held_override_count || 0}
      ],
      note: 'Frames are generated and validated outside the media folder before atomic installation.',
      changeForFile: file => ({operation: 'generate', operationLabel: 'Generate', source: file.video_relative_path, target: file.output_relative_path, detail: `${plan.width}px every ${plan.interval_seconds}s`})
    });
  }

  async function reviewGenerationPlan(confirmMismatch = false) {
    if (!previewScan?.id || !previewSelectedCount()) return;
    try {
      const res = await fetch('/api/maintenance/video-previews/generation/plan', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({
          scan_id: previewScan.id,
          selection: previewSelectionPayload(),
          confirm_profile_mismatch: confirmMismatch
        })
      });
      const data = await readJsonResponse(res);
      if (res.status === 409 && data.profile_mismatch && !confirmMismatch) {
        const recommended = previewScan.recommended_profile;
        const proceed = window.confirm(`Current BIF settings do not match the latest observed Emby BIF (${recommended?.width || '?'}px every ${recommended?.interval_seconds || '?'}s).\n\nContinue with the saved profile?`);
        if (proceed) return reviewGenerationPlan(true);
        return;
      }
      if (!res.ok) throw new Error(data.error || 'Generation plan could not be built');
      previewGenerationPlan = data.plan;
      renderGenerationPlan(data.plan);
      byId('previewGenerationStartButton').disabled = !data.plan.file_count;
      setPreviewMessage('Generation plan ready for review', `${data.plan.file_count} video(s) selected across the scan results. Generate BIFs when the plan looks right.`);
    } catch (e) {
      setPreviewMessage('Generation plan could not be built', e.message || '');
    }
  }

  function stopGenerationPolling() {
    if (previewGenerationPollTimer) clearInterval(previewGenerationPollTimer);
    previewGenerationPollTimer = null;
  }

  function renderGenerationRun(run) {
    const panel = byId('previewGenerationStatus');
    if (!panel || !run) return;
    panel.classList.remove('d-none');
    const active = ['queued', 'running', 'cancelling'].includes(run.status || '');
    const titleByStatus = {
      queued: 'BIF generation is queued',
      running: 'BIF generation is running',
      cancelling: 'Cancelling BIF generation',
      success: Number(run.refused_count || 0) ? 'BIF generation finished with issues' : 'BIF generation finished',
      cancelled: 'BIF generation was cancelled',
      interrupted: 'BIF generation was interrupted',
      failed: 'BIF generation failed'
    };
    const badgeByStatus = {
      queued: ['Queued', 'text-bg-info'],
      running: ['Running', 'text-bg-primary'],
      cancelling: ['Cancelling', 'text-bg-warning'],
      success: [Number(run.refused_count || 0) ? 'Issues' : 'Complete', Number(run.refused_count || 0) ? 'text-bg-warning' : 'text-bg-success'],
      cancelled: ['Cancelled', 'text-bg-secondary'],
      interrupted: ['Interrupted', 'text-bg-warning'],
      failed: ['Failed', 'text-bg-danger']
    };
    const badgeState = badgeByStatus[run.status] || [run.status || 'Unknown', 'text-bg-secondary'];
    const title = byId('previewGenerationTitle');
    const badge = byId('previewGenerationBadge');
    const label = byId('previewGenerationProgressLabel');
    const percent = byId('previewGenerationProgressPercent');
    const bar = byId('previewGenerationProgressBar');
    if (title) title.textContent = titleByStatus[run.status] || 'BIF generation';
    if (badge) {
      badge.textContent = badgeState[0];
      badge.className = `badge ${badgeState[1]}`;
    }
    if (label) label.textContent = run.progress_label || run.progress_detail || '';
    if (percent) percent.textContent = window.vid2gifProgress.valueLabel(run);
    window.vid2gifProgress.apply(bar, run);

    const processed = Number(run.processed_count || 0);
    const total = Number(run.file_count || 0);
    const generated = Number(run.generated_count || 0);
    const refused = Number(run.refused_count || 0);
    const counts = byId('previewGenerationCounts');
    if (counts) counts.textContent = `${processed} of ${total} processed - ${generated} generated - ${refused} skipped`;

    const current = byId('previewGenerationCurrent');
    const itemProgress = byId('previewGenerationItemProgress');
    if (current) {
      current.textContent = active && run.current_video
        ? run.current_video
        : (run.error || 'No generation process is currently running.');
    }
    if (itemProgress) {
      const frameText = Number(run.expected_frame_count || 0)
        ? `${Number(run.current_frame_count || 0)} of about ${Number(run.expected_frame_count)} frames`
        : `${Number(run.current_frame_count || 0)} frames written`;
      itemProgress.textContent = active
        ? `Video ${Number(run.current_index || 0)} of ${total} - ${run.current_stage || 'Working'} - ${frameText}`
        : (run.status === 'success'
          ? 'Run Verify Again to refresh the missing list before starting another batch.'
          : (run.progress_detail || ''));
    }

    const resultTarget = byId('previewGenerationResults');
    if (resultTarget) {
      const items = run.items || run.result?.items || [];
      const recent = items.slice(-5).reverse();
      resultTarget.innerHTML = recent.length
        ? `<div class="metric-label">Latest results</div>${recent.map(item => {
            const generatedItem = item.status === 'generated';
            const detail = item.reason ? `<div class="text-danger mt-1">${escapeHtml(item.reason)}</div>` : '';
            return `<div class="generation-result">` +
              `<span class="badge ${generatedItem ? 'text-bg-success' : 'text-bg-warning'}">${generatedItem ? 'Generated' : 'Skipped'}</span>` +
              `<div><code>${escapeHtml(item.video || item.item_id || 'Video')}</code>${detail}</div>` +
              `</div>`;
          }).join('')}`
        : '<div class="small text-muted">Per-video results will appear here as the batch runs.</div>';
    }

    byId('previewGenerationCancelButton').disabled = !active || run.status === 'cancelling';
    if (active) {
      byId('previewGenerationStartButton').disabled = true;
      byId('previewGenerationPlanButton').disabled = true;
    } else if (run.status === 'success' && run.scan_id === previewScan?.id && Number(run.generated_count || 0) > 0) {
      byId('previewGenerationStartButton').disabled = true;
      byId('previewGenerationPlanButton').disabled = true;
    }
  }

  async function pollGeneration(runId) {
    try {
      const res = await fetch(`/api/maintenance/video-previews/generation/status?run_id=${encodeURIComponent(runId)}`);
      const data = await readJsonResponse(res);
      if (!res.ok) throw new Error(data.error || 'Generation status unavailable');
      previewGenerationRun = data.run;
      const active = ['queued', 'running', 'cancelling'].includes(data.run?.status || '');
      renderGenerationRun(data.run);
      if (active) {
        setPreviewMessage('BIF generation is running', data.run.progress_detail || data.run.progress_label || '');
        return;
      }
      stopGenerationPolling();
      if (data.run?.status === 'success') {
        setPreviewMessage('BIF generation complete', `${data.run.generated_count || 0} generated, ${data.run.refused_count || 0} skipped. Run Verify Again to refresh the missing list.`);
        appendEmbySyncNotice('previewMessageDetail', embySyncFrom(data.run));
        appendEmbyNotificationNotice('previewMessageDetail', notificationFrom(data.run));
        previewGenerationPlan = null;
        byId('previewGenerationStartButton').disabled = true;
        updatePreviewGenerationControls();
        if (Number(data.run.generated_count || 0) === 0 && previewItemsPage) {
          loadPreviewItems(previewPageOffset);
        } else {
          renderPreviewItems(previewItemsPage);
        }
      } else {
        const title = data.run?.status === 'cancelled'
          ? 'BIF generation cancelled'
          : (data.run?.status === 'interrupted' ? 'BIF generation interrupted' : 'BIF generation failed');
        setPreviewMessage(title, data.run?.error || data.run?.progress_detail || '');
        appendEmbyNotificationNotice('previewMessageDetail', notificationFrom(data.run));
      }
    } catch (e) {
      const label = byId('previewGenerationProgressLabel');
      if (label) label.textContent = `Status temporarily unavailable; retrying. ${e.message || ''}`;
    }
  }

  async function refreshGenerationStatus() {
    try {
      const res = await fetch('/api/maintenance/video-previews/generation/status');
      const data = await readJsonResponse(res);
      if (!res.ok || !data.run) return;
      previewGenerationRun = data.run;
      renderGenerationRun(data.run);
      const active = ['queued', 'running', 'cancelling'].includes(data.run.status || '');
      if (active && !previewGenerationPollTimer) {
        previewGenerationPollTimer = setInterval(() => pollGeneration(data.run.id), 1000);
      }
    } catch (_e) {
      // A missing historical run must not block the rest of maintenance hydration.
    }
  }

  async function startGeneration() {
    if (!previewGenerationPlan) return;
    if (!window.confirm(`Generate ${previewGenerationPlan.file_count} missing BIF file(s) using ${previewGenerationPlan.width}px thumbnails every ${previewGenerationPlan.interval_seconds}s?`)) return;
    try {
      const res = await fetch('/api/maintenance/video-previews/generation/start', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({plan_id: previewGenerationPlan.id})});
      const data = await readJsonResponse(res);
      if (!res.ok) throw new Error(data.error || 'BIF generation could not start');
      previewGenerationRun = data.run;
      const summary = byId('previewGenerationSummary');
      if (summary) summary.innerHTML = '';
      renderGenerationRun(data.run);
      setPreviewMessage('BIF generation started', data.run.progress_detail || 'The live run is shown below.');
      byId('previewGenerationStartButton').disabled = true;
      byId('previewGenerationCancelButton').disabled = false;
      stopGenerationPolling();
      previewGenerationPollTimer = setInterval(() => pollGeneration(data.run.id), 1000);
      pollGeneration(data.run.id);
    } catch (e) {
      setPreviewMessage('BIF generation could not start', e.message || '');
    }
  }

  async function cancelGeneration() {
    if (!previewGenerationRun?.id) return;
    try {
      const res = await fetch('/api/maintenance/video-previews/generation/cancel', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({run_id: previewGenerationRun.id})});
      const data = await readJsonResponse(res);
      if (!res.ok) throw new Error(data.error || 'Generation could not be cancelled');
      previewGenerationRun = data.run;
      renderGenerationRun(data.run);
    } catch (e) {
      setPreviewMessage('Generation could not be cancelled', e.message || '');
    }
  }

  function renderPreviewEmbyTasks(data) {
    const configured = byId('previewEmbyConfigured');
    const task = byId('previewEmbyTask');
    const last = byId('previewEmbyLastResult');
    const message = byId('previewEmbyMessage');
    const result = data?.result || {};
    const thumbnailTask = data?.thumbnail_task || {};
    if (configured) configured.textContent = data?.configured ? 'Emby: configured' : 'Emby: not configured';
    if (task) task.textContent = thumbnailTask.id ? `Task: ${thumbnailTask.name || thumbnailTask.id}` : 'Task: not found';
    if (last) last.textContent = `Last action: ${result.status || 'never'}`;
    if (message) {
      message.className = `scan-estimate-detail mt-1 ${result.status === 'failed' ? 'text-danger' : ''}`;
      message.textContent = result.message || 'Uses the global Emby settings.';
    }
  }

  async function refreshPreviewTasks() {
    try {
      const res = await fetch('/api/maintenance/video-previews/emby/tasks');
      const data = await readJsonResponse(res);
      if (!res.ok) {
        renderPreviewEmbyTasks({result: {status: 'failed', message: data.error || 'Task status unavailable'}});
        return;
      }
      renderPreviewEmbyTasks(data);
    } catch (e) {
      renderPreviewEmbyTasks({result: {status: 'failed', message: e.message || 'Task status unavailable'}});
    }
  }

  async function runPreviewExtraction() {
    const button = byId('previewRunExtractionButton');
    if (button) button.disabled = true;
    setPreviewMessage('Requesting Emby thumbnail extraction', 'Emby will handle the actual BIF generation.');
    try {
      const res = await fetch('/api/maintenance/video-previews/emby/run-extraction', {method: 'POST'});
      const data = await readJsonResponse(res);
      renderPreviewEmbyTasks(data.tasks || {});
      const result = data.result || {};
      if (!res.ok) {
        setPreviewMessage('Emby thumbnail extraction could not start', result.message || data.error || '');
        return;
      }
      setPreviewMessage('Emby thumbnail extraction started', result.message || '');
      refreshPreviewTasks();
    } catch (e) {
      setPreviewMessage('Emby thumbnail extraction could not start', e.message || '');
    } finally {
      if (button) button.disabled = false;
    }
  }

  function embyOpsVisible() {
    return !document.hidden && byId('tab-emby-operations')?.classList.contains('active');
  }

  function triggerLabel(trigger) {
    if (!trigger || typeof trigger !== 'object') return '';
    if (trigger.Type === 'DailyTrigger' && trigger.TimeOfDayTicks != null) return 'Daily';
    if (trigger.Type === 'IntervalTrigger' && trigger.IntervalTicks != null) return 'Interval';
    return trigger.DayOfWeek || trigger.SystemEvent || trigger.Type || 'Configured';
  }

  function renderEmbyOperations(data) {
    const tasks = data?.tasks || [];
    if (byId('embyOpsConnection')) byId('embyOpsConnection').textContent = data?.status || 'unavailable';
    if (byId('embyOpsRunning')) byId('embyOpsRunning').textContent = String(data?.running_count || 0);
    if (byId('embyOpsFailed')) byId('embyOpsFailed').textContent = String(data?.failed_count || 0);
    if (byId('embyOpsChecked')) byId('embyOpsChecked').textContent = formatDateLabel(data?.checked_at, 'Never');
    if (byId('embyOpsMessage')) {
      byId('embyOpsMessage').innerHTML = `<i class="bi bi-info-circle" aria-hidden="true"></i><div>${escapeHtml(data?.message || 'Task status unavailable.')}</div>`;
    }
    const rows = byId('embyOpsTaskRows');
    if (!rows) return;
    if (!tasks.length) {
      rows.innerHTML = '<tr><td colspan="7" class="text-muted text-center py-4">No scheduled tasks available.</td></tr>';
      return;
    }
    rows.innerHTML = tasks.map(task => {
      const last = task.last_result || {};
      const lastText = last.status ? `${last.status}${last.end_time ? ` · ${formatDateLabel(last.end_time, '')}` : ''}` : 'Never';
      const error = last.error_message ? `<div class="text-danger small">${escapeHtml(last.error_message)}</div>` : '';
      const triggers = (task.triggers || []).map(triggerLabel).filter(Boolean).join(', ') || 'Manual';
      let action = '<span class="text-muted small">Read only</span>';
      if (task.can_start) action = `<button class="btn btn-outline-primary btn-sm" type="button" data-emby-task-start="${escapeHtml(task.id)}">Start</button>`;
      else if (task.can_cancel) action = `<button class="btn btn-outline-danger btn-sm" type="button" data-emby-task-cancel="${escapeHtml(task.id)}">Cancel</button>`;
      return `<tr>
        <td><div class="fw-semibold">${escapeHtml(task.name || task.id)}</div><div class="text-muted small">${escapeHtml(task.description || task.key || '')}</div></td>
        <td>${escapeHtml(task.category || '—')}</td>
        <td>${escapeHtml(task.state || 'Unknown')}</td>
        <td>${escapeHtml(Math.round(Number(task.progress_percent || 0)))}%</td>
        <td>${escapeHtml(triggers)}</td>
        <td>${escapeHtml(lastText)}${error}</td>
        <td>${action}</td>
      </tr>`;
    }).join('');
  }

  function scheduleEmbyOperationsPoll(runningCount) {
    clearTimeout(embyOperationsTimer);
    if (!embyOpsVisible()) return;
    embyOperationsTimer = setTimeout(() => refreshEmbyOperations(), runningCount ? 2000 : 30000);
  }

  async function refreshEmbyOperations(force = false) {
    if (!embyOpsVisible() && !force) return;
    try {
      const response = await fetch(`/api/emby/tasks${force ? '?force=1' : ''}`);
      const data = await readJsonResponse(response);
      renderEmbyOperations(data);
      const running = Number(data.running_count || 0);
      if (embyOperationsRunning > 0 && running === 0) refreshEmbyActivity();
      embyOperationsRunning = running;
      scheduleEmbyOperationsPoll(running);
    } catch (error) {
      renderEmbyOperations({status: 'unavailable', message: error.message || 'Task status unavailable', tasks: []});
      scheduleEmbyOperationsPoll(0);
    }
  }

  async function refreshEmbyActivity() {
    try {
      const response = await fetch('/api/emby/activity?limit=20');
      const data = await readJsonResponse(response);
      if (byId('embyOpsActivityMessage')) byId('embyOpsActivityMessage').textContent = data.message || '';
      const rows = byId('embyOpsActivityRows');
      if (!rows) return;
      const entries = data.entries || [];
      rows.innerHTML = entries.length ? entries.map(entry => `<tr><td>${escapeHtml(formatDateLabel(entry.date, 'Unknown'))}</td><td>${escapeHtml(entry.severity || 'Info')}</td><td>${escapeHtml(entry.name || '')}</td><td>${escapeHtml(entry.type || '')}</td></tr>`).join('') : '<tr><td colspan="4" class="text-muted text-center py-4">No task-related activity found.</td></tr>';
    } catch (error) {
      if (byId('embyOpsActivityMessage')) byId('embyOpsActivityMessage').textContent = error.message || 'Task activity unavailable.';
    }
  }

  async function controlEmbyTask(taskId, action) {
    if (action === 'cancel' && !window.confirm('Ask Emby to cancel thumbnail extraction?')) return;
    try {
      const response = await fetch(`/api/emby/tasks/${encodeURIComponent(taskId)}/${action}`, {method: 'POST'});
      const data = await readJsonResponse(response);
      if (!response.ok) throw new Error(data.message || data.error || `Task ${action} failed`);
      if (byId('embyOpsMessage')) byId('embyOpsMessage').innerHTML = `<i class="bi bi-info-circle" aria-hidden="true"></i><div>${escapeHtml(data.message || 'Emby accepted the request.')}</div>`;
      setTimeout(() => refreshEmbyOperations(true), 500);
    } catch (error) {
      if (byId('embyOpsMessage')) byId('embyOpsMessage').innerHTML = `<i class="bi bi-exclamation-triangle" aria-hidden="true"></i><div>${escapeHtml(error.message || 'Task request failed.')}</div>`;
    }
  }

  function setQualityMessage(title, detail) {
    const titleEl = byId('qualityMessageTitle');
    const detailEl = byId('qualityMessageDetail');
    if (titleEl) titleEl.textContent = title || '';
    if (detailEl) detailEl.textContent = detail || '';
  }

  function setQualityProgress(scan) {
    const state = byId('qualityScanState');
    const label = byId('qualityProgressLabel');
    const percent = byId('qualityProgressPercent');
    const bar = byId('qualityProgressBar');
    const bad = byId('qualityBadCount');
    const warnings = byId('qualityWarningCount');
    const pct = Math.max(0, Math.min(100, Math.round(Number(scan?.progress_percent || 0))));
    if (state) state.textContent = scan?.status || 'Idle';
    if (label) label.textContent = scan?.progress_label || 'Choose a folder';
    if (percent) percent.textContent = window.vid2gifProgress.valueLabel(scan);
    window.vid2gifProgress.apply(bar, scan || {progress_percent: pct});
    if (bad) bad.textContent = String(scan?.bad_count || 0);
    if (warnings) warnings.textContent = String(scan?.warning_count || 0);
    const active = Boolean(scan?.active || ['queued', 'running', 'cancelling'].includes(scan?.status || ''));
    const scanButton = byId('qualityScanButton');
    const fullScanButton = byId('qualityFullScanButton');
    const cancelButton = byId('qualityCancelButton');
    const planButton = byId('qualityPlanButton');
    if (scanButton) scanButton.disabled = active;
    if (fullScanButton) fullScanButton.disabled = active;
    if (cancelButton) cancelButton.disabled = !active || scan?.status === 'cancelling';
    if (planButton) planButton.disabled = active || !scan || scan.status !== 'success' || !(scan.repairable_count || 0) || scan?.freshness?.status === 'changed';
  }

  function qualityPageRangeText(page) {
    const total = Number(page?.total || 0);
    if (!total) return '0 of 0';
    const start = Number(page.offset || 0) + 1;
    const end = Math.min(total, Number(page.offset || 0) + Number(page.count || 0));
    return `${start}-${end} of ${total}`;
  }

  function qualityPager(page) {
    if (!page) return '';
    return `<div class="maintenance-pager">` +
      `<div class="text-muted small">${escapeHtml(qualityPageRangeText(page))}${page.large_result ? ' - large result set' : ''}</div>` +
      `<div class="toolbar-row mb-0">` +
      `<button class="btn btn-outline-secondary btn-sm" type="button" data-quality-page="prev"${page.has_previous ? '' : ' disabled'}>Previous</button>` +
      `<button class="btn btn-outline-secondary btn-sm" type="button" data-quality-page="next"${page.has_next ? '' : ' disabled'}>Next</button>` +
      `</div>` +
      `</div>`;
  }

  function qualityStatusBadge(status) {
    if (status === 'bad') return '<span class="badge text-bg-danger">Bad</span>';
    if (status === 'warning') return '<span class="badge text-bg-warning">Warning</span>';
    if (status === 'ok') return '<span class="badge text-bg-success">Passed</span>';
    return `<span class="badge text-bg-secondary">${escapeHtml(status || 'Unknown')}</span>`;
  }

  function qualitySampleSummary(item) {
    const sample = item.sample_summary || {};
    const parts = [
      `${sample.sampled_frames || 0} sampled`,
      `${sample.unique_raw_frames || 0} unique`,
      `${sample.max_repeated_run || 0} max run`
    ];
    if (sample.decode_available) {
      parts.push(`${sample.blank_frames || 0} blank`);
    }
    return parts.join(', ');
  }

  function formatIntervalSeconds(value) {
    if (value === null || value === undefined || value === '') return '';
    return `${value}s`;
  }

  function renderQualityItems(page) {
    const target = byId('qualityItems');
    if (!target) return;
    if (!qualityScan || qualityScan.status !== 'success') {
      target.innerHTML = '<div class="text-muted text-center py-4">BIF quality results will appear here after a scan.</div>';
      return;
    }
    if (!page || !(page.items || []).length) {
      target.innerHTML = `${page ? qualityPager(page) : ''}<div class="text-muted text-center py-4">No BIF files in this view.</div>`;
      return;
    }
    const rows = (page.items || []).map(item => {
      const selectedByCategory = qualitySelectedStatuses.has(item.status) && !qualityExcludedItems.has(item.id);
      const selected = selectedByCategory || qualityIncludedItems.has(item.id);
      return `<tr>` +
      `<td>${item.repairable ? `<input class="form-check-input" type="checkbox" data-quality-file="${escapeHtml(item.id)}" data-quality-status="${escapeHtml(item.status)}" aria-label="Select ${escapeHtml(item.name)}"${selected ? ' checked' : ''}>` : ''}</td>` +
      `<td>${qualityStatusBadge(item.status)}</td>` +
      `<td class="path-cell"><code title="${escapeHtml(item.path)}">${escapeHtml(item.relative_path || item.name)}</code></td>` +
      `<td class="path-cell"><code title="${escapeHtml(item.video_path)}">${escapeHtml(item.video_relative_path || item.video_name || '')}</code></td>` +
      `<td>${escapeHtml(item.confidence || 0)}%</td>` +
      `<td>${escapeHtml(item.frame_count_detail || item.frame_count || 0)}</td>` +
      `<td>${escapeHtml(formatIntervalSeconds(item.interval_seconds))}</td>` +
      `<td>${escapeHtml(qualitySampleSummary(item))}</td>` +
      `<td class="path-cell"><code title="${escapeHtml(item.reason || '')}">${escapeHtml(item.reason || '')}</code></td>` +
      `<td>${escapeHtml(item.size_label || formatSize(item.size_bytes))}</td>` +
      `</tr>`;
    }).join('');
    target.innerHTML =
      `${qualityPager(page)}` +
      `<div class="table-responsive workspace-table-wrap">` +
      `<table class="table table-hover align-middle workspace-table" data-table-id="maintenance-quality-bifs" data-sort-mode="server" data-current-sort="${escapeHtml(page.sort || qualitySort.column)}" data-current-direction="${escapeHtml(page.direction || qualitySort.direction)}">` +
      `<thead><tr><th data-column-id="cleanup" data-resizable="false">Clean up</th><th data-column-id="status" data-sortable="true">Status</th><th data-column-id="bif" data-sortable="true">BIF</th><th data-column-id="video" data-sortable="true">Video</th><th data-column-id="confidence" data-sortable="true" data-sort-type="number">Confidence</th><th data-column-id="frames" data-sortable="true" data-sort-type="number">Frames Actual / Expected</th><th data-column-id="interval" data-sortable="true" data-sort-type="number">Interval</th><th data-column-id="sample">Sample</th><th data-column-id="reason" data-sortable="true">Reason</th><th data-column-id="size" data-sortable="true" data-sort-type="number">Size</th></tr></thead>` +
      `<tbody>${rows}</tbody></table></div>` +
      `${qualityPager(page)}`;
  }

  async function loadQualityItems(offset = qualityPageOffset) {
    if (!qualityScan?.id || qualityScan.status !== 'success') return;
    const status = byId('qualityItemStatus')?.value || 'problem';
    const target = byId('qualityItems');
    if (target) target.innerHTML = '<div class="text-muted text-center py-4">Loading BIF quality results...</div>';
    try {
      const res = await fetch(`/api/maintenance/video-previews/quality/items?scan_id=${encodeURIComponent(qualityScan.id)}&status=${encodeURIComponent(status)}&offset=${encodeURIComponent(offset)}&limit=${encodeURIComponent(qualityPageLimit)}&sort=${encodeURIComponent(qualitySort.column)}&direction=${encodeURIComponent(qualitySort.direction)}`);
      const data = await readJsonResponse(res);
      if (!res.ok) {
        setQualityMessage(data.error || 'BIF quality results unavailable', '');
        return;
      }
      qualityItemsPage = data;
      qualitySort = {column: data.sort || qualitySort.column, direction: data.direction || qualitySort.direction};
      qualityPageOffset = Number(data.offset || 0);
      renderQualityItems(data);
      if (data.large_result) {
        setQualityMessage(`${data.total || 0} BIF results in this view`, `Large result set loaded ${data.limit || qualityPageLimit} items at a time.`);
      }
    } catch (e) {
      setQualityMessage('BIF quality results unavailable', e.message || '');
    }
  }

  function handleQualityScan(scan) {
    qualityScan = scan;
    setQualityProgress(scan);
    const terminal = scan && ['success', 'failed', 'cancelled'].includes(scan.status || '');
    if (!scan) {
      setQualityMessage('No BIF quality scan yet.', '');
    } else if (scan.status === 'success') {
      const workSummary = `${scan.reused_count || 0} reused, ${scan.analyzed_count || 0} analyzed`;
      setQualityMessage(
        `${scan.bad_count || 0} bad BIF file${(scan.bad_count || 0) === 1 ? '' : 's'}`,
        withEmbyCoverage(`${scan.warning_count || 0} warnings, ${scan.ok_count || 0} passed; ${workSummary}`, scan)
      );
      appendEmbySyncNotice('qualityMessageDetail', embySyncFrom(apply));
      appendEmbyNotificationNotice('qualityMessageDetail', notificationFrom(apply));
      if (qualityItemsPage?.scan?.id !== scan.id) {
        loadQualityItems(0);
      }
      if (scan.freshness?.status === 'changed') setQualityMessage('BIF quality results are out of date', 'Library files changed after this scan. Rescan before cleanup.');
    } else if (scan.status === 'failed') {
      setQualityMessage('BIF quality scan failed', scan.error || '');
    } else if (scan.status === 'cancelled') {
      setQualityMessage('BIF quality scan cancelled', '');
    } else {
      setQualityMessage(scan.progress_label || 'Checking BIF quality', 'Large libraries can take a while.');
    }
    if (terminal) {
      stopQualityPolling();
    }
  }

  async function pollQualityScan(scanId) {
    if (!scanId) return;
    try {
      const res = await fetch(`/api/maintenance/video-previews/quality/status?scan_id=${encodeURIComponent(scanId)}`);
      const data = await readJsonResponse(res);
      if (!res.ok) {
        setQualityMessage(data.error || 'BIF quality scan unavailable', '');
        stopQualityPolling();
        return;
      }
      handleQualityScan(data.scan);
    } catch (e) {
      setQualityMessage('BIF quality scan unavailable', e.message || '');
      stopQualityPolling();
    }
  }

  async function refreshQualityStatus() {
    try {
      const res = await fetch('/api/maintenance/video-previews/quality/status');
      const data = await readJsonResponse(res);
      if (res.ok) {
        handleQualityScan(data.scan);
        if (data.scan?.active && !qualityPollTimer) qualityPollTimer = setInterval(() => pollQualityScan(data.scan.id), 1000);
      }
    } catch (_e) {
      // Latest-result hydration is best effort.
    }
  }

  async function startQualityScan(forceFull = false) {
    const path = (byId('previewPath')?.value || config.libRoot || '/library').trim();
    if (!path) {
      setQualityMessage('Choose a folder under the library', '');
      return;
    }
    stopQualityPolling();
    stopQualityApplyPolling();
    qualityItemsPage = null;
    qualityPageOffset = 0;
    qualityPlan = null;
    qualityApply = null;
    qualitySelectedStatuses.clear();
    qualitySelectedStatuses.add('bad');
    qualitySelectedStatuses.add('warning');
    qualityExcludedItems.clear();
    qualityIncludedItems.clear();
    const summary = byId('qualityPlanSummary');
    if (summary) summary.innerHTML = '';
    const applyButton = byId('qualityApplyButton');
    if (applyButton) applyButton.disabled = true;
    const modeLabel = forceFull ? 'full BIF quality scan' : 'BIF change scan';
    setQualityMessage(`Starting ${modeLabel}`, '');
    setQualityProgress({status: 'queued', progress_percent: 0, progress_label: 'Queued', scan_mode: forceFull ? 'full' : 'incremental'});
    try {
      const res = await fetch('/api/maintenance/video-previews/quality/scan', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({path, force_full: forceFull})
      });
      const data = await readJsonResponse(res);
      if (!res.ok) {
        setQualityMessage(data.error || 'BIF quality scan could not start', '');
        return;
      }
      handleQualityScan(data.scan);
      qualityPollTimer = setInterval(() => pollQualityScan(data.scan.id), 1000);
    } catch (e) {
      setQualityMessage('BIF quality scan could not start', e.message || '');
    }
  }

  async function cancelQualityScan() {
    if (!qualityScan?.id) return;
    setQualityMessage('Cancelling BIF quality scan', '');
    try {
      const res = await fetch('/api/maintenance/video-previews/quality/cancel', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({scan_id: qualityScan.id})
      });
      const data = await readJsonResponse(res);
      if (!res.ok) {
        setQualityMessage(data.error || 'BIF quality scan could not be cancelled', '');
        return;
      }
      handleQualityScan(data.scan);
    } catch (e) {
      setQualityMessage('BIF quality scan could not be cancelled', e.message || '');
    }
  }

  function renderQualityPlan(plan) {
    const summary = byId('qualityPlanSummary');
    if (!summary) return;
    summary.innerHTML = renderChangePreview({
      title: 'BIF Repair Plan',
      files: plan.files || [],
      metrics: [
        {label: 'Files affected', value: plan.file_count || 0},
        {label: 'Disk data', value: plan.total_size_label || '0 B'},
        {label: 'Manual review', value: (plan.manual_review || []).length},
        {label: 'Action', value: plan.action === 'delete' ? 'Permanent delete' : 'Quarantine'},
        {label: 'Playback deferred', value: plan.emby_playback?.deferred_count || 0}
      ],
      note: `${plan.action === 'delete' ? 'Selected BIF files will be permanently deleted; source videos are not modified.' : `Selected BIF files will be moved to ${plan.move_root || 'the repair quarantine'}; source videos are not modified.`} ${playbackGuardText(plan.emby_playback)}`,
      changeForFile: file => ({
        operation: plan.action,
        operationLabel: plan.action === 'delete' ? 'Delete' : 'Quarantine',
        source: file.relative_path || file.source_path,
        target: file.destination_path || '',
        detail: `${file.confidence || 0}% confidence${file.reason ? `, ${file.reason}` : ''}`
      })
    });
  }

  async function reviewQualityPlan() {
    if (!qualityScan || qualityScan.status !== 'success') {
      setQualityMessage('Run a BIF quality scan first', '');
      return;
    }
    setQualityMessage('Building BIF repair plan', '');
    try {
      const res = await fetch('/api/maintenance/video-previews/quality/plan', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({
          scan_id: qualityScan.id,
          move_root: byId('qualityMoveRoot')?.value || '',
          operation: byId('qualityAction')?.value || 'quarantine',
          statuses: Array.from(qualitySelectedStatuses),
          excluded_item_ids: Array.from(qualityExcludedItems),
          included_item_ids: Array.from(qualityIncludedItems)
        })
      });
      const data = await readJsonResponse(res);
      if (!res.ok) {
        setQualityMessage(data.error || 'BIF repair plan could not be built', '');
        return;
      }
      qualityPlan = data.plan;
      renderQualityPlan(qualityPlan);
      const applyButton = byId('qualityApplyButton');
      if (applyButton) applyButton.disabled = !qualityPlan.file_count;
      setQualityMessage(
        'Review the BIF repair plan before applying',
        Number(qualityPlan.file_count || 0) >= 100
          ? `${qualityPlan.file_count} BIF files selected. This can take a while and will continue in the background.`
          : (qualityPlan.total_size_label || '')
      );
    } catch (e) {
      setQualityMessage('BIF repair plan could not be built', e.message || '');
    }
  }

  function handleQualityApply(apply) {
    qualityApply = apply;
    const button = byId('qualityApplyButton');
    const running = apply && ['queued', 'running'].includes(apply.status || '');
    if (button) button.disabled = running || !qualityPlan;
    if (!apply) return;
    if (running) {
      const counts = `${apply.processed_count || 0} of ${apply.file_count || 0} BIF files`;
      const detail = `${counts}, ${apply.applied_count || 0} applied, ${apply.missing_count || 0} missing, ${apply.refused_count || 0} refused, ${apply.deferred_count || 0} deferred`;
      setQualityMessage(apply.progress_label || 'BIF repair running', apply.large_operation ? `${detail}. Large repair is running in the background.` : detail);
      return;
    }
    if (apply.status === 'success') {
      stopQualityApplyPolling();
      const result = apply.result || {};
      setQualityMessage(
        `${result.applied_count || apply.applied_count || 0} BIF files processed`,
        `${result.total_applied_label || '0 B'} affected, ${result.missing_count || apply.missing_count || 0} missing, ${result.refused_count || apply.refused_count || 0} refused, ${result.deferred_count || apply.deferred_count || 0} deferred. Run a fresh missing scan before generation.`
      );
      startPreviewScan(byId('previewPath')?.value || previewLastPath);
      qualityPlan = null;
      if (button) button.disabled = true;
      return;
    }
    if (apply.status === 'failed') {
      stopQualityApplyPolling();
      setQualityMessage('BIF repair failed', apply.error || '');
    }
  }

  async function pollQualityApply(applyId) {
    if (!applyId) return;
    try {
      const res = await fetch(`/api/maintenance/video-previews/quality/apply/status?apply_id=${encodeURIComponent(applyId)}`);
      const data = await readJsonResponse(res);
      if (!res.ok) {
        setQualityMessage(data.error || 'BIF repair status unavailable', '');
        stopQualityApplyPolling();
        return;
      }
      handleQualityApply(data.apply);
    } catch (e) {
      setQualityMessage('BIF repair status unavailable', e.message || '');
      stopQualityApplyPolling();
    }
  }

  async function applyQualityPlan() {
    if (!qualityPlan) {
      setQualityMessage('Review a BIF repair plan first', '');
      return;
    }
    const prompt = qualityPlan.action === 'delete'
      ? `Permanently delete ${qualityPlan.file_count} bad/warning BIF file(s), totaling ${qualityPlan.total_size_label || '0 B'}?\n\nThis cannot be undone.`
      : `Move ${qualityPlan.file_count} bad/warning BIF file(s), totaling ${qualityPlan.total_size_label || '0 B'}, to:\n${qualityPlan.move_root || 'the repair quarantine'}?`;
    if (!window.confirm(prompt)) {
      return;
    }
    const button = byId('qualityApplyButton');
    if (button) button.disabled = true;
    setQualityMessage('Applying BIF repair plan', '');
    try {
      const res = await fetch('/api/maintenance/video-previews/quality/apply', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({plan_id: qualityPlan.id})
      });
      const data = await readJsonResponse(res);
      if (!res.ok) {
        setQualityMessage(data.error || 'BIF repair failed', '');
        return;
      }
      handleQualityApply(data.apply);
      stopQualityApplyPolling();
      if (data.apply?.id && ['queued', 'running'].includes(data.apply.status || '')) {
        qualityApplyPollTimer = setInterval(() => pollQualityApply(data.apply.id), 1000);
      } else if (data.apply?.id) {
        pollQualityApply(data.apply.id);
      }
    } catch (e) {
      setQualityMessage('BIF repair failed', e.message || '');
    }
  }

  function setSubtitleMessage(title, detail) {
    const titleEl = byId('subtitleMessageTitle');
    const detailEl = byId('subtitleMessageDetail');
    if (titleEl) titleEl.textContent = title || '';
    if (detailEl) detailEl.textContent = detail || '';
  }

  function ensureSubtitleSelection(scan) {
    if (!scan?.id || subtitleSelection.scanId === scan.id) return;
    let saved = null;
    try { saved = JSON.parse(localStorage.getItem(SUBTITLE_SELECTION_STORAGE_KEY) || 'null'); } catch (_e) {}
    const same = saved?.scanId === scan.id;
    subtitleSelection = {
      scanId: scan.id,
      mode: same && saved.mode === 'explicit' ? 'explicit' : 'all_eligible',
      excluded: new Set(same && Array.isArray(saved.excluded) ? saved.excluded : []),
      selected: new Set(same && Array.isArray(saved.selected) ? saved.selected : []),
      total: Number(scan.actionable_file_count || 0),
    };
    updateSubtitleSelectionControls();
  }

  function subtitleFileIsSelected(fileId) {
    return subtitleSelection.mode === 'all_eligible'
      ? !subtitleSelection.excluded.has(fileId)
      : subtitleSelection.selected.has(fileId);
  }

  function subtitleSelectedCount() {
    return subtitleSelection.mode === 'all_eligible'
      ? Math.max(0, subtitleSelection.total - subtitleSelection.excluded.size)
      : subtitleSelection.selected.size;
  }

  function subtitleSelectionPayload() {
    return subtitleSelection.mode === 'all_eligible'
      ? {mode: 'all_eligible', excluded_file_ids: Array.from(subtitleSelection.excluded)}
      : {mode: 'explicit', file_ids: Array.from(subtitleSelection.selected)};
  }

  function updateSubtitleSelectionControls() {
    const count = subtitleSelectedCount();
    const summary = byId('subtitleSelectionSummary');
    if (summary) summary.textContent = `${count} selected across all result pages`;
    if (byId('subtitlePlanButton')) byId('subtitlePlanButton').disabled = !count;
  }

  function subtitleSelectionChanged() {
    subtitlePlan = null;
    try {
      localStorage.setItem(SUBTITLE_SELECTION_STORAGE_KEY, JSON.stringify({
        scanId: subtitleSelection.scanId,
        mode: subtitleSelection.mode,
        excluded: Array.from(subtitleSelection.excluded),
        selected: Array.from(subtitleSelection.selected),
      }));
    } catch (_e) {}
    if (byId('subtitleApplyButton')) byId('subtitleApplyButton').disabled = true;
    const summary = byId('subtitlePlanSummary');
    if (summary) summary.innerHTML = '';
    updateSubtitleSelectionControls();
  }

  function setSubtitleProgress(scan) {
    const state = byId('subtitleScanState');
    const label = byId('subtitleProgressLabel');
    const percent = byId('subtitleProgressPercent');
    const bar = byId('subtitleProgressBar');
    const missing = byId('subtitleMissingCount');
    const review = byId('subtitleReviewCount');
    const pct = Math.max(0, Math.min(100, Math.round(Number(scan?.progress_percent || 0))));
    if (state) state.textContent = scan?.status || 'Idle';
    if (label) label.textContent = scan?.progress_label || 'Choose a folder';
    if (percent) percent.textContent = window.vid2gifProgress.valueLabel(scan);
    window.vid2gifProgress.apply(bar, scan || {progress_percent: pct});
    if (missing) missing.textContent = String(scan?.missing_count || 0);
    if (review) review.textContent = String(scan?.review_count || 0);
    const active = Boolean(scan?.active || ['queued', 'running', 'cancelling'].includes(scan?.status || ''));
    const missingScanButton = byId('subtitleMissingScanButton');
    const coverageScanButton = byId('subtitleCoverageScanButton');
    const cancelButton = byId('subtitleCancelScanButton');
    if (missingScanButton) missingScanButton.disabled = active;
    if (coverageScanButton) coverageScanButton.disabled = active;
    if (cancelButton) cancelButton.disabled = !active || scan?.status === 'cancelling';
    const planButton = byId('subtitlePlanButton');
    if (planButton && scan?.freshness?.status === 'changed') planButton.disabled = true;
  }

  function subtitlePageRangeText(page) {
    const total = Number(page?.total || 0);
    if (!total) return '0 of 0';
    const start = Number(page.offset || 0) + 1;
    const end = Math.min(total, Number(page.offset || 0) + Number(page.count || 0));
    return `${start}-${end} of ${total}`;
  }

  function subtitlePager(page) {
    if (!page) return '';
    return `<div class="maintenance-pager">` +
      `<div class="text-muted small">${escapeHtml(subtitlePageRangeText(page))}${page.large_result ? ' - large result set' : ''}</div>` +
      `<div class="toolbar-row mb-0">` +
      `<button class="btn btn-outline-secondary btn-sm" type="button" data-subtitle-page="prev"${page.has_previous ? '' : ' disabled'}>Previous</button>` +
      `<button class="btn btn-outline-secondary btn-sm" type="button" data-subtitle-page="next"${page.has_next ? '' : ' disabled'}>Next</button>` +
      `</div>` +
      `</div>`;
  }

  function subtitleStatusBadge(status) {
    const labels = {
      missing: ['Missing', 'text-bg-warning'],
      coverage_review: ['Coverage review', 'text-bg-warning'],
      language_review: ['Language Review', 'text-bg-danger'],
      unknown: ['Unknown', 'text-bg-info'],
      incomplete: ['Likely Incomplete', 'text-bg-danger'],
      ok: ['OK', 'text-bg-success']
    };
    const [label, klass] = labels[status] || [status || 'Unknown', 'text-bg-secondary'];
    return `<span class="badge ${klass}">${escapeHtml(label)}</span>`;
  }

  function subtitleFilesCell(item) {
    const files = item.srt_files || [];
    if (!files.length) return '<span class="text-muted">No matching SRT</span>';
    return files.slice(0, 3).map(file => {
      const code = file.language_code || 'unknown';
      const selectable = Boolean(file.actionable);
      const checked = selectable && subtitleFileIsSelected(file.id) ? ' checked' : '';
      const quality = file.subtitle_quality || {};
      const qualityClass = quality.status === 'complete'
        ? 'text-success'
        : (quality.status === 'likely_incomplete' ? 'text-danger fw-semibold' : 'text-warning');
      const qualityDetail = quality.status
        ? `<div class="small ${qualityClass}">${escapeHtml(quality.coverage_percent != null ? `${quality.coverage_percent}% coverage · ends ${quality.last_timestamp_label || '?'} of ${quality.video_duration_label || '?'} · ${quality.cue_count || 0} cues` : (quality.label || 'Coverage unavailable'))}</div>`
        : '';
      return `<div class="mb-2 d-flex gap-2 align-items-start">` +
        (selectable ? `<input class="form-check-input mt-1" type="checkbox" data-subtitle-file="${escapeHtml(file.id)}" aria-label="Select ${escapeHtml(file.name || 'subtitle')}"${checked}>` : '<span class="form-check-input border-0 mt-1"></span>') +
        `<div>` +
        `<code class="path-cell" title="${escapeHtml(file.path || '')}">${escapeHtml(file.relative_path || file.name || '')}</code>` +
        `<div class="text-muted small">${escapeHtml(code)} · ${escapeHtml(file.size_label || '')}${file.action_reason ? ` · ${escapeHtml(file.action_reason)}` : ''}</div>${qualityDetail}` +
        `</div></div>`;
    }).join('') + (files.length > 3 ? `<div class="text-muted small">${files.length - 3} more subtitle file(s)</div>` : '');
  }

  function subtitleStreamsCell(item) {
    const streams = item.emby_subtitle_streams || [];
    if (!streams.length) {
      const labels = {
        not_checked: 'Not checked',
        partial: 'Stream data unavailable',
        ambiguous: 'Ambiguous media source',
        mismatch: 'Emby index mismatch',
        complete: 'No indexed streams'
      };
      return `<span class="text-muted">${escapeHtml(labels[item.emby_index_status] || 'No indexed streams')}</span>`;
    }
    return streams.slice(0, 5).map(stream => {
      const flags = [stream.is_external ? 'external' : 'embedded', stream.is_forced ? 'forced' : '', stream.is_hearing_impaired ? 'HI' : '', stream.is_default ? 'default' : ''].filter(Boolean).join(', ');
      const language = stream.language_code || stream.display_language || 'unknown';
      return `<div class="small mb-1"><strong>${escapeHtml(language)}</strong> · ${escapeHtml(stream.codec || 'unknown')}<div class="text-muted">${escapeHtml(flags)}</div></div>`;
    }).join('') + (streams.length > 5 ? `<div class="text-muted small">${streams.length - 5} more stream(s)</div>` : '');
  }

  function renderSubtitleItems(page) {
    const target = byId('subtitleItems');
    if (!target) return;
    if (!subtitleScan || subtitleScan.status !== 'success') {
      target.innerHTML = '<div class="text-muted text-center py-4">Subtitle review results will appear here after a scan.</div>';
      return;
    }
    if (!page || !(page.items || []).length) {
      target.innerHTML = `${page ? subtitlePager(page) : ''}<div class="text-muted text-center py-4">No videos in this view.</div>`;
      return;
    }
    const rows = (page.items || []).map(item => {
      const codes = (item.language_codes || []).join(', ') || 'none';
      return `<tr>` +
        `<td>${subtitleStatusBadge(item.status)}</td>` +
        `<td class="path-cell"><code title="${escapeHtml(item.path || '')}">${escapeHtml(item.relative_path || item.name || '')}</code><div class="text-muted small">${escapeHtml(item.size_label || '')}</div></td>` +
        `<td>${subtitleFilesCell(item)}</td>` +
        `<td>${subtitleStreamsCell(item)}</td>` +
        `<td>${escapeHtml(codes)}</td>` +
        `<td>${escapeHtml(item.detail || '')}</td>` +
        `</tr>`;
    }).join('');
    target.innerHTML =
      `${subtitlePager(page)}` +
      `<div class="table-responsive workspace-table-wrap">` +
      `<table class="table table-hover align-middle workspace-table" data-table-id="maintenance-subtitles" data-sort-mode="server" data-current-sort="${escapeHtml(page.sort || subtitleSort.column)}" data-current-direction="${escapeHtml(page.direction || subtitleSort.direction)}">` +
      `<thead><tr><th data-column-id="status" data-sortable="true">Status</th><th data-column-id="video" data-sortable="true">Video</th><th data-column-id="subtitles" data-sortable="true" data-sort-type="number">Select flagged SRTs</th><th data-column-id="streams" data-sortable="true" data-sort-type="number">Emby Streams</th><th data-column-id="language" data-sortable="true">Language</th><th data-column-id="reason" data-sortable="true">Reason</th></tr></thead>` +
      `<tbody>${rows}</tbody></table></div>` +
      `${subtitlePager(page)}`;
  }

  async function loadSubtitleItems(offset = subtitlePageOffset) {
    if (!subtitleScan?.id || subtitleScan.status !== 'success') return;
    const status = byId('subtitleItemStatus')?.value || 'review';
    const query = byId('subtitleSearch')?.value || '';
    const target = byId('subtitleItems');
    if (target) target.innerHTML = '<div class="text-muted text-center py-4">Loading subtitle results...</div>';
    try {
      const params = new URLSearchParams({
        scan_id: subtitleScan.id,
        status,
        offset: String(offset),
        limit: String(subtitlePageLimit),
        q: query,
        sort: subtitleSort.column,
        direction: subtitleSort.direction
      });
      const res = await fetch(`/api/maintenance/subtitles/items?${params.toString()}`);
      const data = await readJsonResponse(res);
      if (!res.ok) {
        setSubtitleMessage(data.error || 'Subtitle results unavailable', '');
        return;
      }
      subtitleItemsPage = data;
      subtitleSort = {column: data.sort || subtitleSort.column, direction: data.direction || subtitleSort.direction};
      subtitlePageOffset = Number(data.offset || 0);
      subtitlePlan = null;
      const planButton = byId('subtitlePlanButton');
      const applyButton = byId('subtitleApplyButton');
      if (planButton) planButton.disabled = !subtitleSelectedCount();
      if (applyButton) applyButton.disabled = true;
      const summary = byId('subtitlePlanSummary');
      if (summary) summary.innerHTML = '';
      renderSubtitleItems(data);
      if (data.large_result) {
        setSubtitleMessage(`${data.total || 0} videos in this view`, `Large result set loaded ${data.limit || subtitlePageLimit} items at a time.`);
      }
    } catch (e) {
      setSubtitleMessage('Subtitle results unavailable', e.message || '');
    }
  }

  function handleSubtitleScan(scan) {
    subtitleScan = scan;
    setSubtitleProgress(scan);
    const terminal = scan && ['success', 'failed', 'cancelled'].includes(scan.status || '');
    if (!scan) {
      setSubtitleMessage('No subtitle scan yet.', '');
    } else if (scan.status === 'success') {
      ensureSubtitleSelection(scan);
      const settings = scan.settings || {};
      const streams = scan.emby_streams || {};
      const coverageMode = scan.mode === 'coverage';
      const streamDetail = ['complete', 'partial'].includes(streams.status)
        ? `Emby streams: ${streams.stream_count || 0}, mismatches: ${streams.index_mismatch_count || 0}.`
        : (streams.message || 'Emby stream details are unavailable.');
      setSubtitleMessage(
        `${coverageMode ? 'Coverage' : 'Missing subtitle'} scan: ${scan.review_count || 0} review item${(scan.review_count || 0) === 1 ? '' : 's'}`,
        withEmbyCoverage(
          coverageMode
            ? `${scan.incomplete_count || 0} likely incomplete, ${scan.coverage_review_count || 0} need coverage review, ${scan.ok_count || 0} complete.`
            : `${scan.missing_count || 0} missing, ${scan.language_review_count || 0} language review, ${scan.unknown_count || 0} unknown. ${streamDetail} Expected: ${(settings.expected_languages || []).join(', ') || 'not set'}`,
          scan
        )
      );
      if (subtitleItemsPage?.scan?.id !== scan.id) {
        loadSubtitleItems(0);
      }
      if (scan.freshness?.status === 'changed') setSubtitleMessage('Subtitle results are out of date', 'Library files changed after this scan. Rescan before quarantine or deletion.');
    } else if (scan.status === 'failed') {
      setSubtitleMessage('Subtitle scan failed', scan.error || '');
    } else if (scan.status === 'cancelled') {
      setSubtitleMessage('Subtitle scan cancelled', '');
    } else {
      setSubtitleMessage(scan.progress_label || 'Scanning subtitles', 'Large libraries can take a while.');
    }
    if (terminal) {
      stopSubtitlePolling();
    }
  }

  async function pollSubtitleScan(scanId) {
    if (!scanId) return;
    try {
      const res = await fetch(`/api/maintenance/subtitles/status?scan_id=${encodeURIComponent(scanId)}`);
      const data = await readJsonResponse(res);
      if (!res.ok) {
        setSubtitleMessage(data.error || 'Subtitle scan unavailable', '');
        stopSubtitlePolling();
        return;
      }
      handleSubtitleScan(data.scan);
    } catch (e) {
      setSubtitleMessage('Subtitle scan unavailable', e.message || '');
      stopSubtitlePolling();
    }
  }

  async function refreshSubtitleStatus() {
    try {
      const res = await fetch('/api/maintenance/subtitles/status');
      const data = await readJsonResponse(res);
      if (res.ok) {
        handleSubtitleScan(data.scan);
      }
    } catch (_e) {
      // Status refresh is best-effort on page load.
    }
  }

  async function startSubtitleScan(mode = subtitleScan?.mode || 'missing') {
    const path = (byId('subtitlePath')?.value || config.libRoot || '/library').trim();
    if (!path) {
      setSubtitleMessage('Choose a folder under the library', '');
      return;
    }
    rememberScanSource(path, 'vid2gif_subtitle_scan_source');
    stopSubtitlePolling();
    subtitleItemsPage = null;
    subtitlePageOffset = 0;
    setSubtitleMessage(mode === 'coverage' ? 'Starting subtitle coverage scan' : 'Starting missing subtitle scan', '');
    setSubtitleProgress({status: 'queued', progress_percent: 0, progress_label: 'Queued'});
    try {
      const res = await fetch('/api/maintenance/subtitles/scan', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({path, mode})
      });
      const data = await readJsonResponse(res);
      if (!res.ok) {
        setSubtitleMessage(data.error || 'Subtitle scan could not start', '');
        return;
      }
      handleSubtitleScan(data.scan);
      subtitlePollTimer = setInterval(() => pollSubtitleScan(data.scan.id), 1000);
    } catch (e) {
      setSubtitleMessage('Subtitle scan could not start', e.message || '');
    }
  }

  async function cancelSubtitleScan() {
    if (!subtitleScan?.id) return;
    setSubtitleMessage('Cancelling subtitle scan', '');
    try {
      const res = await fetch('/api/maintenance/subtitles/cancel', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({scan_id: subtitleScan.id})
      });
      const data = await readJsonResponse(res);
      if (!res.ok) {
        setSubtitleMessage(data.error || 'Subtitle scan could not be cancelled', '');
        return;
      }
      handleSubtitleScan(data.scan);
    } catch (e) {
      setSubtitleMessage('Subtitle scan could not be cancelled', e.message || '');
    }
  }

  function visibleSubtitleFiles() {
    return (subtitleItemsPage?.items || []).flatMap(item => item.srt_files || []);
  }

  function renderSubtitlePlan(plan) {
    const target = byId('subtitlePlanSummary');
    if (!target) return;
    target.innerHTML = renderChangePreview({
      title: 'Subtitle Cleanup Plan',
      files: plan.files || [],
      metrics: [
        {label: 'Subtitle files', value: plan.file_count || 0},
        {label: 'Disk data', value: plan.total_size_label || '0 B'},
        {label: 'Selection', value: `${plan.file_count || 0} across all pages`},
        {label: 'Playback deferred', value: plan.emby_playback?.deferred_count || 0}
      ],
      note: plan.operation === 'delete'
        ? `Deletion is permanent. ${playbackGuardText(plan.emby_playback)}`
        : `Files will be moved under ${plan.quarantine_root || 'the subtitle quarantine'}. ${playbackGuardText(plan.emby_playback)}`,
      changeForFile: file => ({
        operation: plan.operation,
        operationLabel: plan.operation === 'delete' ? 'Delete' : 'Quarantine',
        source: file.relative_path,
        target: file.destination_path || '',
        detail: `${file.language_code || 'unknown'}, ${file.size_label || ''}`
      })
    });
  }

  async function reviewSubtitlePlan() {
    if (!subtitleScan?.id || !subtitleItemsPage) return;
    const visibleIds = visibleSubtitleFiles().filter(file => file.actionable).map(file => file.id);
    try {
      const res = await fetch('/api/maintenance/subtitles/plan', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({
          scan_id: subtitleScan.id,
          operation: byId('subtitleAction')?.value || 'quarantine',
          selection: subtitleSelectionPayload(),
          visible_file_ids: visibleIds,
          selected_file_ids: visibleIds.filter(subtitleFileIsSelected)
        })
      });
      const data = await readJsonResponse(res);
      if (!res.ok) throw new Error(data.error || 'Subtitle plan could not be built');
      subtitlePlan = data.plan;
      renderSubtitlePlan(subtitlePlan);
      byId('subtitleApplyButton').disabled = !subtitlePlan.file_count;
      setSubtitleMessage('Review the subtitle cleanup plan', `${subtitlePlan.file_count} file(s) selected across the scan, ${subtitlePlan.total_size_label || '0 B'}`);
    } catch (e) {
      setSubtitleMessage('Subtitle plan could not be built', e.message || '');
    }
  }

  function stopSubtitleApplyPolling() {
    if (subtitleApplyPollTimer) clearInterval(subtitleApplyPollTimer);
    subtitleApplyPollTimer = null;
  }

  async function pollSubtitleApply(applyId) {
    try {
      const res = await fetch(`/api/maintenance/subtitles/apply/status?apply_id=${encodeURIComponent(applyId)}`);
      const data = await readJsonResponse(res);
      if (!res.ok) throw new Error(data.error || 'Subtitle cleanup status unavailable');
      subtitleApply = data.apply;
      if (['queued', 'running'].includes(subtitleApply.status || '')) {
        setSubtitleMessage(subtitleApply.progress_label || 'Applying subtitle cleanup', `${subtitleApply.processed_count || 0} of ${subtitleApply.file_count || 0}`);
        return;
      }
      stopSubtitleApplyPolling();
      if (subtitleApply.status === 'success') {
        setSubtitleMessage('Subtitle cleanup complete', `${subtitleApply.applied_count || 0} applied, ${subtitleApply.refused_count || 0} refused, ${subtitleApply.deferred_count || 0} deferred`);
        appendEmbySyncNotice('subtitleMessageDetail', embySyncFrom(subtitleApply));
        appendEmbyNotificationNotice('subtitleMessageDetail', notificationFrom(subtitleApply));
        subtitlePlan = null;
        byId('subtitleApplyButton').disabled = true;
        await startSubtitleScan(subtitleScan?.mode || 'missing');
      } else {
        setSubtitleMessage('Subtitle cleanup failed', subtitleApply.error || '');
        appendEmbyNotificationNotice('subtitleMessageDetail', notificationFrom(subtitleApply));
      }
    } catch (e) {
      stopSubtitleApplyPolling();
      setSubtitleMessage('Subtitle cleanup status unavailable', e.message || '');
    }
  }

  async function applySubtitlePlan() {
    if (!subtitlePlan) return;
    const prompt = subtitlePlan.operation === 'delete'
      ? `Permanently delete ${subtitlePlan.file_count} selected subtitle file(s)?\n\nThis cannot be undone.`
      : `Move ${subtitlePlan.file_count} selected subtitle file(s) to quarantine?`;
    if (!window.confirm(prompt)) return;
    byId('subtitleApplyButton').disabled = true;
    try {
      const res = await fetch('/api/maintenance/subtitles/apply', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({plan_id: subtitlePlan.id})
      });
      const data = await readJsonResponse(res);
      if (!res.ok) throw new Error(data.error || 'Subtitle cleanup could not start');
      subtitleApply = data.apply;
      stopSubtitleApplyPolling();
      subtitleApplyPollTimer = setInterval(() => pollSubtitleApply(subtitleApply.id), 1000);
      pollSubtitleApply(subtitleApply.id);
    } catch (e) {
      setSubtitleMessage('Subtitle cleanup could not start', e.message || '');
      byId('subtitleApplyButton').disabled = false;
    }
  }

  function setActorMessage(title, detail) {
    const titleEl = byId('actorMessageTitle');
    const detailEl = byId('actorMessageDetail');
    if (titleEl) titleEl.textContent = title || '';
    if (detailEl) detailEl.textContent = detail || '';
  }

  function setActorProgress(scan) {
    const state = byId('actorScanState');
    const label = byId('actorProgressLabel');
    const percent = byId('actorProgressPercent');
    const bar = byId('actorProgressBar');
    const missing = byId('actorMissingCount');
    const ready = byId('actorReadyCount');
    const pct = Math.max(0, Math.min(100, Math.round(Number(scan?.progress_percent || 0))));
    if (state) state.textContent = scan?.status || 'Idle';
    if (label) label.textContent = scan?.progress_label || 'Choose a folder';
    if (percent) percent.textContent = window.vid2gifProgress.valueLabel(scan);
    window.vid2gifProgress.apply(bar, scan || {progress_percent: pct});
    if (missing) missing.textContent = String(scan?.missing_actor_count || 0);
    if (ready) ready.textContent = String(scan?.ready_count || 0);
    const active = Boolean(scan?.active || ['queued', 'running', 'cancelling'].includes(scan?.status || ''));
    const scanButton = byId('actorScanButton');
    const cancelButton = byId('actorCancelScanButton');
    const planButton = byId('actorPlanButton');
    if (scanButton) scanButton.disabled = active;
    if (cancelButton) cancelButton.disabled = !active || scan?.status === 'cancelling';
    if (planButton) planButton.disabled = active || !scan || scan.status !== 'success' || !(scan.ready_count || 0) || scan?.freshness?.status === 'changed';
  }

  function renderActorEmbyStatus(status) {
    const configured = byId('actorEmbyConfigured');
    const lastTest = byId('actorEmbyLastTest');
    const result = status?.last_test || {};
    if (configured) configured.textContent = status?.configured ? 'Emby: configured' : 'Emby: not configured';
    if (lastTest) lastTest.textContent = `Last test: ${embyResultLabel(result, 'never')}`;
  }

  function actorPageRangeText(page) {
    const total = Number(page?.total || 0);
    if (!total) return '0 of 0';
    const start = Number(page.offset || 0) + 1;
    const end = Math.min(total, Number(page.offset || 0) + Number(page.count || 0));
    return `${start}-${end} of ${total}`;
  }

  function actorPager(page) {
    if (!page) return '';
    return `<div class="maintenance-pager">` +
      `<div class="text-muted small">${escapeHtml(actorPageRangeText(page))}${page.large_result ? ' - large result set' : ''}</div>` +
      `<div class="toolbar-row mb-0">` +
      `<button class="btn btn-outline-secondary btn-sm" type="button" data-actor-page="prev"${page.has_previous ? '' : ' disabled'}>Previous</button>` +
      `<button class="btn btn-outline-secondary btn-sm" type="button" data-actor-page="next"${page.has_next ? '' : ' disabled'}>Next</button>` +
      `</div>` +
      `</div>`;
  }

  function actorStatusBadge(status) {
    const labels = {
      ready: ['Ready', 'text-bg-success'],
      ambiguous: ['Ambiguous', 'text-bg-warning'],
      no_candidate: ['No local image', 'text-bg-secondary'],
      ignored: ['Ignored', 'text-bg-secondary'],
      manual: ['Manual needed', 'text-bg-info'],
      blocked: ['Do not import', 'text-bg-dark'],
      imported: ['Imported', 'text-bg-success'],
      failed: ['Failed', 'text-bg-danger']
    };
    const [label, klass] = labels[status] || [status || 'Unknown', 'text-bg-secondary'];
    return `<span class="badge ${klass}">${escapeHtml(label)}</span>`;
  }

  function actorCandidateCell(item) {
    const candidates = item.candidates || [];
    if (!candidates.length) return '<span class="text-muted">No local image found</span>';
    return candidates.slice(0, 3).map(candidate =>
      `<div class="d-flex align-items-center gap-2 mb-1">` +
      `<img src="${escapeHtml(candidate.preview_url || '')}" alt="" style="width:48px;height:48px;object-fit:cover;border-radius:4px">` +
      `<code class="path-cell" title="${escapeHtml(candidate.path || '')}">${escapeHtml(candidate.relative_path || candidate.name || '')}</code>` +
      `</div>`
    ).join('') + (candidates.length > 3 ? `<div class="text-muted small">${candidates.length - 3} more candidate(s)</div>` : '');
  }

  function actorRelatedCell(item) {
    const videos = item.related_videos || [];
    if (!videos.length) return '<span class="text-muted">No local video path matched</span>';
    const first = videos[0] || {};
    const extra = Number(item.related_video_count || videos.length) - 1;
    return `<code class="path-cell" title="${escapeHtml(first.path || '')}">${escapeHtml(first.relative_path || first.name || '')}</code>` +
      (extra > 0 ? `<div class="text-muted small">${extra} more related video${extra === 1 ? '' : 's'}</div>` : '');
  }

  function ensureActorSelection(scan) {
    if (!scan?.id) return;
    if (actorSelection.scanId === scan.id) {
      actorSelection.total = Number(scan.ready_count || 0);
      updateActorSelectionControls();
      return;
    }
    let saved = null;
    try { saved = JSON.parse(localStorage.getItem(ACTOR_SELECTION_STORAGE_KEY) || 'null'); } catch (_e) {}
    const same = saved?.scanId === scan.id;
    actorSelection = {
      scanId: scan.id,
      mode: same && saved.mode === 'explicit' ? 'explicit' : 'all_eligible',
      excluded: new Set(same && Array.isArray(saved.excluded) ? saved.excluded : []),
      selected: new Set(same && Array.isArray(saved.selected) ? saved.selected : []),
      total: Number(scan.ready_count || 0),
    };
    updateActorSelectionControls();
  }

  function actorItemIsSelected(itemId) {
    return actorSelection.mode === 'all_eligible'
      ? !actorSelection.excluded.has(itemId)
      : actorSelection.selected.has(itemId);
  }

  function actorSelectedCount() {
    return actorSelection.mode === 'all_eligible'
      ? Math.max(0, actorSelection.total - actorSelection.excluded.size)
      : actorSelection.selected.size;
  }

  function actorSelectionPayload() {
    return actorSelection.mode === 'all_eligible'
      ? {mode: 'all_eligible', excluded_item_ids: Array.from(actorSelection.excluded)}
      : {mode: 'explicit', item_ids: Array.from(actorSelection.selected)};
  }

  function updateActorSelectionControls() {
    const count = actorSelectedCount();
    const master = byId('actorSelectAllCheckbox');
    if (master) {
      master.disabled = !actorSelection.total;
      master.checked = Boolean(actorSelection.total) && count === actorSelection.total;
      master.indeterminate = count > 0 && count < actorSelection.total;
    }
    const summary = byId('actorSelectionSummary');
    if (summary) summary.textContent = `${count} of ${actorSelection.total} ready actors selected across all pages`;
    if (byId('actorPlanButton')) byId('actorPlanButton').disabled = !count || actorScan?.freshness?.status === 'changed';
  }

  function actorSelectionChanged() {
    actorPlan = null;
    try {
      localStorage.setItem(ACTOR_SELECTION_STORAGE_KEY, JSON.stringify({
        scanId: actorSelection.scanId,
        mode: actorSelection.mode,
        excluded: Array.from(actorSelection.excluded),
        selected: Array.from(actorSelection.selected),
      }));
    } catch (_e) {}
    if (byId('actorApplyButton')) byId('actorApplyButton').disabled = true;
    const summary = byId('actorPlanSummary');
    if (summary) summary.innerHTML = '';
    updateActorSelectionControls();
  }

  function renderActorItems(page) {
    const target = byId('actorItems');
    if (!target) return;
    if (!actorScan || actorScan.status !== 'success') {
      target.innerHTML = '<div class="text-muted text-center py-4">Actor image results will appear here after a scan.</div>';
      return;
    }
    if (!page || !(page.items || []).length) {
      target.innerHTML = `${page ? actorPager(page) : ''}<div class="text-muted text-center py-4">No actors in this view.</div>`;
      return;
    }
    const rows = (page.items || []).map(item => {
      const checked = actorItemIsSelected(item.id);
      const selectable = item.status === 'ready';
      return `<tr>` +
        `<td><input class="form-check-input" type="checkbox" data-actor-select="${escapeHtml(item.id)}"${checked && selectable ? ' checked' : ''}${selectable ? '' : ' disabled'}></td>` +
        `<td>${actorStatusBadge(item.status)}<div class="fw-semibold mt-1">${escapeHtml(item.name || '')}</div><div class="text-muted small">${escapeHtml(item.person_id || '')}</div></td>` +
        `<td>${actorCandidateCell(item)}</td>` +
        `<td>${actorRelatedCell(item)}</td>` +
        `<td><div class="toolbar-row mb-0">` +
        `<button class="btn btn-outline-secondary btn-sm" type="button" data-actor-exception="manual" data-actor-id="${escapeHtml(item.id)}">Manual</button>` +
        `<button class="btn btn-outline-secondary btn-sm" type="button" data-actor-exception="ignored" data-actor-id="${escapeHtml(item.id)}">Ignore</button>` +
        `<button class="btn btn-outline-secondary btn-sm" type="button" data-actor-exception="blocked" data-actor-id="${escapeHtml(item.id)}">Block</button>` +
        `${item.exception ? `<button class="btn btn-outline-primary btn-sm" type="button" data-actor-exception="clear" data-actor-id="${escapeHtml(item.id)}">Clear</button>` : ''}` +
        `</div></td>` +
        `</tr>`;
    }).join('');
    target.innerHTML =
      `${actorPager(page)}` +
      `<div class="table-responsive workspace-table-wrap">` +
      `<table class="table table-hover align-middle workspace-table" data-table-id="maintenance-actor-images" data-sort-mode="server" data-current-sort="${escapeHtml(page.sort || actorSort.column)}" data-current-direction="${escapeHtml(page.direction || actorSort.direction)}">` +
      `<thead><tr><th data-column-id="import" data-resizable="false">Import</th><th data-column-id="actor" data-sortable="true">Actor</th><th data-column-id="candidate" data-sortable="true">Candidate Image</th><th data-column-id="video" data-sortable="true">Related Video</th><th data-column-id="exception" data-sortable="true">Exception</th></tr></thead>` +
      `<tbody>${rows}</tbody></table></div>` +
      `${actorPager(page)}`;
  }

  async function loadActorItems(offset = actorPageOffset) {
    if (!actorScan?.id || actorScan.status !== 'success') return;
    const status = byId('actorItemStatus')?.value || 'ready';
    const target = byId('actorItems');
    if (target) target.innerHTML = '<div class="text-muted text-center py-4">Loading actor image results...</div>';
    try {
      const res = await fetch(`/api/maintenance/actor-images/items?scan_id=${encodeURIComponent(actorScan.id)}&status=${encodeURIComponent(status)}&offset=${encodeURIComponent(offset)}&limit=${encodeURIComponent(actorPageLimit)}&sort=${encodeURIComponent(actorSort.column)}&direction=${encodeURIComponent(actorSort.direction)}`);
      const data = await readJsonResponse(res);
      if (!res.ok) {
        setActorMessage(data.error || 'Actor image results unavailable', '');
        return;
      }
      actorItemsPage = data;
      actorSort = {column: data.sort || actorSort.column, direction: data.direction || actorSort.direction};
      actorPageOffset = Number(data.offset || 0);
      renderActorItems(data);
      updateActorSelectionControls();
      if (data.large_result) {
        setActorMessage(`${data.total || 0} actors in this view`, `Large result set loaded ${data.limit || actorPageLimit} items at a time.`);
      }
    } catch (e) {
      setActorMessage('Actor image results unavailable', e.message || '');
    }
  }

  function handleActorScan(scan, embyStatus) {
    actorScan = scan;
    setActorProgress(scan);
    renderActorEmbyStatus(embyStatus);
    const terminal = scan && ['success', 'failed', 'cancelled'].includes(scan.status || '');
    if (!scan) {
      setActorMessage('No actor image scan yet.', '');
    } else if (scan.status === 'success') {
      ensureActorSelection(scan);
      setActorMessage(
        `${scan.missing_actor_count || 0} missing actor image${(scan.missing_actor_count || 0) === 1 ? '' : 's'}`,
        withEmbyCoverage(`${scan.ready_count || 0} ready, ${scan.ambiguous_count || 0} ambiguous, ${scan.no_candidate_count || 0} without local images`, scan)
      );
      if (actorItemsPage?.scan?.id !== scan.id) {
        loadActorItems(0);
      }
      if (scan.freshness?.status === 'changed') setActorMessage('Actor image results are out of date', 'Library files changed after this scan. Rescan before importing images.');
    } else if (scan.status === 'failed') {
      setActorMessage('Actor image scan failed', scan.error || '');
    } else if (scan.status === 'cancelled') {
      setActorMessage('Actor image scan cancelled', '');
    } else {
      setActorMessage(scan.progress_label || 'Scanning actor images', 'Large libraries can take a while.');
    }
    if (terminal) {
      stopActorPolling();
    }
  }

  async function pollActorScan(scanId) {
    if (!scanId) return;
    try {
      const res = await fetch(`/api/maintenance/actor-images/status?scan_id=${encodeURIComponent(scanId)}`);
      const data = await readJsonResponse(res);
      if (!res.ok) {
        setActorMessage(data.error || 'Actor image scan unavailable', '');
        stopActorPolling();
        return;
      }
      handleActorScan(data.scan, data.emby_status);
    } catch (e) {
      setActorMessage('Actor image scan unavailable', e.message || '');
      stopActorPolling();
    }
  }

  async function refreshActorStatus() {
    try {
      const res = await fetch('/api/maintenance/actor-images/status');
      const data = await readJsonResponse(res);
      if (res.ok) {
        handleActorScan(data.scan, data.emby_status);
      }
    } catch (_e) {
      // Status refresh is best-effort on page load.
    }
  }

  async function startActorScan() {
    const path = (byId('actorPath')?.value || config.libRoot || '/library').trim();
    if (!path) {
      setActorMessage('Choose a folder under the library', '');
      return;
    }
    rememberScanSource(path, 'vid2gif_actor_scan_source');
    stopActorPolling();
    stopActorApplyPolling();
    actorItemsPage = null;
    actorPageOffset = 0;
    actorPlan = null;
    actorApply = null;
    const summary = byId('actorPlanSummary');
    if (summary) summary.innerHTML = '';
    const applyButton = byId('actorApplyButton');
    if (applyButton) applyButton.disabled = true;
    setActorMessage('Starting actor image scan', '');
    setActorProgress({status: 'queued', progress_percent: 0, progress_label: 'Queued'});
    try {
      const res = await fetch('/api/maintenance/actor-images/scan', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({path})
      });
      const data = await readJsonResponse(res);
      if (!res.ok) {
        setActorMessage(data.error || 'Actor image scan could not start', '');
        return;
      }
      handleActorScan(data.scan);
      actorPollTimer = setInterval(() => pollActorScan(data.scan.id), 1000);
    } catch (e) {
      setActorMessage('Actor image scan could not start', e.message || '');
    }
  }

  async function cancelActorScan() {
    if (!actorScan?.id) return;
    setActorMessage('Cancelling actor image scan', '');
    try {
      const res = await fetch('/api/maintenance/actor-images/cancel', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({scan_id: actorScan.id})
      });
      const data = await readJsonResponse(res);
      if (!res.ok) {
        setActorMessage(data.error || 'Actor image scan could not be cancelled', '');
        return;
      }
      handleActorScan(data.scan);
    } catch (e) {
      setActorMessage('Actor image scan could not be cancelled', e.message || '');
    }
  }

  function renderActorPlan(plan) {
    const summary = byId('actorPlanSummary');
    if (!summary) return;
    const totalBytes = (plan.files || []).reduce((total, file) => total + Number(file.size_bytes || 0), 0);
    summary.innerHTML = renderChangePreview({
      title: 'Actor Image Import Plan',
      files: plan.files || [],
      metrics: [
        {label: 'Images uploaded', value: plan.file_count || 0},
        {label: 'Upload data', value: formatSize(totalBytes)},
        {label: 'Skipped', value: (plan.skipped || []).length},
        {label: 'Target', value: 'Emby primary images'}
      ],
      note: 'Each upload replaces the selected person primary image in Emby. Local candidate files are not modified.',
      changeForFile: file => ({
        operation: 'import',
        operationLabel: 'Import',
        source: file.candidate_relative_path || file.candidate_path || file.candidate_name,
        target: `Emby person: ${file.person_name || file.person_id || 'Unknown'}`,
        detail: file.size_label || formatSize(file.size_bytes)
      })
    });
  }

  async function reviewActorPlan() {
    if (!actorScan || actorScan.status !== 'success') {
      setActorMessage('Run an actor image scan first', '');
      return;
    }
    setActorMessage('Building actor image import plan', '');
    try {
      const res = await fetch('/api/maintenance/actor-images/plan', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({scan_id: actorScan.id, selection: actorSelectionPayload()})
      });
      const data = await readJsonResponse(res);
      if (!res.ok) {
        setActorMessage(data.error || 'Actor image import plan could not be built', '');
        return;
      }
      actorPlan = data.plan;
      renderActorPlan(actorPlan);
      const applyButton = byId('actorApplyButton');
      if (applyButton) applyButton.disabled = !actorPlan.file_count;
      setActorMessage('Review the actor image import plan before applying', `${actorPlan.file_count || 0} image(s) selected across the scan`);
    } catch (e) {
      setActorMessage('Actor image import plan could not be built', e.message || '');
    }
  }

  function handleActorApply(apply) {
    actorApply = apply;
    const button = byId('actorApplyButton');
    const running = apply && ['queued', 'running'].includes(apply.status || '');
    if (button) button.disabled = running || !actorPlan;
    if (!apply) return;
    if (running) {
      setActorMessage(
        apply.progress_label || 'Actor image import running',
        `${apply.imported_count || 0} imported, ${apply.refused_count || 0} refused, ${apply.failed_count || 0} failed`
      );
      return;
    }
    if (apply.status === 'success' || apply.status === 'failed') {
      stopActorApplyPolling();
      const result = apply.result || {};
      setActorMessage(
        apply.status === 'success' ? 'Actor image import complete' : 'Actor image import finished with errors',
        `${result.imported_count || apply.imported_count || 0} imported, ${result.refused_count || apply.refused_count || 0} refused, ${result.failed_count || apply.failed_count || 0} failed`
      );
      appendEmbyNotificationNotice('actorMessageDetail', notificationFrom(apply));
      actorPlan = null;
      if (button) button.disabled = true;
      if (actorScan?.id) pollActorScan(actorScan.id);
    }
  }

  async function pollActorApply(applyId) {
    if (!applyId) return;
    try {
      const res = await fetch(`/api/maintenance/actor-images/apply/status?apply_id=${encodeURIComponent(applyId)}`);
      const data = await readJsonResponse(res);
      if (!res.ok) {
        setActorMessage(data.error || 'Actor image import status unavailable', '');
        stopActorApplyPolling();
        return;
      }
      handleActorApply(data.apply);
    } catch (e) {
      setActorMessage('Actor image import status unavailable', e.message || '');
      stopActorApplyPolling();
    }
  }

  async function applyActorPlan() {
    if (!actorPlan) {
      setActorMessage('Review an actor image import plan first', '');
      return;
    }
    if (!window.confirm(`Upload ${actorPlan.file_count} actor image(s) to Emby?\n\nThis will replace each selected person's primary image. Local candidate files will remain unchanged.`)) {
      return;
    }
    const button = byId('actorApplyButton');
    if (button) button.disabled = true;
    setActorMessage('Starting actor image import', '');
    try {
      const res = await fetch('/api/maintenance/actor-images/apply', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({plan_id: actorPlan.id})
      });
      const data = await readJsonResponse(res);
      if (!res.ok) {
        setActorMessage(data.error || 'Actor image import could not start', '');
        return;
      }
      handleActorApply(data.apply);
      stopActorApplyPolling();
      if (data.apply?.id && ['queued', 'running'].includes(data.apply.status || '')) {
        actorApplyPollTimer = setInterval(() => pollActorApply(data.apply.id), 1000);
      } else if (data.apply?.id) {
        pollActorApply(data.apply.id);
      }
    } catch (e) {
      setActorMessage('Actor image import could not start', e.message || '');
    }
  }

  async function updateActorException(itemId, status) {
    const item = (actorItemsPage?.items || []).find(value => value.id === itemId);
    if (!item) return;
    try {
      const res = await fetch('/api/maintenance/actor-images/exceptions', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({person_id: item.person_id, name: item.name, status})
      });
      const data = await readJsonResponse(res);
      if (!res.ok) {
        setActorMessage(data.error || 'Actor exception could not be saved', '');
        return;
      }
      setActorMessage(status === 'clear' ? 'Actor exception cleared' : 'Actor exception saved', item.name || '');
      if (status !== 'clear') {
        actorSelection.excluded.delete(itemId);
        actorSelection.selected.delete(itemId);
        actorSelectionChanged();
      }
      if (actorScan?.id) {
        pollActorScan(actorScan.id);
        loadActorItems(actorPageOffset);
      }
    } catch (e) {
      setActorMessage('Actor exception could not be saved', e.message || '');
    }
  }

  async function refreshActorLogs() {
    const panel = byId('actorLogPanel');
    const list = byId('actorLogList');
    if (panel) panel.classList.remove('d-none');
    if (!list) return;
    list.innerHTML = '<div class="small text-muted">Loading logs...</div>';
    try {
      const res = await fetch('/api/maintenance/actor-images/logs');
      const data = await readJsonResponse(res);
      if (!res.ok) {
        list.innerHTML = `<div class="small text-danger">${escapeHtml(data.error || 'Logs unavailable')}</div>`;
        return;
      }
      const logs = data.logs || [];
      list.innerHTML = logs.length
        ? logs.map(log => `<button class="btn btn-outline-secondary btn-sm me-2 mb-2" type="button" data-actor-log="${escapeHtml(log.id)}">${escapeHtml(log.created_at || log.id)} · ${escapeHtml(log.type || '')} · ${escapeHtml(log.size_label || '')}</button>`).join('')
        : '<div class="small text-muted">No actor image logs yet.</div>';
    } catch (e) {
      list.innerHTML = `<div class="small text-danger">${escapeHtml(e.message || 'Logs unavailable')}</div>`;
    }
  }

  async function openActorLog(logId) {
    const output = byId('actorLogContent');
    if (!output) return;
    output.classList.remove('d-none');
    output.textContent = 'Loading log...';
    try {
      const res = await fetch(`/api/maintenance/actor-images/logs/${encodeURIComponent(logId)}`);
      const data = await readJsonResponse(res);
      if (!res.ok) {
        output.textContent = data.error || 'Log unavailable';
        return;
      }
      output.textContent = data.log?.content || '';
    } catch (e) {
      output.textContent = e.message || 'Log unavailable';
    }
  }

  function setPosterMessage(title, detail) {
    const titleEl = byId('posterMessageTitle');
    const detailEl = byId('posterMessageDetail');
    if (titleEl) titleEl.textContent = title || '';
    if (detailEl) detailEl.textContent = detail || '';
  }

  function formatDateLabel(value, emptyValue) {
    if (!value) return emptyValue || 'unknown';
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return emptyValue || 'unknown';
    return date.toLocaleString();
  }

  function embyResultLabel(result, emptyValue) {
    if (!result || !result.status) return emptyValue || 'never';
    const date = formatDateLabel(result.checked_at || result.finished_at || result.attempted_at, 'unknown time');
    return `${result.status}: ${date}`;
  }

  function renderEmbyStatus(status) {
    const configured = byId('posterEmbyConfigured');
    const lastTest = byId('posterEmbyLastTest');
    const lastRefresh = byId('posterEmbyLastRefresh');
    const server = byId('posterEmbyServer');
    const message = byId('posterEmbyStatusMessage');
    const lastTestResult = status?.last_test || {};
    const lastRefreshResult = status?.last_refresh || {};

    if (configured) {
      configured.className = `badge ${status?.configured ? 'text-bg-success' : 'text-bg-secondary'}`;
      configured.textContent = status?.configured ? 'Configured' : 'Not configured';
    }
    if (lastTest) {
      lastTest.textContent = `Last test: ${embyResultLabel(lastTestResult, 'never')}`;
    }
    if (lastRefresh) {
      lastRefresh.textContent = `Last targeted sync: ${embyResultLabel(lastRefreshResult, 'never')}`;
    }
    if (server) {
      const serverName = lastTestResult.server_name || '';
      const version = lastTestResult.version || '';
      server.textContent = serverName
        ? `Server: ${serverName}${version ? ` (${version})` : ''}`
        : 'Server: unknown';
    }
    if (message) {
      const detail = lastTestResult.message || lastRefreshResult.message || '';
      message.textContent = detail;
      if (lastTestResult.status) {
        message.className = `scan-estimate-detail mt-1 ${lastTestResult.status === 'failed' ? 'text-danger' : ''}`;
      }
    }
  }

  function applyPosterSettings(settings, force = false) {
    if (!settings) return;
    const enabled = byId('posterAutomationEnabled');
    const scan = byId('posterScanInterval');
    const full = byId('posterFullScanInterval');
    const canApply = element => element && (force || (!posterSettingsDirty.has(element.id) && document.activeElement !== element));
    if (canApply(enabled)) enabled.checked = Boolean(settings.enabled);
    if (canApply(scan)) scan.value = settings.scan_interval_seconds || 900;
    if (canApply(full)) full.value = settings.full_scan_interval_seconds || 86400;
  }

  function posterStatusBadge(status) {
    if (status === 'eligible') return '<span class="badge text-bg-primary">Ready</span>';
    if (status === 'already_landscape') return '<span class="badge text-bg-success">Already landscape</span>';
    if (status === 'missing') return '<span class="badge text-bg-warning">Missing poster</span>';
    if (status === 'ambiguous' || status === 'unreadable' || status === 'unsafe') return `<span class="badge text-bg-warning">${escapeHtml(status.charAt(0).toUpperCase() + status.slice(1))}</span>`;
    if (status === 'updated') return '<span class="badge text-bg-success">Updated</span>';
    if (status === 'already_matching') return '<span class="badge text-bg-secondary">Matched</span>';
    if (status === 'missing_poster') return '<span class="badge text-bg-warning">Missing poster</span>';
    if (status === 'error' || status === 'failed') return '<span class="badge text-bg-danger">Error</span>';
    return `<span class="badge text-bg-secondary">${escapeHtml(status || 'Skipped')}</span>`;
  }

  function ensurePosterSelection(scan) {
    if (!scan?.id || posterSelection.scanId === scan.id) return;
    let saved = null;
    try { saved = JSON.parse(localStorage.getItem(POSTER_SELECTION_STORAGE_KEY) || 'null'); } catch (_e) {}
    const same = saved?.scanId === scan.id;
    posterSelection = {
      scanId: scan.id,
      mode: same && saved.mode === 'explicit' ? 'explicit' : 'all_eligible',
      excluded: new Set(same && Array.isArray(saved.excluded) ? saved.excluded : []),
      selected: new Set(same && Array.isArray(saved.selected) ? saved.selected : []),
      total: Number(scan.eligible_count || 0),
    };
    updatePosterSelectionControls();
  }

  function posterItemIsSelected(itemId) {
    return posterSelection.mode === 'all_eligible'
      ? !posterSelection.excluded.has(itemId)
      : posterSelection.selected.has(itemId);
  }

  function posterSelectedCount() {
    return posterSelection.mode === 'all_eligible'
      ? Math.max(0, posterSelection.total - posterSelection.excluded.size)
      : posterSelection.selected.size;
  }

  function posterSelectionPayload() {
    return posterSelection.mode === 'all_eligible'
      ? {mode: 'all_eligible', excluded_item_ids: Array.from(posterSelection.excluded)}
      : {mode: 'explicit', item_ids: Array.from(posterSelection.selected)};
  }

  function updatePosterSelectionControls() {
    const count = posterSelectedCount();
    const master = byId('posterSelectAllCheckbox');
    if (master) {
      master.disabled = !posterSelection.total;
      master.checked = Boolean(posterSelection.total) && count === posterSelection.total;
      master.indeterminate = count > 0 && count < posterSelection.total;
    }
    const summary = byId('posterSelectionSummary');
    if (summary) summary.textContent = `${count} of ${posterSelection.total} safe updates selected across all pages`;
    if (byId('posterPlanButton')) byId('posterPlanButton').disabled = !count || posterScan?.freshness?.status === 'changed';
  }

  function posterSelectionChanged() {
    posterPlan = null;
    try {
      localStorage.setItem(POSTER_SELECTION_STORAGE_KEY, JSON.stringify({
        scanId: posterSelection.scanId,
        mode: posterSelection.mode,
        excluded: Array.from(posterSelection.excluded),
        selected: Array.from(posterSelection.selected),
      }));
    } catch (_e) {}
    if (byId('posterApplyButton')) byId('posterApplyButton').disabled = true;
    if (byId('posterPlanSummary')) byId('posterPlanSummary').innerHTML = '';
    updatePosterSelectionControls();
  }

  function setPosterProgress(scan) {
    const state = byId('posterScanState');
    const label = byId('posterProgressLabel');
    const percent = byId('posterProgressPercent');
    const bar = byId('posterProgressBar');
    const ready = byId('posterReadyCount');
    const issues = byId('posterIssueCount');
    const pct = Math.max(0, Math.min(100, Math.round(Number(scan?.progress_percent || 0))));
    if (state) state.textContent = scan?.status || 'Idle';
    if (label) label.textContent = scan?.progress_label || 'Choose a folder';
    if (percent) percent.textContent = window.vid2gifProgress.valueLabel(scan);
    window.vid2gifProgress.apply(bar, scan || {progress_percent: pct});
    if (ready) ready.textContent = String(scan?.eligible_count || 0);
    if (issues) {
      issues.textContent = String(
        Number(scan?.missing_count || 0)
        + Number(scan?.ambiguous_count || 0)
        + Number(scan?.unreadable_count || 0)
        + Number(scan?.unsafe_count || 0)
      );
    }
    const active = Boolean(scan?.active || ['queued', 'running', 'cancelling'].includes(scan?.status || ''));
    if (byId('posterRunButton')) byId('posterRunButton').disabled = active;
    if (byId('posterCancelScanButton')) {
      byId('posterCancelScanButton').disabled = !active || scan?.status === 'cancelling';
    }
    if (byId('posterPlanButton') && scan?.freshness?.status === 'changed') {
      byId('posterPlanButton').disabled = true;
    }
  }

  function renderPosterItems(page) {
    const wrap = byId('posterRecentItems');
    if (!wrap) return;
    const items = page?.items || [];
    const rows = items.length ? items.map(item =>
      `<tr>` +
      `<td><input class="form-check-input" type="checkbox" data-poster-item="${escapeHtml(item.id)}"${posterItemIsSelected(item.id) && item.eligible ? ' checked' : ''}${item.eligible ? '' : ' disabled'} aria-label="Select poster update"></td>` +
      `<td>${posterStatusBadge(item.status)}</td>` +
      `<td class="path-cell"><code title="${escapeHtml(item.source)}">${escapeHtml(item.source)}</code></td>` +
      `<td class="path-cell"><code title="${escapeHtml(item.poster)}">${escapeHtml(item.poster)}</code></td>` +
      `<td>${escapeHtml(item.message || '')}</td>` +
      `</tr>`
    ).join('') : '<tr><td colspan="5" class="text-muted text-center py-4">No poster analysis results in this view.</td></tr>';
    const start = page?.total ? Number(page.offset || 0) + 1 : 0;
    const end = Math.min(Number(page?.total || 0), Number(page?.offset || 0) + Number(page?.count || 0));
    wrap.innerHTML =
      `<div class="maintenance-pager"><div class="text-muted small">${start}-${end} of ${Number(page?.total || 0)}</div><div class="toolbar-row mb-0"><button class="btn btn-outline-secondary btn-sm" type="button" data-poster-page="prev"${page?.has_previous ? '' : ' disabled'}>Previous</button><button class="btn btn-outline-secondary btn-sm" type="button" data-poster-page="next"${page?.has_next ? '' : ' disabled'}>Next</button></div></div>` +
      `<table class="table table-hover align-middle workspace-table" data-table-id="maintenance-posters" data-sort-mode="server" data-current-sort="${escapeHtml(page.sort || posterSort.column)}" data-current-direction="${escapeHtml(page.direction || posterSort.direction)}">` +
      `<thead><tr><th data-column-id="apply" data-resizable="false">Apply</th><th data-column-id="status" data-sortable="true">Status</th><th data-column-id="background" data-sortable="true">Background</th><th data-column-id="poster" data-sortable="true">Poster</th><th data-column-id="detail" data-sortable="true">Detail</th></tr></thead>` +
      `<tbody>${rows}</tbody></table>`;
    updatePosterSelectionControls();
  }

  async function loadPosterItems(offset = 0) {
    if (!posterScan?.id || posterScan.status !== 'success') return;
    try {
      const status = byId('posterItemStatus')?.value || 'all';
      const search = byId('posterSearch')?.value.trim() || '';
      const res = await fetch(`/api/maintenance/landscape-posters/items?scan_id=${encodeURIComponent(posterScan.id)}&offset=${encodeURIComponent(offset)}&limit=${encodeURIComponent(posterPageLimit)}&status=${encodeURIComponent(status)}&search=${encodeURIComponent(search)}&sort=${encodeURIComponent(posterSort.column)}&direction=${encodeURIComponent(posterSort.direction)}`);
      const data = await readJsonResponse(res);
      if (!res.ok) throw new Error(data.error || 'Poster results unavailable');
      posterItemsPage = data;
      posterPageOffset = Number(data.offset || 0);
      posterSort = {column: data.sort || posterSort.column, direction: data.direction || posterSort.direction};
      posterPlan = null;
      if (byId('posterApplyButton')) byId('posterApplyButton').disabled = true;
      if (byId('posterPlanSummary')) byId('posterPlanSummary').innerHTML = '';
      renderPosterItems(data);
    } catch (e) {
      setPosterMessage('Poster results unavailable', e.message || '');
    }
  }

  function renderPosterStatus(data) {
    const settings = data?.settings || {};
    const current = data?.current_run;
    const last = current || data?.last_run;
    const counters = last?.counters || {};
    const analysis = data?.analysis_scan || null;
    posterScan = analysis;
    setPosterProgress(analysis);
    const automation = byId('posterAutomationState');
    const lastRun = byId('posterLastRun');
    const nextRun = byId('posterNextRun');
    const lastResult = byId('posterLastResult');
    if (automation) {
      automation.textContent = settings.enabled
        ? (current ? `Running ${current.mode || ''}`.trim() : 'Enabled')
        : 'Disabled';
    }
    if (lastRun) {
      lastRun.textContent = `Last run: ${formatDateLabel(last?.finished_at || last?.started_at, 'never')}`;
    }
    if (nextRun) {
      nextRun.textContent = `Next run: ${formatDateLabel(data?.scheduler?.next_run_at, settings.enabled ? 'pending' : 'disabled')}`;
    }
    if (lastResult) {
      lastResult.textContent =
        `Updated: ${counters.updated || 0}, matched: ${counters.already_matching || 0}, errors: ${counters.errors || 0}`;
    }
    if (analysis?.active) {
      setPosterMessage(analysis.progress_label || 'Analyzing poster artwork', analysis.path || '');
    } else if (analysis?.status === 'success') {
      ensurePosterSelection(analysis);
      const stale = analysis.freshness?.status === 'changed';
      setPosterMessage(stale ? 'Poster analysis is out of date' : `${analysis.eligible_count || 0} poster updates ready`, stale ? 'Library artwork changed after this scan. Rescan before applying updates.' : withEmbyCoverage(`${analysis.already_landscape_count || 0} already landscape, ${analysis.ambiguous_count || 0} ambiguous`, analysis));
      if (posterItemsPage?.scan?.id !== analysis.id) loadPosterItems(0);
    } else if (analysis?.status === 'cancelled') {
      setPosterMessage('Landscape poster scan cancelled', analysis.path || '');
    } else if (analysis?.status === 'failed') {
      setPosterMessage('Landscape poster scan failed', analysis.error || '');
    } else if (!current && settings.enabled) {
      setPosterMessage('Landscape poster automation is enabled', `Incremental interval: ${settings.scan_interval_label || ''}`);
    } else if (current) {
      setPosterMessage(current.progress_label || 'Landscape poster run active', current.path || '');
    } else if (last) {
      const emby = last.emby_refresh || {};
      setPosterMessage(last.progress_label || 'Latest landscape poster run complete', emby.message || '');
    } else {
      setPosterMessage('No landscape poster scan yet', 'Choose a scan source, then run a manual scan.');
    }
    renderEmbyStatus(data?.emby_status);
    appendEmbySyncNotice('posterMessageDetail', last?.emby_sync || null);
    appendEmbyNotificationNotice('posterMessageDetail', notificationFrom(last));
  }

  async function refreshPosterStatus() {
    try {
      const res = await fetch('/api/maintenance/landscape-posters/status');
      const data = await readJsonResponse(res);
      if (!res.ok) {
        setPosterMessage(data.error || 'Landscape poster status unavailable', '');
        return;
      }
      applyPosterSettings(data.settings);
      posterSettingsLoaded = true;
      renderPosterStatus(data);
      clearTimeout(posterPollTimer);
      if (!document.hidden && (data.analysis_scan?.active || data.current_run?.status === 'running')) {
        posterPollTimer = setTimeout(refreshPosterStatus, 1000);
      }
    } catch (e) {
      setPosterMessage('Landscape poster status unavailable', e.message || '');
    }
  }

  function setPosterSettingsSaveState(state, message) {
    const target = byId('posterSettingsSaveState');
    if (!target) return;
    const icon = state === 'saving' ? 'bi-cloud-arrow-up' : state === 'error' ? 'bi-exclamation-triangle' : 'bi-cloud-check';
    target.className = `settings-save-state ${state === 'error' ? 'text-danger' : 'text-muted'}`;
    target.innerHTML = `<i class="bi ${icon}" aria-hidden="true"></i><span>${escapeHtml(message)}</span>`;
  }

  function posterSettingPayload(element) {
    if (!element?.checkValidity()) return null;
    if (element.id === 'posterAutomationEnabled') return {enabled: element.checked};
    if (element.id === 'posterScanInterval') return {scan_interval_seconds: Number(element.value)};
    if (element.id === 'posterFullScanInterval') return {full_scan_interval_seconds: Number(element.value)};
    return null;
  }

  function savePosterSettings(element) {
    const payload = posterSettingPayload(element);
    if (!payload) {
      posterSettingsFailures.add(element?.id || 'validation');
      setPosterSettingsSaveState('error', 'Not saved: check the highlighted value');
      return;
    }
    if (!Object.keys(payload).length) return;
    posterSettingsDirty.add(element.id);
    const generation = (posterSettingGenerations.get(element.id) || 0) + 1;
    posterSettingGenerations.set(element.id, generation);
    posterSettingsPending += 1;
    setPosterSettingsSaveState('saving', 'Saving changes');
    posterSettingsSaveChain = posterSettingsSaveChain.then(async () => {
      try {
        const res = await fetch('/api/maintenance/landscape-posters/settings', {
          method: 'PATCH',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify(payload)
        });
        const data = await readJsonResponse(res);
        if (!res.ok) throw new Error(data.error || 'Settings could not be saved');
        posterSettingsFailures.delete(element.id);
        posterSettingsFailures.delete('validation');
        if (posterSettingGenerations.get(element.id) === generation) posterSettingsDirty.delete(element.id);
        applyPosterSettings(data.settings);
        posterSettingsLoaded = true;
        renderPosterStatus(data.status);
      } catch (e) {
        posterSettingsFailures.add(element.id);
        setPosterSettingsSaveState('error', `Not saved: ${e.message || 'request failed'}`);
      } finally {
        posterSettingsPending = Math.max(0, posterSettingsPending - 1);
        if (!posterSettingsPending && !posterSettingsFailures.size) setPosterSettingsSaveState('saved', 'All changes saved');
      }
    });
  }

  async function runLandscapePosters() {
    const button = byId('posterRunButton');
    const path = (byId('posterPath')?.value || '').trim();
    if (!path) {
      setPosterMessage('Choose a folder under the library', '');
      return;
    }
    rememberScanSource(path, 'vid2gif_poster_scan_source');
    if (button) button.disabled = true;
    posterItemsPage = null;
    posterPageOffset = 0;
    posterPlan = null;
    setPosterMessage('Starting poster analysis', path);
    setPosterProgress({status: 'queued', progress_percent: 0, progress_label: 'Queued', active: true});
    try {
      const res = await fetch('/api/maintenance/landscape-posters/scan', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({path})
      });
      const data = await readJsonResponse(res);
      if (!res.ok) {
        setPosterMessage(data.error || 'Poster analysis could not start', '');
        return;
      }
      posterScan = data.scan;
      if (byId('posterPath') && data.scan?.path) byId('posterPath').value = data.scan.path;
      setPosterProgress(data.scan);
      setPosterMessage(data.scan?.progress_label || 'Poster analysis queued', data.scan?.path || '');
      clearTimeout(posterPollTimer);
      posterPollTimer = setTimeout(refreshPosterStatus, 500);
    } catch (e) {
      setPosterMessage('Poster analysis could not start', e.message || '');
    } finally {
      if (button && !posterScan?.active) button.disabled = false;
    }
  }

  async function cancelLandscapePosterScan() {
    if (!posterScan?.id || !posterScan?.active) return;
    if (byId('posterCancelScanButton')) byId('posterCancelScanButton').disabled = true;
    setPosterMessage('Cancelling poster analysis', posterScan.path || '');
    try {
      const res = await fetch('/api/maintenance/landscape-posters/scan/cancel', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({scan_id: posterScan.id})
      });
      const data = await readJsonResponse(res);
      if (!res.ok) throw new Error(data.error || 'Poster scan could not be cancelled');
      posterScan = data.scan;
      setPosterProgress(posterScan);
      clearTimeout(posterPollTimer);
      posterPollTimer = setTimeout(refreshPosterStatus, 250);
    } catch (e) {
      setPosterMessage('Poster scan could not be cancelled', e.message || '');
      setPosterProgress(posterScan);
    }
  }

  async function reviewPosterPlan() {
    if (!posterScan?.id || !posterItemsPage || !posterSelectedCount()) return;
    try {
      const res = await fetch('/api/maintenance/landscape-posters/plan', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({scan_id: posterScan.id, selection: posterSelectionPayload()})
      });
      const data = await readJsonResponse(res);
      if (!res.ok) throw new Error(data.error || 'Poster update plan could not be built');
      posterPlan = data.plan;
      byId('posterPlanSummary').innerHTML = `<div class="scan-estimate"><i class="bi bi-clipboard-check" aria-hidden="true"></i><div><strong>${escapeHtml(posterPlan.file_count)} poster update${posterPlan.file_count === 1 ? '' : 's'} ready to apply</strong><div class="scan-estimate-detail">Across all result pages, each portrait will be renamed to its <code>-poster-backup</code> filename before the landscape poster is installed.</div></div></div>`;
      byId('posterApplyButton').disabled = false;
    } catch (e) {
      setPosterMessage('Poster update review failed', e.message || '');
    }
  }

  async function applyPosterPlan() {
    if (!posterPlan?.id || !window.confirm(`Apply ${posterPlan.file_count} selected poster update${posterPlan.file_count === 1 ? '' : 's'}?`)) return;
    byId('posterApplyButton').disabled = true;
    try {
      const res = await fetch('/api/maintenance/landscape-posters/apply', {
        method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({plan_id: posterPlan.id})
      });
      const data = await readJsonResponse(res);
      if (!res.ok) throw new Error(data.error || 'Poster updates could not start');
      posterApply = data.apply;
      setPosterMessage(posterApply.progress_label || 'Applying poster updates', 'Verified backups and rollback protection remain active.');
      const poll = async () => {
        const statusRes = await fetch(`/api/maintenance/landscape-posters/apply/status?apply_id=${encodeURIComponent(posterApply.id)}`);
        const statusData = await readJsonResponse(statusRes);
        posterApply = statusData.apply;
        setPosterMessage(posterApply.progress_label || 'Applying poster updates', posterApply.error || '');
        if (['queued', 'running'].includes(posterApply.status)) setTimeout(poll, 1000);
        else {
          await refreshPosterStatus();
          setPosterMessage(posterApply.progress_label || 'Poster updates complete', posterApply.error || '');
          appendEmbySyncNotice('posterMessageDetail', embySyncFrom(posterApply));
          appendEmbyNotificationNotice('posterMessageDetail', notificationFrom(posterApply));
        }
      };
      setTimeout(poll, 500);
    } catch (e) {
      setPosterMessage('Poster updates could not start', e.message || '');
    }
  }

  function activateMaintenanceTab(hash, updateUrl) {
    const safeHash = maintenanceTabHashes.includes(hash) ? hash : 'overview';
    const button = document.querySelector(`[data-maint-tab-hash="${safeHash}"]`);
    if (!button || !window.bootstrap) return;
    window.bootstrap.Tab.getOrCreateInstance(button).show();
    localStorage.setItem('maintenance_active_tab_v2', safeHash);
    if (updateUrl) {
      history.replaceState(null, '', `#${safeHash}`);
    }
  }

  function initMaintenanceTabs() {
    const requested = location.hash.replace('#', '');
    const saved = localStorage.getItem('maintenance_active_tab_v2');
    activateMaintenanceTab(maintenanceTabHashes.includes(requested) ? requested : (saved || 'overview'), false);
    document.querySelectorAll('[data-maint-tab-hash]').forEach(button => {
      button.addEventListener('shown.bs.tab', event => {
        const hash = event.target.getAttribute('data-maint-tab-hash') || 'overview';
        localStorage.setItem('maintenance_active_tab_v2', hash);
        history.replaceState(null, '', `#${hash}`);
        if (hash === 'emby-operations') {
          refreshEmbyOperations(true);
          refreshEmbyActivity();
        } else {
          clearTimeout(embyOperationsTimer);
        }
      });
    });
    document.querySelectorAll('[data-maint-tab-shortcut]').forEach(link => {
      link.addEventListener('click', event => {
        event.preventDefault();
        activateMaintenanceTab(link.getAttribute('data-maint-tab-shortcut'), true);
      });
    });
    window.addEventListener('hashchange', () => activateMaintenanceTab(location.hash.replace('#', ''), false));
  }

  async function checkMaintenanceFreshness() {
    try {
      const res = await fetch('/api/dashboard/maintenance-scans/freshness', {
        method: 'POST', headers: {'Content-Type': 'application/json'}, body: '{}'
      });
      const data = await readJsonResponse(res);
      if (data.freshness?.active && !document.hidden) {
        clearTimeout(maintenanceFreshnessTimer);
        maintenanceFreshnessTimer = setTimeout(pollMaintenanceFreshness, 1000);
      }
    } catch (_e) {
      // Unknown freshness keeps existing per-file identity checks in force.
    }
  }

  async function pollMaintenanceFreshness() {
    if (document.hidden) return;
    try {
      const res = await fetch('/api/dashboard/maintenance-scans/status');
      const data = await readJsonResponse(res);
      if (data.freshness?.active) {
        maintenanceFreshnessTimer = setTimeout(pollMaintenanceFreshness, 1000);
        return;
      }
      refreshDuplicateStatus();
      refreshPreviewStatus();
      refreshQualityStatus();
      refreshSubtitleStatus();
      refreshActorStatus();
      refreshPosterStatus();
    } catch (_e) {
      // Freshness display will remain at its previous state.
    }
  }

  function initEvents() {
    byId('overviewRefreshButton')?.addEventListener('click', startOverviewScan);
    byId('overviewLoadFoldersButton')?.addEventListener('click', () => loadOverviewFolders(0));
    byId('overviewPageLimit')?.addEventListener('change', event => {
      overviewPageLimit = PAGE_SIZE_OPTIONS.includes(Number(event.target.value)) ? Number(event.target.value) : PAGE_SIZE_DEFAULT;
      try { localStorage.setItem(OVERVIEW_PAGE_SIZE_STORAGE_KEY, String(overviewPageLimit)); } catch (_e) {}
      loadOverviewFolders(0);
    });
    byId('overviewSort')?.addEventListener('change', () => loadOverviewFolders(0));
    byId('overviewDirection')?.addEventListener('change', () => loadOverviewFolders(0));
    byId('overviewSearch')?.addEventListener('input', () => {
      clearTimeout(overviewSearchTimer);
      overviewSearchTimer = setTimeout(() => loadOverviewFolders(0), 250);
    });
    byId('overviewFolderInventory')?.addEventListener('shown.bs.collapse', () => {
      const button = byId('overviewToggleFoldersButton');
      if (button) button.querySelector('span').textContent = 'Hide Subfolders';
      if (!overviewFolderPage) loadOverviewFolders(0);
    });
    byId('overviewFolderInventory')?.addEventListener('hidden.bs.collapse', () => {
      const button = byId('overviewToggleFoldersButton');
      if (button) button.querySelector('span').textContent = 'Show Subfolders';
    });
    byId('overviewFolders')?.addEventListener('click', event => {
      const page = event.target.closest('[data-overview-page]');
      const folder = event.target.closest('[data-overview-folder-toggle]');
      if (page) {
        const direction = page.getAttribute('data-overview-page');
        if (direction === 'next' && overviewFolderPage?.has_next) {
          loadOverviewFolders(overviewFolderPage.next_offset);
        } else if (direction === 'prev' && overviewFolderPage?.has_previous) {
          loadOverviewFolders(overviewFolderPage.previous_offset);
        }
        return;
      }
      if (folder) {
        const folderId = folder.getAttribute('data-overview-folder-toggle') || '';
        if (overviewExpandedFolders.has(folderId)) {
          overviewExpandedFolders.delete(folderId);
        } else {
          overviewExpandedFolders.add(folderId);
        }
        renderOverviewFolders(overviewFolderPage);
      }
    });
    byId('maintenanceBrowseButton')?.addEventListener('click', () => {
      if (maintenanceBrowserIsOpen()) {
        setMaintenanceBrowserOpen(false);
      } else {
        openBrowser(byId('maintenancePath')?.value.trim() || config.libRoot || '/library');
      }
    });
    byId('maintenanceScanButton')?.addEventListener('click', startScan);
    byId('maintenanceCancelScanButton')?.addEventListener('click', cancelScan);
    byId('maintenancePlanButton')?.addEventListener('click', reviewPlan);
    byId('maintenanceApplyButton')?.addEventListener('click', applyPlan);
    byId('duplicateSelectAllCheckbox')?.addEventListener('change', event => {
      duplicateSelection.mode = event.target.checked ? 'all_eligible' : 'explicit';
      duplicateSelection.excluded.clear();
      duplicateSelection.selected.clear();
      groupState.forEach(state => { state.enabled = Boolean(event.target.checked); });
      duplicateSelectionChanged();
      renderGroups();
    });
    byId('duplicateTogglePageButton')?.addEventListener('click', () => {
      const visible = currentGroupsPage?.groups || [];
      const allExpanded = visible.length && visible.every(group => ensureGroupState(group).expanded);
      setCurrentPageExpanded(!allExpanded);
    });
    byId('duplicateReviewFilter')?.addEventListener('change', event => {
      duplicateReviewFilter = ['all', 'attention', 'ready'].includes(event.target.value) ? event.target.value : 'all';
      groupPageOffset = 0;
      loadGroupsPage(0);
    });
    byId('duplicatePageLimit')?.addEventListener('change', event => {
      groupPageLimit = PAGE_SIZE_OPTIONS.includes(Number(event.target.value)) ? Number(event.target.value) : PAGE_SIZE_DEFAULT;
      try { localStorage.setItem(DUPLICATE_PAGE_SIZE_STORAGE_KEY, String(groupPageLimit)); } catch (_e) {}
      groupPageOffset = 0;
      loadGroupsPage(0);
    });
    byId('previewBrowseButton')?.addEventListener('click', () => {
      if (previewBrowserIsOpen()) {
        setPreviewBrowserOpen(false);
      } else {
        openPreviewBrowser(byId('previewPath')?.value.trim() || config.previewScanPath || config.libRoot || '/library');
      }
    });
    byId('previewScanButton')?.addEventListener('click', () => startPreviewScan());
    byId('previewCancelScanButton')?.addEventListener('click', cancelPreviewScan);
    byId('previewVerifyButton')?.addEventListener('click', () => startPreviewScan(previewLastPath || byId('previewPath')?.value || config.libRoot || '/library'));
    byId('previewRefreshTasksButton')?.addEventListener('click', refreshPreviewTasks);
    byId('previewRunExtractionButton')?.addEventListener('click', runPreviewExtraction);
    byId('embyOpsRefreshButton')?.addEventListener('click', () => refreshEmbyOperations(true));
    byId('embyOpsActivityButton')?.addEventListener('click', refreshEmbyActivity);
    byId('embyOpsTaskRows')?.addEventListener('click', event => {
      const start = event.target.closest('[data-emby-task-start]');
      const cancel = event.target.closest('[data-emby-task-cancel]');
      if (start) controlEmbyTask(start.getAttribute('data-emby-task-start'), 'start');
      if (cancel) controlEmbyTask(cancel.getAttribute('data-emby-task-cancel'), 'cancel');
    });
    byId('previewSaveBifSettingsButton')?.addEventListener('click', saveCurrentBifProfile);
    byId('previewUseRecommendationButton')?.addEventListener('click', useBifRecommendation);
    byId('previewGenerationPlanButton')?.addEventListener('click', () => reviewGenerationPlan(false));
    byId('previewGenerationStartButton')?.addEventListener('click', startGeneration);
    byId('previewGenerationCancelButton')?.addEventListener('click', cancelGeneration);
    byId('previewSelectMissingButton')?.addEventListener('click', () => {
      previewSelection.mode = 'all_eligible';
      previewSelection.excluded.clear();
      previewSelection.includedHeld.clear();
      previewSelection.selected.clear();
      previewSelectionChanged();
      renderPreviewItems(previewItemsPage);
    });
    byId('previewDeselectMissingButton')?.addEventListener('click', () => {
      previewSelection.mode = 'explicit';
      previewSelection.excluded.clear();
      previewSelection.includedHeld.clear();
      previewSelection.selected.clear();
      previewSelectionChanged();
      renderPreviewItems(previewItemsPage);
    });
    byId('previewPageLimit')?.addEventListener('change', event => {
      previewPageLimit = PAGE_SIZE_OPTIONS.includes(Number(event.target.value)) ? Number(event.target.value) : PAGE_SIZE_DEFAULT;
      try { localStorage.setItem(PREVIEW_PAGE_SIZE_STORAGE_KEY, String(previewPageLimit)); } catch (_e) {}
      previewPageOffset = 0;
      loadPreviewItems(0);
    });
    byId('previewItemStatus')?.addEventListener('change', () => {
      previewPageOffset = 0;
      loadPreviewItems(0);
    });
    byId('qualityScanButton')?.addEventListener('click', () => startQualityScan(false));
    byId('qualityFullScanButton')?.addEventListener('click', () => startQualityScan(true));
    byId('qualityCancelButton')?.addEventListener('click', cancelQualityScan);
    byId('qualityPlanButton')?.addEventListener('click', reviewQualityPlan);
    byId('qualityApplyButton')?.addEventListener('click', applyQualityPlan);
    [['qualitySelectBadButton', 'bad', true], ['qualityDeselectBadButton', 'bad', false], ['qualitySelectWarningButton', 'warning', true], ['qualityDeselectWarningButton', 'warning', false]].forEach(([id, status, selected]) => {
      byId(id)?.addEventListener('click', () => {
        if (selected) qualitySelectedStatuses.add(status);
        else qualitySelectedStatuses.delete(status);
        qualityExcludedItems.clear();
        qualityIncludedItems.clear();
        qualityPlan = null;
        byId('qualityApplyButton').disabled = true;
        byId('qualityPlanButton').disabled = !qualitySelectedStatuses.size;
        renderQualityItems(qualityItemsPage);
      });
    });
    byId('qualityAction')?.addEventListener('change', () => {
      qualityPlan = null;
      byId('qualityApplyButton').disabled = true;
      const summary = byId('qualityPlanSummary');
      if (summary) summary.innerHTML = '';
    });
    byId('qualityItemStatus')?.addEventListener('change', () => {
      qualityPageOffset = 0;
      loadQualityItems(0);
    });
    byId('qualityPageLimit')?.addEventListener('change', event => {
      qualityPageLimit = PAGE_SIZE_OPTIONS.includes(Number(event.target.value)) ? Number(event.target.value) : PAGE_SIZE_DEFAULT;
      try { localStorage.setItem(QUALITY_PAGE_SIZE_STORAGE_KEY, String(qualityPageLimit)); } catch (_e) {}
      qualityPageOffset = 0;
      loadQualityItems(0);
    });
    byId('subtitleBrowseButton')?.addEventListener('click', () => {
      subtitleFolderPicker?.toggle();
    });
    byId('subtitleMissingScanButton')?.addEventListener('click', () => startSubtitleScan('missing'));
    byId('subtitleCoverageScanButton')?.addEventListener('click', () => startSubtitleScan('coverage'));
    byId('subtitleCancelScanButton')?.addEventListener('click', cancelSubtitleScan);
    byId('subtitlePlanButton')?.addEventListener('click', reviewSubtitlePlan);
    byId('subtitleApplyButton')?.addEventListener('click', applySubtitlePlan);
    byId('subtitleSelectAllButton')?.addEventListener('click', () => {
      visibleSubtitleFiles().filter(file => file.actionable).forEach(file => {
        if (subtitleSelection.mode === 'all_eligible') subtitleSelection.excluded.delete(file.id);
        else subtitleSelection.selected.add(file.id);
      });
      subtitleSelectionChanged();
      renderSubtitleItems(subtitleItemsPage);
    });
    byId('subtitleDeselectAllButton')?.addEventListener('click', () => {
      visibleSubtitleFiles().filter(file => file.actionable).forEach(file => {
        if (subtitleSelection.mode === 'all_eligible') subtitleSelection.excluded.add(file.id);
        else subtitleSelection.selected.delete(file.id);
      });
      subtitleSelectionChanged();
      renderSubtitleItems(subtitleItemsPage);
    });
    byId('subtitleAction')?.addEventListener('change', () => {
      subtitlePlan = null;
      byId('subtitleApplyButton').disabled = true;
      const summary = byId('subtitlePlanSummary');
      if (summary) summary.innerHTML = '';
    });
    byId('subtitleItemStatus')?.addEventListener('change', () => {
      subtitlePageOffset = 0;
      loadSubtitleItems(0);
    });
    byId('subtitlePageLimit')?.addEventListener('change', event => {
      subtitlePageLimit = PAGE_SIZE_OPTIONS.includes(Number(event.target.value)) ? Number(event.target.value) : PAGE_SIZE_DEFAULT;
      try { localStorage.setItem(SUBTITLE_PAGE_SIZE_STORAGE_KEY, String(subtitlePageLimit)); } catch (_e) {}
      subtitlePageOffset = 0;
      loadSubtitleItems(0);
    });
    byId('subtitleSearch')?.addEventListener('input', () => {
      clearTimeout(subtitleSearchTimer);
      subtitleSearchTimer = setTimeout(() => {
        subtitlePageOffset = 0;
        loadSubtitleItems(0);
      }, 250);
    });
    byId('actorBrowseButton')?.addEventListener('click', () => {
      actorFolderPicker?.toggle();
    });
    byId('actorScanButton')?.addEventListener('click', startActorScan);
    byId('actorCancelScanButton')?.addEventListener('click', cancelActorScan);
    byId('actorPlanButton')?.addEventListener('click', reviewActorPlan);
    byId('actorApplyButton')?.addEventListener('click', applyActorPlan);
    byId('actorLogsButton')?.addEventListener('click', refreshActorLogs);
    byId('actorItemStatus')?.addEventListener('change', () => {
      actorPageOffset = 0;
      loadActorItems(0);
    });
    byId('actorPageLimit')?.addEventListener('change', event => {
      actorPageLimit = PAGE_SIZE_OPTIONS.includes(Number(event.target.value)) ? Number(event.target.value) : PAGE_SIZE_DEFAULT;
      try { localStorage.setItem(ACTOR_PAGE_SIZE_STORAGE_KEY, String(actorPageLimit)); } catch (_e) {}
      actorPageOffset = 0;
      loadActorItems(0);
    });
    byId('actorSelectAllCheckbox')?.addEventListener('change', event => {
      actorSelection = {
        ...actorSelection,
        mode: event.target.checked ? 'all_eligible' : 'explicit',
        excluded: new Set(),
        selected: new Set(),
      };
      actorSelectionChanged();
      renderActorItems(actorItemsPage);
    });
    ['posterAutomationEnabled', 'posterScanInterval', 'posterFullScanInterval'].forEach(id => {
      const element = byId(id);
      element?.addEventListener('change', event => {
        clearTimeout(posterSettingInputTimers.get(element));
        savePosterSettings(event.target);
      });
      if (element && ['posterScanInterval', 'posterFullScanInterval'].includes(id)) {
        element.addEventListener('input', event => {
          clearTimeout(posterSettingInputTimers.get(element));
          posterSettingInputTimers.set(element, setTimeout(() => savePosterSettings(event.target), 500));
        });
      }
    });
    byId('posterRunButton')?.addEventListener('click', runLandscapePosters);
    byId('posterCancelScanButton')?.addEventListener('click', cancelLandscapePosterScan);
    byId('posterBrowseButton')?.addEventListener('click', () => posterFolderPicker?.toggle());
    byId('posterPlanButton')?.addEventListener('click', reviewPosterPlan);
    byId('posterApplyButton')?.addEventListener('click', applyPosterPlan);
    byId('posterRefreshButton')?.addEventListener('click', refreshPosterStatus);
    byId('posterPageLimit')?.addEventListener('change', event => {
      posterPageLimit = PAGE_SIZE_OPTIONS.includes(Number(event.target.value)) ? Number(event.target.value) : PAGE_SIZE_DEFAULT;
      try { localStorage.setItem(POSTER_PAGE_SIZE_STORAGE_KEY, String(posterPageLimit)); } catch (_e) {}
      loadPosterItems(0);
    });
    byId('posterItemStatus')?.addEventListener('change', () => loadPosterItems(0));
    byId('posterSearch')?.addEventListener('input', () => {
      clearTimeout(posterSearchTimer);
      posterSearchTimer = setTimeout(() => loadPosterItems(0), 250);
    });
    byId('posterSelectAllCheckbox')?.addEventListener('change', event => {
      posterSelection = {
        ...posterSelection,
        mode: event.target.checked ? 'all_eligible' : 'explicit',
        excluded: new Set(),
        selected: new Set(),
      };
      posterSelectionChanged();
      renderPosterItems(posterItemsPage);
    });
    byId('posterRecentItems')?.addEventListener('change', event => {
      const checkbox = event.target.closest('[data-poster-item]');
      if (!checkbox) return;
      const itemId = checkbox.dataset.posterItem;
      if (posterSelection.mode === 'all_eligible') {
        if (checkbox.checked) posterSelection.excluded.delete(itemId);
        else posterSelection.excluded.add(itemId);
      } else if (checkbox.checked) posterSelection.selected.add(itemId);
      else posterSelection.selected.delete(itemId);
      posterSelectionChanged();
    });
    byId('posterRecentItems')?.addEventListener('click', event => {
      const button = event.target.closest('[data-poster-page]');
      if (!button || !posterItemsPage) return;
      const offset = button.dataset.posterPage === 'next'
        ? Number(posterItemsPage.offset || 0) + Number(posterItemsPage.limit || 10)
        : Math.max(0, Number(posterItemsPage.offset || 0) - Number(posterItemsPage.limit || 10));
      loadPosterItems(offset);
    });
    byId('maintenanceRefreshLogsButton')?.addEventListener('click', refreshMaintenanceLogs);
    byId('maintenanceAction')?.addEventListener('change', () => {
      invalidatePlan();
      renderGroups();
    });

    byId('maintenanceGroups')?.addEventListener('click', event => {
      const page = event.target.closest('[data-maint-page]');
      const expand = event.target.closest('[data-maint-expand]');
      const defaults = event.target.closest('[data-maint-group-defaults]');
      const sidecars = event.target.closest('[data-maint-group-sidecars]');
      if (defaults) {
        resetGroupSuggestedActions(defaults.getAttribute('data-maint-group-defaults'));
        return;
      }
      if (sidecars) {
        setGroupSidecarActions(
          sidecars.getAttribute('data-maint-group'),
          sidecars.getAttribute('data-maint-group-sidecars')
        );
        return;
      }
      if (page) {
        const direction = page.getAttribute('data-maint-page');
        if (direction === 'next' && currentGroupsPage?.has_next) {
          loadGroupsPage(currentGroupsPage.next_offset);
        } else if (direction === 'prev' && currentGroupsPage?.has_previous) {
          loadGroupsPage(currentGroupsPage.previous_offset);
        }
        return;
      }
      if (expand) {
        const groupId = expand.getAttribute('data-maint-expand');
        const group = groupSummaries.get(groupId) || {id: groupId};
        const state = ensureGroupState(group);
        state.expanded = !state.expanded;
        if (state.expanded && !(group.videos || []).length) {
          loadGroupDetails(groupId);
        } else {
          renderGroups();
        }
      }
    });

    byId('maintenanceBrowser')?.addEventListener('click', event => {
      const folder = event.target.closest('[data-maint-folder]');
      const choose = event.target.closest('[data-maint-choose]');
      if (folder) {
        openBrowser(folder.getAttribute('data-maint-folder'));
      } else if (choose) {
        const path = choose.getAttribute('data-maint-choose') || '';
        if (byId('maintenancePath')) byId('maintenancePath').value = path;
        setMaintenanceBrowserOpen(false);
      }
    });

    byId('previewBrowser')?.addEventListener('click', event => {
      const folder = event.target.closest('[data-preview-folder]');
      const choose = event.target.closest('[data-preview-choose]');
      if (folder) {
        openPreviewBrowser(folder.getAttribute('data-preview-folder'));
      } else if (choose) {
        const path = choose.getAttribute('data-preview-choose') || '';
        persistPreviewPath(path)
          .then(() => {
            setPreviewBrowserOpen(false);
            setPreviewMessage('Scan source saved', path);
          })
          .catch(error => setPreviewMessage('Scan source could not be saved', error.message || ''));
      }
    });

    byId('actorBrowser')?.addEventListener('click', event => {
      const folder = event.target.closest('[data-actor-folder]');
      const choose = event.target.closest('[data-actor-choose]');
      if (folder) {
        openActorBrowser(folder.getAttribute('data-actor-folder'));
      } else if (choose) {
        const path = choose.getAttribute('data-actor-choose') || '';
        if (byId('actorPath')) byId('actorPath').value = path;
      }
    });

    byId('subtitleBrowser')?.addEventListener('click', event => {
      const folder = event.target.closest('[data-subtitle-folder]');
      const choose = event.target.closest('[data-subtitle-choose]');
      if (folder) {
        openSubtitleBrowser(folder.getAttribute('data-subtitle-folder'));
      } else if (choose) {
        const path = choose.getAttribute('data-subtitle-choose') || '';
        if (byId('subtitlePath')) byId('subtitlePath').value = path;
      }
    });

    byId('previewItems')?.addEventListener('click', event => {
      const page = event.target.closest('[data-preview-page]');
      if (!page) return;
      const direction = page.getAttribute('data-preview-page');
      if (direction === 'next' && previewItemsPage?.has_next) {
        loadPreviewItems(previewItemsPage.next_offset);
      } else if (direction === 'prev' && previewItemsPage?.has_previous) {
        loadPreviewItems(previewItemsPage.previous_offset);
      }
    });

    byId('previewItems')?.addEventListener('change', event => {
      const checkbox = event.target.closest('[data-preview-generate]');
      if (!checkbox) return;
      const itemId = checkbox.getAttribute('data-preview-generate');
      const item = (previewItemsPage?.items || []).find(candidate => candidate.id === itemId);
      if (previewSelection.mode === 'explicit') {
        if (checkbox.checked) previewSelection.selected.add(itemId);
        else previewSelection.selected.delete(itemId);
      } else if (item?.generation_held) {
        if (checkbox.checked) previewSelection.includedHeld.add(itemId);
        else previewSelection.includedHeld.delete(itemId);
      } else if (checkbox.checked) {
        previewSelection.excluded.delete(itemId);
      } else {
        previewSelection.excluded.add(itemId);
      }
      previewSelectionChanged();
    });

    byId('subtitleItems')?.addEventListener('click', event => {
      const page = event.target.closest('[data-subtitle-page]');
      if (!page) return;
      const direction = page.getAttribute('data-subtitle-page');
      if (direction === 'next' && subtitleItemsPage?.has_next) {
        loadSubtitleItems(subtitleItemsPage.next_offset);
      } else if (direction === 'prev' && subtitleItemsPage?.has_previous) {
        loadSubtitleItems(subtitleItemsPage.previous_offset);
      }
    });

    byId('subtitleItems')?.addEventListener('change', event => {
      const checkbox = event.target.closest('[data-subtitle-file]');
      if (!checkbox) return;
      const fileId = checkbox.getAttribute('data-subtitle-file');
      if (subtitleSelection.mode === 'all_eligible') {
        if (checkbox.checked) subtitleSelection.excluded.delete(fileId);
        else subtitleSelection.excluded.add(fileId);
      } else if (checkbox.checked) subtitleSelection.selected.add(fileId);
      else subtitleSelection.selected.delete(fileId);
      subtitleSelectionChanged();
    });

    byId('actorItems')?.addEventListener('click', event => {
      const page = event.target.closest('[data-actor-page]');
      if (page) {
        const direction = page.getAttribute('data-actor-page');
        if (direction === 'next' && actorItemsPage?.has_next) {
          loadActorItems(actorItemsPage.next_offset);
        } else if (direction === 'prev' && actorItemsPage?.has_previous) {
          loadActorItems(actorItemsPage.previous_offset);
        }
        return;
      }
      const exception = event.target.closest('[data-actor-exception]');
      if (exception) {
        updateActorException(
          exception.getAttribute('data-actor-id'),
          exception.getAttribute('data-actor-exception')
        );
      }
    });

    byId('actorItems')?.addEventListener('change', event => {
      const selected = event.target.closest('[data-actor-select]');
      if (!selected) return;
      const itemId = selected.getAttribute('data-actor-select');
      if (actorSelection.mode === 'all_eligible') {
        if (selected.checked) actorSelection.excluded.delete(itemId);
        else actorSelection.excluded.add(itemId);
      } else if (selected.checked) actorSelection.selected.add(itemId);
      else actorSelection.selected.delete(itemId);
      actorSelectionChanged();
    });

    byId('qualityItems')?.addEventListener('click', event => {
      const page = event.target.closest('[data-quality-page]');
      if (!page) return;
      const direction = page.getAttribute('data-quality-page');
      if (direction === 'next' && qualityItemsPage?.has_next) {
        loadQualityItems(qualityItemsPage.next_offset);
      } else if (direction === 'prev' && qualityItemsPage?.has_previous) {
        loadQualityItems(qualityItemsPage.previous_offset);
      }
    });

    byId('qualityItems')?.addEventListener('change', event => {
      const checkbox = event.target.closest('[data-quality-file]');
      if (!checkbox) return;
      const itemId = checkbox.getAttribute('data-quality-file');
      const status = checkbox.getAttribute('data-quality-status');
      if (checkbox.checked) {
        qualityExcludedItems.delete(itemId);
        if (!qualitySelectedStatuses.has(status)) qualityIncludedItems.add(itemId);
      } else {
        qualityIncludedItems.delete(itemId);
        if (qualitySelectedStatuses.has(status)) qualityExcludedItems.add(itemId);
      }
      qualityPlan = null;
      byId('qualityApplyButton').disabled = true;
    });

    byId('maintenanceGroups')?.addEventListener('change', event => {
      const enabled = event.target.closest('[data-maint-group-enabled]');
      const keep = event.target.closest('[data-maint-keep]');
      const operation = event.target.closest('[data-maint-operation]');
      if (enabled) {
        const groupId = enabled.getAttribute('data-maint-group-enabled');
        const state = ensureGroupState(groupSummaries.get(groupId) || {id: groupId});
        setDuplicateGroupSelected(groupId, enabled.checked);
        if (state) state.enabled = enabled.checked;
        duplicateSelectionChanged();
        renderGroups();
        return;
      }
      if (keep) {
        const groupId = keep.getAttribute('data-maint-keep');
        const state = groupState.get(groupId);
        if (state) {
          state.keepId = keep.value;
          state.projectionPending = true;
          state.dirty = true;
        }
        markGroupDirty(groupId);
        loadGroupDetails(groupId, keep.value);
        return;
      }
      if (operation) {
        const groupId = operation.getAttribute('data-maint-group');
        const fileId = operation.getAttribute('data-maint-operation');
        const state = groupState.get(groupId);
        if (state && fileId) {
          setFileAction(state, fileId, operation.value);
          state.dirty = true;
        }
        markGroupDirty(groupId);
        renderGroups();
      }
    });

    byId('maintenanceLogList')?.addEventListener('click', event => {
      const button = event.target.closest('[data-maint-log]');
      const restore = event.target.closest('[data-maint-restore-preview]');
      if (button) openMaintenanceLog(button.getAttribute('data-maint-log'));
      if (restore) previewDuplicateRestore(restore.getAttribute('data-maint-restore-preview'));
    });
    byId('maintenanceRestoreSummary')?.addEventListener('click', event => {
      const button = event.target.closest('[data-maint-restore-apply]');
      if (button) applyDuplicateRestore(button.getAttribute('data-maint-restore-apply'));
    });

    byId('actorLogList')?.addEventListener('click', event => {
      const button = event.target.closest('[data-actor-log]');
      if (!button) return;
      openActorLog(button.getAttribute('data-actor-log'));
    });
  }

  document.addEventListener('DOMContentLoaded', () => {
    initMaintenanceTabs();
    initFolderPickers();
    initEvents();
    try {
      const savedDuplicatePageLimit = Number(localStorage.getItem(DUPLICATE_PAGE_SIZE_STORAGE_KEY) || PAGE_SIZE_DEFAULT);
      groupPageLimit = PAGE_SIZE_OPTIONS.includes(savedDuplicatePageLimit) ? savedDuplicatePageLimit : PAGE_SIZE_DEFAULT;
      if (byId('duplicatePageLimit')) byId('duplicatePageLimit').value = String(groupPageLimit);
    } catch (_e) {
      groupPageLimit = PAGE_SIZE_DEFAULT;
    }
    try {
      const savedPreviewPageLimit = Number(localStorage.getItem(PREVIEW_PAGE_SIZE_STORAGE_KEY) || PAGE_SIZE_DEFAULT);
      previewPageLimit = PAGE_SIZE_OPTIONS.includes(savedPreviewPageLimit) ? savedPreviewPageLimit : PAGE_SIZE_DEFAULT;
      if (byId('previewPageLimit')) byId('previewPageLimit').value = String(previewPageLimit);
    } catch (_e) {
      previewPageLimit = PAGE_SIZE_DEFAULT;
    }
    [
      ['overviewPageLimit', OVERVIEW_PAGE_SIZE_STORAGE_KEY, value => { overviewPageLimit = value; }],
      ['qualityPageLimit', QUALITY_PAGE_SIZE_STORAGE_KEY, value => { qualityPageLimit = value; }],
      ['subtitlePageLimit', SUBTITLE_PAGE_SIZE_STORAGE_KEY, value => { subtitlePageLimit = value; }],
      ['actorPageLimit', ACTOR_PAGE_SIZE_STORAGE_KEY, value => { actorPageLimit = value; }],
      ['posterPageLimit', POSTER_PAGE_SIZE_STORAGE_KEY, value => { posterPageLimit = value; }],
    ].forEach(([id, key, setter]) => {
      let value = PAGE_SIZE_DEFAULT;
      try {
        const saved = Number(localStorage.getItem(key) || PAGE_SIZE_DEFAULT);
        value = PAGE_SIZE_OPTIONS.includes(saved) ? saved : PAGE_SIZE_DEFAULT;
      } catch (_e) {}
      setter(value);
      if (byId(id)) byId(id).value = String(value);
    });
    document.addEventListener('vid2gif:table-sort', event => {
      const {tableId, column, direction} = event.detail || {};
      if (!column || !direction) return;
      if (tableId === 'maintenance-missing-bifs') {
        previewSort = {column, direction};
        loadPreviewItems(0);
      } else if (tableId === 'maintenance-quality-bifs') {
        qualitySort = {column, direction};
        qualityPlan = null;
        loadQualityItems(0);
      } else if (tableId === 'maintenance-subtitles') {
        subtitleSort = {column, direction};
        subtitlePlan = null;
        loadSubtitleItems(0);
      } else if (tableId === 'maintenance-actor-images') {
        actorSort = {column, direction};
        actorPlan = null;
        loadActorItems(0);
      } else if (tableId === 'maintenance-posters') {
        posterSort = {column, direction};
        posterPlan = null;
        loadPosterItems(0);
      }
    });
    setProgress(null);
    setOverviewProgress(null);
    setPreviewProgress(null);
    setQualityProgress(null);
    setSubtitleProgress(null);
    setActorProgress(null);
    setPosterProgress(null);
    refreshMaintenanceLogs();
    refreshOverviewStatus();
    refreshDuplicateStatus();
    refreshDuplicateApplyStatus();
    refreshPreviewStatus();
    refreshGenerationStatus();
    refreshQualityStatus();
    refreshPreviewTasks();
    if (embyOpsVisible()) {
      refreshEmbyOperations(true);
      refreshEmbyActivity();
    }
    refreshSubtitleStatus();
    refreshActorStatus();
    refreshPosterStatus();
    checkMaintenanceFreshness();
    document.addEventListener('visibilitychange', () => {
      if (document.hidden) {
        clearTimeout(maintenanceFreshnessTimer);
        clearTimeout(embyOperationsTimer);
      } else {
        checkMaintenanceFreshness();
        if (embyOpsVisible()) refreshEmbyOperations(true);
      }
    });
    window.addEventListener('beforeunload', event => {
      clearTimeout(overviewPollTimer);
      clearTimeout(overviewSearchTimer);
      clearTimeout(subtitleSearchTimer);
      stopSubtitlePolling();
      stopSubtitleApplyPolling();
      stopGenerationPolling();
      clearTimeout(posterPollTimer);
      clearTimeout(posterSearchTimer);
      clearTimeout(maintenanceFreshnessTimer);
      clearTimeout(embyOperationsTimer);
      if (posterSettingsPending || posterSettingsFailures.size) {
        event.preventDefault();
        event.returnValue = '';
      }
    });
  });
}());
