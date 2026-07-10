(function () {
  const config = window.vid2gifMaintenanceConfig || {};
  const maintenanceTabHashes = ['posters', 'duplicates', 'video-previews'];
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
      `<td>${escapeHtml(item.frame_count || 0)}</td>` +
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
      `<thead><tr><th>Status</th><th>BIF</th><th>Video</th><th>Confidence</th><th>Frames</th><th>Interval</th><th>Sample</th><th>Reason</th><th>Size</th></tr></thead>` +
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
    const safeHash = maintenanceTabHashes.includes(hash) ? hash : 'posters';
    const button = document.querySelector(`[data-maint-tab-hash="${safeHash}"]`);
    if (!button || !window.bootstrap) return;
    window.bootstrap.Tab.getOrCreateInstance(button).show();
    localStorage.setItem('maintenance_active_tab', safeHash);
    if (updateUrl) {
      history.replaceState(null, '', `#${safeHash}`);
    }
  }

  function initMaintenanceTabs() {
    const requested = location.hash.replace('#', '');
    const saved = localStorage.getItem('maintenance_active_tab');
    activateMaintenanceTab(maintenanceTabHashes.includes(requested) ? requested : (saved || 'posters'), false);
    document.querySelectorAll('[data-maint-tab-hash]').forEach(button => {
      button.addEventListener('shown.bs.tab', event => {
        const hash = event.target.getAttribute('data-maint-tab-hash') || 'posters';
        localStorage.setItem('maintenance_active_tab', hash);
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
  }

  document.addEventListener('DOMContentLoaded', () => {
    initMaintenanceTabs();
    initEvents();
    setProgress(null);
    setPreviewProgress(null);
    setQualityProgress(null);
    openBrowser(config.libRoot || '/library');
    openPreviewBrowser(config.libRoot || '/library');
    refreshMaintenanceLogs();
    refreshPreviewTasks();
    refreshPosterStatus();
    posterPollTimer = setInterval(refreshPosterStatus, 10000);
  });
}());
