import {decompressFrames, parseGIF} from 'gifuct-js';

import {buildFrameTimeline, frameIndexForPhase} from './logic.js';

export function applyFrameToContext(context, frame, previousState, canvasWidth, canvasHeight) {
  if (previousState?.disposalType === 2) {
    const dims = previousState.dims;
    context.clearRect(dims.left, dims.top, dims.width, dims.height);
  } else if (previousState?.disposalType === 3 && previousState.restore) {
    context.putImageData(previousState.restore, 0, 0);
  }

  const restore = frame.disposalType === 3
    ? context.getImageData(0, 0, canvasWidth, canvasHeight)
    : null;
  const imageData = context.createImageData(frame.dims.width, frame.dims.height);
  imageData.data.set(frame.patch);
  context.putImageData(imageData, frame.dims.left, frame.dims.top);
  return {
    dims: {...frame.dims},
    disposalType: Number(frame.disposalType) || 0,
    restore,
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
    this.renderedIndex = -1;
    this.previousState = null;
    this.lastPhase = -1;
  }

  get duration() {
    return this.timeline.duration;
  }

  reset() {
    this.context.clearRect(0, 0, this.canvas.width, this.canvas.height);
    this.renderedIndex = -1;
    this.previousState = null;
    this.lastPhase = -1;
  }

  renderPhase(phase) {
    const targetIndex = frameIndexForPhase(this.timeline, phase);
    if (targetIndex < 0) return;
    if (phase < this.lastPhase || targetIndex < this.renderedIndex) this.reset();
    for (let index = this.renderedIndex + 1; index <= targetIndex; index += 1) {
      this.previousState = applyFrameToContext(
        this.context,
        this.frames[index],
        this.previousState,
        this.canvas.width,
        this.canvas.height,
      );
      this.renderedIndex = index;
    }
    this.lastPhase = phase;
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
  constructor({onStateChange, onTileState} = {}) {
    this.onStateChange = onStateChange || (() => {});
    this.onTileState = onTileState || (() => {});
    this.tracks = new Map();
    this.abortController = null;
    this.loadGeneration = 0;
    this.phase = 0;
    this.speed = 1;
    this.playing = false;
    this.duration = 1000;
    this.animationFrame = null;
    this.lastTimestamp = null;
    this.tick = this.tick.bind(this);
  }

  emitState() {
    this.onStateChange({
      phase: this.phase,
      speed: this.speed,
      playing: this.playing,
      readyCount: this.tracks.size,
    });
  }

  clear() {
    this.pause();
    this.loadGeneration += 1;
    this.abortController?.abort();
    this.abortController = null;
    this.tracks.clear();
    this.phase = 0;
    this.duration = 1000;
    this.emitState();
  }

  async load(files, canvasById, {autoplay = true} = {}) {
    this.pause();
    this.loadGeneration += 1;
    const generation = this.loadGeneration;
    this.abortController?.abort();
    this.abortController = new AbortController();
    this.tracks.clear();
    this.phase = 0;

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
    if (autoplay && this.tracks.size) this.play();
    else this.emitState();
  }

  render() {
    this.tracks.forEach(track => track.renderPhase(this.phase));
    this.emitState();
  }

  play() {
    if (this.playing || !this.tracks.size) return;
    this.playing = true;
    this.lastTimestamp = null;
    this.animationFrame = requestAnimationFrame(this.tick);
    this.emitState();
  }

  pause() {
    if (this.animationFrame !== null) cancelAnimationFrame(this.animationFrame);
    this.animationFrame = null;
    this.lastTimestamp = null;
    const changed = this.playing;
    this.playing = false;
    if (changed) this.emitState();
  }

  restart(autoplay = true) {
    this.pause();
    this.phase = 0;
    this.tracks.forEach(track => track.reset());
    this.render();
    if (autoplay) this.play();
  }

  seek(phase) {
    const shouldResume = this.playing;
    this.pause();
    this.phase = Math.max(0, Math.min(1, Number(phase) || 0));
    this.render();
    if (shouldResume) this.play();
  }

  setSpeed(speed) {
    this.speed = [0.5, 1, 2].includes(Number(speed)) ? Number(speed) : 1;
    this.emitState();
  }

  tick(timestamp) {
    if (!this.playing) return;
    if (this.lastTimestamp !== null) {
      const elapsed = Math.min(250, Math.max(0, timestamp - this.lastTimestamp));
      this.phase = (this.phase + (elapsed * this.speed / this.duration)) % 1;
      this.tracks.forEach(track => track.renderPhase(this.phase));
      this.emitState();
    }
    this.lastTimestamp = timestamp;
    this.animationFrame = requestAnimationFrame(this.tick);
  }
}
