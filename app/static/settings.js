(function () {
  'use strict';

  const form = document.querySelector('.settings-form');
  const status = document.getElementById('settingsSaveState');
  const preset = document.getElementById('preview_height_preset');
  const custom = document.getElementById('preview_height_custom');
  if (!form || !status) return;

  let pending = 0;
  const failedKeys = new Set();
  let saveChain = Promise.resolve();
  const inputTimers = new Map();

  function setStatus(state, message) {
    const icons = {
      saving: 'bi-cloud-arrow-up',
      saved: 'bi-cloud-check',
      error: 'bi-exclamation-triangle'
    };
    status.className = `settings-save-state ${state === 'error' ? 'text-danger' : 'text-muted'}`;
    status.innerHTML = `<i class="bi ${icons[state] || icons.saved}" aria-hidden="true"></i><span>${message}</span>`;
  }

  function syncCustom(focus = false) {
    if (!preset || !custom) return;
    const show = preset.value === 'custom';
    custom.classList.toggle('d-none', !show);
    custom.disabled = !show;
    if (show && focus) custom.focus();
  }

  function settingPayload(element) {
    if (element === preset || element === custom) {
      if (preset.value === 'custom') {
        if (!custom.value.trim() || !custom.checkValidity()) return null;
        return {test_lab_preview_height: custom.value.trim()};
      }
      return {test_lab_preview_height: preset.value === 'original' ? null : preset.value};
    }
    if (!element.name || !element.checkValidity()) return null;
    return {[element.name]: element.type === 'checkbox' ? element.checked : element.value};
  }

  function save(payload) {
    if (!payload) {
      failedKeys.add('validation');
      setStatus('error', 'Not saved: check the highlighted value');
      return;
    }
    pending += 1;
    const keys = Object.keys(payload);
    setStatus('saving', 'Saving changes');
    saveChain = saveChain.then(async () => {
      try {
        const response = await fetch('/api/settings', {
          method: 'PATCH',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify(payload)
        });
        const data = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(data.error || 'Settings could not be saved');
        keys.forEach(key => failedKeys.delete(key));
        failedKeys.delete('validation');
      } catch (error) {
        keys.forEach(key => failedKeys.add(key));
        setStatus('error', `Not saved: ${error.message || 'request failed'}`);
      } finally {
        pending = Math.max(0, pending - 1);
        if (!pending && !failedKeys.size) setStatus('saved', 'All changes saved');
      }
    });
  }

  form.addEventListener('submit', event => event.preventDefault());
  form.addEventListener('change', event => {
    const element = event.target;
    if (!(element instanceof HTMLInputElement || element instanceof HTMLSelectElement || element instanceof HTMLTextAreaElement)) return;
    clearTimeout(inputTimers.get(element));
    if (element === preset) {
      syncCustom(true);
      if (preset.value === 'custom') return;
    }
    save(settingPayload(element));
  });
  form.addEventListener('input', event => {
    const element = event.target;
    if (!(element instanceof HTMLInputElement || element instanceof HTMLTextAreaElement) || element.type === 'checkbox') return;
    clearTimeout(inputTimers.get(element));
    inputTimers.set(element, setTimeout(() => save(settingPayload(element)), 500));
  });
  window.addEventListener('beforeunload', event => {
    if (!pending && !failedKeys.size) return;
    event.preventDefault();
    event.returnValue = '';
  });
  syncCustom();

  document.getElementById('embyTestButton')?.addEventListener('click', async () => {
    const button = document.getElementById('embyTestButton');
    const message = document.getElementById('embyTestMessage');
    const url = document.getElementById('emby_url');
    const key = document.getElementById('emby_api_key');
    button.disabled = true;
    message.textContent = 'Testing Emby connection…';
    try {
      const response = await fetch('/api/emby/test', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({
          emby_url: url?.value.trim() || '',
          ...(key?.value ? {emby_api_key: key.value} : {})
        })
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(data.error || 'Connection test failed');
      message.textContent = data.result?.message || 'Connection test completed.';
    } catch (error) {
      message.textContent = error.message || 'Connection test failed.';
    } finally {
      button.disabled = false;
    }
  });

  document.getElementById('embyPlaybackButton')?.addEventListener('click', async () => {
    const button = document.getElementById('embyPlaybackButton');
    const message = document.getElementById('embyTestMessage');
    button.disabled = true;
    message.textContent = 'Checking active Emby playback…';
    try {
      const response = await fetch('/api/emby/playback');
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(data.error || 'Playback check failed');
      const playback = data.playback || {};
      message.textContent = `${playback.message || 'Playback check completed.'} Active sessions: ${playback.active_session_count || 0}.`;
    } catch (error) {
      message.textContent = error.message || 'Playback check failed.';
    } finally {
      button.disabled = false;
    }
  });
})();
