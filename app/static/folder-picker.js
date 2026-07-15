(function () {
  const SHARED_SOURCE_KEY = 'vid2gif_maintenance_scan_source';

  function byId(id) {
    return document.getElementById(id);
  }

  function setOpen(panel, button, open) {
    if (!panel) return;
    if (window.bootstrap?.Collapse) {
      const collapse = window.bootstrap.Collapse.getOrCreateInstance(panel, {toggle: false});
      if (open) collapse.show();
      else collapse.hide();
    } else {
      panel.classList.toggle('show', open);
    }
    if (button) {
      button.setAttribute('aria-expanded', open ? 'true' : 'false');
      const label = button.querySelector('span');
      if (label) label.textContent = open ? 'Hide folders' : 'Choose folder';
    }
  }

  function create(options) {
    const input = byId(options.inputId);
    const button = byId(options.buttonId);
    const panel = byId(options.panelId);
    const container = byId(options.containerId);
    if (!input || !button || !panel || !container) return null;
    let currentPath = input.value || options.defaultPath || '/library';
    let folders = [];
    let loaded = false;

    try {
      const saved = localStorage.getItem(SHARED_SOURCE_KEY) || localStorage.getItem(options.storageKey || '');
      if (saved && !options.preserveInitialValue) input.value = saved;
    } catch (_err) {}

    function selectedFolder() {
      const select = container.querySelector('[data-folder-picker-select]');
      return folders.find((folder) => folder.path === select?.value) || null;
    }

    function filterFolders() {
      const query = String(container.querySelector('[data-folder-picker-search]')?.value || '').trim().toLowerCase();
      const select = container.querySelector('[data-folder-picker-select]');
      if (!select) return;
      select.innerHTML = folders
        .filter((folder) => !query || String(folder.name || '').toLowerCase().includes(query))
        .map((folder) => `<option value="${escapeHtml(folder.path)}" title="${escapeHtml(folder.path)}">${escapeHtml(folder.name)}</option>`)
        .join('');
    }

    function escapeHtml(value) {
      return String(value == null ? '' : value)
        .replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;')
        .replaceAll('"', '&quot;').replaceAll("'", '&#39;');
    }

    async function choose(path) {
      let selected = path || currentPath;
      try {
        if (options.onChoose) selected = (await options.onChoose(selected)) || selected;
      } catch (error) {
        const prior = container.querySelector('[data-folder-picker-error]');
        if (prior) prior.remove();
        container.insertAdjacentHTML(
          'afterbegin',
          `<div class="small text-danger mb-2" data-folder-picker-error>${escapeHtml(error.message || 'Folder could not be selected')}</div>`,
        );
        return null;
      }
      input.value = selected;
      try {
        localStorage.setItem(SHARED_SOURCE_KEY, selected);
        if (options.storageKey) localStorage.setItem(options.storageKey, selected);
      } catch (_err) {}
      input.dispatchEvent(new Event('change', {bubbles: true}));
      setOpen(panel, button, false);
      return selected;
    }

    async function load(path) {
      currentPath = path || input.value || options.defaultPath || '/library';
      setOpen(panel, button, true);
      container.innerHTML = '<div class="small text-muted">Loading folders...</div>';
      try {
        const response = await fetch(`/api/media-browser?path=${encodeURIComponent(currentPath)}`);
        const data = await response.json();
        if (!response.ok) throw new Error(data.error || 'Path not found');
        currentPath = data.path || currentPath;
        folders = Array.isArray(data.folders) ? data.folders : [];
        loaded = true;
        container.innerHTML = `
          <div class="folder-picker-current"><span>Current folder</span><code title="${escapeHtml(currentPath)}">${escapeHtml(currentPath)}</code></div>
          <label class="form-label small mb-1">Search immediate subfolders
            <input class="form-control form-control-sm" data-folder-picker-search type="search" placeholder="Type to filter this list" autocomplete="off">
          </label>
          <select class="form-select form-select-sm folder-picker-select" data-folder-picker-select size="8" aria-label="Subfolders"></select>
          <div class="folder-picker-actions">
            <button class="btn btn-outline-secondary btn-sm" type="button" data-folder-picker-up${data.parent ? '' : ' disabled'}><i class="bi bi-arrow-up" aria-hidden="true"></i><span>Up</span></button>
            <button class="btn btn-outline-secondary btn-sm" type="button" data-folder-picker-open${folders.length ? '' : ' disabled'}><i class="bi bi-folder2-open" aria-hidden="true"></i><span>Open selected</span></button>
            <button class="btn btn-primary btn-sm" type="button" data-folder-picker-use-selected${folders.length ? '' : ' disabled'}><i class="bi bi-check2" aria-hidden="true"></i><span>Use selected</span></button>
            <button class="btn btn-outline-primary btn-sm" type="button" data-folder-picker-use-current><i class="bi bi-check2-all" aria-hidden="true"></i><span>Use current</span></button>
          </div>`;
        container.dataset.parentPath = data.parent || '';
        filterFolders();
      } catch (error) {
        container.innerHTML = `<div class="small text-danger">${escapeHtml(error.message || 'Folder list unavailable')}</div>`;
      }
    }

    container.addEventListener('input', (event) => {
      if (event.target.matches('[data-folder-picker-search]')) filterFolders();
    });
    container.addEventListener('change', (event) => {
      if (!event.target.matches('[data-folder-picker-select]')) return;
      const enabled = Boolean(selectedFolder());
      container.querySelector('[data-folder-picker-open]')?.toggleAttribute('disabled', !enabled);
      container.querySelector('[data-folder-picker-use-selected]')?.toggleAttribute('disabled', !enabled);
    });
    container.addEventListener('dblclick', (event) => {
      if (event.target.matches('[data-folder-picker-select]') && selectedFolder()) load(selectedFolder().path);
    });
    container.addEventListener('click', (event) => {
      if (event.target.closest('[data-folder-picker-up]')) load(container.dataset.parentPath);
      else if (event.target.closest('[data-folder-picker-open]') && selectedFolder()) load(selectedFolder().path);
      else if (event.target.closest('[data-folder-picker-use-selected]') && selectedFolder()) choose(selectedFolder().path);
      else if (event.target.closest('[data-folder-picker-use-current]')) choose(currentPath);
    });
    if (options.bindButton !== false) {
      button.addEventListener('click', () => {
        const open = panel.classList.contains('show');
        if (open) setOpen(panel, button, false);
        else load(input.value || currentPath);
      });
    }

    return {
      load,
      choose,
      isOpen: () => panel.classList.contains('show'),
      toggle: () => (panel.classList.contains('show') ? setOpen(panel, button, false) : load(input.value || currentPath)),
      get loaded() { return loaded; },
    };
  }

  window.vid2gifFolderPicker = {create};
}());
