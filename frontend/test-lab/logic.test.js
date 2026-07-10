import test from 'node:test';
import assert from 'node:assert/strict';

import {
  buildFrameTimeline,
  frameIndexForPhase,
  loadComparisonIds,
  makeDefaultVariant,
  normalizeComparisonIds,
  reorderIds,
  successfulFileIds,
  variantRequest,
  variantSummary,
} from './logic.js';

test('comparison migration compacts legacy slots, removes duplicates, and caps at four', () => {
  const storage = {
    getItem(key) {
      if (key === 'testlab_slots') return JSON.stringify(['a', '', 'b', 'a', 'c', 'd', 'e']);
      return null;
    },
  };

  assert.deepEqual(loadComparisonIds(storage), ['a', 'b', 'c', 'd']);
  assert.deepEqual(normalizeComparisonIds(['a', 'missing', 'b'], ['a', 'b']), ['a', 'b']);
});

test('variant defaults and requests preserve one editable configuration', () => {
  const variant = makeDefaultVariant({height: 720, fps: 24, clip_len: 2}, 1, 'variant-1');

  assert.equal(variantSummary(variant), '720px · 24fps · 2s');
  assert.equal(variantRequest(variant).settings.height_preset, '720');
  assert.equal(variantRequest(variant).settings.loop_forever, 'on');
});

test('shared phase maps different frame timings to their own frame indexes', () => {
  const slow = buildFrameTimeline([{delay: 100}, {delay: 300}, {delay: 100}]);
  const even = buildFrameTimeline([{delay: 100}, {delay: 100}, {delay: 100}, {delay: 100}]);

  assert.equal(frameIndexForPhase(slow, 0), 0);
  assert.equal(frameIndexForPhase(slow, 0.5), 1);
  assert.equal(frameIndexForPhase(slow, 1), 2);
  assert.equal(frameIndexForPhase(even, 0.5), 2);
});

test('comparison reordering and generated output selection remain deterministic', () => {
  assert.deepEqual(reorderIds(['a', 'b', 'c'], 0, 2), ['b', 'c', 'a']);
  assert.deepEqual(successfulFileIds({variants: [
    {status: 'success', file_id: 'run/a.gif'},
    {status: 'failed', file_id: 'run/b.gif'},
    {status: 'success', file_id: 'run/a.gif'},
    {status: 'success', file_id: 'old/c.gif'},
  ]}), ['run/a.gif', 'old/c.gif']);
});
