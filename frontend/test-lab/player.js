import {decompressFrames, parseGIF} from 'gifuct-js';

import {buildFrameTimeline, frameIndexForTime, trackTimeForElapsed} from './logic.js';

function gifBackground(parsedGif) {
  const index = Number(parsedGif?.lsd?.backgroundColorIndex);
  const color = parsedGif?.gct?.[index];
  return {
    index: Number.isInteger(index) ? index : null,
    color: Array.isArray(color) && color.length >= 3 ? color.slice(0, 3) : null,
  };
}

function restoreBackground(context, dims, background, transparentIndex) {
  context.clearRect(dims.left, dims.top, dims.width, dims.height);
  if (!background?.color || Number(transparentIndex) === background.index) return;
  const previousFill = context.fillStyle;
  context.fillStyle = `rgb(${background.color.join(',')})`;
  context.fillRect(dims.left, dims.top, dims.width, dims.height);
  context.fillStyle = previousFill;
}

export function applyFrameToContext(
  context,
  patchSurface,
  frame,
  previousState,
  canvasWidth,
  canvasHeight,
  background,
) {
  if (previousState?.disposalType === 2) {
    restoreBackground(context, previousState.dims, background, previousState.transparentIndex);
  } else if (previousState?.disposalType === 3 && previousState.restore) {
    context.putImageData(previousState.restore, 0, 0);
  }

  const restore = frame.disposalType === 3
    ? context.getImageData(0, 0, canvasWidth, canvasHeight)
    : null;
  const patchContext = patchSurface.context;
  const {width, height, left, top} = frame.dims;
  patchContext.clearRect(0, 0, patchSurface.canvas.width, patchSurface.canvas.height);
  const imageData = patchContext.createImageData(width, height);
  imageData.data.set(frame.patch);
  patchContext.putImageData(imageData, 0, 0);

  context.save();
  context.globalCompositeOperation = 'source-over';
  context.drawImage(patchSurface.canvas, 0, 0, width, height, left, top, width, height);
  context.restore();
  return {
    dims: {...frame.dims},
    disposalType: Number(frame.disposalType) || 0,
    restore,
    transparentIndex: frame.transparentIndex,
  };
}

export class GifCanvasTrack {
  constructor(canvas, parsedGif, frames) {
    this.canvas = canvas;
    this.context = canvas.getContext('2d', {alpha: true});
    this.frames = frames;
    this.timeline = buildFrameTimeline(frames);
    this.canvas.width = Number(parsedGif?.lsd?.width) || frames[0]?.dims?.width || 1;
    this.canvas.height = Number(parsedGif?.lsd?.height) || frames[0]?.dims?.height || 1;
    const documentRef = canvas.ownerDocument || document;
    const patchCanvas = documentRef.createElement('canvas');
    patchCanvas.width = this.canvas.width;
    patchCanvas.height = this.canvas.height;
    this.patchSurface = {
      canvas: patchCanvas,
      context: patchCanvas.getContext('2d', {alpha: true}),
    };
    this.background = gifBackground(parsedGif);
    this.renderedIndex = -1;
    this.previousState = null;
    this.lastTime = -1;
  }

  get duration() {
    return this.timeline.duration;
  }

  reset() {
    restoreBackground(
      this.context,
      {left: 0, top: 0, width: this.canvas.width, height: this.canvas.height},
      this.background,
      this.frames[0]?.transparentIndex,
    );
    this.renderedIndex = -1;
    this.previousState = null;
    this.lastTime = -1;
  }

  renderTime(elapsedMs) {
    const localTime = trackTimeForElapsed(elapsedMs, this.duration);
    const targetIndex = frameIndexForTime(this.timeline, localTime);
    if (targetIndex < 0) return;
    if (localTime < this.lastTime || targetIndex < this.renderedIndex) this.reset();
    for (let index = this.renderedIndex + 1; index <= targetIndex; index += 1) {
      this.previousState = applyFrameToContext(
        this.context,
        this.patchSurface,
        this.frames[index],
        this.previousState,
        this.canvas.width,
        this.canvas.height,
        this.background,
      );
      this.renderedIndex = index;
    }
    this.lastTime = localTime;
  }
}

async function decodeTrack(file, canvas, signal) {
  const response = await fetch(file.display_url, {signal, cache: 'no-store'});
  if (!response.ok) throw new Error(`Preview request failed (${response.status})`);
  const parsed = parseGIF(await response.arrayBuffer());
  const frames = decompressFrames(parsed, true);
  if (!frames.length) throw new Error('No GIF frames were decoded');
  return new GifCanvasTrack(canvas, parsed, frames);
}

export class SynchronizedGifPlayer {
  constructor({onStateChange, onFrame, onTileState, requestFrame, cancelFrame} = {}) {
    this.onStateChange = onStateChange || (() => {});
    this.onFrame = onFrame || (() => {});
    this.onTileState = onTileState || (() => {});
    this.requestFrame = requestFrame || (callback => requestAnimationFrame(callback));
    this.cancelFrame = cancelFrame || (handle => cancelAnimationFrame(handle));
    this.tracks = new Map();
    this.abortController = null;
    this.loadGeneration = 0;
    this.elapsedMs = 0;
    this.speed = 1;
    this.playing = false;
    this.duration = 1000;
    this.animationFrame = null;
    this.lastTimestamp = null;
    this.tick = this.tick.bind(this);
  }

  get phase() {
    return trackTimeForElapsed(this.elapsedMs, this.duration) / this.duration;
  }

  playerState() {
    return {
      phase: this.phase,
      elapsedMs: this.elapsedMs,
      duration: this.duration,
      speed: this.speed,
      playing: this.playing,
      readyCount: this.tracks.size,
    };
  }

  trackStates() {
    return Array.from(this.tracks, ([id, track]) => ({
      id,
      duration: track.duration,
      time: trackTimeForElapsed(this.elapsedMs, track.duration),
    }));
  }

  emitState() {
    this.onStateChange(this.playerState());
  }

  emitFrame() {
    this.onFrame(this.playerState());
  }

  stopClock() {
    if (this.animationFrame !== null) this.cancelFrame(this.animationFrame);
    this.animationFrame = null;
    this.lastTimestamp = null;
  }

  clear() {
    this.pause();
    this.loadGeneration += 1;
    this.abortController?.abort();
    this.abortController = null;
    this.tracks.clear();
    this.elapsedMs = 0;
    this.duration = 1000;
    this.emitFrame();
    this.emitState();
  }

  async load(files, canvasById, {autoplay = true} = {}) {
    this.pause();
    this.loadGeneration += 1;
    const generation = this.loadGeneration;
    this.abortController?.abort();
    this.abortController = new AbortController();
    this.tracks.clear();
    this.elapsedMs = 0;
    this.emitFrame();
    this.emitState();

    const results = await Promise.allSettled(files.map(async file => {
      const canvas = canvasById.get(file.id);
      if (!canvas) throw new Error('Player canvas is unavailable');
      this.onTileState(file.id, 'loading');
      const track = await decodeTrack(file, canvas, this.abortController.signal);
      this.onTileState(file.id, 'ready');
      return {id: file.id, track};
    }));
    if (generation !== this.loadGeneration) return;

    results.forEach((result, index) => {
      const file = files[index];
      if (result.status === 'fulfilled') {
        this.tracks.set(result.value.id, result.value.track);
      } else if (result.reason?.name !== 'AbortError') {
        this.onTileState(file.id, 'error', result.reason?.message || 'GIF decode failed');
      }
    });
    this.duration = Math.max(10, ...Array.from(this.tracks.values(), track => track.duration));
    this.render();
    this.emitState();
    if (autoplay && this.tracks.size) this.play();
  }

  render() {
    this.tracks.forEach(track => track.renderTime(this.elapsedMs));
    this.emitFrame();
  }

  play() {
    if (this.playing || !this.tracks.size) return;
    this.playing = true;
    this.lastTimestamp = null;
    this.animationFrame = this.requestFrame(this.tick);
    this.emitState();
  }

  pause() {
    this.stopClock();
    const changed = this.playing;
    this.playing = false;
    if (changed) this.emitState();
  }

  restart(autoplay = true) {
    const wasPlaying = this.playing;
    this.stopClock();
    this.elapsedMs = 0;
    this.tracks.forEach(track => track.reset());
    this.render();
    this.playing = Boolean(autoplay && this.tracks.size);
    if (this.playing) this.animationFrame = this.requestFrame(this.tick);
    if (this.playing !== wasPlaying) this.emitState();
  }

  seek(phase) {
    const shouldResume = this.playing;
    this.stopClock();
    const normalized = Math.max(0, Math.min(1, Number(phase) || 0));
    this.elapsedMs = Math.min(this.duration - 0.001, normalized * this.duration);
    this.render();
    if (shouldResume) this.animationFrame = this.requestFrame(this.tick);
  }

  setSpeed(speed) {
    this.speed = [0.5, 1, 2].includes(Number(speed)) ? Number(speed) : 1;
    this.emitState();
  }

  tick(timestamp) {
    if (!this.playing) return;
    if (this.lastTimestamp !== null) {
      const elapsed = Math.max(0, timestamp - this.lastTimestamp);
      this.elapsedMs += elapsed * this.speed;
      this.render();
    }
    this.lastTimestamp = timestamp;
    this.animationFrame = this.requestFrame(this.tick);
  }
}
