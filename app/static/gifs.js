(function () {
  const config = window.vid2gifConfig || {};
  const limit = Number(config.queueLimit || 10);
  const tabHashes = ['new', 'queue', 'completed', 'logs'];

  let currentJob = '';
  let lastJob = '';
  let logJob = '';
  let logOffset = 0;
  let autoMode = true;
  let pollTimer = null;
  let polling = false;

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

  function formatDuration(seconds, emptyValue) {
    if (seconds === null || seconds === undefined) return emptyValue ?? 'unknown';
    seconds = Math.max(0, Math.round(Number(seconds) || 0));
    if (seconds < 60) return `${seconds}s`;
    const mins = Math.floor(seconds / 60);
    const sec = seconds % 60;
    if (mins < 60) return `${mins}m ${String(sec).padStart(2, '0')}s`;
    const hours = Math.floor(mins / 60);
    return `${hours}h ${String(mins % 60).padStart(2, '0')}m`;
  }

  function formatSize(bytes, emptyValue) {
    if (bytes === null || bytes === undefined) return emptyValue ?? '';
    let value = Number(bytes) || 0;
    const units = ['B', 'KB', 'MB', 'GB', 'TB'];
    for (let i = 0; i < units.length; i += 1) {
      if (value < 1024 || i === units.length - 1) {
        return i === 0 ? `${Math.round(value)} ${units[i]}` : `${value.toFixed(1)} ${units[i]}`;
      }
      value /= 1024;
    }
    return emptyValue ?? '';
  }

  function clampPercent(percent) {
    return Math.max(0, Math.min(100, Math.round(Number(percent || 0))));
  }

  function setProgressBar(id, percent) {
    const bar = byId(id);
    if (!bar) return;
    const pct = clampPercent(percent);
    bar.style.width = `${pct}%`;
    bar.textContent = bar.classList.contains('progress-bar') && bar.parentElement.classList.contains('progress-thin') ? '' : `${pct}%`;
    bar.parentElement.setAttribute('aria-valuenow', pct);
  }

  function jobLogHref(id) {
    return `/logs/${encodeURIComponent(id || '')}`;
  }

  function statusBadgeClass(status) {
    if (status === 'success') return 'text-bg-success';
    if (status === 'failed') return 'text-bg-danger';
    if (status === 'running') return 'text-bg-primary';
    return 'text-bg-secondary';
  }

  function statusBadge(status) {
    return `<span class="badge ${statusBadgeClass(status)}">${escapeHtml(status || 'unknown')}</span>`;
  }

  function progressCell(j) {
    const pct = clampPercent(j.progress_percent);
    const label = escapeHtml(j.progress_label || j.progress_text || (j.status === 'queued' ? 'Waiting' : 'Starting'));
    return `<div class="progress" role="progressbar" aria-label="Job progress" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${pct}">` +
           `<div class="progress-bar" style="width: ${pct}%">${pct}%</div></div>` +
           `<div class="small text-muted mt-1">${label}</div>`;
  }

  function queueMoveAction(id, direction) {
    return `/api/queue/move/${encodeURIComponent(id || '')}/${direction}?limit=${encodeURIComponent(limit)}`;
  }

  function updateTopFromQueue(data) {
    const topQueueStatus = byId('topQueueStatus');
    if (topQueueStatus) {
      if (data.paused) {
        topQueueStatus.textContent = 'Paused';
      } else if ((data.running || []).length || (data.queued || []).length) {
        topQueueStatus.textContent = 'Running';
      } else {
        topQueueStatus.textContent = 'Idle';
      }
    }

    const current = (data.running || [])[0];
    const topCurrentJob = byId('topCurrentJob');
    const topCurrentPercent = byId('topCurrentPercent');
    if (topCurrentJob) {
      topCurrentJob.textContent = current ? (current.progress_label || current.status || 'Running') : 'No current job';
    }
    if (topCurrentPercent) topCurrentPercent.textContent = `${current ? clampPercent(current.progress_percent) : 0}%`;
    setProgressBar('topCurrentProgressBar', current ? current.progress_percent : 0);
  }

  function updateTopFromJobs(all) {
    const completed = (all || []).filter(j => j.status === 'success' || j.status === 'failed' || j.status === 'stopped');
    const topCompletedCount = byId('topCompletedCount');
    const topSavings = byId('topSavings');
    if (topCompletedCount) topCompletedCount.textContent = String(completed.length);
    if (topSavings) {
      const latest = completed
        .slice()
        .sort((a, b) => String(b.id || '').localeCompare(String(a.id || '')))
        .find(j => j.gif_optimization_label);
      topSavings.textContent = latest ? latest.gif_optimization_label : 'Pending';
    }
  }

  function updateQueueSummary(data) {
    const pct = clampPercent(data.queue_progress_percent);
    const summaryLabel = byId('queue-summary-label');
    if (summaryLabel) summaryLabel.textContent = data.queue_progress_label || `${pct}% complete`;
    setProgressBar('queue-progress-bar', pct);
    const items = byId('queue-items');
    const elapsed = byId('queue-elapsed');
    const eta = byId('queue-eta');
    if (items) items.textContent = `${data.completed_active_items || 0} of ${data.total_active_items || 0} items complete`;
    if (elapsed) {
      elapsed.textContent =
        `Elapsed: ${data.queue_elapsed_seconds === null || data.queue_elapsed_seconds === undefined ? 'not started' : formatDuration(data.queue_elapsed_seconds)}`;
    }
    if (eta) {
      eta.textContent =
        `Remaining: ${data.queue_eta_seconds === null || data.queue_eta_seconds === undefined ? 'unknown' : formatDuration(data.queue_eta_seconds)}`;
    }
  }

  function updateQueue(data) {
    const tbody = byId('queue-body');
    const qs = byId('queue-status');
    const running = data.running || [];
    const queued = data.queued || [];
    if (qs) {
      qs.innerHTML = data.paused
        ? '<span class="badge text-bg-secondary">Paused</span>'
        : '<span class="badge text-bg-success">Running</span>';
    }

    if (tbody) {
      let rows = '';
      running.forEach(j => {
        rows += `<tr><td class="control-cell">` +
                `<button class="btn btn-outline-secondary btn-icon btn-sm" disabled title="Move up"><i class="bi bi-arrow-up" aria-hidden="true"></i></button>` +
                `<button class="btn btn-outline-secondary btn-icon btn-sm" disabled title="Move down"><i class="bi bi-arrow-down" aria-hidden="true"></i></button></td>` +
                `<td><code>${escapeHtml(j.id)}</code></td>` +
                `<td class="path-cell"><code title="${escapeHtml(j.video)}">${escapeHtml(j.video)}</code></td>` +
                `<td>${statusBadge(j.status)}</td><td class="progress-cell">${progressCell(j)}</td>` +
                `<td><a class="btn btn-outline-secondary btn-icon btn-sm" href="${jobLogHref(j.id)}" title="Open raw log"><i class="bi bi-file-text" aria-hidden="true"></i></a></td></tr>`;
      });
      const remaining = Math.max(0, limit - running.length);
      queued.slice(0, remaining).forEach(j => {
        rows += `<tr><td class="control-cell">` +
                `<form method="post" action="${queueMoveAction(j.id, 'up')}" class="d-inline">` +
                `<button class="btn btn-outline-secondary btn-icon btn-sm" title="Move up"><i class="bi bi-arrow-up" aria-hidden="true"></i></button></form>` +
                `<form method="post" action="${queueMoveAction(j.id, 'down')}" class="d-inline">` +
                `<button class="btn btn-outline-secondary btn-icon btn-sm" title="Move down"><i class="bi bi-arrow-down" aria-hidden="true"></i></button></form></td>` +
                `<td><code>${escapeHtml(j.id)}</code></td>` +
                `<td class="path-cell"><code title="${escapeHtml(j.video)}">${escapeHtml(j.video)}</code></td>` +
                `<td>${statusBadge(j.status)}</td><td class="progress-cell">${progressCell(j)}</td>` +
                `<td><a class="btn btn-outline-secondary btn-icon btn-sm" href="${jobLogHref(j.id)}" title="Open raw log"><i class="bi bi-file-text" aria-hidden="true"></i></a></td></tr>`;
      });
      if (!rows) {
        rows = '<tr><td colspan="6" class="text-muted text-center py-4">No queued jobs.</td></tr>';
      }
      tbody.innerHTML = rows;
    }

    const shownQueued = queued.slice(0, Math.max(0, limit - running.length));
    const count = byId('queue-count');
    if (count) {
      count.textContent = `${running.length + shownQueued.length} of ${running.length + queued.length} items in queue displayed`;
    }
    updateQueueSummary(data);
    setQueueMetrics(data);
    updateTopFromQueue(data);
  }

  async function refreshQueue() {
    try {
      const res = await fetch('/api/queue/status');
      if (!res.ok) return;
      updateQueue(await res.json());
    } catch (e) {
      // Transient polling failures are ignored.
    }
  }

  function completedRow(j) {
    return `<tr><td><code>${escapeHtml(j.id)}</code></td>` +
           `<td class="path-cell"><code title="${escapeHtml(j.video)}">${escapeHtml(j.video)}</code></td>` +
           `<td>${statusBadge(j.status)}</td>` +
           `<td>${escapeHtml(formatDuration(j.elapsed_seconds, ''))}</td>` +
           `<td>${escapeHtml(formatSize(j.output_size_bytes, ''))}</td>` +
           `<td>${escapeHtml(j.gif_optimization_label || '')}</td>` +
           `<td class="path-cell"><code title="${escapeHtml(j.out_gif)}">${escapeHtml(j.out_gif)}</code></td>` +
           `<td><a class="btn btn-outline-secondary btn-icon btn-sm" href="${jobLogHref(j.id)}" title="Open raw log"><i class="bi bi-file-text" aria-hidden="true"></i></a></td></tr>`;
  }

  async function refreshCompleted() {
    try {
      const res = await fetch('/api/status');
      if (!res.ok) return;
      const all = await res.json();
      updateTopFromJobs(all);
      updateJobSelector(all);
      const completed = all
        .filter(j => j.status === 'success' || j.status === 'failed' || j.status === 'stopped')
        .sort((a, b) => String(b.id || '').localeCompare(String(a.id || '')));
      const tbody = byId('completed-body');
      if (!tbody) return;
      tbody.innerHTML = completed.length
        ? completed.map(completedRow).join('')
        : '<tr><td colspan="8" class="text-muted text-center py-4">No completed jobs yet.</td></tr>';
    } catch (e) {
      // Ignore transient polling failures.
    }
  }

  function append(line) {
    const box = byId('logbox');
    if (!box) return;
    box.textContent += (box.textContent ? '\n' : '') + line;
    const autoscroll = byId('autoscroll');
    if (!autoscroll || autoscroll.checked) {
      box.scrollTop = box.scrollHeight;
    }
  }

  function clearLog() {
    const box = byId('logbox');
    if (box) box.textContent = '';
  }

  function stopStream() {
    polling = false;
    if (pollTimer) {
      clearInterval(pollTimer);
      pollTimer = null;
    }
  }

  function setStatus(text) {
    const pill = byId('jobStatus');
    if (!pill) return;
    const status = text || 'idle';
    pill.textContent = status;
    pill.className = `status-pill ${status}`;
  }

  function setProgress(text) {
    const progressText = byId('progressText');
    if (progressText) progressText.textContent = text || '';
  }

  function setJobMetrics(job) {
    if (!job) {
      setStatus('idle');
      setProgress('');
      setProgressBar('jobProgressBar', 0);
      byId('jobProgressLabel').textContent = 'Idle';
      byId('jobElapsed').textContent = 'Elapsed: not started';
      byId('jobEta').textContent = 'Remaining: unknown';
      byId('jobSize').textContent = 'Size: pending';
      byId('jobOptimization').textContent = 'Optimization: pending';
      return;
    }
    setStatus(job.status);
    setProgress(job.progress_label || job.progress_text || '');
    setProgressBar('jobProgressBar', job.progress_percent);
    byId('jobProgressLabel').textContent = job.progress_label || job.progress_text || job.status || 'Idle';
    byId('jobElapsed').textContent =
      `Elapsed: ${job.elapsed_seconds === null || job.elapsed_seconds === undefined ? 'not started' : formatDuration(job.elapsed_seconds)}`;
    byId('jobEta').textContent =
      `Remaining: ${job.eta_seconds === null || job.eta_seconds === undefined ? 'unknown' : formatDuration(job.eta_seconds)}`;
    byId('jobSize').textContent = `Size: ${formatSize(job.output_size_bytes, 'pending')}`;
    byId('jobOptimization').textContent = `Optimization: ${job.gif_optimization_label || 'pending'}`;
  }

  function setQueueMetrics(data) {
    if (!data) return;
    setProgressBar('queueProgressBar', data.queue_progress_percent);
    byId('queueProgressLabel').textContent = data.queue_progress_label || 'No active queue';
    byId('queueItems').textContent = `${data.completed_active_items || 0} of ${data.total_active_items || 0} items`;
    byId('queueElapsed').textContent =
      `Elapsed: ${data.queue_elapsed_seconds === null || data.queue_elapsed_seconds === undefined ? 'not started' : formatDuration(data.queue_elapsed_seconds)}`;
    byId('queueEta').textContent =
      `Remaining: ${data.queue_eta_seconds === null || data.queue_eta_seconds === undefined ? 'unknown' : formatDuration(data.queue_eta_seconds)}`;
  }

  function newestFinishedJob(all) {
    return all
      .filter(x => x.status === 'success' || x.status === 'failed' || x.status === 'stopped')
      .sort((a, b) => String(b.id || '').localeCompare(String(a.id || '')))[0];
  }

  function setCurrentJob(jobId) {
    if (!jobId) return;
    currentJob = jobId;
    lastJob = jobId;
    if (logJob !== jobId) {
      logJob = jobId;
      logOffset = 0;
      clearLog();
    }
  }

  function updateJobSelector(all) {
    const sel = byId('jobSel');
    if (!sel) return;
    const selected = sel.value || 'live';
    const known = new Set(Array.from(sel.options).map(opt => opt.value));
    (all || [])
      .slice()
      .sort((a, b) => String(b.id || '').localeCompare(String(a.id || '')))
      .forEach(j => {
        if (!j.id || known.has(j.id)) return;
        const opt = document.createElement('option');
        opt.value = j.id;
        opt.textContent = `${j.id} | ${j.status} | ${j.video}`;
        sel.appendChild(opt);
      });
    if (Array.from(sel.options).some(opt => opt.value === selected)) {
      sel.value = selected;
    }
  }

  async function refreshStatus() {
    try {
      const res = await fetch('/api/status');
      if (!res.ok) return;
      const all = await res.json();
      updateTopFromJobs(all);
      updateJobSelector(all);
      if (autoMode) {
        const running = all.find(x => x.status === 'running');
        if (running) {
          setCurrentJob(running.id);
          setJobMetrics(running);
          return;
        }
        const last = all.find(x => x.id === lastJob || x.id === currentJob) || newestFinishedJob(all);
        if (last) {
          setCurrentJob(last.id);
          setJobMetrics(last);
        } else {
          currentJob = '';
          setJobMetrics(null);
        }
      } else if (currentJob) {
        const j = all.find(x => x.id === currentJob);
        if (!j) return;
        setCurrentJob(j.id);
        setJobMetrics(j);
      }
    } catch (e) {
      // Ignore transient polling failures.
    }
  }

  async function refreshQueueSummary() {
    try {
      const res = await fetch('/api/queue/status');
      if (!res.ok) return;
      const data = await res.json();
      setQueueMetrics(data);
      updateTopFromQueue(data);
    } catch (e) {
      // Ignore transient polling failures.
    }
  }

  async function refreshLog() {
    if (!polling || !currentJob) return;
    try {
      const res = await fetch(`/api/logs/${encodeURIComponent(currentJob)}?offset=${encodeURIComponent(logOffset)}`);
      if (!res.ok) return;
      const data = await res.json();
      if (data.reset) clearLog();
      logOffset = data.offset || 0;
      (data.lines || []).forEach(append);
      if (data.job) setJobMetrics(data.job);
    } catch (e) {
      // Ignore transient polling failures.
    }
  }

  async function poll() {
    await refreshStatus();
    await refreshQueueSummary();
    await refreshLog();
  }

  function startStream() {
    stopStream();
    const sel = byId('jobSel');
    const job = (sel && sel.value) || 'live';
    autoMode = job === 'live';
    currentJob = autoMode ? '' : job;
    logJob = '';
    logOffset = 0;
    clearLog();
    polling = true;
    poll();
    pollTimer = setInterval(poll, 1000);
  }

  function toggleCustom(selectElId, customInputId, originalCheckboxId) {
    const sel = byId(selectElId);
    const inp = byId(customInputId);
    const orig = originalCheckboxId ? byId(originalCheckboxId) : null;
    if (!sel || !inp) return;
    const useCustom = sel.value === 'custom' && !(orig && orig.checked);
    inp.classList.toggle('d-none', !useCustom);
    inp.disabled = !useCustom;
    if (!useCustom) inp.value = '';
  }

  async function fetchDirs(path) {
    const res = await fetch(`/api/listdir?path=${encodeURIComponent(path)}`);
    return res.json();
  }

  async function addSelect(basePath) {
    const dirs = await fetchDirs(basePath);
    if (!dirs.length) return;
    const container = byId('lib-browser');
    if (!container) return;
    const sel = document.createElement('select');
    sel.className = 'form-select form-select-sm';
    sel.appendChild(new Option('Select folder', ''));
    dirs.forEach(d => sel.appendChild(new Option(d, d)));
    sel.addEventListener('change', () => {
      while (sel.nextSibling) sel.parentNode.removeChild(sel.nextSibling);
      const vid = byId('video');
      const choice = sel.value;
      const newPath = choice ? `${basePath}/${choice}` : basePath;
      if (vid) vid.value = newPath;
      if (choice) addSelect(newPath);
    });
    container.appendChild(sel);
  }

  function saveForm() {
    const form = byId('newJobForm');
    if (!form) return;
    Array.from(form.elements).forEach(el => {
      if (!el.name) return;
      const key = `newjob_${el.name}`;
      if (el.type === 'checkbox') {
        localStorage.setItem(key, el.checked ? '1' : '0');
      } else {
        localStorage.setItem(key, el.value);
      }
    });
  }

  function loadForm() {
    const form = byId('newJobForm');
    if (!form) return;
    Array.from(form.elements).forEach(el => {
      if (!el.name) return;
      const key = `newjob_${el.name}`;
      const val = localStorage.getItem(key);
      if (val === null) return;
      if (el.type === 'checkbox') {
        el.checked = val === '1';
      } else {
        el.value = val;
      }
    });
  }

  function initNewJob() {
    const form = byId('newJobForm');
    if (!form) return;
    loadForm();
    toggleCustom('height_preset', 'height_custom');
    toggleCustom('fps_preset', 'fps_custom', 'fps_original');
    toggleCustom('clip_len_preset', 'clip_len_custom');
    const vid = byId('video');
    if (vid && !vid.value) vid.value = config.libRoot || '/library';
    addSelect(config.libRoot || '/library');
    form.addEventListener('input', saveForm);
    form.addEventListener('change', saveForm);
    byId('height_preset').addEventListener('change', () => toggleCustom('height_preset', 'height_custom'));
    byId('fps_preset').addEventListener('change', () => toggleCustom('fps_preset', 'fps_custom', 'fps_original'));
    byId('fps_original').addEventListener('change', () => toggleCustom('fps_preset', 'fps_custom', 'fps_original'));
    byId('clip_len_preset').addEventListener('change', () => toggleCustom('clip_len_preset', 'clip_len_custom'));
  }

  function activateTab(hash, updateUrl) {
    const safeHash = tabHashes.includes(hash) ? hash : 'new';
    const button = document.querySelector(`[data-tab-hash="${safeHash}"]`);
    if (!button || !window.bootstrap) return;
    window.bootstrap.Tab.getOrCreateInstance(button).show();
    localStorage.setItem('gifs_active_tab', safeHash);
    if (updateUrl) {
      history.replaceState(null, '', `#${safeHash}`);
    }
  }

  function initTabs() {
    const requested = location.hash.replace('#', '');
    const saved = localStorage.getItem('gifs_active_tab');
    activateTab(tabHashes.includes(requested) ? requested : (saved || 'new'), false);
    document.querySelectorAll('[data-tab-hash]').forEach(button => {
      button.addEventListener('shown.bs.tab', event => {
        const hash = event.target.getAttribute('data-tab-hash') || 'new';
        localStorage.setItem('gifs_active_tab', hash);
        history.replaceState(null, '', `#${hash}`);
      });
    });
    document.querySelectorAll('[data-tab-shortcut]').forEach(link => {
      link.addEventListener('click', event => {
        event.preventDefault();
        activateTab(link.getAttribute('data-tab-shortcut'), true);
      });
    });
    window.addEventListener('hashchange', () => activateTab(location.hash.replace('#', ''), false));
  }

  function initQueueLimit() {
    const sel = byId('queueLimit');
    if (!sel) return;
    const params = new URLSearchParams(location.search);
    const hash = location.hash.replace('#', '');
    const savedTab = localStorage.getItem('gifs_active_tab');
    const saved = localStorage.getItem('queue_limit');
    if (saved && !params.has('limit') && (hash === 'queue' || (!hash && savedTab === 'queue')) && sel.value !== saved) {
      const url = new URL(location.href);
      url.searchParams.set('limit', saved);
      url.hash = 'queue';
      location.replace(url.toString());
      return;
    }
    sel.addEventListener('change', () => {
      localStorage.setItem('queue_limit', sel.value);
      sel.form.submit();
    });
  }

  function initLogs() {
    const jobSel = byId('jobSel');
    const auto = byId('autoscroll');
    const savedJob = localStorage.getItem('live_jobSel');
    if (jobSel && savedJob) {
      const opt = Array.from(jobSel.options).find(o => o.value === savedJob);
      if (opt) jobSel.value = savedJob;
    }
    const savedAuto = localStorage.getItem('live_autoscroll');
    if (auto && savedAuto !== null) auto.checked = savedAuto === 'true';
    if (jobSel) {
      jobSel.addEventListener('change', () => {
        localStorage.setItem('live_jobSel', jobSel.value);
        startStream();
      });
    }
    if (auto) {
      auto.addEventListener('change', () => {
        localStorage.setItem('live_autoscroll', auto.checked);
      });
    }
    byId('startLogBtn').addEventListener('click', startStream);
    byId('stopLogBtn').addEventListener('click', stopStream);
    byId('clearLogBtn').addEventListener('click', clearLog);
    startStream();
  }

  document.addEventListener('DOMContentLoaded', () => {
    initTabs();
    initNewJob();
    initQueueLimit();
    initLogs();
    refreshQueue();
    refreshCompleted();
    setInterval(refreshQueue, 1000);
    setInterval(refreshCompleted, 5000);
  });
}());
