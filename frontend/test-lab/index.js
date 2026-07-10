import Sortable from 'sortablejs';

import {
  COMPARISON_STORAGE_KEY,
  MAX_COMPARISON_ITEMS,
  MAX_VARIANTS,
  createId,
  loadComparisonIds,
  makeDefaultVariant,
  normalizeComparisonIds,
  normalizeVariant,
  reorderIds,
  successfulFileIds,
  variantRequest,
  variantSummary,
} from './logic.js';
import {SynchronizedGifPlayer} from './player.js';

const DRAFT_STORAGE_KEY = 'testlab_workbench_v1';
const PENDING_RUN_STORAGE_KEY = 'testlab_pending_run';
const TERMINAL_RUN_STATUSES = new Set(['success', 'failed', 'partial', 'stopped']);

const byId = id => document.getElementById(id);
const config = window.vid2gifConfig || {};

function escapeHtml(value) {
  return String(value ?? '').replace(/[&<>"']/g, character => ({
    '&': '&amp;',
    '<': '&lt;',
    '>': '&gt;',
    '"': '&quot;',
    "'": '&#39;',
  }[character]));
}

function safeJsonParse(value, fallback) {
  try {
    return JSON.parse(value || 'null') ?? fallback;
  } catch (error) {
    return fallback;
  }
}

function storageGet(key) {
  try {
    return localStorage.getItem(key);
  } catch (error) {
    return null;
  }
}

function storageSet(key, value) {
  try {
    localStorage.setItem(key, value);
  } catch (error) {
    // The workbench remains usable when browser storage is unavailable.
  }
}

function formatDuration(seconds, empty = '') {
  if (seconds === null || seconds === undefined) return empty;
  const total = Math.max(0, Math.round(Number(seconds) || 0));
  if (total < 60) return `${total}s`;
  return `${Math.floor(total / 60)}m ${String(total % 60).padStart(2, '0')}s`;
}

function statusClass(status) {
  if (status === 'success') return 'text-bg-success';
  if (status === 'failed') return 'text-bg-danger';
  if (status === 'partial') return 'text-bg-warning';
  if (status === 'running') return 'text-bg-primary';
  return 'text-bg-secondary';
}

function clampPercent(value) {
  return Math.max(0, Math.min(100, Math.round(Number(value) || 0)));
}

function option(value, label, selected) {
  return `<option value="${escapeHtml(value)}"${String(value) === String(selected) ? ' selected' : ''}>${escapeHtml(label)}</option>`;
}

function loadDraft() {
  const saved = safeJsonParse(storageGet(DRAFT_STORAGE_KEY), {});
  const variants = (Array.isArray(saved.variants) ? saved.variants : [])
    .slice(0, MAX_VARIANTS)
    .map((variant, index) => normalizeVariant(variant, config.defaults || {}, index + 1));
  if (!variants.length) variants.push(makeDefaultVariant(config.defaults || {}, 1));
  const selectedVariantId = variants.some(variant => variant.id === saved.selectedVariantId)
    ? saved.selectedVariantId
    : variants[0].id;
  return {
    video: String(saved.video || ''),
    variants,
    selectedVariantId,
  };
}

const draft = loadDraft();
const state = {
  ...draft,
  comparisonIds: loadComparisonIds(localStorage),
  filesById: new Map(),
  selectedFileIds: new Set(),
  renameDrafts: new Map(),
  previewRequests: new Set(),
  run: null,
  hasActiveRun: false,
  pendingRunId: storageGet(PENDING_RUN_STORAGE_KEY) || '',
  inventoryLoaded: false,
  trayQuery: '',
};

let runPollTimer = null;
let previewPollTimer = null;
let deckSortable = null;
let traySortable = null;
let comparisonSignature = '';
let keyboardDrag = null;
let resumeAfterVisibility = false;
let resumeAfterTab = false;
let inventoryRefreshing = false;
let runRefreshing = false;

function saveDraft() {
  storageSet(DRAFT_STORAGE_KEY, JSON.stringify({
    schema_version: 1,
    video: state.video,
    variants: state.variants,
    selectedVariantId: state.selectedVariantId,
  }));
}

function saveComparison() {
  storageSet(COMPARISON_STORAGE_KEY, JSON.stringify(state.comparisonIds));
}

function announce(message) {
  const region = byId('testLabLiveRegion');
  if (!region) return;
  region.textContent = '';
  requestAnimationFrame(() => {
    region.textContent = message;
  });
}

function selectedVariant() {
  return state.variants.find(variant => variant.id === state.selectedVariantId) || state.variants[0];
}

function renderVariantTabs() {
  const tabs = byId('testLabVariantTabs');
  if (!tabs) return;
  tabs.innerHTML = state.variants.map((variant, index) => (
    `<button type="button" class="test-lab-variant-tab${variant.id === state.selectedVariantId ? ' active' : ''}" ` +
    `role="tab" aria-selected="${variant.id === state.selectedVariantId}" data-variant-id="${escapeHtml(variant.id)}">` +
    `<span>${escapeHtml(variant.name || `Variant ${index + 1}`)}</span>` +
    `<small>${escapeHtml(variantSummary(variant))}</small></button>`
  )).join('');
  const addButton = byId('testLabAddVariant');
  if (addButton) addButton.disabled = state.variants.length >= MAX_VARIANTS;
  updateRunButton();
}

function renderVariantEditor() {
  const editor = byId('testLabVariantEditor');
  const variant = selectedVariant();
  if (!editor || !variant) return;
  editor.innerHTML = `
    <div class="test-lab-editor-heading">
      <label class="form-label test-lab-name-field">Name
        <input class="form-control form-control-sm" data-variant-field="name" value="${escapeHtml(variant.name)}" maxlength="80">
      </label>
      <div class="test-lab-editor-actions">
        <button type="button" class="btn btn-outline-secondary btn-icon btn-sm" id="testLabDuplicateVariant" title="Duplicate variant" aria-label="Duplicate variant">
          <i class="bi bi-files" aria-hidden="true"></i>
        </button>
        <button type="button" class="btn btn-outline-danger btn-icon btn-sm" id="testLabRemoveVariant" title="Remove variant" aria-label="Remove variant"${state.variants.length <= 1 ? ' disabled' : ''}>
          <i class="bi bi-trash" aria-hidden="true"></i>
        </button>
      </div>
    </div>
    <div class="variant-settings-grid">
      <label class="form-label">Height
        <select class="form-select form-select-sm" data-variant-field="height_preset">
          ${option('240', '240', variant.height_preset)}${option('360', '360', variant.height_preset)}${option('480', '480', variant.height_preset)}${option('720', '720', variant.height_preset)}${option('1080', '1080', variant.height_preset)}${option('custom', 'Custom', variant.height_preset)}
        </select>
        <input type="number" min="120" step="1" class="form-control form-control-sm mt-2" data-variant-field="height_custom" value="${escapeHtml(variant.height_custom)}">
      </label>
      <label class="form-label">FPS
        <select class="form-select form-select-sm" data-variant-field="fps_preset">
          ${option('10', '10', variant.fps_preset)}${option('12', '12', variant.fps_preset)}${option('15', '15', variant.fps_preset)}${option('20', '20', variant.fps_preset)}${option('24', '24', variant.fps_preset)}${option('30', '30', variant.fps_preset)}${option('original', 'Source', variant.fps_preset)}${option('custom', 'Custom', variant.fps_preset)}
        </select>
        <input type="number" min="1" step="1" class="form-control form-control-sm mt-2" data-variant-field="fps_custom" value="${escapeHtml(variant.fps_custom)}">
      </label>
      <label class="form-label">Clip length
        <select class="form-select form-select-sm" data-variant-field="clip_len_preset">
          ${option('1', '1 second', variant.clip_len_preset)}${option('2', '2 seconds', variant.clip_len_preset)}${option('3', '3 seconds', variant.clip_len_preset)}${option('4', '4 seconds', variant.clip_len_preset)}${option('5', '5 seconds', variant.clip_len_preset)}${option('custom', 'Custom', variant.clip_len_preset)}
        </select>
        <input type="number" min="0.1" step="0.1" class="form-control form-control-sm mt-2" data-variant-field="clip_len_custom" value="${escapeHtml(variant.clip_len_custom)}">
      </label>
      <label class="form-label wide-field">Percent points
        <input class="form-control form-control-sm" data-variant-field="percent_points" value="${escapeHtml(variant.percent_points)}">
      </label>
      <label class="form-label">Early
        <input type="number" step="0.1" class="form-control form-control-sm" data-variant-field="abs_early" value="${escapeHtml(variant.abs_early)}">
      </label>
      <label class="form-label">Late
        <input type="number" step="0.1" class="form-control form-control-sm" data-variant-field="abs_late_from_end" value="${escapeHtml(variant.abs_late_from_end)}">
      </label>
      <label class="form-label">Start buffer
        <input type="number" step="0.1" class="form-control form-control-sm" data-variant-field="start_buffer" value="${escapeHtml(variant.start_buffer)}">
      </label>
      <label class="form-label">End buffer
        <input type="number" step="0.1" class="form-control form-control-sm" data-variant-field="end_buffer" value="${escapeHtml(variant.end_buffer)}">
      </label>
    </div>
    <div class="variant-switches">
      <label class="form-check form-switch"><input class="form-check-input" type="checkbox" data-variant-field="loop_forever"${variant.loop_forever ? ' checked' : ''}><span class="form-check-label">Loop forever</span></label>
      <label class="form-check form-switch"><input class="form-check-input" type="checkbox" data-variant-field="smooth"${variant.smooth ? ' checked' : ''}><span class="form-check-label">Smooth motion</span></label>
      <label class="form-check form-switch"><input class="form-check-input" type="checkbox" data-variant-field="optimize"${variant.optimize ? ' checked' : ''}><span class="form-check-label">Optimize GIF</span></label>
    </div>`;
  updateCustomFields();
}

function updateCustomFields() {
  const editor = byId('testLabVariantEditor');
  if (!editor) return;
  [['height_preset', 'height_custom'], ['fps_preset', 'fps_custom'], ['clip_len_preset', 'clip_len_custom']]
    .forEach(([presetName, customName]) => {
      const preset = editor.querySelector(`[data-variant-field="${presetName}"]`);
      const custom = editor.querySelector(`[data-variant-field="${customName}"]`);
      const visible = preset?.value === 'custom';
      custom?.classList.toggle('d-none', !visible);
      if (custom) custom.disabled = !visible;
    });
}

function updateRunButton() {
  const button = byId('testLabRunButton');
  if (!button) return;
  const count = state.variants.length;
  const label = button.querySelector('span');
  if (label) label.textContent = count === 1 ? 'Generate GIF' : `Generate ${count} GIFs`;
  button.disabled = state.hasActiveRun;
}

function addVariant() {
  if (state.variants.length >= MAX_VARIANTS) return;
  const variant = makeDefaultVariant(config.defaults || {}, state.variants.length + 1);
  state.variants.push(variant);
  state.selectedVariantId = variant.id;
  saveDraft();
  renderVariantTabs();
  renderVariantEditor();
}

function duplicateVariant() {
  if (state.variants.length >= MAX_VARIANTS) return;
  const source = selectedVariant();
  const duplicate = normalizeVariant(
    {...source, id: createId('variant'), name: `${source.name} copy`.slice(0, 80)},
    config.defaults || {},
    state.variants.length + 1,
  );
  state.variants.push(duplicate);
  state.selectedVariantId = duplicate.id;
  saveDraft();
  renderVariantTabs();
  renderVariantEditor();
}

function removeVariant() {
  if (state.variants.length <= 1) return;
  const index = state.variants.findIndex(variant => variant.id === state.selectedVariantId);
  state.variants.splice(index, 1);
  state.selectedVariantId = state.variants[Math.min(index, state.variants.length - 1)].id;
  saveDraft();
  renderVariantTabs();
  renderVariantEditor();
}

async function fetchMediaBrowser(path) {
  const response = await fetch(`/api/media-browser?path=${encodeURIComponent(path || config.libRoot || '/library')}`);
  const payload = await response.json();
  if (!response.ok) throw new Error(payload.error || 'Path not found');
  return payload;
}

function renderMediaBrowser(payload) {
  const browser = byId('testLabBrowser');
  if (!browser) return;
  const parent = payload.parent
    ? `<button type="button" class="btn btn-outline-secondary btn-sm" data-media-path="${escapeHtml(payload.parent)}"><i class="bi bi-arrow-up" aria-hidden="true"></i><span>Parent</span></button>`
    : '';
  const folders = (payload.folders || []).map(item =>
    `<button type="button" class="btn btn-outline-secondary btn-sm" data-media-path="${escapeHtml(item.path)}"><i class="bi bi-folder2" aria-hidden="true"></i><span>${escapeHtml(item.name)}</span></button>`
  ).join('');
  const files = (payload.files || []).map(item =>
    `<button type="button" class="btn btn-outline-primary btn-sm" data-media-file="${escapeHtml(item.path)}"><i class="bi bi-film" aria-hidden="true"></i><span>${escapeHtml(item.name)}</span></button>`
  ).join('');
  browser.innerHTML = `<div class="media-browser-current"><code title="${escapeHtml(payload.path || '')}">${escapeHtml(payload.path || '')}</code></div>` +
    `<div class="media-browser-actions">${parent}${folders || '<span class="text-muted small">No folders</span>'}</div>` +
    `<div class="media-browser-files">${files || '<span class="text-muted small">No compatible videos</span>'}</div>`;
}

async function openMediaBrowser(path) {
  const browser = byId('testLabBrowser');
  if (browser) browser.innerHTML = '<div class="text-muted small">Loading library...</div>';
  try {
    renderMediaBrowser(await fetchMediaBrowser(path));
  } catch (error) {
    if (browser) browser.innerHTML = `<div class="text-danger small">${escapeHtml(error.message)}</div>`;
  }
}

function setMessage(message, detail = '', error = false) {
  const messageElement = byId('testLabMessage');
  const detailElement = byId('testLabDetail');
  if (messageElement) {
    messageElement.textContent = message;
    messageElement.classList.toggle('text-danger', error);
  }
  if (detailElement) detailElement.textContent = detail;
}

function renderRun(run) {
  const status = byId('testLabRunStatus');
  const variants = byId('testLabRunVariants');
  const bar = byId('testLabRunProgressBar');
  const progress = clampPercent(run?.progress_percent);
  if (bar) {
    bar.style.width = `${progress}%`;
    bar.textContent = `${progress}%`;
    bar.parentElement?.setAttribute('aria-valuenow', String(progress));
  }
  if (!run) {
    if (status) status.innerHTML = '<div class="text-muted">No test run yet.</div>';
    if (variants) variants.innerHTML = '';
    return;
  }
  if (status) status.innerHTML = `
    <div class="test-lab-run-heading">
      <div><strong>${escapeHtml(run.progress_label || run.status)}</strong><span>${escapeHtml(run.source_name || '')}</span></div>
      <span class="badge ${statusClass(run.status)}">${escapeHtml(run.status)}</span>
    </div>`;
  if (variants) {
    variants.innerHTML = (run.variants || []).map(variant => `
      <div class="test-lab-run-variant">
        <div><strong>${escapeHtml(variant.name)}</strong><span>${escapeHtml(variant.settings_label || '')}</span></div>
        <div class="test-lab-run-metrics"><span>${escapeHtml(variant.progress_label || variant.status)}</span><span>${escapeHtml(formatDuration(variant.elapsed_seconds))}</span><span>${escapeHtml(variant.gif_optimization_label || '')}</span></div>
      </div>`).join('');
  }
}

function tileStatusMarkup(file) {
  if (file.preview_status === 'failed') {
    return `<div class="test-player-status error" data-player-status="${escapeHtml(file.id)}"><i class="bi bi-exclamation-triangle" aria-hidden="true"></i><span>${escapeHtml(file.preview_error || 'Preview generation failed')}</span><button type="button" class="btn btn-outline-light btn-sm" data-retry-preview="${escapeHtml(file.id)}">Retry</button></div>`;
  }
  if (!file.display_url) {
    return `<div class="test-player-status" data-player-status="${escapeHtml(file.id)}"><span class="spinner-border spinner-border-sm" aria-hidden="true"></span><span>Preparing preview</span></div>`;
  }
  return `<div class="test-player-status" data-player-status="${escapeHtml(file.id)}"><span class="spinner-border spinner-border-sm" aria-hidden="true"></span><span>Decoding GIF</span></div>`;
}

function playerTile(file) {
  const badge = file.display_is_scaled
    ? `<span class="preview-badge">${escapeHtml(file.preview_label || 'Scaled preview')}</span>`
    : '<span class="preview-badge">Original preview</span>';
  return `<article class="test-player-tile" data-file-id="${escapeHtml(file.id)}">
    <header class="test-player-tile-header">
      <button type="button" class="btn btn-icon btn-sm test-drag-handle" data-keyboard-deck="${escapeHtml(file.id)}" aria-label="Move ${escapeHtml(file.name)}" title="Drag to reorder"><i class="bi bi-grip-vertical" aria-hidden="true"></i></button>
      <strong title="${escapeHtml(file.name)}">${escapeHtml(file.name)}</strong>
      <button type="button" class="btn btn-icon btn-sm" data-remove-comparison="${escapeHtml(file.id)}" aria-label="Remove ${escapeHtml(file.name)}" title="Remove from comparison"><i class="bi bi-x-lg" aria-hidden="true"></i></button>
    </header>
    <div class="test-player-canvas-wrap">
      <canvas data-player-canvas="${escapeHtml(file.id)}" aria-label="${escapeHtml(file.name)} preview"></canvas>
      ${tileStatusMarkup(file)}
    </div>
    <footer class="test-player-tile-footer">
      <div class="test-player-tile-meta">${badge}<span>${escapeHtml(file.size_label || '')}</span></div>
      <div class="test-player-tile-settings" title="${escapeHtml(file.settings_label || '')}">${escapeHtml(file.settings_label || '')}</div>
      <div class="test-player-tile-actions">
        <a class="btn btn-outline-secondary btn-icon btn-sm" href="${escapeHtml(file.original_url || file.url)}" target="_blank" rel="noopener" title="Open original" aria-label="Open original"><i class="bi bi-box-arrow-up-right" aria-hidden="true"></i></a>
        <a class="btn btn-outline-secondary btn-icon btn-sm" href="${escapeHtml(file.download_url || file.url)}" title="Download original" aria-label="Download original"><i class="bi bi-download" aria-hidden="true"></i></a>
      </div>
    </footer>
  </article>`;
}

function dropzoneMarkup(empty) {
  return `<div class="test-player-dropzone" data-comparison-dropzone><i class="bi bi-plus-square" aria-hidden="true"></i><span>${empty ? 'Drag a saved GIF here' : 'Add another GIF'}</span></div>`;
}

function setTileState(fileId, status, message = '') {
  const deck = byId('testLabPreviews');
  const target = Array.from(deck?.querySelectorAll('[data-player-status]') || [])
    .find(element => element.dataset.playerStatus === fileId);
  if (!target) return;
  target.classList.toggle('error', status === 'error');
  target.classList.toggle('ready', status === 'ready');
  if (status === 'ready') {
    target.innerHTML = '';
  } else if (status === 'loading') {
    target.innerHTML = '<span class="spinner-border spinner-border-sm" aria-hidden="true"></span><span>Decoding GIF</span>';
  } else if (status === 'error') {
    target.innerHTML = `<i class="bi bi-exclamation-triangle" aria-hidden="true"></i><span>${escapeHtml(message || 'GIF decode failed')}</span><button type="button" class="btn btn-outline-light btn-sm" data-retry-decode="${escapeHtml(fileId)}">Retry</button>`;
  }
}

function updatePlayerControls(playerState) {
  const deck = byId('testLabPreviews');
  const play = byId('testLabPlayPause');
  const timeline = byId('testLabTimeline');
  if (deck) deck.dataset.playerPhase = String(playerState.phase);
  if (play) {
    play.disabled = playerState.readyCount === 0;
    play.title = playerState.playing ? 'Pause previews' : 'Play previews';
    play.setAttribute('aria-label', play.title);
    play.innerHTML = `<i class="bi ${playerState.playing ? 'bi-pause-fill' : 'bi-play-fill'}" aria-hidden="true"></i>`;
  }
  if (timeline && document.activeElement !== timeline) timeline.value = String(Math.round(playerState.phase * 1000));
}

const player = new SynchronizedGifPlayer({
  onStateChange: updatePlayerControls,
  onTileState: setTileState,
});

function comparisonFiles() {
  return state.comparisonIds.map(id => state.filesById.get(id)).filter(Boolean);
}

function comparisonRenderSignature(files) {
  return JSON.stringify(files.map(file => [
    file.id,
    file.name,
    file.display_url,
    file.preview_status,
    file.preview_label,
    file.size_label,
    file.settings_label,
  ]));
}

function destroyDeckSortable() {
  deckSortable?.destroy();
  deckSortable = null;
}

function initializeDeckSortable() {
  destroyDeckSortable();
  const deck = byId('testLabPreviews');
  if (!deck) return;
  deckSortable = Sortable.create(deck, {
    group: {name: 'test-lab-comparison', pull: true, put: true},
    draggable: '[data-file-id]',
    handle: '.test-drag-handle',
    animation: 150,
    forceFallback: true,
    fallbackTolerance: 5,
    delay: 120,
    delayOnTouchOnly: true,
    touchStartThreshold: 4,
    ghostClass: 'test-sortable-ghost',
    chosenClass: 'test-sortable-chosen',
    dragClass: 'test-sortable-drag',
    onMove(event) {
      const id = event.dragged?.dataset.fileId || '';
      if (event.from !== deck && (state.comparisonIds.length >= MAX_COMPARISON_ITEMS || state.comparisonIds.includes(id))) return false;
      return true;
    },
    onAdd(event) {
      const id = event.item?.dataset.fileId || '';
      event.item?.remove();
      if (!id || !state.filesById.has(id) || state.comparisonIds.includes(id) || state.comparisonIds.length >= MAX_COMPARISON_ITEMS) {
        renderComparison(true);
        return;
      }
      const index = Math.max(0, Math.min(event.newIndex, state.comparisonIds.length));
      state.comparisonIds.splice(index, 0, id);
      saveComparison();
      renderComparison(true);
      announce(`${state.filesById.get(id).name} added to comparison`);
    },
    onEnd(event) {
      if (event.from !== deck || event.to !== deck) return;
      state.comparisonIds = Array.from(deck.querySelectorAll('.test-player-tile'), item => item.dataset.fileId);
      saveComparison();
      announce('Comparison order updated');
    },
  });
}

function requestSelectedPreviews(files) {
  files.filter(file => file.preview_status === 'needed').forEach(file => requestPreview(file.id));
  const pending = files.some(file => ['needed', 'generating'].includes(file.preview_status));
  clearTimeout(previewPollTimer);
  previewPollTimer = null;
  if (pending && !document.hidden) {
    previewPollTimer = setTimeout(async () => {
      await refreshInventory();
    }, 1000);
  }
}

function renderComparison(force = false) {
  const deck = byId('testLabPreviews');
  if (!deck) return;
  if (state.inventoryLoaded) {
    state.comparisonIds = normalizeComparisonIds(state.comparisonIds, state.filesById.keys());
    saveComparison();
  }
  const files = comparisonFiles();
  const signature = comparisonRenderSignature(files);
  if (!force && signature === comparisonSignature) return;
  comparisonSignature = signature;
  keyboardDrag = null;
  deck.dataset.count = String(files.length);
  deck.innerHTML = files.map(playerTile).join('') + (files.length < MAX_COMPARISON_ITEMS ? dropzoneMarkup(files.length === 0) : '');
  initializeDeckSortable();

  const waiting = files.some(file => !file.display_url && file.preview_status !== 'failed');
  const playable = files.filter(file => file.display_url);
  requestSelectedPreviews(files);
  if (waiting || !playable.length) {
    player.clear();
    return;
  }
  const canvases = new Map();
  deck.querySelectorAll('[data-player-canvas]').forEach(canvas => canvases.set(canvas.dataset.playerCanvas, canvas));
  player.load(playable, canvases, {autoplay: true});
}

async function requestPreview(fileId, retry = false) {
  if (state.previewRequests.has(fileId)) return;
  state.previewRequests.add(fileId);
  try {
    const response = await fetch('/api/test-lab/preview', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({file_id: fileId, retry}),
    });
    if (!response.ok) throw new Error((await response.json()).error || 'Preview request failed');
    await refreshInventory();
  } catch (error) {
    setMessage('Preview request failed', error.message, true);
  } finally {
    state.previewRequests.delete(fileId);
  }
}

function renderSavedTray() {
  const tray = byId('testLabSavedTray');
  if (!tray) return;
  const query = state.trayQuery.trim().toLowerCase();
  const files = Array.from(state.filesById.values()).filter(file => {
    const haystack = `${file.name} ${file.source_name} ${file.settings_label}`.toLowerCase();
    return !query || haystack.includes(query);
  });
  tray.innerHTML = files.length ? files.map(file => `
    <div class="test-saved-tray-item" data-file-id="${escapeHtml(file.id)}">
      <button type="button" class="btn btn-icon btn-sm test-drag-handle" data-keyboard-tray="${escapeHtml(file.id)}" title="Drag into comparison" aria-label="Add ${escapeHtml(file.name)} to comparison"><i class="bi bi-grip-vertical" aria-hidden="true"></i></button>
      <div><strong title="${escapeHtml(file.name)}">${escapeHtml(file.name)}</strong><span title="${escapeHtml(file.settings_label || '')}">${escapeHtml(file.settings_label || '')}</span></div>
      <span>${escapeHtml(file.size_label || '')}</span>
    </div>`).join('') : '<div class="text-muted small py-3 text-center">No matching saved GIFs.</div>';
  traySortable?.destroy();
  traySortable = Sortable.create(tray, {
    group: {name: 'test-lab-comparison', pull: 'clone', put: false},
    sort: false,
    draggable: '[data-file-id]',
    handle: '.test-drag-handle',
    forceFallback: true,
    fallbackTolerance: 5,
    delay: 120,
    delayOnTouchOnly: true,
    touchStartThreshold: 4,
    ghostClass: 'test-sortable-ghost',
    chosenClass: 'test-sortable-chosen',
  });
}

function renderSavedTable() {
  const body = byId('testLabFilesBody');
  const total = byId('testLabTotalSize');
  if (!body) return;
  const files = Array.from(state.filesById.values());
  if (total) total.textContent = state.totalSizeLabel || '0 B';
  body.innerHTML = files.length ? files.map(file => {
    const name = state.renameDrafts.has(file.id) ? state.renameDrafts.get(file.id) : file.name;
    return `<tr>
      <td><input class="form-check-input" type="checkbox" data-test-file-id="${escapeHtml(file.id)}" aria-label="Select ${escapeHtml(file.name)}"${state.selectedFileIds.has(file.id) ? ' checked' : ''}></td>
      <td><div class="rename-inline"><input class="form-control form-control-sm" data-test-rename-id="${escapeHtml(file.id)}" value="${escapeHtml(name)}" maxlength="80" aria-label="Saved GIF name"><button type="button" class="btn btn-outline-secondary btn-icon btn-sm" data-save-rename="${escapeHtml(file.id)}" title="Save name" aria-label="Save name"><i class="bi bi-check-lg" aria-hidden="true"></i></button></div></td>
      <td class="path-cell"><code title="${escapeHtml(file.source_name || '')}">${escapeHtml(file.source_name || '')}</code></td>
      <td>${escapeHtml(file.size_label || '')}</td>
      <td>${escapeHtml(file.gif_optimization_label || '')}</td>
      <td>${escapeHtml(file.settings_label || '')}</td>
      <td><div class="table-actions"><a class="btn btn-outline-secondary btn-icon btn-sm" href="${escapeHtml(file.original_url || file.url)}" target="_blank" rel="noopener" title="Open original" aria-label="Open original"><i class="bi bi-box-arrow-up-right" aria-hidden="true"></i></a><a class="btn btn-outline-secondary btn-icon btn-sm" href="${escapeHtml(file.download_url || file.url)}" title="Download original" aria-label="Download original"><i class="bi bi-download" aria-hidden="true"></i></a></div></td>
    </tr>`;
  }).join('') : '<tr><td colspan="7" class="text-muted text-center py-4">No saved test GIFs.</td></tr>';
  updateSelectAll();
}

function updateSelectAll() {
  const selectAll = byId('testLabSelectAll');
  if (!selectAll) return;
  const ids = Array.from(state.filesById.keys());
  const selected = ids.filter(id => state.selectedFileIds.has(id)).length;
  selectAll.checked = ids.length > 0 && selected === ids.length;
  selectAll.indeterminate = selected > 0 && selected < ids.length;
  const deleteButton = byId('testLabDeleteSelected');
  if (deleteButton) deleteButton.disabled = selected === 0;
}

async function refreshInventory() {
  if (inventoryRefreshing || document.hidden) return;
  inventoryRefreshing = true;
  try {
    const response = await fetch('/api/test-lab/files');
    if (!response.ok) return;
    const payload = await response.json();
    state.filesById = new Map((payload.files || []).map(file => [file.id, file]));
    state.inventoryLoaded = true;
    state.totalSizeLabel = payload.total_size_label || '0 B';
    state.selectedFileIds = new Set(Array.from(state.selectedFileIds).filter(id => state.filesById.has(id)));
    state.renameDrafts = new Map(Array.from(state.renameDrafts.entries()).filter(([id]) => state.filesById.has(id)));
    renderSavedTray();
    renderSavedTable();
    renderComparison();
  } catch (error) {
    // A manual refresh remains available after transient failures.
  } finally {
    inventoryRefreshing = false;
  }
}

function scheduleRunPoll() {
  clearTimeout(runPollTimer);
  runPollTimer = null;
  if (!state.hasActiveRun || document.hidden) return;
  runPollTimer = setTimeout(refreshRunStatus, 1000);
}

async function handleCompletedRun(run) {
  if (!run || !state.pendingRunId || run.id !== state.pendingRunId || !TERMINAL_RUN_STATUSES.has(run.status)) return;
  await refreshInventory();
  state.comparisonIds = successfulFileIds(run);
  saveComparison();
  comparisonSignature = '';
  renderComparison(true);
  state.pendingRunId = '';
  storageSet(PENDING_RUN_STORAGE_KEY, '');
}

async function refreshRunStatus() {
  if (runRefreshing || document.hidden) return;
  runRefreshing = true;
  try {
    const response = await fetch('/api/test-lab/run-status');
    if (!response.ok) return;
    const payload = await response.json();
    state.run = payload.active_run || null;
    state.hasActiveRun = Boolean(payload.has_active_run);
    renderRun(state.run);
    updateRunButton();
    await handleCompletedRun(state.run);
  } catch (error) {
    // Active runs will retry while their polling loop remains scheduled.
  } finally {
    runRefreshing = false;
    scheduleRunPoll();
  }
}

async function startRun() {
  const video = byId('testLabVideo')?.value.trim() || '';
  if (!video) {
    setMessage('Choose one compatible video file', '', true);
    return;
  }
  if (state.variants.length < 1 || state.variants.length > MAX_VARIANTS) {
    setMessage('Choose 1 to 4 variants', '', true);
    return;
  }
  state.video = video;
  saveDraft();
  state.hasActiveRun = true;
  updateRunButton();
  setMessage('Starting test run', `${state.variants.length} ${state.variants.length === 1 ? 'GIF' : 'GIFs'}`);
  try {
    const response = await fetch('/api/test-lab/run', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({video, variants: state.variants.map(variantRequest)}),
    });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || 'Could not start test run');
    state.pendingRunId = payload.run_id;
    storageSet(PENDING_RUN_STORAGE_KEY, state.pendingRunId);
    state.run = payload.status?.active_run || null;
    state.hasActiveRun = true;
    renderRun(state.run);
    setMessage('Test run started', 'The comparison will update when generation finishes.');
    scheduleRunPoll();
  } catch (error) {
    state.hasActiveRun = false;
    updateRunButton();
    setMessage('Could not start test run', error.message, true);
  }
}

async function saveRename(fileId) {
  const file = state.filesById.get(fileId);
  const name = String(state.renameDrafts.get(fileId) ?? file?.name ?? '').trim();
  if (!file || !name) return;
  try {
    const response = await fetch('/api/test-lab/rename', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({file_id: fileId, name}),
    });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || 'Could not save name');
    state.renameDrafts.delete(fileId);
    comparisonSignature = '';
    await refreshInventory();
  } catch (error) {
    setMessage('Could not save name', error.message, true);
  }
}

async function deleteSelected() {
  const ids = Array.from(state.selectedFileIds);
  if (!ids.length || !window.confirm(`Delete ${ids.length} saved test ${ids.length === 1 ? 'GIF' : 'GIFs'}?`)) return;
  try {
    const response = await fetch('/api/test-lab/delete', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({file_ids: ids}),
    });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || 'Delete failed');
    (payload.deleted || []).forEach(id => state.selectedFileIds.delete(id));
    comparisonSignature = '';
    await refreshInventory();
    const refused = (payload.refused || []).length;
    setMessage('Saved GIFs updated', refused ? `${refused} active GIFs could not be deleted.` : 'Selected GIFs were deleted.');
  } catch (error) {
    setMessage('Could not delete saved GIFs', error.message, true);
  }
}

function syncDeckDomOrder() {
  const deck = byId('testLabPreviews');
  if (!deck) return;
  const dropzone = deck.querySelector('[data-comparison-dropzone]');
  state.comparisonIds.forEach(id => {
    const item = Array.from(deck.querySelectorAll('.test-player-tile')).find(element => element.dataset.fileId === id);
    if (item) deck.insertBefore(item, dropzone || null);
  });
}

function focusDeckHandle(fileId) {
  requestAnimationFrame(() => {
    const handle = Array.from(byId('testLabPreviews')?.querySelectorAll('[data-keyboard-deck]') || [])
      .find(element => element.dataset.keyboardDeck === fileId);
    handle?.focus();
  });
}

function handleKeyboardDrag(event, fileId) {
  const currentIndex = state.comparisonIds.indexOf(fileId);
  if (currentIndex < 0) return;
  if (!keyboardDrag) {
    if (![' ', 'Enter'].includes(event.key)) return;
    event.preventDefault();
    keyboardDrag = {fileId, original: [...state.comparisonIds]};
    event.currentTarget.setAttribute('aria-pressed', 'true');
    announce(`${state.filesById.get(fileId)?.name || 'GIF'} picked up. Use arrow keys to move; Enter to drop; Escape to cancel.`);
    return;
  }
  if (keyboardDrag.fileId !== fileId) return;
  if (event.key === 'Escape') {
    event.preventDefault();
    state.comparisonIds = keyboardDrag.original;
    keyboardDrag = null;
    syncDeckDomOrder();
    saveComparison();
    focusDeckHandle(fileId);
    announce('Move cancelled');
    return;
  }
  if ([' ', 'Enter'].includes(event.key)) {
    event.preventDefault();
    keyboardDrag = null;
    event.currentTarget.setAttribute('aria-pressed', 'false');
    saveComparison();
    announce('GIF dropped');
    return;
  }
  const delta = ['ArrowLeft', 'ArrowUp'].includes(event.key) ? -1 : (['ArrowRight', 'ArrowDown'].includes(event.key) ? 1 : 0);
  if (!delta) return;
  event.preventDefault();
  const target = Math.max(0, Math.min(state.comparisonIds.length - 1, currentIndex + delta));
  state.comparisonIds = reorderIds(state.comparisonIds, currentIndex, target);
  syncDeckDomOrder();
  focusDeckHandle(fileId);
  announce(`Moved to position ${target + 1} of ${state.comparisonIds.length}`);
}

function addFromKeyboard(fileId) {
  if (state.comparisonIds.includes(fileId)) {
    announce('That GIF is already in the comparison');
    return;
  }
  if (state.comparisonIds.length >= MAX_COMPARISON_ITEMS) {
    announce('The comparison already has four GIFs');
    return;
  }
  state.comparisonIds.push(fileId);
  saveComparison();
  renderComparison(true);
  announce(`${state.filesById.get(fileId)?.name || 'GIF'} added to comparison`);
}

function bindEvents() {
  byId('testLabVideo')?.addEventListener('input', event => {
    state.video = event.target.value;
    saveDraft();
  });
  byId('testLabBrowseButton')?.addEventListener('click', () => openMediaBrowser(byId('testLabVideo')?.value.trim() || config.libRoot || '/library'));
  byId('testLabBrowser')?.addEventListener('click', event => {
    const pathButton = event.target.closest('[data-media-path]');
    const fileButton = event.target.closest('[data-media-file]');
    if (pathButton) openMediaBrowser(pathButton.dataset.mediaPath);
    if (fileButton) {
      const input = byId('testLabVideo');
      if (input) input.value = fileButton.dataset.mediaFile;
      state.video = fileButton.dataset.mediaFile;
      saveDraft();
      setMessage('Source selected', fileButton.dataset.mediaFile);
    }
  });
  byId('testLabAddVariant')?.addEventListener('click', addVariant);
  byId('testLabVariantTabs')?.addEventListener('click', event => {
    const button = event.target.closest('[data-variant-id]');
    if (!button) return;
    state.selectedVariantId = button.dataset.variantId;
    saveDraft();
    renderVariantTabs();
    renderVariantEditor();
  });
  byId('testLabVariantEditor')?.addEventListener('input', event => {
    const field = event.target.dataset.variantField;
    const variant = selectedVariant();
    if (!field || !variant) return;
    variant[field] = event.target.type === 'checkbox' ? event.target.checked : event.target.value;
    saveDraft();
    renderVariantTabs();
  });
  byId('testLabVariantEditor')?.addEventListener('change', event => {
    if (event.target.dataset.variantField?.endsWith('_preset')) updateCustomFields();
  });
  byId('testLabVariantEditor')?.addEventListener('click', event => {
    if (event.target.closest('#testLabDuplicateVariant')) duplicateVariant();
    if (event.target.closest('#testLabRemoveVariant')) removeVariant();
  });
  byId('testLabRunButton')?.addEventListener('click', startRun);
  byId('testLabPlayPause')?.addEventListener('click', () => player.playing ? player.pause() : player.play());
  byId('testLabRestartPreviews')?.addEventListener('click', () => player.restart(true));
  byId('testLabTimeline')?.addEventListener('input', event => player.seek(Number(event.target.value) / 1000));
  byId('testLabPlaybackSpeed')?.addEventListener('change', event => player.setSpeed(event.target.value));
  byId('testLabRefreshFiles')?.addEventListener('click', refreshInventory);
  byId('testLabTraySearch')?.addEventListener('input', event => {
    state.trayQuery = event.target.value;
    renderSavedTray();
  });
  byId('testLabPreviews')?.addEventListener('click', event => {
    const remove = event.target.closest('[data-remove-comparison]');
    const retryPreview = event.target.closest('[data-retry-preview]');
    const retryDecode = event.target.closest('[data-retry-decode]');
    if (remove) {
      state.comparisonIds = state.comparisonIds.filter(id => id !== remove.dataset.removeComparison);
      saveComparison();
      comparisonSignature = '';
      renderComparison(true);
    }
    if (retryPreview) requestPreview(retryPreview.dataset.retryPreview, true);
    if (retryDecode) {
      comparisonSignature = '';
      renderComparison(true);
    }
  });
  byId('testLabPreviews')?.addEventListener('keydown', event => {
    const handle = event.target.closest('[data-keyboard-deck]');
    if (handle) handleKeyboardDrag(event, handle.dataset.keyboardDeck);
  });
  byId('testLabSavedTray')?.addEventListener('keydown', event => {
    const handle = event.target.closest('[data-keyboard-tray]');
    if (!handle || ![' ', 'Enter'].includes(event.key)) return;
    event.preventDefault();
    addFromKeyboard(handle.dataset.keyboardTray);
  });
  byId('testLabSelectAll')?.addEventListener('change', event => {
    state.selectedFileIds = event.target.checked ? new Set(state.filesById.keys()) : new Set();
    renderSavedTable();
  });
  byId('testLabFilesBody')?.addEventListener('change', event => {
    const checkbox = event.target.closest('[data-test-file-id]');
    if (!checkbox) return;
    if (checkbox.checked) state.selectedFileIds.add(checkbox.dataset.testFileId);
    else state.selectedFileIds.delete(checkbox.dataset.testFileId);
    updateSelectAll();
  });
  byId('testLabFilesBody')?.addEventListener('input', event => {
    if (event.target.dataset.testRenameId) state.renameDrafts.set(event.target.dataset.testRenameId, event.target.value);
  });
  byId('testLabFilesBody')?.addEventListener('click', event => {
    const save = event.target.closest('[data-save-rename]');
    if (save) saveRename(save.dataset.saveRename);
  });
  byId('testLabFilesBody')?.addEventListener('keydown', event => {
    if (event.key === 'Enter' && event.target.dataset.testRenameId) {
      event.preventDefault();
      saveRename(event.target.dataset.testRenameId);
    }
  });
  byId('testLabDeleteSelected')?.addEventListener('click', deleteSelected);
  byId('tab-test')?.addEventListener('shown.bs.tab', () => {
    if (resumeAfterTab) player.play();
    resumeAfterTab = false;
    refreshRunStatus();
    refreshInventory();
  });
  byId('tab-test')?.addEventListener('hidden.bs.tab', () => {
    resumeAfterTab = player.playing;
    player.pause();
  });
  document.addEventListener('visibilitychange', () => {
    if (document.hidden) {
      resumeAfterVisibility = player.playing;
      player.pause();
      clearTimeout(runPollTimer);
      clearTimeout(previewPollTimer);
    } else {
      refreshRunStatus();
      refreshInventory();
      if (resumeAfterVisibility && byId('tab-test')?.classList.contains('active')) player.play();
      resumeAfterVisibility = false;
    }
  });
}

function init() {
  if (!byId('testLabWorkbench')) return;
  const video = byId('testLabVideo');
  if (video) video.value = state.video;
  renderVariantTabs();
  renderVariantEditor();
  renderRun(null);
  renderComparison(true);
  bindEvents();
  refreshRunStatus();
  refreshInventory();
  window.vid2gifTestLab = {
    refreshInventory,
    getState: () => ({
      variants: state.variants.map(variant => ({...variant})),
      comparisonIds: [...state.comparisonIds],
      run: state.run ? {...state.run} : null,
      player: {phase: player.phase, playing: player.playing, readyCount: player.tracks.size},
    }),
  };
}

document.addEventListener('DOMContentLoaded', init);
