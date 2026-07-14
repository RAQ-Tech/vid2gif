(function () {
  'use strict';

  const byId = id => document.getElementById(id);
  const escapeHtml = value => String(value ?? '').replace(/[&<>"']/g, character => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[character]));

  let timer = null;
  let expanded = false;

  function statusClass(status) {
    if (['success', 'completed'].includes(status)) return 'text-bg-success';
    if (['failed'].includes(status)) return 'text-bg-danger';
    if (['cancelled', 'stopped', 'interrupted'].includes(status)) return 'text-bg-warning';
    if (['running', 'cancelling'].includes(status)) return 'text-bg-primary';
    return 'text-bg-secondary';
  }

  function operationMarkup(operation, includeCancel = false) {
    if (!operation) return '';
    const action = operation.href
      ? `<a class="btn btn-outline-secondary btn-sm" href="${escapeHtml(operation.href)}">Open</a>`
      : '';
    const cancel = includeCancel && operation.cancel_url
      ? `<button type="button" class="btn btn-outline-danger btn-sm" data-activity-cancel="${escapeHtml(operation.cancel_url)}">Cancel</button>`
      : '';
    return `<div class="global-activity-row">
      <div class="global-activity-row-copy">
        <div><strong>${escapeHtml(operation.label)}</strong> <span class="badge ${statusClass(operation.status)}">${escapeHtml(operation.status)}</span></div>
        <div class="text-muted small">${escapeHtml(operation.progress_label || operation.path || '')}</div>
        ${operation.path ? `<code title="${escapeHtml(operation.path)}">${escapeHtml(operation.path)}</code>` : ''}
      </div>
      <div class="global-activity-actions">${cancel}${action}</div>
    </div>`;
  }

  function render(payload) {
    const root = byId('globalActivity');
    if (!root) return;
    const current = payload.current || null;
    const waiting = payload.waiting || [];
    const recent = payload.recent || [];
    const hasContent = Boolean(current || waiting.length || recent.length);
    root.classList.toggle('d-none', !hasContent);
    if (!hasContent) return;

    byId('globalActivityTitle').textContent = current ? current.label : 'Recent library activity';
    byId('globalActivityLabel').textContent = current
      ? (current.progress_label || current.status || 'Running')
      : (recent[0]?.progress_label || recent[0]?.status || 'Idle');
    const waitingBadge = byId('globalActivityWaiting');
    waitingBadge.classList.toggle('d-none', waiting.length === 0);
    waitingBadge.textContent = `${waiting.length} waiting`;

    const progress = byId('globalActivityProgress');
    const rawPercent = current?.progress_percent;
    const percent = rawPercent === null || rawPercent === ''
      ? Number.NaN
      : Number(rawPercent);
    const determinate = current && Number.isFinite(percent);
    progress.classList.toggle('d-none', !current);
    progress.classList.toggle('progress-indeterminate', Boolean(current && !determinate));
    const bounded = determinate ? Math.max(0, Math.min(100, Math.round(percent))) : 100;
    byId('globalActivityProgressBar').style.width = `${bounded}%`;
    if (determinate) progress.setAttribute('aria-valuenow', String(bounded));
    else progress.removeAttribute('aria-valuenow');
    progress.setAttribute('aria-valuetext', current?.progress_label || current?.status || 'In progress');

    byId('globalActivityCurrent').innerHTML = current
      ? `<h2 class="global-activity-section-title">Current</h2>${operationMarkup(current, true)}`
      : '';
    byId('globalActivityQueue').innerHTML = waiting.length
      ? `<h2 class="global-activity-section-title">Waiting</h2>${waiting.map(item => operationMarkup(item)).join('')}`
      : '';
    byId('globalActivityRecent').innerHTML = recent.length
      ? `<h2 class="global-activity-section-title">Recent</h2>${recent.slice(0, 5).map(item => operationMarkup(item)).join('')}`
      : '';
  }

  async function refresh() {
    clearTimeout(timer);
    if (document.hidden) return;
    try {
      const response = await fetch('/api/activity', {cache: 'no-store'});
      if (response.ok) {
        const payload = await response.json();
        render(payload);
        timer = setTimeout(refresh, payload.active || payload.waiting_count ? 1000 : 5000);
        return;
      }
    } catch (error) {
      // A transient polling failure will be retried.
    }
    timer = setTimeout(refresh, 5000);
  }

  document.addEventListener('DOMContentLoaded', () => {
    byId('globalActivityToggle')?.addEventListener('click', () => {
      expanded = !expanded;
      byId('globalActivityToggle').setAttribute('aria-expanded', String(expanded));
      byId('globalActivityDetails').classList.toggle('d-none', !expanded);
    });
    byId('globalActivityDetails')?.addEventListener('click', async event => {
      const button = event.target.closest('[data-activity-cancel]');
      if (!button) return;
      button.disabled = true;
      try {
        await fetch(button.getAttribute('data-activity-cancel'), {method: 'POST'});
      } finally {
        refresh();
      }
    });
    document.addEventListener('visibilitychange', () => {
      if (document.hidden) clearTimeout(timer);
      else refresh();
    });
    refresh();
  });
}());
