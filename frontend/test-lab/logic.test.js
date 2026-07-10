import test from 'node:test';
import assert from 'node:assert/strict';

import {
  buildFrameTimeline,
  comparisonPlayerSignature,
  comparisonStructureSignature,
  frameIndexForPhase,
  frameIndexForTime,
  loadComparisonIds,
  makeDefaultVariant,
  normalizeComparisonIds,
  reorderIds,
  successfulFileIds,
  trackTimeForElapsed,
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
  assert.equal(frameIndexForTime(slow, 399), 1);
  assert.equal(frameIndexForTime(slow, 400), 2);
});

test('shared elapsed time preserves native timing for unequal durations', () => {
  assert.equal(trackTimeForElapsed(1250, 1000), 250);
  assert.equal(trackTimeForElapsed(1250, 2000), 1250);
  assert.equal(trackTimeForElapsed(3000, 2000), 1000);
  assert.equal(trackTimeForElapsed(3000, 3000), 0);
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

test('metadata-only inventory changes do not invalidate comparison canvases', () => {
  const before = [{
    id: 'run/a.gif',
    display_url: '/preview/a.gif',
    preview_status: 'ready',
    name: 'Before',
    size_label: '1 MB',
  }];
  const afterMetadata = [{
    ...before[0],
    name: 'After',
    size_label: '900 KB',
    preview_label: 'Scaled preview · 720px',
  }];
  const afterUrl = [{...afterMetadata[0], display_url: '/preview/a-v2.gif'}];

  assert.equal(comparisonStructureSignature(before), comparisonStructureSignature(afterMetadata));
  assert.equal(comparisonPlayerSignature(before), comparisonPlayerSignature(afterMetadata));
  assert.notEqual(comparisonStructureSignature(before), comparisonStructureSignature(afterUrl));
  assert.notEqual(comparisonPlayerSignature(before), comparisonPlayerSignature(afterUrl));
});
