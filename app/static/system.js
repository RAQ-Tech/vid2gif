(function () {
  const byId = id => document.getElementById(id);

  function escapeHtml(value) {
    return String(value ?? '')
      .replaceAll('&', '&amp;')
      .replaceAll('<', '&lt;')
      .replaceAll('>', '&gt;')
      .replaceAll('"', '&quot;')
      .replaceAll("'", '&#039;');
  }

  function statusLabel(status) {
    if (status === 'pass') return 'Ready';
    if (status === 'warn') return 'Attention';
    return 'Unavailable';
  }

  function statusBadge(status) {
    const tone = status === 'pass' ? 'success' : (status === 'warn' ? 'warning' : 'danger');
    return `<span class="badge text-bg-${tone}">${escapeHtml(statusLabel(status))}</span>`;
  }

  function renderChecks(checks) {
    const target = byId('systemChecks');
    if (!target) return;
    target.innerHTML = (checks || []).map(check =>
      `<div class="system-check-row">` +
        `<div class="system-check-state">${statusBadge(check.status)}</div>` +
        `<div class="system-check-copy">` +
          `<strong>${escapeHtml(check.label)}</strong>` +
          `<span>${escapeHtml(check.detail)}</span>` +
          `${check.path ? `<code title="${escapeHtml(check.path)}">${escapeHtml(check.path)}</code>` : ''}` +
        `</div>` +
      `</div>`
    ).join('') || '<div class="text-muted text-center py-4">No checks were returned.</div>';
  }

  function renderStorage(items) {
    const target = byId('systemStorage');
    if (!target) return;
    target.innerHTML = (items || []).map(item => {
      const percent = Math.max(0, Math.min(100, Number(item.used_percent || 0)));
      return `<div class="system-storage-item">` +
        `<div class="d-flex justify-content-between gap-3">` +
          `<strong>${escapeHtml(item.label)}</strong>` +
          `<span class="text-muted small">${escapeHtml(item.free_label)} free</span>` +
        `</div>` +
        `<code title="${escapeHtml(item.path)}">${escapeHtml(item.path)}</code>` +
        `<div class="progress progress-thin mt-2" role="progressbar" aria-label="${escapeHtml(item.label)} storage used" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${percent}">` +
          `<div class="progress-bar" style="width: ${percent}%"></div>` +
        `</div>` +
        `<div class="metric-row"><span>${escapeHtml(item.used_label)} used</span><span>${escapeHtml(item.total_label)} total</span></div>` +
      `</div>`;
    }).join('') || '<div class="text-muted text-center py-4">Storage details are unavailable.</div>';
  }

  function renderRuntime(runtime) {
    const target = byId('systemRuntime');
    if (!target) return;
    const rows = [
      ['Python', runtime?.python || 'Unknown'],
      ['Platform', runtime?.platform || 'Unknown'],
      ['Process', runtime?.process_id || 'Unknown'],
      ['Started', runtime?.started_at || 'Unknown']
    ];
    target.innerHTML = rows.map(([label, value]) =>
      `<div><dt>${escapeHtml(label)}</dt><dd>${escapeHtml(value)}</dd></div>`
    ).join('');
  }

  function renderStatus(payload) {
    const state = String(payload.overall || 'unhealthy');
    const stateLabel = state === 'healthy' ? 'Healthy' : (state === 'attention' ? 'Needs attention' : 'Unhealthy');
    byId('systemOverall').textContent = stateLabel;
    byId('systemOverall').className = `metric-value system-overall-${state}`;
    byId('systemUptime').textContent = payload.uptime_label || '--';
    byId('systemActiveWork').textContent = String(payload.active_work_count || 0);
    const stateStorage = (payload.storage || []).find(item => item.label === 'State');
    byId('systemStateFree').textContent = stateStorage?.free_label || 'Unavailable';

    const issueCount = Number(payload.failed_count || 0) + Number(payload.warning_count || 0);
    byId('systemMessageTitle').textContent = issueCount ? `${issueCount} system check(s) need attention.` : 'All system checks passed.';
    byId('systemMessageDetail').textContent = payload.generated_at ? `Last checked ${payload.generated_at}` : '';
    renderChecks(payload.checks);
    renderRuntime(payload.runtime || {});
    renderStorage(payload.storage);
  }

  async function refreshStatus() {
    const button = byId('systemRefreshButton');
    if (button) button.disabled = true;
    try {
      const response = await fetch('/api/system/status');
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.error || 'Status request failed');
      renderStatus(payload);
    } catch (error) {
      byId('systemOverall').textContent = 'Unavailable';
      byId('systemMessageTitle').textContent = 'System status could not be loaded.';
      byId('systemMessageDetail').textContent = error.message || '';
    } finally {
      if (button) button.disabled = false;
    }
  }

  byId('systemRefreshButton')?.addEventListener('click', refreshStatus);
  refreshStatus();
  window.setInterval(refreshStatus, 30000);
})();
