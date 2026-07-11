export function nextSort(current, column) {
  if (current?.column === column) {
    return {column, direction: current.direction === 'asc' ? 'desc' : 'asc'};
  }
  return {column, direction: 'asc'};
}

export function compareTableValues(left, right, type = 'text') {
  if (type === 'number') {
    const a = Number(left);
    const b = Number(right);
    if (Number.isFinite(a) && Number.isFinite(b)) return a - b;
  }
  return String(left ?? '').localeCompare(String(right ?? ''), undefined, {
    numeric: true,
    sensitivity: 'base'
  });
}

export function normalizePreference(value = {}) {
  const widths = {};
  Object.entries(value.widths || {}).forEach(([key, width]) => {
    const parsed = Math.round(Number(width));
    if (/^[a-z0-9][a-z0-9._-]{0,79}$/.test(key) && parsed >= 48 && parsed <= 4096) {
      widths[key] = parsed;
    }
  });
  const result = {widths};
  if (value.sort?.column && ['asc', 'desc'].includes(value.sort.direction)) {
    result.sort = {column: value.sort.column, direction: value.sort.direction};
  }
  return result;
}
