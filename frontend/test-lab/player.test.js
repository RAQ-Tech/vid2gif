import test from 'node:test';
import assert from 'node:assert/strict';

import {applyFrameToContext} from './player.js';

function fakeContext() {
  const calls = [];
  return {
    calls,
    clearRect(...args) {
      calls.push(['clearRect', ...args]);
    },
    createImageData(width, height) {
      calls.push(['createImageData', width, height]);
      return {data: new Uint8ClampedArray(width * height * 4)};
    },
    getImageData(...args) {
      calls.push(['getImageData', ...args]);
      return {backup: true};
    },
    putImageData(...args) {
      calls.push(['putImageData', ...args]);
    },
  };
}

function frame(disposalType = 1) {
  return {
    disposalType,
    dims: {left: 2, top: 3, width: 1, height: 1},
    patch: new Uint8ClampedArray([1, 2, 3, 255]),
  };
}

test('frame composition clears the previous frame for disposal mode 2', () => {
  const context = fakeContext();
  applyFrameToContext(
    context,
    frame(1),
    {disposalType: 2, dims: {left: 5, top: 6, width: 7, height: 8}},
    20,
    10,
  );

  assert.deepEqual(context.calls[0], ['clearRect', 5, 6, 7, 8]);
  assert.equal(context.calls.at(-1)[0], 'putImageData');
});

test('frame composition restores mode 3 and captures mode 3 state before drawing', () => {
  const context = fakeContext();
  const restore = {old: true};
  const state = applyFrameToContext(
    context,
    frame(3),
    {disposalType: 3, dims: {}, restore},
    20,
    10,
  );

  assert.deepEqual(context.calls[0], ['putImageData', restore, 0, 0]);
  assert.deepEqual(context.calls[1], ['getImageData', 0, 0, 20, 10]);
  assert.equal(state.disposalType, 3);
  assert.deepEqual(state.restore, {backup: true});
});
