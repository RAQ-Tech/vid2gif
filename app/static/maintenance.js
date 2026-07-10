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
  let groupPageLimit = 25;
  let pollTimer = null;
  let applyPollTimer = null;
  let previewScan = null;
  let previewPollTimer = null;
  let previewItemsPage = null;
  let previewPageOffset = 0;
  let previewPageLimit = 25;
  let previewLastPath = '';
  let qualityScan = null;
  let qualityPollTimer = null;
  let qualityItemsPage = null;
  let qualityPageOffset = 0;
  let qualityPageLimit = 25;
  let qualityPlan = null;
  let qualityApply = null;
  let qualityApplyPollTimer = null;
  let subtitleScan = null;
  let subtitlePollTimer = null;
  let subtitleItemsPage = null;
  let subtitlePageOffset = 0;
  let subtitlePageLimit = 25;
  let subtitleSearchTimer = null;
  let actorScan = null;
  let actorPollTimer = null;
  let actorItemsPage = null;
  let actorPageOffset = 0;
  let actorPageLimit = 25;
  let actorPlan = null;
  let actorApply = null;
  let actorApplyPollTimer = null;
  const actorSelected = new Set();
  let posterPollTimer = null;
  let posterSettingsLoaded = false;

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

  function fileOperationOptions(group, file, state) {
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
        loading: false
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
        loading: false
      });
    }
    const state = groupState.get(group.id);
    if (!state.keepId && group.recommended_keep_id) {
      state.keepId = group.recommended_keep_id;
    }
    if (!(group.videos || []).length) {
      return state;
    }
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
    let total = Number(currentScan?.reclaimable_bytes || 0);
    groupState.forEach((state, groupId) => {
      if (!state.dirty) return;
      const summary = groupSummaries.get(groupId);
      if (!summary) return;
      const original = Number(summary.reclaimable_bytes || 0);
      if (!state.enabled) {
        total -= original;
        return;
      }
      const detail = summary.videos ? summary : null;
      if (detail) {
        let selected = 0;
        groupCandidateFiles(detail, state).forEach(file => {
          if (state.includedFileIds.has(file.id)) {
            selected += Number(file.size_bytes || 0);
          }
        });
        total += selected - original;
      }
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
    const pageSizes = [25, 50, 100].map(size =>
      `<option value="${size}"${Number(page.limit) === size ? ' selected' : ''}>${size}</option>`
    ).join('');
    return `<div class="maintenance-pager">` +
      `<div class="text-muted small">${escapeHtml(pageRangeText(page))}${page.large_result ? ' - large result set' : ''}</div>` +
      `<div class="toolbar-row mb-0">` +
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
    const checked = state.includedFileIds.has(file.id) ? ' checked' : '';
    const disabled = state.enabled ? '' : ' disabled';
    const kind = file.kind === 'video' ? 'Video' : 'Accessory';
    return `<tr>` +
      `<td><input class="form-check-input" type="checkbox" data-maint-file="${escapeHtml(file.id)}" data-maint-group="${escapeHtml(group.id)}" aria-label="Include ${escapeHtml(file.name)}"${checked}${disabled}></td>` +
      `<td>${escapeHtml(kind)}</td>` +
      `<td class="path-cell"><code title="${escapeHtml(file.path)}">${escapeHtml(file.name)}</code></td>` +
      `<td class="path-cell"><code title="${escapeHtml(fileDetails(file))}">${escapeHtml(fileDetails(file))}</code></td>` +
      `<td>${fileOperationOptions(group, file, state)}</td>` +
      `<td>${escapeHtml(file.size_label || formatSize(file.size_bytes))}</td>` +
      `</tr>`;
  }

  async function loadGroupDetails(groupId) {
    if (!currentScan?.id || !groupId) return;
    const state = groupState.get(groupId);
    if (state) state.loading = true;
    renderGroups();
    try {
      const res = await fetch(`/api/maintenance/duplicates/groups/${encodeURIComponent(groupId)}?scan_id=${encodeURIComponent(currentScan.id)}`);
      const data = await readJsonResponse(res);
      if (!res.ok) {
        setMessage(data.error || 'Group details unavailable', '');
        return;
      }
      mergeGroupDetail(data.group);
    } catch (e) {
      setMessage('Group details unavailable', e.message || '');
    } finally {
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
    const candidates = hasDetails ? groupCandidateFiles(group, state) : [];
    const rows = candidates.length
      ? candidates.map(file => fileRow(group, file, state)).join('')
      : '<tr><td colspan="6" class="text-muted text-center py-3">No files selected for cleanup in this group.</td></tr>';
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
            `<table class="table table-hover align-middle workspace-table maintenance-table">` +
            `<thead><tr><th>Include</th><th>Kind</th><th>File</th><th>Details</th><th>Operation</th><th>Size</th></tr></thead>` +
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
    if (planButton) planButton.disabled = !scan || scan.status !== 'success' || !(scan.duplicate_group_count || 0);
    if (!scan) {
      setMessage('No scan results yet.', '');
    } else if (scan.status === 'success') {
      setMessage(
        `${scan.duplicate_group_count || 0} duplicate groups found`,
        scan.large_result
          ? `Large result set. Loading ${groupPageLimit} groups at a time.`
          : (scan.reclaimable_label ? `Default reclaimable size: ${scan.reclaimable_label}` : '')
      );
      if (scan.duplicate_group_count && currentGroupsPage?.scan?.id !== scan.id) {
        loadGroupsPage(0);
      }
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
    const sample = (plan.files || []).slice(0, 8).map(file =>
      `<li><strong>${escapeHtml(file.operation || action)}</strong> <code title="${escapeHtml(file.source_path)}">${escapeHtml(file.relative_path || file.source_path)}</code>${file.destination_path ? ` -> <code title="${escapeHtml(file.destination_path)}">${escapeHtml(file.destination_name || file.destination_path)}</code>` : ''}</li>`
    ).join('');
    const review = (plan.manual_review || []).length
      ? `<div class="text-muted mt-2">${escapeHtml((plan.manual_review || []).length)} file(s) kept for manual review.</div>`
      : '';
    summary.innerHTML =
      `<div class="settings-panel">` +
      `<div class="panel-subheading"><i class="bi bi-list-check" aria-hidden="true"></i><span>Cleanup Plan</span></div>` +
      `<div class="metric-row mt-0"><span>${escapeHtml(action)} ${escapeHtml(plan.file_count || 0)} files</span><span>${escapeHtml(plan.total_size_label || '0 B')}</span>` +
      `${plan.move_root ? `<span>Quarantine: <code>${escapeHtml(plan.move_root)}</code></span>` : ''}</div>` +
      `${sample ? `<ul class="maintenance-plan-list mt-2">${sample}${(plan.files || []).length > 8 ? '<li class="text-muted">Additional files are included in the plan.</li>' : ''}</ul>` : '<div class="text-muted mt-2">No files selected.</div>'}` +
      `${review}` +
      `</div>`;
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
          groups: collectOverrides()
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
        Number(currentPlan.file_count || 0) >= 100
          ? `${currentPlan.file_count} files selected. This can take a while and will continue in the background.`
          : (currentPlan.total_size_label || '')
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
      const detail = `${counts}, ${apply.applied_count || 0} applied, ${apply.missing_count || 0} missing, ${apply.refused_count || 0} refused`;
      setMessage(apply.progress_label || 'Cleanup running', apply.large_operation ? `${detail}. Large cleanup is running in the background.` : detail);
      return;
    }
    if (apply.status === 'success') {
      stopApplyPolling();
      const result = apply.result || {};
      setMessage(
        `${result.applied_count || apply.applied_count || 0} files processed`,
        `${result.total_applied_label || '0 B'} cleaned, ${result.missing_count || apply.missing_count || 0} missing, ${result.refused_count || apply.refused_count || 0} refused`
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
    const actionLabel = currentPlan.action === 'delete' ? 'delete' : 'move';
    if (!window.confirm(`Apply this plan to ${actionLabel} ${currentPlan.file_count} files?`)) {
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
      `<table class="table table-hover align-middle workspace-table">` +
      `<thead><tr><th></th><th>Time</th><th>Action</th><th>Result</th><th>Log size</th></tr></thead>` +
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
      `<table class="table table-hover align-middle workspace-table">` +
      `<thead><tr><th>Status</th><th>Video</th><th>Size</th><th>Detail</th><th>BIF files</th></tr></thead>` +
      `<tbody>${rows}</tbody></table></div>` +
      `${previewPager(page)}`;
  }

  async function loadPreviewItems(offset = previewPageOffset) {
    if (!previewScan?.id || previewScan.status !== 'success') return;
    const status = byId('previewItemStatus')?.value || 'missing';
    const target = byId('previewItems');
    if (target) target.innerHTML = '<div class="text-muted text-center py-4">Loading video preview results...</div>';
    try {
      const res = await fetch(`/api/maintenance/video-previews/items?scan_id=${encodeURIComponent(previewScan.id)}&status=${encodeURIComponent(status)}&offset=${encodeURIComponent(offset)}&limit=${encodeURIComponent(previewPageLimit)}`);
      const data = await readJsonResponse(res);
      if (!res.ok) {
        setPreviewMessage(data.error || 'Video preview results unavailable', '');
        return;
      }
      previewItemsPage = data;
      previewPageOffset = Number(data.offset || 0);
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
      setPreviewMessage(
        `${scan.missing_count || 0} missing video preview${(scan.missing_count || 0) === 1 ? '' : 's'}`,
        `${scan.present_count || 0} present`
      );
      if (previewItemsPage?.scan?.id !== scan.id) {
        loadPreviewItems(0);
      }
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
      message.textContent = result.message || 'Uses the Emby settings from the Landscape Posters panel.';
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
    if (planButton) planButton.disabled = active || !scan || scan.status !== 'success' || !(scan.repairable_count || 0);
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
    const rows = (page.items || []).map(item =>
      `<tr>` +
      `<td>${qualityStatusBadge(item.status)}</td>` +
      `<td class="path-cell"><code title="${escapeHtml(item.path)}">${escapeHtml(item.relative_path || item.name)}</code></td>` +
      `<td class="path-cell"><code title="${escapeHtml(item.video_path)}">${escapeHtml(item.video_relative_path || item.video_name || '')}</code></td>` +
      `<td>${escapeHtml(item.confidence || 0)}%</td>` +
      `<td>${escapeHtml(item.frame_count_detail || item.frame_count || 0)}</td>` +
      `<td>${escapeHtml(formatIntervalSeconds(item.interval_seconds))}</td>` +
      `<td>${escapeHtml(qualitySampleSummary(item))}</td>` +
      `<td class="path-cell"><code title="${escapeHtml(item.reason || '')}">${escapeHtml(item.reason || '')}</code></td>` +
      `<td>${escapeHtml(item.size_label || formatSize(item.size_bytes))}</td>` +
      `</tr>`
    ).join('');
    target.innerHTML =
      `${qualityPager(page)}` +
      `<div class="table-responsive workspace-table-wrap">` +
      `<table class="table table-hover align-middle workspace-table">` +
      `<thead><tr><th>Status</th><th>BIF</th><th>Video</th><th>Confidence</th><th>Frames Actual / Expected</th><th>Interval</th><th>Sample</th><th>Reason</th><th>Size</th></tr></thead>` +
      `<tbody>${rows}</tbody></table></div>` +
      `${qualityPager(page)}`;
  }

  async function loadQualityItems(offset = qualityPageOffset) {
    if (!qualityScan?.id || qualityScan.status !== 'success') return;
    const status = byId('qualityItemStatus')?.value || 'problem';
    const target = byId('qualityItems');
    if (target) target.innerHTML = '<div class="text-muted text-center py-4">Loading BIF quality results...</div>';
    try {
      const res = await fetch(`/api/maintenance/video-previews/quality/items?scan_id=${encodeURIComponent(qualityScan.id)}&status=${encodeURIComponent(status)}&offset=${encodeURIComponent(offset)}&limit=${encodeURIComponent(qualityPageLimit)}`);
      const data = await readJsonResponse(res);
      if (!res.ok) {
        setQualityMessage(data.error || 'BIF quality results unavailable', '');
        return;
      }
      qualityItemsPage = data;
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
        `${scan.warning_count || 0} warnings, ${scan.ok_count || 0} passed`
      );
      if (qualityItemsPage?.scan?.id !== scan.id) {
        loadQualityItems(0);
      }
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
    const sample = (plan.files || []).slice(0, 8).map(file =>
      `<li><strong>move</strong> <code title="${escapeHtml(file.source_path)}">${escapeHtml(file.relative_path || file.source_path)}</code> -> <code title="${escapeHtml(file.destination_path)}">${escapeHtml(file.destination_path || '')}</code><div class="text-muted small">${escapeHtml(file.confidence || 0)}% confidence - ${escapeHtml(file.reason || '')}</div></li>`
    ).join('');
    const review = (plan.manual_review || []).length
      ? `<div class="text-muted mt-2">${escapeHtml((plan.manual_review || []).length)} file(s) kept for manual review.</div>`
      : '';
    summary.innerHTML =
      `<div class="settings-panel">` +
      `<div class="panel-subheading"><i class="bi bi-list-check" aria-hidden="true"></i><span>BIF Repair Plan</span></div>` +
      `<div class="metric-row mt-0"><span>Move ${escapeHtml(plan.file_count || 0)} BIF files</span><span>${escapeHtml(plan.total_size_label || '0 B')}</span>` +
      `<span>Quarantine: <code>${escapeHtml(plan.move_root || '')}</code></span>` +
      `<span>Emby: ${plan.trigger_emby ? 'trigger after move' : 'no automatic trigger'}</span></div>` +
      `${sample ? `<ul class="maintenance-plan-list mt-2">${sample}${(plan.files || []).length > 8 ? '<li class="text-muted">Additional files are included in the plan.</li>' : ''}</ul>` : '<div class="text-muted mt-2">No files selected.</div>'}` +
      `${review}` +
      `</div>`;
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
          trigger_emby: Boolean(byId('qualityTriggerEmby')?.checked)
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
      const detail = `${counts}, ${apply.applied_count || 0} moved, ${apply.missing_count || 0} missing, ${apply.refused_count || 0} refused`;
      setQualityMessage(apply.progress_label || 'BIF repair running', apply.large_operation ? `${detail}. Large repair is running in the background.` : detail);
      return;
    }
    if (apply.status === 'success') {
      stopQualityApplyPolling();
      const result = apply.result || {};
      const emby = result.emby || {};
      const extraction = emby.extraction || {};
      setQualityMessage(
        `${result.applied_count || apply.applied_count || 0} BIF files moved`,
        `${result.total_applied_label || '0 B'} quarantined, ${result.missing_count || apply.missing_count || 0} missing, ${result.refused_count || apply.refused_count || 0} refused${extraction.status ? `. Emby extraction: ${extraction.status}` : ''}`
      );
      refreshPreviewTasks();
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
    if (!window.confirm(`Move ${qualityPlan.file_count} bad BIF file(s) to the repair quarantine?`)) {
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
      return `<div class="mb-1">` +
        `<code class="path-cell" title="${escapeHtml(file.path || '')}">${escapeHtml(file.relative_path || file.name || '')}</code>` +
        `<div class="text-muted small">${escapeHtml(code)} · ${escapeHtml(file.size_label || '')}${file.modified_at ? ` · ${escapeHtml(file.modified_at)}` : ''}</div>` +
        `</div>`;
    }).join('') + (files.length > 3 ? `<div class="text-muted small">${files.length - 3} more subtitle file(s)</div>` : '');
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
        `<td>${escapeHtml(codes)}</td>` +
        `<td>${escapeHtml(item.detail || '')}</td>` +
        `</tr>`;
    }).join('');
    target.innerHTML =
      `${subtitlePager(page)}` +
      `<div class="table-responsive workspace-table-wrap">` +
      `<table class="table table-hover align-middle workspace-table">` +
      `<thead><tr><th>Status</th><th>Video</th><th>Matched SRTs</th><th>Language</th><th>Reason</th></tr></thead>` +
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
        q: query
      });
      const res = await fetch(`/api/maintenance/subtitles/items?${params.toString()}`);
      const data = await readJsonResponse(res);
      if (!res.ok) {
        setSubtitleMessage(data.error || 'Subtitle results unavailable', '');
        return;
      }
      subtitleItemsPage = data;
      subtitlePageOffset = Number(data.offset || 0);
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
      setSubtitleMessage(
        `${scan.review_count || 0} subtitle review item${(scan.review_count || 0) === 1 ? '' : 's'}`,
        `${scan.missing_count || 0} missing, ${scan.language_review_count || 0} language review, ${scan.unknown_count || 0} unknown. Expected: ${(settings.expected_languages || []).join(', ') || 'not set'}`
      );
      if (subtitleItemsPage?.scan?.id !== scan.id) {
        loadSubtitleItems(0);
      }
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
    if (planButton) planButton.disabled = active || !scan || scan.status !== 'success' || !(scan.ready_count || 0);
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
      `<table class="table table-hover align-middle workspace-table">` +
      `<thead><tr><th>Import</th><th>Actor</th><th>Candidate Image</th><th>Related Video</th><th>Exception</th></tr></thead>` +
      `<tbody>${rows}</tbody></table></div>` +
      `${actorPager(page)}`;
  }

  async function loadActorItems(offset = actorPageOffset) {
    if (!actorScan?.id || actorScan.status !== 'success') return;
    const status = byId('actorItemStatus')?.value || 'ready';
    const target = byId('actorItems');
    if (target) target.innerHTML = '<div class="text-muted text-center py-4">Loading actor image results...</div>';
    try {
      const res = await fetch(`/api/maintenance/actor-images/items?scan_id=${encodeURIComponent(actorScan.id)}&status=${encodeURIComponent(status)}&offset=${encodeURIComponent(offset)}&limit=${encodeURIComponent(actorPageLimit)}`);
      const data = await readJsonResponse(res);
      if (!res.ok) {
        setActorMessage(data.error || 'Actor image results unavailable', '');
        return;
      }
      actorItemsPage = data;
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
        `${scan.ready_count || 0} ready, ${scan.ambiguous_count || 0} ambiguous, ${scan.no_candidate_count || 0} without local images`
      );
      if (actorItemsPage?.scan?.id !== scan.id) {
        loadActorItems(0);
      }
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
    const sample = (plan.files || []).slice(0, 8).map(file =>
      `<li><strong>import</strong> <code title="${escapeHtml(file.candidate_path)}">${escapeHtml(file.candidate_relative_path || file.candidate_name)}</code> -> ${escapeHtml(file.person_name || '')}</li>`
    ).join('');
    summary.innerHTML =
      `<div class="settings-panel">` +
      `<div class="panel-subheading"><i class="bi bi-list-check" aria-hidden="true"></i><span>Actor Image Import Plan</span></div>` +
      `<div class="metric-row mt-0"><span>Import ${escapeHtml(plan.file_count || 0)} actor image(s)</span><span>${escapeHtml((plan.skipped || []).length)} skipped</span></div>` +
      `${sample ? `<ul class="maintenance-plan-list mt-2">${sample}${(plan.files || []).length > 8 ? '<li class="text-muted">Additional actors are included in the plan.</li>' : ''}</ul>` : '<div class="text-muted mt-2">No actor images selected.</div>'}` +
      `</div>`;
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
    if (!window.confirm(`Import ${actorPlan.file_count} actor image(s) into Emby?`)) {
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
    const date = formatDateLabel(result.checked_at, 'unknown time');
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
      lastRefresh.textContent = `Last refresh: ${embyResultLabel(lastRefreshResult, 'never')}`;
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

  function applyPosterSettings(settings) {
    if (!settings) return;
    const enabled = byId('posterAutomationEnabled');
    const scan = byId('posterScanInterval');
    const full = byId('posterFullScanInterval');
    const embyEnabled = byId('posterEmbyRefreshEnabled');
    const embyUrl = byId('posterEmbyUrl');
    const apiKey = byId('posterEmbyApiKey');
    if (enabled) enabled.checked = Boolean(settings.enabled);
    if (scan) scan.value = settings.scan_interval_seconds || 900;
    if (full) full.value = settings.full_scan_interval_seconds || 86400;
    if (embyEnabled) embyEnabled.checked = Boolean(settings.emby_refresh_enabled);
    if (embyUrl) embyUrl.value = settings.emby_url || '';
    if (apiKey) {
      apiKey.value = '';
      apiKey.placeholder = settings.emby_api_key_configured ? 'Configured; leave blank to keep current' : 'API key';
    }
  }

  function posterStatusBadge(status) {
    if (status === 'updated') return '<span class="badge text-bg-success">Updated</span>';
    if (status === 'already_matching') return '<span class="badge text-bg-secondary">Matched</span>';
    if (status === 'missing_poster') return '<span class="badge text-bg-warning">Missing poster</span>';
    if (status === 'error' || status === 'failed') return '<span class="badge text-bg-danger">Error</span>';
    return `<span class="badge text-bg-secondary">${escapeHtml(status || 'Skipped')}</span>`;
  }

  function renderPosterItems(run) {
    const wrap = byId('posterRecentItems');
    if (!wrap) return;
    const items = run?.items || [];
    const rows = items.length ? items.map(item =>
      `<tr>` +
      `<td>${posterStatusBadge(item.status)}</td>` +
      `<td class="path-cell"><code title="${escapeHtml(item.source)}">${escapeHtml(item.source)}</code></td>` +
      `<td class="path-cell"><code title="${escapeHtml(item.poster)}">${escapeHtml(item.poster)}</code></td>` +
      `<td>${escapeHtml(item.message || '')}</td>` +
      `</tr>`
    ).join('') : '<tr><td colspan="4" class="text-muted text-center py-4">No landscape poster changes in the latest run.</td></tr>';
    wrap.innerHTML =
      `<table class="table table-hover align-middle workspace-table">` +
      `<thead><tr><th>Status</th><th>Background</th><th>Poster</th><th>Detail</th></tr></thead>` +
      `<tbody>${rows}</tbody></table>`;
  }

  function renderPosterStatus(data) {
    const settings = data?.settings || {};
    const current = data?.current_run;
    const last = current || data?.last_run;
    const counters = last?.counters || {};
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
    if (!current && settings.enabled) {
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
    renderPosterItems(last);
  }

  async function refreshPosterStatus() {
    try {
      const res = await fetch('/api/maintenance/landscape-posters/status');
      const data = await readJsonResponse(res);
      if (!res.ok) {
        setPosterMessage(data.error || 'Landscape poster status unavailable', '');
        return;
      }
      if (!posterSettingsLoaded) {
        applyPosterSettings(data.settings);
        posterSettingsLoaded = true;
      }
      renderPosterStatus(data);
    } catch (e) {
      setPosterMessage('Landscape poster status unavailable', e.message || '');
    }
  }

  function collectPosterSettings() {
    const apiKeyValue = byId('posterEmbyApiKey')?.value || '';
    const payload = {
      enabled: Boolean(byId('posterAutomationEnabled')?.checked),
      scan_interval_seconds: Number(byId('posterScanInterval')?.value || 900),
      full_scan_interval_seconds: Number(byId('posterFullScanInterval')?.value || 86400),
      emby_refresh_enabled: Boolean(byId('posterEmbyRefreshEnabled')?.checked),
      emby_url: byId('posterEmbyUrl')?.value.trim() || ''
    };
    if (apiKeyValue) {
      payload.emby_api_key = apiKeyValue;
    }
    return payload;
  }

  async function savePosterSettings() {
    setPosterMessage('Saving landscape poster settings', '');
    try {
      const res = await fetch('/api/maintenance/landscape-posters/settings', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(collectPosterSettings())
      });
      const data = await readJsonResponse(res);
      if (!res.ok) {
        setPosterMessage(data.error || 'Settings could not be saved', '');
        return;
      }
      applyPosterSettings(data.settings);
      posterSettingsLoaded = true;
      renderPosterStatus(data.status);
      setPosterMessage('Landscape poster settings saved', '');
    } catch (e) {
      setPosterMessage('Settings could not be saved', e.message || '');
    }
  }

  async function testEmbyConnection() {
    const button = byId('posterEmbyTestButton');
    const message = byId('posterEmbyStatusMessage');
    if (button) button.disabled = true;
    if (message) {
      message.className = 'scan-estimate-detail mt-1';
      message.textContent = 'Testing Emby connection...';
    }
    setPosterMessage('Testing Emby connection', '');
    try {
      const res = await fetch('/api/maintenance/landscape-posters/emby/test', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(collectPosterSettings())
      });
      const data = await readJsonResponse(res);
      if (!res.ok) {
        setPosterMessage(data.error || 'Emby connection test failed', '');
        return;
      }
      renderPosterStatus(data.status);
      setPosterMessage('Emby connection test complete', data.result?.message || '');
    } catch (e) {
      setPosterMessage('Emby connection test failed', e.message || '');
      if (message) {
        message.className = 'scan-estimate-detail mt-1 text-danger';
        message.textContent = e.message || '';
      }
    } finally {
      if (button) button.disabled = false;
    }
  }

  async function runLandscapePosters() {
    const button = byId('posterRunButton');
    if (button) button.disabled = true;
    setPosterMessage('Starting landscape poster run', '');
    try {
      const res = await fetch('/api/maintenance/landscape-posters/run', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({path: config.libRoot || '/library', mode: 'full'})
      });
      const data = await readJsonResponse(res);
      if (!res.ok) {
        setPosterMessage(data.error || 'Landscape poster run could not start', '');
        return;
      }
      renderPosterStatus({current_run: data.run, settings: collectPosterSettings(), scheduler: {}});
      if (!posterPollTimer) {
        posterPollTimer = setInterval(refreshPosterStatus, 3000);
      }
    } catch (e) {
      setPosterMessage('Landscape poster run could not start', e.message || '');
    } finally {
      if (button) button.disabled = false;
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
    byId('previewItemStatus')?.addEventListener('change', () => {
      previewPageOffset = 0;
      loadPreviewItems(0);
    });
    byId('qualityScanButton')?.addEventListener('click', startQualityScan);
    byId('qualityCancelButton')?.addEventListener('click', cancelQualityScan);
    byId('qualityPlanButton')?.addEventListener('click', reviewQualityPlan);
    byId('qualityApplyButton')?.addEventListener('click', applyQualityPlan);
    byId('qualityItemStatus')?.addEventListener('change', () => {
      qualityPageOffset = 0;
      loadQualityItems(0);
    });
    byId('subtitleBrowseButton')?.addEventListener('click', () => {
      openSubtitleBrowser(byId('subtitlePath')?.value.trim() || config.libRoot || '/library');
    });
    byId('subtitleScanButton')?.addEventListener('click', startSubtitleScan);
    byId('subtitleCancelScanButton')?.addEventListener('click', cancelSubtitleScan);
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
    byId('posterSaveSettingsButton')?.addEventListener('click', savePosterSettings);
    byId('posterEmbyTestButton')?.addEventListener('click', testEmbyConnection);
    byId('posterRunButton')?.addEventListener('click', runLandscapePosters);
    byId('posterRefreshButton')?.addEventListener('click', refreshPosterStatus);
    byId('maintenanceRefreshLogsButton')?.addEventListener('click', refreshMaintenanceLogs);
    byId('maintenanceAction')?.addEventListener('change', () => {
      invalidatePlan();
      updateSelectedSize();
    });

    byId('maintenanceGroups')?.addEventListener('click', event => {
      const page = event.target.closest('[data-maint-page]');
      const expand = event.target.closest('[data-maint-expand]');
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

    byId('maintenanceGroups')?.addEventListener('change', event => {
      const enabled = event.target.closest('[data-maint-group-enabled]');
      const keep = event.target.closest('[data-maint-keep]');
      const file = event.target.closest('[data-maint-file]');
      const operation = event.target.closest('[data-maint-operation]');
      const pageLimit = event.target.closest('[data-maint-page-limit]');
      if (pageLimit) {
        groupPageLimit = Number(pageLimit.value || 25);
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
          state.candidateSignature = '';
        }
        markGroupDirty(groupId);
        renderGroups(currentScan);
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
    refreshPreviewTasks();
    refreshSubtitleStatus();
    refreshActorStatus();
    refreshPosterStatus();
    posterPollTimer = setInterval(refreshPosterStatus, 10000);
    window.addEventListener('beforeunload', () => {
      clearTimeout(overviewPollTimer);
      clearTimeout(overviewSearchTimer);
      clearTimeout(subtitleSearchTimer);
      stopSubtitlePolling();
    });
  });
}());
