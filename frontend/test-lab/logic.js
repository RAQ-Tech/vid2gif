export const MAX_VARIANTS = 4;
export const MAX_COMPARISON_ITEMS = 4;
export const COMPARISON_STORAGE_KEY = 'testlab_comparison_ids';
export const LEGACY_COMPARISON_STORAGE_KEY = 'testlab_slots';

const HEIGHT_PRESETS = ['240', '360', '480', '720', '1080'];
const FPS_PRESETS = ['10', '12', '15', '20', '24', '30', 'original'];
const CLIP_PRESETS = ['1', '2', '3', '4', '5'];

function splitPreset(value, presets) {
  const text = String(value ?? '');
  return presets.includes(text)
    ? {preset: text, custom: ''}
    : {preset: 'custom', custom: text};
}

export function createId(prefix = 'item') {
  if (globalThis.crypto && typeof globalThis.crypto.randomUUID === 'function') {
    return `${prefix}-${globalThis.crypto.randomUUID()}`;
  }
  return `${prefix}-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

export function normalizeComparisonIds(value, validIds = null) {
  const allowed = validIds ? new Set(validIds) : null;
  const seen = new Set();
  const normalized = [];
  (Array.isArray(value) ? value : []).forEach(raw => {
    const id = String(raw || '').trim();
    if (!id || seen.has(id) || (allowed && !allowed.has(id))) return;
    seen.add(id);
    normalized.push(id);
  });
  return normalized.slice(0, MAX_COMPARISON_ITEMS);
}

export function loadComparisonIds(storage) {
  const parse = key => {
    try {
      return JSON.parse(storage.getItem(key) || 'null');
    } catch (error) {
      return null;
    }
  };
  const current = parse(COMPARISON_STORAGE_KEY);
  if (Array.isArray(current)) return normalizeComparisonIds(current);
  return normalizeComparisonIds(parse(LEGACY_COMPARISON_STORAGE_KEY));
}

export function makeDefaultVariant(defaults = {}, index = 1, id = createId('variant')) {
  const height = splitPreset(defaults.height ?? 480, HEIGHT_PRESETS);
  const fps = splitPreset(defaults.fps ?? 15, FPS_PRESETS);
  const clip = splitPreset(defaults.clip_len ?? 2, CLIP_PRESETS);
  return {
    id,
    name: `Variant ${index}`,
    height_preset: height.preset,
    height_custom: height.custom,
    fps_preset: fps.preset,
    fps_custom: fps.custom,
    clip_len_preset: clip.preset,
    clip_len_custom: clip.custom,
    percent_points: String(defaults.percent_points ?? '10,20,30,40,50,60,70,80,90'),
    abs_early: String(defaults.abs_early ?? 15),
    abs_late_from_end: String(defaults.abs_late_from_end ?? 10),
    start_buffer: String(defaults.start_buffer ?? 5),
    end_buffer: String(defaults.end_buffer ?? 5),
    loop_forever: defaults.loop_forever !== false,
    smooth: Boolean(defaults.smooth),
    optimize: defaults.optimize !== false,
  };
}

export function normalizeVariant(raw, defaults = {}, index = 1) {
  const fallback = makeDefaultVariant(defaults, index);
  if (!raw || typeof raw !== 'object') return fallback;
  const normalized = {...fallback};
  Object.keys(fallback).forEach(key => {
    if (raw[key] !== undefined && raw[key] !== null) normalized[key] = raw[key];
  });
  normalized.id = String(normalized.id || createId('variant'));
  normalized.name = String(normalized.name || `Variant ${index}`).slice(0, 80);
  ['loop_forever', 'smooth', 'optimize'].forEach(key => {
    normalized[key] = Boolean(normalized[key]);
  });
  return normalized;
}

export function variantSummary(variant) {
  const effective = (preset, custom) => preset === 'custom' ? custom : preset;
  const height = effective(variant.height_preset, variant.height_custom) || '?';
  const fps = effective(variant.fps_preset, variant.fps_custom) || '?';
  const clip = effective(variant.clip_len_preset, variant.clip_len_custom) || '?';
  return `${height}px · ${fps === 'original' ? 'source' : `${fps}fps`} · ${clip}s`;
}

export function variantRequest(variant) {
  return {
    name: String(variant.name || 'Variant').trim() || 'Variant',
    settings: {
      height_preset: variant.height_preset || '',
      height_custom: variant.height_custom || '',
      fps_preset: variant.fps_preset || '',
      fps_custom: variant.fps_custom || '',
      clip_len_preset: variant.clip_len_preset || '',
      clip_len_custom: variant.clip_len_custom || '',
      percent_points: variant.percent_points || '',
      abs_early: variant.abs_early || '',
      abs_late_from_end: variant.abs_late_from_end || '',
      start_buffer: variant.start_buffer || '',
      end_buffer: variant.end_buffer || '',
      loop_forever: variant.loop_forever ? 'on' : 'off',
      smooth: variant.smooth ? 'on' : 'off',
      optimize: variant.optimize ? 'on' : 'off',
    },
  };
}

export function reorderIds(ids, fromIndex, toIndex) {
  const copy = [...ids];
  if (
    fromIndex < 0 || fromIndex >= copy.length ||
    toIndex < 0 || toIndex >= copy.length ||
    fromIndex === toIndex
  ) return copy;
  const [item] = copy.splice(fromIndex, 1);
  copy.splice(toIndex, 0, item);
  return copy;
}

export function successfulFileIds(run) {
  return normalizeComparisonIds(
    (run?.variants || [])
      .filter(variant => variant.status === 'success' && variant.file_id)
      .map(variant => variant.file_id),
  );
}

export function comparisonStructureSignature(files) {
  return JSON.stringify((files || []).map(file => [
    file.id,
    file.display_url,
    file.preview_status === 'failed' ? 'failed' : (file.display_url ? 'playable' : 'waiting'),
  ]));
}

export function comparisonPlayerSignature(files) {
  return JSON.stringify((files || []).map(file => [file.id, file.display_url]));
}

export function buildFrameTimeline(frames) {
  let total = 0;
  const starts = (frames || []).map(frame => {
    const start = total;
    total += Math.max(10, Number(frame.delay) || 100);
    return start;
  });
  return {starts, duration: Math.max(total, 10)};
}

export function frameIndexForPhase(timeline, phase) {
  if (!timeline?.starts?.length) return -1;
  if (Number(phase) >= 1) return timeline.starts.length - 1;
  const normalized = Math.max(0, Number(phase) || 0);
  return frameIndexForTime(timeline, normalized * timeline.duration);
}

export function frameIndexForTime(timeline, timeMs) {
  if (!timeline?.starts?.length) return -1;
  const target = Math.max(0, Math.min(
    timeline.duration - Number.EPSILON,
    Number(timeMs) || 0,
  ));
  let low = 0;
  let high = timeline.starts.length - 1;
  while (low <= high) {
    const middle = Math.floor((low + high) / 2);
    if (timeline.starts[middle] <= target) low = middle + 1;
    else high = middle - 1;
  }
  return Math.max(0, high);
}

export function trackTimeForElapsed(elapsedMs, durationMs) {
  const duration = Math.max(10, Number(durationMs) || 0);
  const elapsed = Number(elapsedMs) || 0;
  return ((elapsed % duration) + duration) % duration;
}
