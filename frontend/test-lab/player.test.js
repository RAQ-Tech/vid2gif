import test from 'node:test';
import assert from 'node:assert/strict';

import {applyFrameToContext, SynchronizedGifPlayer} from './player.js';
import {trackTimeForElapsed} from './logic.js';

function fakeContext() {
  const calls = [];
  return {
    calls,
    fillStyle: '#000000',
    globalCompositeOperation: 'source-over',
    clearRect(...args) {
      calls.push(['clearRect', ...args]);
    },
    createImageData(width, height) {
      calls.push(['createImageData', width, height]);
      return {data: new Uint8ClampedArray(width * height * 4)};
    },
    drawImage(...args) {
      calls.push(['drawImage', ...args]);
    },
    fillRect(...args) {
      calls.push(['fillRect', ...args]);
    },
    getImageData(...args) {
      calls.push(['getImageData', ...args]);
      return {backup: true};
    },
    putImageData(...args) {
      calls.push(['putImageData', ...args]);
    },
    restore() {
      calls.push(['restore']);
    },
    save() {
      calls.push(['save']);
    },
  };
}

function patchSurface() {
  return {
    canvas: {width: 20, height: 10},
    context: fakeContext(),
  };
}

function frame(disposalType = 1, transparentIndex = 255) {
  return {
    disposalType,
    transparentIndex,
    dims: {left: 2, top: 3, width: 1, height: 1},
    patch: new Uint8ClampedArray([1, 2, 3, 255]),
  };
}

test('transparent GIF patches are drawn with source-over instead of replacing the canvas', () => {
  const context = fakeContext();
  const patch = patchSurface();
  applyFrameToContext(context, patch, frame(), null, 20, 10, {index: 255, color: [0, 0, 0]});

  assert.equal(context.calls.some(call => call[0] === 'putImageData'), false);
  assert.equal(context.calls.some(call => call[0] === 'drawImage'), true);
  assert.equal(patch.context.calls.some(call => call[0] === 'putImageData'), true);
});

test('frame composition restores the logical background for disposal mode 2', () => {
  const context = fakeContext();
  applyFrameToContext(
    context,
    patchSurface(),
    frame(1),
    {
      disposalType: 2,
      dims: {left: 5, top: 6, width: 7, height: 8},
      transparentIndex: 10,
    },
    20,
    10,
    {index: 255, color: [10, 20, 30]},
  );

  assert.deepEqual(context.calls[0], ['clearRect', 5, 6, 7, 8]);
  assert.deepEqual(context.calls[1], ['fillRect', 5, 6, 7, 8]);
});

test('frame composition clears disposal mode 2 when the background is transparent', () => {
  const context = fakeContext();
  applyFrameToContext(
    context,
    patchSurface(),
    frame(1),
    {
      disposalType: 2,
      dims: {left: 5, top: 6, width: 7, height: 8},
      transparentIndex: 255,
    },
    20,
    10,
    {index: 255, color: [10, 20, 30]},
  );

  assert.deepEqual(context.calls[0], ['clearRect', 5, 6, 7, 8]);
  assert.equal(context.calls.some(call => call[0] === 'fillRect'), false);
});

test('frame composition restores mode 3 and captures mode 3 state before drawing', () => {
  const context = fakeContext();
  const restore = {old: true};
  const state = applyFrameToContext(
    context,
    patchSurface(),
    frame(3),
    {disposalType: 3, dims: {}, restore},
    20,
    10,
    null,
  );

  assert.deepEqual(context.calls[0], ['putImageData', restore, 0, 0]);
  assert.deepEqual(context.calls[1], ['getImageData', 0, 0, 20, 10]);
  assert.equal(state.disposalType, 3);
  assert.deepEqual(state.restore, {backup: true});
});

function fakeScheduler() {
  let nextId = 0;
  const pending = new Map();
  return {
    request(callback) {
      nextId += 1;
      pending.set(nextId, callback);
      return nextId;
    },
    cancel(id) {
      pending.delete(id);
    },
    run(timestamp) {
      const entry = pending.entries().next().value;
      if (!entry) return false;
      pending.delete(entry[0]);
      entry[1](timestamp);
      return true;
    },
    get size() {
      return pending.size;
    },
  };
}

function timingTrack(duration, rendered) {
  return {
    duration,
    renderTime(elapsed) {
      rendered.push(trackTimeForElapsed(elapsed, duration));
    },
    reset() {
      rendered.push('reset');
    },
  };
}

test('player preserves native timing and frame updates do not emit control state', () => {
  const scheduler = fakeScheduler();
  const states = [];
  const frames = [];
  const short = [];
  const long = [];
  const player = new SynchronizedGifPlayer({
    onStateChange: state => states.push(state),
    onFrame: state => frames.push(state),
    requestFrame: callback => scheduler.request(callback),
    cancelFrame: id => scheduler.cancel(id),
  });
  player.tracks.set('short', timingTrack(1000, short));
  player.tracks.set('long', timingTrack(2000, long));
  player.duration = 2000;

  player.play();
  const stateCountAfterPlay = states.length;
  scheduler.run(0);
  scheduler.run(1250);

  assert.equal(short.at(-1), 250);
  assert.equal(long.at(-1), 1250);
  assert.deepEqual(player.trackStates().map(track => track.time), [250, 1250]);
  assert.equal(states.length, stateCountAfterPlay);
  assert.equal(frames.at(-1).elapsedMs, 1250);

  player.pause();
  const pausedElapsed = player.elapsedMs;
  assert.equal(scheduler.size, 0);
  assert.equal(scheduler.run(1500), false);
  assert.equal(player.elapsedMs, pausedElapsed);
});

test('player seek, restart, and speed controls share one elapsed clock', () => {
  const scheduler = fakeScheduler();
  const short = [];
  const long = [];
  const player = new SynchronizedGifPlayer({
    requestFrame: callback => scheduler.request(callback),
    cancelFrame: id => scheduler.cancel(id),
  });
  player.tracks.set('short', timingTrack(2000, short));
  player.tracks.set('long', timingTrack(3000, long));
  player.duration = 3000;

  player.seek(0.5);
  assert.equal(player.elapsedMs, 1500);
  assert.equal(short.at(-1), 1500);
  assert.equal(long.at(-1), 1500);

  player.setSpeed(2);
  player.play();
  scheduler.run(1000);
  scheduler.run(1250);
  assert.equal(player.elapsedMs, 2000);
  assert.equal(short.at(-1), 0);
  assert.equal(long.at(-1), 2000);

  player.restart(false);
  assert.equal(player.elapsedMs, 0);
  assert.equal(player.playing, false);
  assert.equal(short.at(-1), 0);
  assert.equal(long.at(-1), 0);
});
