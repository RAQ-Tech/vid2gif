(function () {
  const config = window.vid2gifMaintenanceConfig || {};
  const maintenanceTabHashes = ['overview', 'posters', 'duplicates', 'video-previews', 'subtitles', 'actor-images'];
  const overviewExpandedFolders = new Set();
  let overviewFolderPage = null;
  let overviewPageOffset = 0;
  let overviewPageLimit = 25;
  let overviewSearchTimer = null;
  let overviewPollTimer = null;
  const groupState = new Map();
  const groupSummaries = new Map();
  let currentScan = null;
  let currentPlan = null;
  let currentApply = null;
  let currentGroupsPage = null;
  let groupPageOffset = 0;
  let groupPageLimit = 10;
  let pollTimer = null;
  let applyPollTimer = null;
  let previewScan = null;
  let previewPollTimer = null;
  let previewItemsPage = null;
  let previewPageOffset = 0;
  let previewPageLimit = 25;
  let previewSort = {column: 'video', direction: 'asc'};
  let previewLastPath = '';
  const previewSelectedMissing = new Set();
  let previewGenerationPlan = null;
  let previewGenerationRun = null;
  let previewGenerationPollTimer = null;
  let qualityScan = null;
  let qualityPollTimer = null;
  let qualityItemsPage = null;
  let qualityPageOffset = 0;
  let qualityPageLimit = 25;
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
  let subtitlePageLimit = 25;
  let subtitleSort = {column: 'video', direction: 'asc'};
  let subtitleSearchTimer = null;
  const subtitleSelected = new Set();
  let subtitlePlan = null;
  let subtitleApply = null;
  let subtitleApplyPollTimer = null;
  let actorScan = null;
  let actorPollTimer = null;
  let actorItemsPage = null;
  let actorPageOffset = 0;
  let actorPageLimit = 25;
  let actorSort = {column: 'actor', direction: 'asc'};
  let actorPlan = null;
  let actorApply = null;
  let actorApplyPollTimer = null;
  const actorSelected = new Set();
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
  let posterSort = {column: 'background', direction: 'asc'};
  let posterPlan = null;
  let posterApply = null;
  const posterSelected = new Set();
  const groupDetailGenerations = new Map();
  let maintenanceFreshnessTimer = null;

  function byId(id) {
    return document.getElementById(id);
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
    if (percent) percent.textContent = `${pct}%`;
    if (bar) {
      bar.style.width = `${pct}%`;
      bar.parentElement.setAttribute('aria-valuenow', pct);
    }
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
    if (percent) percent.textContent = `${pct}%`;
    if (bar) {
      bar.style.width = `${pct}%`;
      bar.parentElement.setAttribute('aria-valuenow', pct);
    }
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

  function fileDetails(file) {
    if (file.kind === 'video') {
      return file.metadata_label || 'Video';
    }
    const parts = [file.role ? `Accessory: ${file.role}` : 'Accessory'];
    if (file.default_reason) parts.push(file.default_reason);
    return parts.join(' - ');
  }

  function defaultSelected(file) {
    return file.default_selected !== false && file.default_operation !== 'keep';
  }

  function fileOperationOptions(group, file, state, locked = false) {
    if (locked) return '<span class="badge text-bg-secondary">Keep</span>';
    const selected = state.fileOperations?.get(file.id) || 'default';
    const options = file.kind === 'video'
      ? [
        ['default', 'Default cleanup'],
        ['keep', 'Keep']
      ]
      : [
        ['default', `Default: ${file.default_operation || 'review'}`],
        ['cleanup', 'Move/Delete'],
        ['rename', 'Rename to keeper'],
        ['keep', 'Keep']
      ];
    return `<select class="form-select form-select-sm" data-maint-operation="${escapeHtml(file.id)}" data-maint-group="${escapeHtml(group.id)}">` +
      options.map(([value, label]) => `<option value="${escapeHtml(value)}"${selected === value ? ' selected' : ''}>${escapeHtml(label)}</option>`).join('') +
      `</select>`;
  }

  function groupCandidateFiles(group, state) {
    const keepId = state.keepId || group.recommended_keep_id;
    const files = [];
    (group.videos || []).forEach(video => {
      if (video.id === keepId) return;
      files.push(video);
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

  function fileBelongsToKeeper(file, state) {
    const keeperId = state.keepId || '';
    return file.kind === 'video' ? file.id === keeperId : file.parent_video_id === keeperId;
  }

  function ensureGroupState(group) {
    if (!group?.id) {
      return {
        enabled: true,
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
        enabled: true,
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

  function updateSelectedSize() {
    let total = 0;
    currentPageGroups().forEach(summary => {
      const state = ensureGroupState(summary);
      if (!state.enabled) return;
      if (!(summary.videos || []).length) {
        total += Number(summary.reclaimable_bytes || 0);
        return;
      }
      groupCandidateFiles(summary, state).forEach(file => {
        if (state.includedFileIds.has(file.id)) total += Number(file.size_bytes || 0);
      });
    });
    total = Math.max(0, total);
    const selected = byId('maintenanceSelectedSize');
    if (selected) selected.textContent = currentPlan?.total_size_label || formatSize(total);
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
    const pageSizes = [10, 25, 50].map(size =>
      `<option value="${size}"${Number(page.limit) === size ? ' selected' : ''}>${size}</option>`
    ).join('');
    return `<div class="maintenance-pager">` +
      `<div class="text-muted small">${escapeHtml(pageRangeText(page))}${page.large_result ? ' - large result set' : ''}</div>` +
      `<div class="toolbar-row mb-0">` +
      `<button class="btn btn-outline-secondary btn-sm" type="button" data-maint-bulk="select">Select all candidates</button>` +
      `<button class="btn btn-outline-secondary btn-sm" type="button" data-maint-bulk="deselect">Deselect all candidates</button>` +
      `<button class="btn btn-outline-secondary btn-sm" type="button" data-maint-bulk="expand">Expand all</button>` +
      `<button class="btn btn-outline-secondary btn-sm" type="button" data-maint-bulk="collapse">Collapse all</button>` +
      `<label class="form-label mb-0 compact-control">Show` +
      `<select class="form-select form-select-sm" data-maint-page-limit>` +
      `${pageSizes}` +
      `</select></label>` +
      `<button class="btn btn-outline-secondary btn-sm" type="button" data-maint-page="prev"${page.has_previous ? '' : ' disabled'}>Previous</button>` +
      `<button class="btn btn-outline-secondary btn-sm" type="button" data-maint-page="next"${page.has_next ? '' : ' disabled'}>Next</button>` +
      `</div>` +
      `</div>`;
  }

  function groupOption(video, recommendedId) {
    const label = `${video.name}${video.metadata_label ? ` - ${video.metadata_label}` : ''}`;
    return `<option value="${escapeHtml(video.id)}"${video.id === recommendedId ? ' selected' : ''}>${escapeHtml(label)}</option>`;
  }

  function fileRow(group, file, state) {
    const locked = fileBelongsToKeeper(file, state);
    const checked = !locked && state.includedFileIds.has(file.id) ? ' checked' : '';
    const disabled = state.enabled && !locked ? '' : ' disabled';
    const kind = file.kind === 'video' ? 'Video' : 'Accessory';
    return `<tr>` +
      `<td><input class="form-check-input" type="checkbox" data-maint-file="${escapeHtml(file.id)}" data-maint-group="${escapeHtml(group.id)}" aria-label="Include ${escapeHtml(file.name)}"${checked}${disabled}></td>` +
      `<td data-sort-value="${escapeHtml(kind)}">${escapeHtml(kind)}</td>` +
      `<td class="path-cell" data-sort-value="${escapeHtml(file.name)}"><code title="${escapeHtml(file.path)}">${escapeHtml(file.name)}</code></td>` +
      `<td class="path-cell" data-sort-value="${escapeHtml(fileDetails(file))}"><code title="${escapeHtml(fileDetails(file))}">${escapeHtml(fileDetails(file))}</code></td>` +
      `<td data-sort-value="${locked ? 'keep' : escapeHtml(state.fileOperations?.get(file.id) || file.default_operation || 'default')}">${fileOperationOptions(group, file, state, locked)}</td>` +
      `<td data-sort-value="${Number(file.size_bytes || 0)}">${escapeHtml(file.size_label || formatSize(file.size_bytes))}</td>` +
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
      const res = await fetch(`/api/maintenance/duplicates/groups?scan_id=${encodeURIComponent(currentScan.id)}&offset=${encodeURIComponent(offset)}&limit=${encodeURIComponent(groupPageLimit)}`);
      const data = await readJsonResponse(res);
      if (!res.ok) {
        setMessage(data.error || 'Duplicate groups unavailable', '');
        return;
      }
      groupState.clear();
      groupSummaries.clear();
      currentGroupsPage = data;
      groupPageOffset = Number(data.offset || 0);
      groupPageLimit = Number(data.limit || groupPageLimit);
      (data.groups || []).forEach(group => {
        const existing = groupSummaries.get(group.id) || {};
        groupSummaries.set(group.id, {...existing, ...group});
      });
      renderGroups();
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

  function renderGroup(group) {
    const state = ensureGroupState(group);
    const expanded = Boolean(state.expanded);
    const hasDetails = Boolean((group.videos || []).length);
    const loading = Boolean(state.loading);
    const displayFiles = hasDetails ? groupDisplayFiles(group) : [];
    const rows = displayFiles.length
      ? displayFiles.map(file => fileRow(group, file, state)).join('')
      : '<tr><td colspan="6" class="text-muted text-center py-3">No files are available in this group.</td></tr>';
    const keeper = hasDetails
      ? (group.videos || []).find(video => video.id === state.keepId)
      : null;
    const recommended = keeper?.name || group.recommended_keep_name || '';
    const detail = expanded
      ? (loading
        ? '<div class="text-muted small mt-3">Loading group details...</div>'
        : (hasDetails
          ? `<div class="maintenance-group-detail">` +
            `<label class="form-label mb-0 compact-control">Keeper` +
            `<select class="form-select form-select-sm" data-maint-keep="${escapeHtml(group.id)}">` +
            `${(group.videos || []).map(video => groupOption(video, state.keepId)).join('')}` +
            `</select></label>` +
            `<div class="table-responsive workspace-table-wrap mt-2">` +
            `<table class="table table-hover align-middle workspace-table maintenance-table" data-table-id="maintenance-duplicate-files" data-sort-mode="client">` +
            `<thead><tr><th data-column-id="include" data-resizable="false">Include</th><th data-column-id="kind" data-sortable="true">Kind</th><th data-column-id="file" data-sortable="true">File</th><th data-column-id="details" data-sortable="true">Details</th><th data-column-id="operation" data-sortable="true">Operation</th><th data-column-id="size" data-sortable="true" data-sort-type="number">Size</th></tr></thead>` +
            `<tbody>${rows}</tbody>` +
            `</table></div></div>`
          : '<div class="text-muted small mt-3">Open this group to load file details.</div>'))
      : '';
    return `<section class="maintenance-group" data-maint-group-card="${escapeHtml(group.id)}">` +
      `<div class="maintenance-group-heading">` +
      `<div class="form-check">` +
      `<input class="form-check-input" type="checkbox" data-maint-group-enabled="${escapeHtml(group.id)}" id="enabled-${escapeHtml(group.id)}"${state.enabled ? ' checked' : ''}>` +
      `<label class="form-check-label" for="enabled-${escapeHtml(group.id)}">Clean group</label>` +
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
      `<span>${escapeHtml(group.accessory_count || 0)} accessory files</span>` +
      `<span>Recommended: ${escapeHtml(recommended)}</span>` +
      `<span>Default reclaimable: ${escapeHtml(group.reclaimable_label || '')}</span>` +
      `</div>` +
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
      target.innerHTML = currentScan.duplicate_group_count
        ? `${currentGroupsPage ? renderPager(currentGroupsPage) : ''}<div class="text-muted text-center py-4">No duplicate groups on this page.</div>`
        : '<div class="text-muted text-center py-4">No duplicate groups found.</div>';
      updateSelectedSize();
      return;
    }
    const groups = currentPageGroups();
    target.innerHTML = `${renderPager(currentGroupsPage)}${groups.map(renderGroup).join('')}${renderPager(currentGroupsPage)}`;
    updateSelectedSize();
  }

  async function openBrowser(path) {
    const browser = byId('maintenanceBrowser');
    if (!browser) return;
    browser.innerHTML = '<div class="small text-muted">Loading folders...</div>';
    try {
      const res = await fetch(`/api/media-browser?path=${encodeURIComponent(path || config.libRoot || '/library')}`);
      const data = await readJsonResponse(res);
      if (!res.ok) {
        browser.innerHTML = `<div class="small text-danger">${escapeHtml(data.error || 'Path not found')}</div>`;
        return;
      }
      const folders = (data.folders || []).map(folder =>
        `<button class="btn btn-outline-secondary btn-sm" type="button" data-maint-folder="${escapeHtml(folder.path)}">` +
        `<i class="bi bi-folder" aria-hidden="true"></i><span>${escapeHtml(folder.name)}</span></button>`
      ).join('');
      const parent = data.parent
        ? `<button class="btn btn-outline-secondary btn-sm" type="button" data-maint-folder="${escapeHtml(data.parent)}"><i class="bi bi-arrow-up" aria-hidden="true"></i><span>Parent</span></button>`
        : '';
      browser.innerHTML =
        `<div class="media-browser-current"><code title="${escapeHtml(data.path || '')}">${escapeHtml(data.path || '')}</code></div>` +
        `<div class="media-browser-actions">${parent}` +
        `<button class="btn btn-primary btn-sm" type="button" data-maint-choose="${escapeHtml(data.path || '')}"><i class="bi bi-check2" aria-hidden="true"></i><span>Use This Folder</span></button></div>` +
        `<div class="media-browser-files">${folders || '<span class="small text-muted">No folders in this location</span>'}</div>`;
    } catch (e) {
      browser.innerHTML = `<div class="small text-danger">${escapeHtml(e.message || 'Browser unavailable')}</div>`;
    }
  }

  async function openPreviewBrowser(path) {
    const browser = byId('previewBrowser');
    if (!browser) return;
    browser.innerHTML = '<div class="small text-muted">Loading folders...</div>';
    try {
      const res = await fetch(`/api/media-browser?path=${encodeURIComponent(path || config.libRoot || '/library')}`);
      const data = await readJsonResponse(res);
      if (!res.ok) {
        browser.innerHTML = `<div class="small text-danger">${escapeHtml(data.error || 'Path not found')}</div>`;
        return;
      }
      const folders = (data.folders || []).map(folder =>
        `<button class="btn btn-outline-secondary btn-sm" type="button" data-preview-folder="${escapeHtml(folder.path)}">` +
        `<i class="bi bi-folder" aria-hidden="true"></i><span>${escapeHtml(folder.name)}</span></button>`
      ).join('');
      const parent = data.parent
        ? `<button class="btn btn-outline-secondary btn-sm" type="button" data-preview-folder="${escapeHtml(data.parent)}"><i class="bi bi-arrow-up" aria-hidden="true"></i><span>Parent</span></button>`
        : '';
      browser.innerHTML =
        `<div class="media-browser-current"><code title="${escapeHtml(data.path || '')}">${escapeHtml(data.path || '')}</code></div>` +
        `<div class="media-browser-actions">${parent}` +
        `<button class="btn btn-primary btn-sm" type="button" data-preview-choose="${escapeHtml(data.path || '')}"><i class="bi bi-check2" aria-hidden="true"></i><span>Use This Folder</span></button></div>` +
        `<div class="media-browser-files">${folders || '<span class="small text-muted">No folders in this location</span>'}</div>`;
    } catch (e) {
      browser.innerHTML = `<div class="small text-danger">${escapeHtml(e.message || 'Browser unavailable')}</div>`;
    }
  }

  async function openSubtitleBrowser(path) {
    const browser = byId('subtitleBrowser');
    if (!browser) return;
    browser.innerHTML = '<div class="small text-muted">Loading folders...</div>';
    try {
      const res = await fetch(`/api/media-browser?path=${encodeURIComponent(path || config.libRoot || '/library')}`);
      const data = await readJsonResponse(res);
      if (!res.ok) {
        browser.innerHTML = `<div class="small text-danger">${escapeHtml(data.error || 'Path not found')}</div>`;
        return;
      }
      const folders = (data.folders || []).map(folder =>
        `<button class="btn btn-outline-secondary btn-sm" type="button" data-subtitle-folder="${escapeHtml(folder.path)}">` +
        `<i class="bi bi-folder" aria-hidden="true"></i><span>${escapeHtml(folder.name)}</span></button>`
      ).join('');
      const parent = data.parent
        ? `<button class="btn btn-outline-secondary btn-sm" type="button" data-subtitle-folder="${escapeHtml(data.parent)}"><i class="bi bi-arrow-up" aria-hidden="true"></i><span>Parent</span></button>`
        : '';
      browser.innerHTML =
        `<div class="media-browser-current"><code title="${escapeHtml(data.path || '')}">${escapeHtml(data.path || '')}</code></div>` +
        `<div class="media-browser-actions">${parent}` +
        `<button class="btn btn-primary btn-sm" type="button" data-subtitle-choose="${escapeHtml(data.path || '')}"><i class="bi bi-check2" aria-hidden="true"></i><span>Use This Folder</span></button></div>` +
        `<div class="media-browser-files">${folders || '<span class="small text-muted">No folders in this location</span>'}</div>`;
    } catch (e) {
      browser.innerHTML = `<div class="small text-danger">${escapeHtml(e.message || 'Browser unavailable')}</div>`;
    }
  }

  async function openActorBrowser(path) {
    const browser = byId('actorBrowser');
    if (!browser) return;
    browser.innerHTML = '<div class="small text-muted">Loading folders...</div>';
    try {
      const res = await fetch(`/api/media-browser?path=${encodeURIComponent(path || config.libRoot || '/library')}`);
      const data = await readJsonResponse(res);
      if (!res.ok) {
        browser.innerHTML = `<div class="small text-danger">${escapeHtml(data.error || 'Path not found')}</div>`;
        return;
      }
      const folders = (data.folders || []).map(folder =>
        `<button class="btn btn-outline-secondary btn-sm" type="button" data-actor-folder="${escapeHtml(folder.path)}">` +
        `<i class="bi bi-folder" aria-hidden="true"></i><span>${escapeHtml(folder.name)}</span></button>`
      ).join('');
      const parent = data.parent
        ? `<button class="btn btn-outline-secondary btn-sm" type="button" data-actor-folder="${escapeHtml(data.parent)}"><i class="bi bi-arrow-up" aria-hidden="true"></i><span>Parent</span></button>`
        : '';
      browser.innerHTML =
        `<div class="media-browser-current"><code title="${escapeHtml(data.path || '')}">${escapeHtml(data.path || '')}</code></div>` +
        `<div class="media-browser-actions">${parent}` +
        `<button class="btn btn-primary btn-sm" type="button" data-actor-choose="${escapeHtml(data.path || '')}"><i class="bi bi-check2" aria-hidden="true"></i><span>Use This Folder</span></button></div>` +
        `<div class="media-browser-files">${folders || '<span class="small text-muted">No folders in this location</span>'}</div>`;
    } catch (e) {
      browser.innerHTML = `<div class="small text-danger">${escapeHtml(e.message || 'Browser unavailable')}</div>`;
    }
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
    setProgress(scan);
    renderGroups();
    const planButton = byId('maintenancePlanButton');
    const stale = scan?.freshness?.status === 'changed';
    if (planButton) planButton.disabled = !scan || scan.status !== 'success' || !(scan.duplicate_group_count || 0) || stale;
    if (!scan) {
      setMessage('No scan results yet.', '');
    } else if (scan.status === 'success') {
      setMessage(
        `${scan.duplicate_group_count || 0} duplicate groups found`,
        scan.large_result
          ? withEmbyCoverage(`Large result set. Loading ${groupPageLimit} groups at a time.`, scan)
          : withEmbyCoverage(scan.reclaimable_label ? `Default reclaimable size: ${scan.reclaimable_label}` : '', scan)
      );
      appendEmbySyncNotice('maintenanceMessageDetail', embySyncFrom(apply));
      if (scan.duplicate_group_count && currentGroupsPage?.scan?.id !== scan.id) {
        loadGroupsPage(0);
      }
      if (stale) setMessage('Duplicate results are out of date', 'Library files changed after this scan. Rescan before creating a cleanup plan.');
    } else if (scan.status === 'failed') {
      setMessage('Scan failed', scan.error || '');
    } else if (scan.status === 'cancelled') {
      setMessage('Scan cancelled', '');
    } else {
      setMessage(scan.progress_label || 'Scanning', '');
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
      pollTimer = setInterval(() => pollScan(data.scan.id), 1000);
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
    currentPageGroups().forEach(pageGroup => {
      const groupId = pageGroup.id;
      const state = ensureGroupState(pageGroup);
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
          visible_group_ids: currentPageGroups().map(group => group.id)
        })
      });
      const data = await readJsonResponse(res);
      if (!res.ok) {
        setMessage(data.error || 'Plan could not be built', '');
        return;
      }
      currentPlan = data.plan;
      renderPlan(currentPlan);
      byId('maintenanceApplyButton').disabled = !currentPlan.file_count;
      setMessage(
        'Review the cleanup plan before applying',
        `${pageRangeText(currentGroupsPage)}; ${currentPlan.visible_group_count || 0} visible groups. ` + (Number(currentPlan.file_count || 0) >= 100
          ? `${currentPlan.file_count} files selected. This can take a while and will continue in the background.`
          : (currentPlan.total_size_label || ''))
      );
    } catch (e) {
      setMessage('Plan could not be built', e.message || '');
    }
  }

  function handleApply(apply) {
    currentApply = apply;
    const button = byId('maintenanceApplyButton');
    const running = apply && ['queued', 'running'].includes(apply.status || '');
    if (button) button.disabled = running || !currentPlan;
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
      if (button) button.disabled = true;
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
      `<td><button class="btn btn-outline-secondary btn-sm" type="button" data-maint-log="${escapeHtml(log.id)}">Open</button></td>` +
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
    if (percent) percent.textContent = `${pct}%`;
    if (bar) {
      bar.style.width = `${pct}%`;
      bar.parentElement.setAttribute('aria-valuenow', pct);
    }
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
      return `<tr>` +
        `<td>${item.status === 'missing' ? `<input class="form-check-input" type="checkbox" data-preview-generate="${escapeHtml(item.id)}" aria-label="Generate BIF for ${escapeHtml(item.name)}"${previewSelectedMissing.has(item.id) ? ' checked' : ''}>` : ''}</td>` +
        `<td>${previewStatusBadge(item.status)}</td>` +
        `<td class="path-cell"><code title="${escapeHtml(item.path)}">${escapeHtml(item.relative_path || item.name)}</code></td>` +
        `<td>${escapeHtml(item.size_label || '')}</td>` +
        `<td>${escapeHtml(item.detail || '')}</td>` +
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
      previewSelectedMissing.clear();
      (data.items || []).filter(item => item.status === 'missing').forEach(item => previewSelectedMissing.add(item.id));
      previewGenerationPlan = null;
      byId('previewGenerationPlanButton').disabled = !previewSelectedMissing.size || previewScan?.freshness?.status === 'changed';
      byId('previewGenerationStartButton').disabled = true;
      byId('previewSelectMissingButton').disabled = !(data.items || []).some(item => item.status === 'missing');
      byId('previewDeselectMissingButton').disabled = !previewSelectedMissing.size;
      const generationSummary = byId('previewGenerationSummary');
      if (generationSummary) generationSummary.innerHTML = '';
      renderPreviewItems(data);
      if (data.large_result) {
        setPreviewMessage(`${data.total || 0} results in this view`, `Large result set loaded ${data.limit || previewPageLimit} items at a time.`);
      }
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
      setPreviewMessage(scan.progress_label || 'Scanning video previews', 'Large libraries can take a while.');
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
    stopPreviewPolling();
    previewItemsPage = null;
    previewPageOffset = 0;
    previewLastPath = path;
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
        {label: 'Current page', value: previewPageRangeText(previewItemsPage)}
      ],
      note: 'Frames are generated and validated outside the media folder before atomic installation.',
      changeForFile: file => ({operation: 'generate', operationLabel: 'Generate', source: file.video_relative_path, target: file.output_relative_path, detail: `${plan.width}px every ${plan.interval_seconds}s`})
    });
  }

  async function reviewGenerationPlan(confirmMismatch = false) {
    if (!previewScan?.id || !previewSelectedMissing.size) return;
    try {
      const res = await fetch('/api/maintenance/video-previews/generation/plan', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({scan_id: previewScan.id, item_ids: Array.from(previewSelectedMissing), confirm_profile_mismatch: confirmMismatch})
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
      setPreviewMessage('Review the missing BIF generation plan', `${data.plan.file_count} video(s)`);
    } catch (e) {
      setPreviewMessage('Generation plan could not be built', e.message || '');
    }
  }

  function stopGenerationPolling() {
    if (previewGenerationPollTimer) clearInterval(previewGenerationPollTimer);
    previewGenerationPollTimer = null;
  }

  async function pollGeneration(runId) {
    try {
      const res = await fetch(`/api/maintenance/video-previews/generation/status?run_id=${encodeURIComponent(runId)}`);
      const data = await readJsonResponse(res);
      if (!res.ok) throw new Error(data.error || 'Generation status unavailable');
      previewGenerationRun = data.run;
      const active = ['queued', 'running', 'cancelling'].includes(data.run?.status || '');
      byId('previewGenerationCancelButton').disabled = !active;
      if (active) {
        setPreviewMessage(data.run.progress_label || 'Generating BIFs', `${data.run.generated_count || 0} generated, ${data.run.refused_count || 0} refused`);
        return;
      }
      stopGenerationPolling();
      if (data.run?.status === 'success') {
        setPreviewMessage('BIF generation complete', `${data.run.generated_count || 0} generated, ${data.run.refused_count || 0} refused`);
        appendEmbySyncNotice('previewMessageDetail', embySyncFrom(data.run));
        previewGenerationPlan = null;
        await startPreviewScan(previewLastPath);
      } else {
        setPreviewMessage(data.run?.status === 'cancelled' ? 'BIF generation cancelled' : 'BIF generation failed', data.run?.error || '');
      }
    } catch (e) {
      stopGenerationPolling();
      setPreviewMessage('Generation status unavailable', e.message || '');
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
    await fetch('/api/maintenance/video-previews/generation/cancel', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({run_id: previewGenerationRun.id})});
    byId('previewGenerationCancelButton').disabled = true;
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
    if (percent) percent.textContent = `${pct}%`;
    if (bar) {
      bar.style.width = `${pct}%`;
      bar.parentElement.setAttribute('aria-valuenow', pct);
    }
    if (bad) bad.textContent = String(scan?.bad_count || 0);
    if (warnings) warnings.textContent = String(scan?.warning_count || 0);
    const active = Boolean(scan?.active || ['queued', 'running', 'cancelling'].includes(scan?.status || ''));
    const scanButton = byId('qualityScanButton');
    const cancelButton = byId('qualityCancelButton');
    const planButton = byId('qualityPlanButton');
    if (scanButton) scanButton.disabled = active;
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
      setQualityMessage(
        `${scan.bad_count || 0} bad BIF file${(scan.bad_count || 0) === 1 ? '' : 's'}`,
        withEmbyCoverage(`${scan.warning_count || 0} warnings, ${scan.ok_count || 0} passed`, scan)
      );
      appendEmbySyncNotice('qualityMessageDetail', embySyncFrom(apply));
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

  async function startQualityScan() {
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
    setQualityMessage('Starting BIF quality scan', '');
    setQualityProgress({status: 'queued', progress_percent: 0, progress_label: 'Queued'});
    try {
      const res = await fetch('/api/maintenance/video-previews/quality/scan', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({path})
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
    if (percent) percent.textContent = `${pct}%`;
    if (bar) {
      bar.style.width = `${pct}%`;
      bar.parentElement.setAttribute('aria-valuenow', pct);
    }
    if (missing) missing.textContent = String(scan?.missing_count || 0);
    if (review) review.textContent = String(scan?.review_count || 0);
    const active = Boolean(scan?.active || ['queued', 'running', 'cancelling'].includes(scan?.status || ''));
    const scanButton = byId('subtitleScanButton');
    const cancelButton = byId('subtitleCancelScanButton');
    if (scanButton) scanButton.disabled = active;
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
      language_review: ['Language Review', 'text-bg-danger'],
      unknown: ['Unknown', 'text-bg-info'],
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
      const checked = selectable && subtitleSelected.has(file.id) ? ' checked' : '';
      return `<div class="mb-2 d-flex gap-2 align-items-start">` +
        (selectable ? `<input class="form-check-input mt-1" type="checkbox" data-subtitle-file="${escapeHtml(file.id)}" aria-label="Select ${escapeHtml(file.name || 'subtitle')}"${checked}>` : '<span class="form-check-input border-0 mt-1"></span>') +
        `<div>` +
        `<code class="path-cell" title="${escapeHtml(file.path || '')}">${escapeHtml(file.relative_path || file.name || '')}</code>` +
        `<div class="text-muted small">${escapeHtml(code)} · ${escapeHtml(file.size_label || '')}${file.action_reason ? ` · ${escapeHtml(file.action_reason)}` : ''}</div>` +
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
    const status = byId('subtitleItemStatus')?.value || 'language_review';
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
      subtitleSelected.clear();
      (data.items || []).forEach(item => (item.srt_files || []).forEach(file => {
        if (file.actionable) subtitleSelected.add(file.id);
      }));
      subtitlePlan = null;
      const planButton = byId('subtitlePlanButton');
      const applyButton = byId('subtitleApplyButton');
      if (planButton) planButton.disabled = !subtitleSelected.size;
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
      const settings = scan.settings || {};
      const streams = scan.emby_streams || {};
      const streamDetail = ['complete', 'partial'].includes(streams.status)
        ? `Emby streams: ${streams.stream_count || 0}, mismatches: ${streams.index_mismatch_count || 0}.`
        : (streams.message || 'Emby stream details are unavailable.');
      setSubtitleMessage(
        `${scan.review_count || 0} subtitle review item${(scan.review_count || 0) === 1 ? '' : 's'}`,
        withEmbyCoverage(`${scan.missing_count || 0} missing, ${scan.language_review_count || 0} language review, ${scan.unknown_count || 0} unknown. ${streamDetail} Expected: ${(settings.expected_languages || []).join(', ') || 'not set'}`, scan)
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

  async function startSubtitleScan() {
    const path = (byId('subtitlePath')?.value || config.libRoot || '/library').trim();
    if (!path) {
      setSubtitleMessage('Choose a folder under the library', '');
      return;
    }
    stopSubtitlePolling();
    subtitleItemsPage = null;
    subtitlePageOffset = 0;
    setSubtitleMessage('Starting subtitle scan', '');
    setSubtitleProgress({status: 'queued', progress_percent: 0, progress_label: 'Queued'});
    try {
      const res = await fetch('/api/maintenance/subtitles/scan', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({path})
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
        {label: 'Page', value: subtitlePageRangeText(subtitleItemsPage)},
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
          visible_file_ids: visibleIds,
          selected_file_ids: Array.from(subtitleSelected)
        })
      });
      const data = await readJsonResponse(res);
      if (!res.ok) throw new Error(data.error || 'Subtitle plan could not be built');
      subtitlePlan = data.plan;
      renderSubtitlePlan(subtitlePlan);
      byId('subtitleApplyButton').disabled = !subtitlePlan.file_count;
      setSubtitleMessage('Review the subtitle cleanup plan', `${subtitlePlan.file_count} visible file(s), ${subtitlePlan.total_size_label || '0 B'}`);
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
        subtitlePlan = null;
        byId('subtitleApplyButton').disabled = true;
        await startSubtitleScan();
      } else {
        setSubtitleMessage('Subtitle cleanup failed', subtitleApply.error || '');
      }
    } catch (e) {
      stopSubtitleApplyPolling();
      setSubtitleMessage('Subtitle cleanup status unavailable', e.message || '');
    }
  }

  async function applySubtitlePlan() {
    if (!subtitlePlan) return;
    const prompt = subtitlePlan.operation === 'delete'
      ? `Permanently delete ${subtitlePlan.file_count} visible subtitle file(s)?\n\nThis cannot be undone.`
      : `Move ${subtitlePlan.file_count} visible subtitle file(s) to quarantine?`;
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
    if (percent) percent.textContent = `${pct}%`;
    if (bar) {
      bar.style.width = `${pct}%`;
      bar.parentElement.setAttribute('aria-valuenow', pct);
    }
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
      const checked = actorSelected.has(item.id) || (item.status === 'ready' && !actorSelected.size);
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
      if (!actorSelected.size && status === 'ready') {
        (data.items || []).forEach(item => {
          if (item.status === 'ready') actorSelected.add(item.id);
        });
      }
      renderActorItems(data);
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
    stopActorPolling();
    stopActorApplyPolling();
    actorItemsPage = null;
    actorPageOffset = 0;
    actorSelected.clear();
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
    const selected = Array.from(actorSelected).map(itemId => ({item_id: itemId}));
    setActorMessage('Building actor image import plan', '');
    try {
      const res = await fetch('/api/maintenance/actor-images/plan', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({scan_id: actorScan.id, items: selected})
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
      setActorMessage('Review the actor image import plan before applying', `${actorPlan.file_count || 0} image(s) selected`);
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
    if (status === 'ambiguous' || status === 'unreadable' || status === 'unsafe') return `<span class="badge text-bg-warning">${escapeHtml(status)}</span>`;
    if (status === 'updated') return '<span class="badge text-bg-success">Updated</span>';
    if (status === 'already_matching') return '<span class="badge text-bg-secondary">Matched</span>';
    if (status === 'missing_poster') return '<span class="badge text-bg-warning">Missing poster</span>';
    if (status === 'error' || status === 'failed') return '<span class="badge text-bg-danger">Error</span>';
    return `<span class="badge text-bg-secondary">${escapeHtml(status || 'Skipped')}</span>`;
  }

  function renderPosterItems(page) {
    const wrap = byId('posterRecentItems');
    if (!wrap) return;
    const items = page?.items || [];
    const rows = items.length ? items.map(item =>
      `<tr>` +
      `<td><input class="form-check-input" type="checkbox" data-poster-item="${escapeHtml(item.id)}"${posterSelected.has(item.id) ? ' checked' : ''}${item.eligible ? '' : ' disabled'} aria-label="Select poster update"></td>` +
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
    if (byId('posterPlanButton')) byId('posterPlanButton').disabled = !posterSelected.size || posterScan?.freshness?.status === 'changed';
  }

  async function loadPosterItems(offset = 0) {
    if (!posterScan?.id || posterScan.status !== 'success') return;
    try {
      const res = await fetch(`/api/maintenance/landscape-posters/items?scan_id=${encodeURIComponent(posterScan.id)}&offset=${encodeURIComponent(offset)}&limit=10&sort=${encodeURIComponent(posterSort.column)}&direction=${encodeURIComponent(posterSort.direction)}`);
      const data = await readJsonResponse(res);
      posterItemsPage = data;
      posterSort = {column: data.sort || posterSort.column, direction: data.direction || posterSort.direction};
      posterSelected.clear();
      (data.items || []).filter(item => item.eligible).forEach(item => posterSelected.add(item.id));
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
      const stale = analysis.freshness?.status === 'changed';
      setPosterMessage(stale ? 'Poster analysis is out of date' : `${analysis.eligible_count || 0} poster updates ready`, stale ? 'Library artwork changed after this scan. Rescan before applying updates.' : withEmbyCoverage(`${analysis.already_landscape_count || 0} already landscape, ${analysis.ambiguous_count || 0} ambiguous`, analysis));
      if (posterItemsPage?.scan?.id !== analysis.id) loadPosterItems(0);
    } else if (!current && settings.enabled) {
      setPosterMessage('Landscape poster automation is enabled', `Incremental interval: ${settings.scan_interval_label || ''}`);
    } else if (current) {
      setPosterMessage(current.progress_label || 'Landscape poster run active', current.path || '');
    } else if (last) {
      const emby = last.emby_refresh || {};
      setPosterMessage(last.progress_label || 'Latest landscape poster run complete', emby.message || '');
    } else {
      setPosterMessage('Landscape poster automation is disabled', 'Run manually or enable automatic scans.');
    }
    renderEmbyStatus(data?.emby_status);
    appendEmbySyncNotice('posterMessageDetail', last?.emby_sync || null);
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
    if (button) button.disabled = true;
    setPosterMessage('Starting poster analysis', '');
    try {
      const res = await fetch('/api/maintenance/landscape-posters/scan', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({path: config.libRoot || '/library'})
      });
      const data = await readJsonResponse(res);
      if (!res.ok) {
        setPosterMessage(data.error || 'Poster analysis could not start', '');
        return;
      }
      posterScan = data.scan;
      setPosterMessage(data.scan?.progress_label || 'Poster analysis queued', data.scan?.path || '');
      clearTimeout(posterPollTimer);
      posterPollTimer = setTimeout(refreshPosterStatus, 500);
    } catch (e) {
      setPosterMessage('Poster analysis could not start', e.message || '');
    } finally {
      if (button) button.disabled = false;
    }
  }

  async function reviewPosterPlan() {
    if (!posterScan?.id || !posterItemsPage || !posterSelected.size) return;
    const visible = (posterItemsPage.items || []).map(item => item.id);
    try {
      const res = await fetch('/api/maintenance/landscape-posters/plan', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({scan_id: posterScan.id, visible_item_ids: visible, item_ids: [...posterSelected]})
      });
      const data = await readJsonResponse(res);
      posterPlan = data.plan;
      byId('posterPlanSummary').innerHTML = `<div class="scan-estimate"><i class="bi bi-clipboard-check" aria-hidden="true"></i><div><strong>${escapeHtml(posterPlan.file_count)} poster update${posterPlan.file_count === 1 ? '' : 's'} reviewed</strong><div class="scan-estimate-detail">Only selected items on this visible page will be changed.</div></div></div>`;
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
      overviewPageLimit = Number(event.target.value || 25);
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
      openBrowser(byId('maintenancePath')?.value.trim() || config.libRoot || '/library');
    });
    byId('maintenanceScanButton')?.addEventListener('click', startScan);
    byId('maintenanceCancelScanButton')?.addEventListener('click', cancelScan);
    byId('maintenancePlanButton')?.addEventListener('click', reviewPlan);
    byId('maintenanceApplyButton')?.addEventListener('click', applyPlan);
    byId('previewBrowseButton')?.addEventListener('click', () => {
      openPreviewBrowser(byId('previewPath')?.value.trim() || config.libRoot || '/library');
    });
    byId('previewScanButton')?.addEventListener('click', () => startPreviewScan());
    byId('previewCancelScanButton')?.addEventListener('click', cancelPreviewScan);
    byId('previewVerifyButton')?.addEventListener('click', () => startPreviewScan(previewLastPath || byId('previewPath')?.value || config.libRoot || '/library'));
    byId('previewRefreshTasksButton')?.addEventListener('click', refreshPreviewTasks);
    byId('previewRunExtractionButton')?.addEventListener('click', runPreviewExtraction);
    byId('previewSaveBifSettingsButton')?.addEventListener('click', saveCurrentBifProfile);
    byId('previewUseRecommendationButton')?.addEventListener('click', useBifRecommendation);
    byId('previewGenerationPlanButton')?.addEventListener('click', () => reviewGenerationPlan(false));
    byId('previewGenerationStartButton')?.addEventListener('click', startGeneration);
    byId('previewGenerationCancelButton')?.addEventListener('click', cancelGeneration);
    byId('previewSelectMissingButton')?.addEventListener('click', () => {
      (previewItemsPage?.items || []).filter(item => item.status === 'missing').forEach(item => previewSelectedMissing.add(item.id));
      previewGenerationPlan = null;
      byId('previewGenerationPlanButton').disabled = !previewSelectedMissing.size;
      byId('previewGenerationStartButton').disabled = true;
      renderPreviewItems(previewItemsPage);
    });
    byId('previewDeselectMissingButton')?.addEventListener('click', () => {
      previewSelectedMissing.clear();
      previewGenerationPlan = null;
      byId('previewGenerationPlanButton').disabled = true;
      byId('previewGenerationStartButton').disabled = true;
      renderPreviewItems(previewItemsPage);
    });
    byId('previewItemStatus')?.addEventListener('change', () => {
      previewPageOffset = 0;
      loadPreviewItems(0);
    });
    byId('qualityScanButton')?.addEventListener('click', startQualityScan);
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
    byId('subtitleBrowseButton')?.addEventListener('click', () => {
      openSubtitleBrowser(byId('subtitlePath')?.value.trim() || config.libRoot || '/library');
    });
    byId('subtitleScanButton')?.addEventListener('click', startSubtitleScan);
    byId('subtitleCancelScanButton')?.addEventListener('click', cancelSubtitleScan);
    byId('subtitlePlanButton')?.addEventListener('click', reviewSubtitlePlan);
    byId('subtitleApplyButton')?.addEventListener('click', applySubtitlePlan);
    byId('subtitleSelectAllButton')?.addEventListener('click', () => {
      visibleSubtitleFiles().filter(file => file.actionable).forEach(file => subtitleSelected.add(file.id));
      subtitlePlan = null;
      byId('subtitlePlanButton').disabled = !subtitleSelected.size;
      byId('subtitleApplyButton').disabled = true;
      renderSubtitleItems(subtitleItemsPage);
    });
    byId('subtitleDeselectAllButton')?.addEventListener('click', () => {
      subtitleSelected.clear();
      subtitlePlan = null;
      byId('subtitlePlanButton').disabled = true;
      byId('subtitleApplyButton').disabled = true;
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
      subtitlePageLimit = Number(event.target.value || 25);
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
      openActorBrowser(byId('actorPath')?.value.trim() || config.libRoot || '/library');
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
    byId('posterPlanButton')?.addEventListener('click', reviewPosterPlan);
    byId('posterApplyButton')?.addEventListener('click', applyPosterPlan);
    byId('posterRefreshButton')?.addEventListener('click', refreshPosterStatus);
    byId('posterRecentItems')?.addEventListener('change', event => {
      const checkbox = event.target.closest('[data-poster-item]');
      if (!checkbox) return;
      if (checkbox.checked) posterSelected.add(checkbox.dataset.posterItem);
      else posterSelected.delete(checkbox.dataset.posterItem);
      posterPlan = null;
      if (byId('posterApplyButton')) byId('posterApplyButton').disabled = true;
      if (byId('posterPlanButton')) byId('posterPlanButton').disabled = !posterSelected.size || posterScan?.freshness?.status === 'changed';
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
      updateSelectedSize();
    });

    byId('maintenanceGroups')?.addEventListener('click', event => {
      const page = event.target.closest('[data-maint-page]');
      const expand = event.target.closest('[data-maint-expand]');
      const bulk = event.target.closest('[data-maint-bulk]');
      if (bulk) {
        const action = bulk.getAttribute('data-maint-bulk');
        if (action === 'collapse') {
          currentPageGroups().forEach(group => { ensureGroupState(group).expanded = false; });
          renderGroups();
          return;
        }
        if (action === 'expand') {
          currentPageGroups().forEach(group => { ensureGroupState(group).expanded = true; });
          renderGroups();
          Promise.allSettled(currentPageGroups()
            .filter(group => !(group.videos || []).length)
            .map(group => loadGroupDetails(group.id)));
          return;
        }
        currentPageGroups().forEach(group => {
          const state = ensureGroupState(group);
          state.enabled = action === 'select';
          if ((group.videos || []).length) {
            state.includedFileIds = action === 'select'
              ? new Set(groupCandidateFiles(group, state).map(file => file.id))
              : new Set();
          }
          state.dirty = true;
        });
        invalidatePlan();
        renderGroups();
        updateSelectedSize();
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
      }
    });

    byId('previewBrowser')?.addEventListener('click', event => {
      const folder = event.target.closest('[data-preview-folder]');
      const choose = event.target.closest('[data-preview-choose]');
      if (folder) {
        openPreviewBrowser(folder.getAttribute('data-preview-folder'));
      } else if (choose) {
        const path = choose.getAttribute('data-preview-choose') || '';
        if (byId('previewPath')) byId('previewPath').value = path;
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
      if (checkbox.checked) previewSelectedMissing.add(itemId);
      else previewSelectedMissing.delete(itemId);
      previewGenerationPlan = null;
      byId('previewGenerationPlanButton').disabled = !previewSelectedMissing.size;
      byId('previewGenerationStartButton').disabled = true;
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
      if (checkbox.checked) subtitleSelected.add(fileId);
      else subtitleSelected.delete(fileId);
      subtitlePlan = null;
      byId('subtitlePlanButton').disabled = !subtitleSelected.size;
      byId('subtitleApplyButton').disabled = true;
      const summary = byId('subtitlePlanSummary');
      if (summary) summary.innerHTML = '';
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
      if (selected.checked) {
        actorSelected.add(itemId);
      } else {
        actorSelected.delete(itemId);
      }
      actorPlan = null;
      const applyButton = byId('actorApplyButton');
      if (applyButton) applyButton.disabled = true;
      const summary = byId('actorPlanSummary');
      if (summary) summary.innerHTML = '';
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
      const file = event.target.closest('[data-maint-file]');
      const operation = event.target.closest('[data-maint-operation]');
      const pageLimit = event.target.closest('[data-maint-page-limit]');
      if (pageLimit) {
        groupPageLimit = Number(pageLimit.value || 10);
        groupPageOffset = 0;
        loadGroupsPage(0);
        return;
      }
      if (enabled) {
        const groupId = enabled.getAttribute('data-maint-group-enabled');
        const state = ensureGroupState(groupSummaries.get(groupId) || {id: groupId});
        if (state) state.enabled = enabled.checked;
        markGroupDirty(groupId);
        renderGroups(currentScan);
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
      if (file) {
        const groupId = file.getAttribute('data-maint-group');
        const fileId = file.getAttribute('data-maint-file');
        const state = groupState.get(groupId);
        if (state && fileId) {
          if (file.checked) {
            state.includedFileIds.add(fileId);
          } else {
            state.includedFileIds.delete(fileId);
          }
        }
        markGroupDirty(groupId);
        return;
      }
      if (operation) {
        const groupId = operation.getAttribute('data-maint-group');
        const fileId = operation.getAttribute('data-maint-operation');
        const state = groupState.get(groupId);
        if (state && fileId) {
          if (operation.value === 'default') {
            state.fileOperations.delete(fileId);
          } else {
            state.fileOperations.set(fileId, operation.value);
          }
        }
        markGroupDirty(groupId);
      }
    });

    byId('maintenanceLogList')?.addEventListener('click', event => {
      const button = event.target.closest('[data-maint-log]');
      if (!button) return;
      openMaintenanceLog(button.getAttribute('data-maint-log'));
    });

    byId('actorLogList')?.addEventListener('click', event => {
      const button = event.target.closest('[data-actor-log]');
      if (!button) return;
      openActorLog(button.getAttribute('data-actor-log'));
    });
  }

  document.addEventListener('DOMContentLoaded', () => {
    initMaintenanceTabs();
    initEvents();
    document.addEventListener('vid2gif:table-sort', event => {
      const {tableId, column, direction} = event.detail || {};
      if (!column || !direction) return;
      if (tableId === 'maintenance-missing-bifs') {
        previewSort = {column, direction};
        previewSelectedMissing.clear();
        loadPreviewItems(0);
      } else if (tableId === 'maintenance-quality-bifs') {
        qualitySort = {column, direction};
        qualityExcludedItems.clear();
        qualityIncludedItems.clear();
        qualityPlan = null;
        loadQualityItems(0);
      } else if (tableId === 'maintenance-subtitles') {
        subtitleSort = {column, direction};
        subtitleSelected.clear();
        subtitlePlan = null;
        loadSubtitleItems(0);
      } else if (tableId === 'maintenance-actor-images') {
        actorSort = {column, direction};
        actorSelected.clear();
        actorPlan = null;
        loadActorItems(0);
      } else if (tableId === 'maintenance-posters') {
        posterSort = {column, direction};
        posterSelected.clear();
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
    openBrowser(config.libRoot || '/library');
    openPreviewBrowser(config.libRoot || '/library');
    openSubtitleBrowser(config.libRoot || '/library');
    openActorBrowser(config.libRoot || '/library');
    refreshMaintenanceLogs();
    refreshOverviewStatus();
    refreshDuplicateStatus();
    refreshPreviewStatus();
    refreshQualityStatus();
    refreshPreviewTasks();
    refreshSubtitleStatus();
    refreshActorStatus();
    refreshPosterStatus();
    checkMaintenanceFreshness();
    document.addEventListener('visibilitychange', () => {
      if (document.hidden) clearTimeout(maintenanceFreshnessTimer);
      else checkMaintenanceFreshness();
    });
    window.addEventListener('beforeunload', event => {
      clearTimeout(overviewPollTimer);
      clearTimeout(overviewSearchTimer);
      clearTimeout(subtitleSearchTimer);
      stopSubtitlePolling();
      stopSubtitleApplyPolling();
      stopGenerationPolling();
      clearTimeout(posterPollTimer);
      clearTimeout(maintenanceFreshnessTimer);
      if (posterSettingsPending || posterSettingsFailures.size) {
        event.preventDefault();
        event.returnValue = '';
      }
    });
  });
}());
