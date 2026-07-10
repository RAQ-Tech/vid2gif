(function () {
  const config = window.vid2gifDashboardConfig || {};
  let refreshTimer = null;
  let libraryTimer = null;

  function byId(id) {
    return document.getElementById(id);
  }

  function escapeHtml(value) {
    return String(value == null ? '' : value)
      .replaceAll('&', '&amp;')
      .replaceAll('<', '&lt;')
      .replaceAll('>', '&gt;')
      .replaceAll('"', '&quot;')
      .replaceAll("'", '&#39;');
  }

  function clampPercent(value) {
    const number = Number(value);
    if (!Number.isFinite(number)) return 0;
    return Math.max(0, Math.min(100, Math.round(number)));
  }

  function setText(id, value) {
    const el = byId(id);
    if (el) el.textContent = value == null ? '' : String(value);
  }

  function setProgress(id, value) {
    const el = byId(id);
    if (!el) return;
    const pct = clampPercent(value);
    el.style.width = `${pct}%`;
    const parent = el.closest('.progress');
    if (parent) parent.setAttribute('aria-valuenow', String(pct));
  }

  function setMessage(title, detail, tone) {
    setText('dashboardMessageTitle', title);
    setText('dashboardMessageDetail', detail || '');
    const box = byId('dashboardMessage');
    if (!box) return;
    box.classList.toggle('border-warning', tone === 'warning');
    box.classList.toggle('border-danger', tone === 'danger');
  }

  async function readJsonResponse(response) {
    const text = await response.text();
    let data = {};
    if (text) {
      try {
        data = JSON.parse(text);
      } catch (_err) {
        throw new Error(`Server returned ${response.status}`);
      }
    }
    if (!response.ok) {
      throw new Error(data.error || `Server returned ${response.status}`);
    }
    return data;
  }

  function stateLabel(state) {
    return {
      active: 'Running',
      attention: 'Needs review',
      needs_verification: 'Verify',
      clean: 'Clear',
      idle: 'Idle',
      not_scanned: 'Not scanned',
    }[state] || 'Idle';
  }

  function stateIcon(state) {
    return {
      active: 'bi-activity',
      attention: 'bi-exclamation-triangle',
      needs_verification: 'bi-clipboard-check',
      clean: 'bi-check-circle',
      idle: 'bi-dash-circle',
      not_scanned: 'bi-circle',
    }[state] || 'bi-circle';
  }

  function renderHealth(data) {
    const health = data.health || {};
    const score = clampPercent(health.score);
    setText('dashboardHealthLabel', health.label || 'Unknown');
    setText('dashboardHealthDetail', score >= 90 ? 'Library maintenance looks current' : 'Some areas need review');
    setText('dashboardHealthScore', `${score}%`);
    setText('dashboardUnresolvedCount', health.unresolved_count || 0);
    setText('dashboardActiveCount', health.active_count || 0);
    setProgress('dashboardHealthBar', score);

    if (health.active_count) {
      setMessage('Work is currently running.', 'Progress updates will refresh automatically.', 'warning');
    } else if (health.unresolved_count) {
      setMessage('There are unresolved maintenance items.', 'Open the matching section to review candidates before applying changes.', 'warning');
    } else {
      setMessage('No unresolved maintenance items are currently reported.', 'Run scans from the maintenance tabs when you want a fresh check.', '');
    }
  }

  function renderWorkstreams(workstreams) {
    const container = byId('dashboardWorkstreams');
    if (!container) return;
    const items = Array.isArray(workstreams) ? workstreams : [];
    if (!items.length) {
      container.innerHTML = '<div class="text-muted text-center py-4">No workstream data yet.</div>';
      return;
    }
    container.innerHTML = items.map((item) => {
      const pct = clampPercent(item.progress_percent);
      const title = escapeHtml(item.title);
      const detail = escapeHtml(item.detail || '');
      const href = escapeHtml(item.href || '#');
      const action = escapeHtml(item.action_label || 'Open');
      const state = escapeHtml(item.state || 'idle');
      return `
        <article class="dashboard-workstream dashboard-state-${state}">
          <div class="dashboard-workstream-heading">
            <div>
              <h3>${title}</h3>
              <div class="dashboard-workstream-detail">${detail}</div>
            </div>
            <span class="dashboard-state-pill">
              <i class="bi ${stateIcon(item.state)}" aria-hidden="true"></i>
              ${escapeHtml(stateLabel(item.state))}
            </span>
          </div>
          <div class="dashboard-workstream-metrics">
            <span>${escapeHtml(item.remaining || 0)} remaining</span>
            <span>${escapeHtml(item.resolved || 0)} resolved</span>
            <span>${pct}%</span>
          </div>
          <div class="progress progress-thin" role="progressbar" aria-label="${title} progress" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${pct}">
            <div class="progress-bar" style="width: ${pct}%"></div>
          </div>
          <a class="btn btn-outline-secondary btn-sm dashboard-workstream-link" href="${href}">${action}</a>
        </article>
      `;
    }).join('');
  }

  function renderIssueChart(workstreams) {
    const container = byId('dashboardIssueChart');
    if (!container) return;
    const items = (Array.isArray(workstreams) ? workstreams : [])
      .filter((item) => item.key !== 'gifs')
      .map((item) => ({ ...item, remaining: Number(item.remaining || 0) }))
      .filter((item) => item.remaining > 0);

    if (!items.length) {
      container.innerHTML = '<div class="dashboard-empty-chart"><i class="bi bi-check2-circle" aria-hidden="true"></i><span>No open maintenance issues.</span></div>';
      return;
    }

    const total = items.reduce((sum, item) => sum + item.remaining, 0);
    container.innerHTML = `
      <div class="dashboard-stack" aria-label="Issue mix">
        ${items.map((item) => {
          const width = Math.max(6, (item.remaining / total) * 100);
          return `<div class="dashboard-stack-segment dashboard-stack-${escapeHtml(item.key)}" style="width: ${width}%" title="${escapeHtml(item.title)}: ${escapeHtml(item.remaining)}"></div>`;
        }).join('')}
      </div>
      <div class="dashboard-issue-list">
        ${items.map((item) => {
          const pct = Math.round((item.remaining / total) * 100);
          return `
            <a class="dashboard-issue-row" href="${escapeHtml(item.href)}">
              <span class="dashboard-issue-dot dashboard-stack-${escapeHtml(item.key)}"></span>
              <span>${escapeHtml(item.title)}</span>
              <strong>${escapeHtml(item.remaining)}</strong>
              <em>${pct}%</em>
            </a>
          `;
        }).join('')}
      </div>
    `;
  }

  function coveragePercent(count, videoCount) {
    const videos = Number(videoCount || 0);
    if (!videos) return 0;
    return clampPercent((Number(count || 0) / videos) * 100);
  }

  function renderLibraries(library) {
    const scan = library || {};
    setText('dashboardLibraryState', stateLabel(scan.active ? 'active' : scan.status || 'not_scanned'));
    setText('dashboardLibrarySummary', `${scan.video_count || 0} videos, ${scan.video_size_label || '0 B'}`);
    setText('dashboardLibraryUpdated', scan.finished_at ? `Last scan: ${scan.finished_at}` : 'Last scan: never');
    setProgress('dashboardLibraryBar', scan.progress_percent || 0);

    const container = byId('dashboardLibraries');
    if (!container) return;
    const libraries = Array.isArray(scan.libraries) ? scan.libraries : [];
    if (!libraries.length) {
      container.innerHTML = '<div class="text-muted text-center py-4">Run a library stat refresh to populate this view.</div>';
      return;
    }

    container.innerHTML = libraries.map((item) => {
      const videoCount = item.video_count || 0;
      const subtitlePct = coveragePercent(item.subtitle_count, videoCount);
      const posterPct = coveragePercent(item.poster_count, videoCount);
      const previewPct = coveragePercent(item.bif_count, videoCount);
      const actorPct = coveragePercent(item.actor_image_count, videoCount);
      return `
        <article class="dashboard-library-row">
          <div class="dashboard-library-main">
            <div>
              <h3>${escapeHtml(item.name || 'Library')}</h3>
              <code>${escapeHtml(item.path || '')}</code>
            </div>
            <strong>${escapeHtml(videoCount)} videos</strong>
          </div>
          <div class="dashboard-library-metrics">
            <span>${escapeHtml(item.video_size_label || '0 B')}</span>
            <span>${escapeHtml(item.nfo_count || 0)} NFO</span>
            <span>${escapeHtml(item.bif_count || 0)} BIF</span>
            <span>${escapeHtml(item.poster_count || 0)} posters</span>
            <span>${escapeHtml(item.background_count || 0)} backgrounds</span>
          </div>
          <div class="dashboard-library-bars">
            ${libraryBar('Subtitles', subtitlePct)}
            ${libraryBar('Posters', posterPct)}
            ${libraryBar('Previews', previewPct)}
            ${libraryBar('Actor images', actorPct)}
          </div>
        </article>
      `;
    }).join('');
  }

  function libraryBar(label, value) {
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

  function statCard(label, value, detail, icon) {
    return `
      <div class="dashboard-stat">
        <i class="bi ${icon}" aria-hidden="true"></i>
        <div>
          <span>${escapeHtml(label)}</span>
          <strong>${escapeHtml(value)}</strong>
          <small>${escapeHtml(detail || '')}</small>
        </div>
      </div>
    `;
  }

  function renderGifStats(data) {
    const gifs = data.gifs || {};
    const lab = data.test_lab || {};
    const container = byId('dashboardGifStats');
    if (!container) return;
    container.innerHTML = [
      statCard('Queue', `${gifs.running_count || 0} running`, `${gifs.queued_count || 0} queued`, 'bi-list-task'),
      statCard('Completed GIFs', gifs.completed_count || 0, `${gifs.failed_count || 0} failed, ${gifs.stopped_count || 0} stopped`, 'bi-filetype-gif'),
      statCard('GIF output', gifs.output_size_label || '0 B', 'known completed job output', 'bi-hdd'),
      statCard('Test Lab', lab.run_count || 0, `${lab.file_count || 0} files, ${lab.total_size_label || '0 B'}`, 'bi-beaker'),
    ].join('');
  }

  function renderActivity(activity) {
    const container = byId('dashboardActivity');
    if (!container) return;
    const items = Array.isArray(activity) ? activity : [];
    if (!items.length) {
      container.innerHTML = '<div class="text-muted text-center py-4">No recent activity yet.</div>';
      return;
    }
    container.innerHTML = items.map((item) => `
      <div class="dashboard-activity-item">
        <div>
          <strong>${escapeHtml(item.area || 'Maintenance')}</strong>
          <span>${escapeHtml(item.type || 'log')}</span>
        </div>
        <div>
          <code>${escapeHtml(item.id || '')}</code>
          <span>${escapeHtml(item.created_at || '')}</span>
        </div>
      </div>
    `).join('');
  }

  function renderDashboard(data) {
    renderHealth(data);
    renderWorkstreams(data.workstreams || []);
    renderIssueChart(data.workstreams || []);
    renderLibraries(data.library || {});
    renderGifStats(data);
    renderActivity(data.recent_activity || []);
  }

  async function refreshDashboard() {
    try {
      const response = await fetch('/api/dashboard/status');
      const data = await readJsonResponse(response);
      renderDashboard(data);
    } catch (err) {
      setMessage('Dashboard status could not load.', err.message, 'danger');
    }
  }

  async function refreshLibraryStatus() {
    try {
      const response = await fetch('/api/dashboard/library-scan/status');
      const data = await readJsonResponse(response);
      renderLibraries(data.scan || {});
      if (data.scan && data.scan.active) {
        clearTimeout(libraryTimer);
        libraryTimer = setTimeout(refreshLibraryStatus, 1200);
      } else {
        refreshDashboard();
      }
    } catch (err) {
      setMessage('Library stats could not refresh.', err.message, 'danger');
    }
  }

  async function startLibraryScan() {
    const button = byId('dashboardLibraryScanButton');
    if (button) button.disabled = true;
    try {
      const response = await fetch('/api/dashboard/library-scan', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ path: config.libRoot || '' }),
      });
      const data = await readJsonResponse(response);
      renderLibraries(data.scan || {});
      clearTimeout(libraryTimer);
      libraryTimer = setTimeout(refreshLibraryStatus, 600);
    } catch (err) {
      setMessage('Library stat refresh could not start.', err.message, 'danger');
    } finally {
      if (button) button.disabled = false;
    }
  }

  document.addEventListener('DOMContentLoaded', () => {
    const refreshButton = byId('dashboardRefreshButton');
    const libraryButton = byId('dashboardLibraryScanButton');
    if (refreshButton) refreshButton.addEventListener('click', refreshDashboard);
    if (libraryButton) libraryButton.addEventListener('click', startLibraryScan);
    refreshDashboard();
    refreshTimer = setInterval(refreshDashboard, 10000);
    window.addEventListener('beforeunload', () => {
      clearInterval(refreshTimer);
      clearTimeout(libraryTimer);
    });
  });
}());
