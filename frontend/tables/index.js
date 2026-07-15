import {compareTableValues, nextSort, normalizePreference} from './logic.js';

const initialized = new WeakSet();
const pendingRefresh = new Set();
const preferences = new Map();
let saveTimer = null;

function tableId(table) {
  return String(table.dataset.tableId || '').trim().toLowerCase();
}

function headers(table) {
  return Array.from(table.tHead?.rows?.[0]?.cells || []);
}

function preferenceFor(table) {
  const id = tableId(table);
  if (!preferences.has(id)) preferences.set(id, {widths: {}});
  return preferences.get(id);
}

function ensureColgroup(table) {
  let group = table.querySelector(':scope > colgroup[data-table-columns]');
  if (!group) {
    group = document.createElement('colgroup');
    group.dataset.tableColumns = '';
    table.insertBefore(group, table.firstChild);
  }
  const count = headers(table).length;
  while (group.children.length < count) group.appendChild(document.createElement('col'));
  while (group.children.length > count) group.lastElementChild.remove();
  return group;
}

function persist(table) {
  const id = tableId(table);
  if (!id) return;
  clearTimeout(saveTimer);
  saveTimer = setTimeout(async () => {
    try {
      await fetch('/api/settings', {
        method: 'PATCH',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({table_preferences: {[id]: preferences.get(id)}})
      });
    } catch (_error) {
      // The current layout remains usable; the next interaction retries persistence.
    }
  }, 300);
}

function establishWidths(table) {
  const cols = Array.from(ensureColgroup(table).children);
  const pref = preferenceFor(table);
  headers(table).forEach((header, index) => {
    const key = header.dataset.columnId;
    if (!pref.widths[key]) pref.widths[key] = Math.max(48, Math.ceil(header.getBoundingClientRect().width));
    cols[index].style.width = `${pref.widths[key]}px`;
  });
  table.classList.add('workspace-table-sized');
  table.style.width = `${headers(table).reduce((sum, header) => sum + Number(pref.widths[header.dataset.columnId] || 0), 0)}px`;
}

function applyWidths(table) {
  const pref = preferenceFor(table);
  if (!Object.keys(pref.widths || {}).length) return;
  const cols = Array.from(ensureColgroup(table).children);
  headers(table).forEach((header, index) => {
    const width = pref.widths[header.dataset.columnId];
    if (width) {
      cols[index].style.width = `${width}px`;
      const handle = header.querySelector('.table-resize-handle');
      if (handle) {
        handle.setAttribute('aria-valuenow', String(Math.round(width)));
        handle.setAttribute('aria-valuetext', `${Math.round(width)} pixels`);
      }
    }
  });
  table.classList.add('workspace-table-sized');
  table.style.width = `${headers(table).reduce((sum, header) => sum + Number(pref.widths[header.dataset.columnId] || header.getBoundingClientRect().width || 80), 0)}px`;
}

function setWidth(table, header, width, save = true) {
  establishWidths(table);
  const pref = preferenceFor(table);
  const appliedWidth = Math.max(48, Math.min(4096, Math.round(width)));
  pref.widths[header.dataset.columnId] = appliedWidth;
  const handle = header.querySelector('.table-resize-handle');
  if (handle) {
    handle.setAttribute('aria-valuenow', String(appliedWidth));
    handle.setAttribute('aria-valuetext', `${appliedWidth} pixels`);
  }
  applyWidths(table);
  if (save) persist(table);
}

function autoSize(table, header) {
  const index = header.cellIndex;
  let width = Math.max(header.scrollWidth, 48);
  Array.from(table.tBodies).forEach(body => {
    Array.from(body.rows).forEach(row => {
      const cell = row.cells[index];
      if (!cell) return;
      width = Math.max(width, cell.scrollWidth, ...Array.from(cell.querySelectorAll('*'), item => item.scrollWidth || 0));
    });
  });
  setWidth(table, header, width + 20);
}

function cellValue(row, index) {
  const cell = row.cells[index];
  return cell?.dataset.sortValue ?? cell?.textContent?.trim() ?? '';
}

function sortClient(table) {
  const pref = preferenceFor(table);
  const sort = pref.sort;
  if (!sort) return;
  const header = headers(table).find(item => item.dataset.columnId === sort.column);
  if (!header) return;
  const index = header.cellIndex;
  const type = header.dataset.sortType || 'text';
  Array.from(table.tBodies).forEach(body => {
    const rows = Array.from(body.rows);
    if (rows.length < 2 || rows.some(row => row.cells.length !== headers(table).length)) return;
    const ordered = rows.map((row, position) => ({row, position})).sort((a, b) => {
      const compared = compareTableValues(cellValue(a.row, index), cellValue(b.row, index), type);
      return (sort.direction === 'desc' ? -compared : compared) || a.position - b.position;
    });
    if (ordered.some((item, position) => item.row !== rows[position])) {
      ordered.forEach(item => body.appendChild(item.row));
    }
  });
}

function updateSortHeaders(table) {
  const sort = preferenceFor(table).sort;
  headers(table).forEach(header => {
    if (header.dataset.sortable !== 'true') {
      header.removeAttribute('aria-sort');
      return;
    }
    const active = sort?.column === header.dataset.columnId;
    header.setAttribute('aria-sort', active ? (sort.direction === 'asc' ? 'ascending' : 'descending') : 'none');
    const icon = header.querySelector('.table-sort-icon');
    if (icon) icon.className = `bi table-sort-icon ${active ? (sort.direction === 'asc' ? 'bi-sort-up' : 'bi-sort-down') : 'bi-arrow-down-up'}`;
  });
}

function activateSort(table, header) {
  const pref = preferenceFor(table);
  pref.sort = nextSort(pref.sort, header.dataset.columnId);
  updateSortHeaders(table);
  persist(table);
  if (table.dataset.sortMode === 'server') {
    table.dispatchEvent(new CustomEvent('vid2gif:table-sort', {bubbles: true, detail: {...pref.sort, tableId: tableId(table)}}));
  } else {
    sortClient(table);
  }
}

function decorateHeader(table, header) {
  if (!header.dataset.columnId) return;
  const sortable = header.dataset.sortable === 'true' && table.dataset.sortMode !== 'none';
  if (sortable && !header.querySelector('.table-sort-button')) {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'table-sort-button';
    while (header.firstChild) button.appendChild(header.firstChild);
    const icon = document.createElement('i');
    icon.className = 'bi bi-arrow-down-up table-sort-icon';
    icon.setAttribute('aria-hidden', 'true');
    button.appendChild(icon);
    button.addEventListener('click', () => activateSort(table, header));
    header.appendChild(button);
  }
  if (header.dataset.resizable === 'false' || header.querySelector('.table-resize-handle')) return;
  const handle = document.createElement('span');
  handle.className = 'table-resize-handle';
  handle.setAttribute('role', 'separator');
  handle.setAttribute('aria-orientation', 'vertical');
  handle.setAttribute('aria-label', `Resize ${header.textContent.trim()} column`);
  handle.setAttribute('aria-valuemin', '48');
  handle.setAttribute('aria-valuemax', '4096');
  const currentWidth = Math.max(48, Math.round(header.getBoundingClientRect().width));
  handle.setAttribute('aria-valuenow', String(currentWidth));
  handle.setAttribute('aria-valuetext', `${currentWidth} pixels`);
  handle.tabIndex = 0;
  let startX = 0;
  let startWidth = 0;
  let dragged = false;
  let lastTap = 0;
  handle.addEventListener('pointerdown', event => {
    event.preventDefault();
    startX = event.clientX;
    startWidth = header.getBoundingClientRect().width;
    dragged = false;
    handle.setPointerCapture(event.pointerId);
  });
  handle.addEventListener('pointermove', event => {
    if (!handle.hasPointerCapture(event.pointerId)) return;
    const delta = event.clientX - startX;
    if (Math.abs(delta) > 2) dragged = true;
    setWidth(table, header, startWidth + delta, false);
  });
  handle.addEventListener('pointerup', event => {
    if (handle.hasPointerCapture(event.pointerId)) handle.releasePointerCapture(event.pointerId);
    if (dragged) {
      persist(table);
      return;
    }
    const now = Date.now();
    if (now - lastTap < 350) autoSize(table, header);
    lastTap = now;
  });
  handle.addEventListener('dblclick', event => {
    event.preventDefault();
    autoSize(table, header);
  });
  handle.addEventListener('keydown', event => {
    if (event.key === 'Enter') {
      event.preventDefault();
      autoSize(table, header);
    } else if (event.key === 'ArrowLeft' || event.key === 'ArrowRight') {
      event.preventDefault();
      setWidth(table, header, header.getBoundingClientRect().width + (event.key === 'ArrowRight' ? 16 : -16));
    }
  });
  header.appendChild(handle);
}

function enhance(table) {
  if (!(table instanceof HTMLTableElement) || !tableId(table)) return;
  if (!initialized.has(table)) {
    initialized.add(table);
    headers(table).forEach(header => decorateHeader(table, header));
  }
  applyWidths(table);
  updateSortHeaders(table);
  if (table.dataset.sortMode === 'server') {
    const sort = preferenceFor(table).sort;
    const supported = headers(table).some(header => header.dataset.columnId === sort?.column && header.dataset.sortable === 'true');
    if (sort && supported && (table.dataset.currentSort !== sort.column || table.dataset.currentDirection !== sort.direction)) {
      table.dataset.currentSort = sort.column;
      table.dataset.currentDirection = sort.direction;
      table.dispatchEvent(new CustomEvent('vid2gif:table-sort', {bubbles: true, detail: {...sort, tableId: tableId(table)}}));
    }
  } else if (table.dataset.sortMode !== 'none') {
    sortClient(table);
  }
}

function refresh(root = document) {
  if (root instanceof HTMLTableElement) enhance(root);
  root.querySelectorAll?.('table[data-table-id]').forEach(enhance);
}

function scheduleRefresh(root) {
  pendingRefresh.add(root instanceof Element ? root : document);
  queueMicrotask(() => {
    pendingRefresh.forEach(refresh);
    pendingRefresh.clear();
  });
}

async function init() {
  try {
    const response = await fetch('/api/settings');
    const data = response.ok ? await response.json() : {};
    Object.entries(data.settings?.table_preferences || {}).forEach(([id, value]) => preferences.set(id, normalizePreference(value)));
  } catch (_error) {
    // Tables remain fully functional with default widths and ordering.
  }
  refresh();
  new MutationObserver(mutations => mutations.forEach(mutation => scheduleRefresh(mutation.target))).observe(document.body, {childList: true, subtree: true});
}

window.vid2gifTables = {refresh, preference: id => preferences.get(id) || {widths: {}}};
document.addEventListener('DOMContentLoaded', init);
