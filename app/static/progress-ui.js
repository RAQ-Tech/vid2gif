(function () {
  function clamp(value) {
    return Math.max(0, Math.min(100, Math.round(Number(value) || 0)));
  }

  function apply(bar, model) {
    if (!bar) return;
    const data = typeof model === 'object' && model !== null
      ? model
      : {progress_percent: model};
    const indeterminate = Boolean(data.progress_indeterminate);
    const percent = clamp(data.progress_percent);
    const parent = bar.closest('.progress');
    bar.classList.toggle('progress-bar-striped', indeterminate);
    bar.classList.toggle('progress-bar-animated', indeterminate);
    bar.classList.toggle('progress-indeterminate', indeterminate);
    bar.style.width = indeterminate ? '100%' : `${percent}%`;
    bar.textContent = '';
    if (parent) {
      parent.setAttribute('aria-busy', indeterminate ? 'true' : 'false');
      if (indeterminate) parent.removeAttribute('aria-valuenow');
      else parent.setAttribute('aria-valuenow', String(percent));
    }
  }

  function valueLabel(model) {
    if (model?.progress_indeterminate) return 'In progress';
    return `${clamp(model?.progress_percent)}%`;
  }

  function etaLabel(seconds, confidence) {
    if (seconds !== null && seconds !== undefined) return `About ${window.vid2gifProgress.formatDuration(seconds)} remaining`;
    if (confidence === 'calibrating' || confidence === 'learning') return 'Learning timing from this run';
    return 'Remaining time unavailable';
  }

  function formatDuration(seconds) {
    const total = Math.max(0, Math.round(Number(seconds) || 0));
    if (total < 60) return `${total}s`;
    const minutes = Math.floor(total / 60);
    if (minutes < 60) return `${minutes}m ${String(total % 60).padStart(2, '0')}s`;
    return `${Math.floor(minutes / 60)}h ${String(minutes % 60).padStart(2, '0')}m`;
  }

  window.vid2gifProgress = {apply, clamp, valueLabel, etaLabel, formatDuration};
}());
