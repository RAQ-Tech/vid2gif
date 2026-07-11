import assert from 'node:assert/strict';
import test from 'node:test';

import {compareTableValues, nextSort, normalizePreference} from './logic.js';

test('sort toggles only the active column', () => {
  assert.deepEqual(nextSort(null, 'name'), {column: 'name', direction: 'asc'});
  assert.deepEqual(nextSort({column: 'name', direction: 'asc'}, 'name'), {column: 'name', direction: 'desc'});
  assert.deepEqual(nextSort({column: 'name', direction: 'desc'}, 'size'), {column: 'size', direction: 'asc'});
});

test('numeric and natural text comparisons are stable', () => {
  assert.ok(compareTableValues('9', '10', 'number') < 0);
  assert.ok(compareTableValues('Copy 2', 'Copy 10') < 0);
});

test('preferences discard unsafe keys and invalid widths', () => {
  assert.deepEqual(normalizePreference({widths: {name: 240, '../bad': 200, size: 12}, sort: {column: 'name', direction: 'desc'}}), {
    widths: {name: 240},
    sort: {column: 'name', direction: 'desc'}
  });
});
