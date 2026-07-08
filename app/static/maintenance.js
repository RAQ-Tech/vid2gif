(function () {
  const config = window.vid2gifMaintenanceConfig || {};
  const groupState = new Map();
  let currentScan = null;
  let currentPlan = null;
  let pollTimer = null;

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
    return 'Accessory';
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
    if (!groupState.has(group.id)) {
      groupState.set(group.id, {
        enabled: true,
        keepId: group.recommended_keep_id,
        includedFileIds: new Set(),
        candidateSignature: ''
      });
    }
    const state = groupState.get(group.id);
    const candidates = groupCandidateFiles(group, state);
    const signature = candidates.map(file => file.id).join('|');
    if (state.candidateSignature !== signature) {
      state.candidateSignature = signature;
      state.includedFileIds = new Set(candidates.map(file => file.id));
    }
    return state;
  }

  function updateSelectedSize() {
    let total = 0;
    (currentScan?.groups || []).forEach(group => {
      const state = ensureGroupState(group);
      if (!state.enabled) return;
      groupCandidateFiles(group, state).forEach(file => {
        if (state.includedFileIds.has(file.id)) {
          total += Number(file.size_bytes || 0);
        }
      });
    });
    const selected = byId('maintenanceSelectedSize');
    if (selected) selected.textContent = formatSize(total);
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
      `<td>${escapeHtml(file.size_label || formatSize(file.size_bytes))}</td>` +
      `</tr>`;
  }

  function renderGroup(group) {
    const state = ensureGroupState(group);
    const candidates = groupCandidateFiles(group, state);
    const rows = candidates.length
      ? candidates.map(file => fileRow(group, file, state)).join('')
      : '<tr><td colspan="5" class="text-muted text-center py-3">No files selected for cleanup in this group.</td></tr>';
    const keeper = (group.videos || []).find(video => video.id === state.keepId);
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
      `<label class="form-label mb-0 compact-control">Keeper` +
      `<select class="form-select form-select-sm" data-maint-keep="${escapeHtml(group.id)}">` +
      `${(group.videos || []).map(video => groupOption(video, state.keepId)).join('')}` +
      `</select></label>` +
      `</div>` +
      `<div class="maintenance-group-summary">` +
      `<span>${escapeHtml((group.videos || []).length)} videos</span>` +
      `<span>${escapeHtml(group.accessory_count || 0)} accessory files</span>` +
      `<span>Recommended: ${escapeHtml(keeper?.name || '')}</span>` +
      `<span>Default reclaimable: ${escapeHtml(group.reclaimable_label || '')}</span>` +
      `</div>` +
      `<div class="table-responsive workspace-table-wrap mt-2">` +
      `<table class="table table-hover align-middle workspace-table maintenance-table">` +
      `<thead><tr><th>Include</th><th>Kind</th><th>File</th><th>Details</th><th>Size</th></tr></thead>` +
      `<tbody>${rows}</tbody>` +
      `</table></div>` +
      `</section>`;
  }

  function renderGroups(scan) {
    const target = byId('maintenanceGroups');
    if (!target) return;
    if (!scan || !(scan.groups || []).length) {
      target.innerHTML = '<div class="text-muted text-center py-4">No duplicate groups found.</div>';
      updateSelectedSize();
      return;
    }
    target.innerHTML = scan.groups.map(renderGroup).join('');
    updateSelectedSize();
  }

  async function openBrowser(path) {
    const browser = byId('maintenanceBrowser');
    if (!browser) return;
    browser.innerHTML = '<div class="small text-muted">Loading folders...</div>';
    try {
      const res = await fetch(`/api/media-browser?path=${encodeURIComponent(path || config.libRoot || '/library')}`);
      const data = await res.json();
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

  function stopPolling() {
    if (pollTimer) {
      clearInterval(pollTimer);
      pollTimer = null;
    }
  }

  function handleScan(scan) {
    currentScan = scan;
    setProgress(scan);
    renderGroups(scan);
    const planButton = byId('maintenancePlanButton');
    if (planButton) planButton.disabled = !scan || scan.status !== 'success' || !(scan.groups || []).length;
    if (!scan) {
      setMessage('No scan results yet.', '');
    } else if (scan.status === 'success') {
      setMessage(
        `${scan.duplicate_group_count || 0} duplicate groups found`,
        scan.reclaimable_label ? `Default reclaimable size: ${scan.reclaimable_label}` : ''
      );
    } else if (scan.status === 'failed') {
      setMessage('Scan failed', scan.error || '');
    } else {
      setMessage(scan.progress_label || 'Scanning', '');
    }
    if (scan && (scan.status === 'success' || scan.status === 'failed')) {
      stopPolling();
    }
  }

  async function pollScan(scanId) {
    if (!scanId) return;
    try {
      const res = await fetch(`/api/maintenance/duplicates/status?scan_id=${encodeURIComponent(scanId)}`);
      const data = await res.json();
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
    currentPlan = null;
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
      const data = await res.json();
      if (!res.ok) {
        setMessage(data.error || 'Scan could not start', '');
        return;
      }
      handleScan(data.scan);
      pollTimer = setInterval(() => pollScan(data.scan.id), 1000);
    } catch (e) {
      setMessage('Scan could not start', e.message || '');
    } finally {
      if (button) button.disabled = false;
    }
  }

  function collectOverrides() {
    return (currentScan?.groups || []).map(group => {
      const state = ensureGroupState(group);
      const keepId = state.keepId || group.recommended_keep_id;
      const candidates = groupCandidateFiles(group, state);
      return {
        id: group.id,
        enabled: state.enabled,
        keep_video_id: keepId,
        remove_video_ids: (group.videos || []).filter(video => video.id !== keepId).map(video => video.id),
        include_file_ids: candidates.filter(file => state.includedFileIds.has(file.id)).map(file => file.id)
      };
    });
  }

  function renderPlan(plan) {
    const summary = byId('maintenancePlanSummary');
    if (!summary) return;
    const action = plan.action === 'delete' ? 'Delete' : 'Move';
    const sample = (plan.files || []).slice(0, 8).map(file =>
      `<li><code title="${escapeHtml(file.source_path)}">${escapeHtml(file.relative_path || file.source_path)}</code></li>`
    ).join('');
    summary.innerHTML =
      `<div class="settings-panel">` +
      `<div class="panel-subheading"><i class="bi bi-list-check" aria-hidden="true"></i><span>Cleanup Plan</span></div>` +
      `<div class="metric-row mt-0"><span>${escapeHtml(action)} ${escapeHtml(plan.file_count || 0)} files</span><span>${escapeHtml(plan.total_size_label || '0 B')}</span>` +
      `${plan.move_root ? `<span>Quarantine: <code>${escapeHtml(plan.move_root)}</code></span>` : ''}</div>` +
      `${sample ? `<ul class="maintenance-plan-list mt-2">${sample}${(plan.files || []).length > 8 ? '<li class="text-muted">Additional files are included in the plan.</li>' : ''}</ul>` : '<div class="text-muted mt-2">No files selected.</div>'}` +
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
      const data = await res.json();
      if (!res.ok) {
        setMessage(data.error || 'Plan could not be built', '');
        return;
      }
      currentPlan = data.plan;
      renderPlan(currentPlan);
      byId('maintenanceApplyButton').disabled = !currentPlan.file_count;
      setMessage('Review the cleanup plan before applying', currentPlan.total_size_label || '');
    } catch (e) {
      setMessage('Plan could not be built', e.message || '');
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
      const data = await res.json();
      if (!res.ok) {
        setMessage(data.error || 'Cleanup failed', '');
        return;
      }
      const result = data.result || {};
      setMessage(
        `${result.applied_count || 0} files processed`,
        `${result.total_applied_label || '0 B'} cleaned, ${result.missing_count || 0} missing, ${result.refused_count || 0} refused`
      );
      currentPlan = null;
    } catch (e) {
      setMessage('Cleanup failed', e.message || '');
    }
  }

  function initEvents() {
    byId('maintenanceBrowseButton')?.addEventListener('click', () => {
      openBrowser(byId('maintenancePath')?.value.trim() || config.libRoot || '/library');
    });
    byId('maintenanceScanButton')?.addEventListener('click', startScan);
    byId('maintenancePlanButton')?.addEventListener('click', reviewPlan);
    byId('maintenanceApplyButton')?.addEventListener('click', applyPlan);
    byId('maintenanceAction')?.addEventListener('change', () => {
      invalidatePlan();
      updateSelectedSize();
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

    byId('maintenanceGroups')?.addEventListener('change', event => {
      const enabled = event.target.closest('[data-maint-group-enabled]');
      const keep = event.target.closest('[data-maint-keep]');
      const file = event.target.closest('[data-maint-file]');
      if (enabled) {
        const groupId = enabled.getAttribute('data-maint-group-enabled');
        const state = groupState.get(groupId);
        if (state) state.enabled = enabled.checked;
        invalidatePlan();
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
        invalidatePlan();
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
        invalidatePlan();
        updateSelectedSize();
      }
    });
  }

  document.addEventListener('DOMContentLoaded', () => {
    initEvents();
    setProgress(null);
    openBrowser(config.libRoot || '/library');
  });
}());
