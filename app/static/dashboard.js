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

  function formatNumber(value) {
    return new Intl.NumberFormat().format(Number(value || 0));
  }

  function formatDate(value, includeTime) {
    if (!value) return 'Not yet';
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return String(value);
    return new Intl.DateTimeFormat(undefined, includeTime
      ? { dateStyle: 'medium', timeStyle: 'short' }
      : { dateStyle: 'medium' }).format(date);
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

  function categoryIcon(key) {
    return {
      duplicates: 'bi-copy',
      video_previews: 'bi-film',
      subtitles: 'bi-badge-cc',
      posters: 'bi-image',
      actor_images: 'bi-person-bounding-box',
    }[key] || 'bi-tools';
  }

  function renderImpact(data) {
    const impact = data.impact || {};
    const operations = impact.operations || {};
    const pct = clampPercent(impact.resolution_percent);
    setText('dashboardTotalFixes', formatNumber(impact.total_fixes));
    setText('dashboardResolutionRate', `${pct}%`);
    setText('dashboardResolutionDetail', `${formatNumber(impact.resolved_count)} of ${formatNumber(impact.discovered_count)} actionable issues`);
    setText('dashboardDiscoveredCount', formatNumber(impact.discovered_count));
    setText('dashboardClearedElsewhere', `${formatNumber(impact.cleared_elsewhere_count)} cleared elsewhere`);
    setText('dashboardImpactOpenCount', formatNumber(impact.open_count));
    setText('dashboardTrackingSince', impact.tracking_started_at ? `Tracking since ${formatDate(impact.tracking_started_at)}` : 'Tracking unavailable');
    setText('dashboardQuarantinedFiles', formatNumber(operations.quarantined_files));
    setText('dashboardQuarantinedSize', operations.quarantined_size_label || '0 B');
    setText('dashboardDeletedFiles', formatNumber(operations.deleted_files));
    setText('dashboardDeletedSize', `${operations.deleted_size_label || '0 B'} reclaimed`);
    setProgress('dashboardImpactProgressBar', pct);

    const band = document.querySelector('.dashboard-impact-band');
    if (band) band.classList.toggle('dashboard-impact-error', impact.status === 'error');
    if (impact.status === 'error') {
      setText('dashboardTrackingSince', 'Impact tracking unavailable');
    } else if (impact.status === 'warning' && impact.error) {
      setText('dashboardTrackingSince', impact.error);
    }

    renderImpactCategories(impact.categories || []);
    renderImpactTrend(impact.daily || []);
    renderMilestones(impact.milestones || {});
  }

  function renderImpactCategories(categories) {
    const container = byId('dashboardImpactCategories');
    if (!container) return;
    const items = Array.isArray(categories) ? categories : [];
    container.innerHTML = items.map((item) => {
      const pct = clampPercent(item.resolution_percent);
      const title = escapeHtml(item.title || 'Maintenance');
      const lastFix = item.last_fix_at ? `Last fix ${formatDate(item.last_fix_at, true)}` : 'No fixes recorded yet';
      return `
        <a class="dashboard-impact-category dashboard-impact-${escapeHtml(item.key)}" href="${escapeHtml(item.href || '/maintenance')}">
          <div class="dashboard-impact-category-icon"><i class="bi ${categoryIcon(item.key)}" aria-hidden="true"></i></div>
          <div class="dashboard-impact-category-body">
            <div class="dashboard-impact-category-heading">
              <h3>${title}</h3>
              <strong>${formatNumber(item.resolved_count)}</strong>
            </div>
            <div class="dashboard-impact-category-meta">
              <span>${formatNumber(item.resolved_count)} fixed of ${formatNumber(item.discovered_count)}</span>
              <span>${formatNumber(item.open_count)} open</span>
            </div>
            <div class="progress progress-thin" role="progressbar" aria-label="${title} lifetime resolution" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${pct}">
              <div class="progress-bar" style="width: ${pct}%"></div>
            </div>
            <small>${escapeHtml(lastFix)}</small>
          </div>
          <i class="bi bi-chevron-right dashboard-impact-category-arrow" aria-hidden="true"></i>
        </a>
      `;
    }).join('');
  }

  function renderImpactTrend(daily) {
    const container = byId('dashboardImpactTrend');
    if (!container) return;
    const items = Array.isArray(daily) ? daily : [];
    const max = Math.max(1, ...items.map((item) => Number(item.fixes || 0)));
    const total = items.reduce((sum, item) => sum + Number(item.fixes || 0), 0);
    container.innerHTML = `
      <div class="dashboard-trend-summary"><strong>${formatNumber(total)}</strong><span>fixes in the last 30 days</span></div>
      <div class="dashboard-trend-bars">
        ${items.map((item) => {
          const value = Number(item.fixes || 0);
          const height = value ? Math.max(8, Math.round((value / max) * 100)) : 3;
          return `<span class="dashboard-trend-bar${value ? ' has-value' : ''}" style="height: ${height}%" title="${escapeHtml(formatDate(`${item.date}T00:00:00Z`))}: ${formatNumber(value)} fixes"><i>${formatNumber(value)}</i></span>`;
        }).join('')}
      </div>
      <div class="dashboard-trend-axis"><span>${items[0] ? escapeHtml(formatDate(`${items[0].date}T00:00:00Z`)) : ''}</span><span>Today</span></div>
    `;
  }

  function renderMilestones(milestones) {
    const container = byId('dashboardMilestones');
    if (!container) return;
    const earned = Array.isArray(milestones.earned) ? milestones.earned : [];
    const next = milestones.next || null;
    const earnedMarkup = earned.length
      ? `<div class="dashboard-milestone-earned">${earned.slice(-6).map((item) => `<span><i class="bi bi-check2" aria-hidden="true"></i>${escapeHtml(item.label)}</span>`).join('')}</div>`
      : '<div class="dashboard-milestone-empty"><i class="bi bi-flag" aria-hidden="true"></i><span>Your first completed maintenance fix earns the first milestone.</span></div>';
    const nextMarkup = next ? `
      <div class="dashboard-next-milestone">
        <div><span>Next milestone</span><strong>${escapeHtml(next.label)}</strong><em>${formatNumber(next.current)} / ${formatNumber(next.target)}</em></div>
        <div class="progress progress-thin" role="progressbar" aria-label="Progress to ${escapeHtml(next.label)}" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${clampPercent(next.progress_percent)}">
          <div class="progress-bar" style="width: ${clampPercent(next.progress_percent)}%"></div>
        </div>
      </div>` : '<div class="dashboard-milestone-complete"><i class="bi bi-award" aria-hidden="true"></i><strong>All current milestones earned</strong></div>';
    container.innerHTML = earnedMarkup + nextMarkup;
  }

  function renderHealth(data) {
    const health = data.health || {};
    const score = clampPercent(health.score);
    setText('dashboardHealthLabel', health.label || 'Unknown');
    const notScanned = Number(health.not_scanned_count || 0);
    setText('dashboardHealthDetail', health.label === 'Not scanned'
      ? 'Run maintenance scans to establish current health'
      : (notScanned ? `${notScanned} maintenance areas have not been scanned` : (score >= 90 ? 'Library maintenance looks current' : 'Some areas need review')));
    setText('dashboardHealthScore', `${score}%`);
    setText('dashboardUnresolvedCount', health.unresolved_count || 0);
    setText('dashboardActiveCount', health.active_count || 0);
    setProgress('dashboardHealthBar', score);

    if (health.active_count) {
      setMessage('Work is currently running.', 'Progress updates will refresh automatically.', 'warning');
    } else if (health.unresolved_count) {
      setMessage('There are unresolved maintenance items.', 'Open the matching section to review candidates before applying changes.', 'warning');
    } else if (notScanned) {
      setMessage('Current library health is not fully known.', 'Run the maintenance scans you want included in the current-health view.', '');
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
            <span>${escapeHtml(item.remaining || 0)} currently open</span>
            <span>${escapeHtml(item.ready || 0)} ready for action</span>
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
    const root = scan.root || {};
    if (!root.path && !scan.finished_at && !scan.active) {
      container.innerHTML = '<div class="text-muted text-center py-4">Run a library stat refresh to populate this view.</div>';
      return;
    }
    const videoCount = root.video_count || scan.video_count || 0;
    const subtitlePct = coveragePercent(root.subtitle_count, videoCount);
    const posterPct = coveragePercent(root.poster_count, videoCount);
    const previewPct = coveragePercent(root.bif_count, videoCount);
    const actorPct = coveragePercent(root.actor_image_count, videoCount);
    container.innerHTML = `
      <article class="dashboard-library-row">
        <div class="dashboard-library-main">
          <div>
            <h3>${escapeHtml(root.name || 'Library')}</h3>
            <code>${escapeHtml(root.path || scan.path || '')}</code>
          </div>
          <strong>${escapeHtml(videoCount)} videos</strong>
        </div>
        <div class="dashboard-library-metrics">
          <span>${escapeHtml(root.video_size_label || scan.video_size_label || '0 B')}</span>
          <span>${escapeHtml(scan.folder_count || 0)} direct folders</span>
          <span>${escapeHtml(root.nfo_count || 0)} NFO</span>
          <span>${escapeHtml(root.bif_count || 0)} BIF</span>
          <span>${escapeHtml(root.poster_count || 0)} posters</span>
          <span>${escapeHtml(root.background_count || 0)} backgrounds</span>
        </div>
        <div class="dashboard-library-bars">
          ${libraryBar('Subtitles', subtitlePct)}
          ${libraryBar('Posters', posterPct)}
          ${libraryBar('Previews', previewPct)}
          ${libraryBar('Actor images', actorPct)}
        </div>
        <a class="btn btn-outline-secondary btn-sm dashboard-workstream-link" href="/maintenance#overview">Open folder overview</a>
      </article>
    `;
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
    const creative = data.creative_output || {};
    const container = byId('dashboardGifStats');
    if (!container) return;
    container.innerHTML = [
      statCard('Queue', `${gifs.running_count || 0} running`, `${gifs.queued_count || 0} queued`, 'bi-list-task'),
      statCard('Standard GIFs', formatNumber(creative.standard_gifs), 'created since tracking began', 'bi-filetype-gif'),
      statCard('Test Lab variants', formatNumber(creative.test_lab_variants), `${lab.file_count || 0} currently saved`, 'bi-beaker'),
      statCard('Lifetime output', creative.output_size_label || '0 B', 'new GIF data created', 'bi-hdd'),
      statCard('Optimization saved', creative.optimization_saved_label || '0 B', 'avoided output size', 'bi-arrows-collapse'),
      statCard('Current history', gifs.completed_count || 0, `${gifs.failed_count || 0} failed, ${gifs.stopped_count || 0} stopped`, 'bi-clock-history'),
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
    renderImpact(data);
    renderWorkstreams(data.workstreams || []);
    renderIssueChart(data.workstreams || []);
    renderLibraries(data.library || {});
    renderGifStats(data);
    const impactEvents = (((data.impact || {}).recent_events) || []).map((item) => ({
      area: ((data.impact || {}).categories || []).find((category) => category.key === item.category)?.title || 'Maintenance',
      type: item.label || (item.kind === 'scan' ? 'New actionable issues' : 'Maintenance completed'),
      id: item.id,
      created_at: item.timestamp,
    }));
    renderActivity(impactEvents.length ? impactEvents : (data.recent_activity || []));
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

  window.vid2gifDashboardTest = {
    clampPercent,
    formatNumber,
    renderImpact,
  };

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
