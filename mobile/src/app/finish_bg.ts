// 边框背景绘制：实色、放射性暗角光晕、纸张纤维颗粒与编辑后小图的模糊背景（照桌面 `web/assets/finish_background.js`）。

import type { FrameLayoutData } from '../lib/frame_spec';
import { paintGrain } from './finish_texture';

export interface BackdropSource {
  image: HTMLCanvasElement;
  width: number;
  height: number;
}

interface CacheEntry {
  canvases: Map<string, HTMLCanvasElement>;
}

const rgb = (c: readonly [number, number, number]): string => `rgb(${c[0]},${c[1]},${c[2]})`;
const cache = new WeakMap<HTMLCanvasElement, CacheEntry>();

function entry(photo: HTMLCanvasElement): CacheEntry {
  let existing = cache.get(photo);
  if (!existing) {
    existing = { canvases: new Map() };
    cache.set(photo, existing);
  }
  return existing;
}

function makeCanvas(w: number, h: number): HTMLCanvasElement {
  const cv = document.createElement('canvas');
  cv.width = w;
  cv.height = h;
  return cv;
}

function keyFor(f: FrameLayoutData): string {
  const fx = f.bgfx;
  return `${f.width}x${f.height}|${fx?.sigma ?? 0}|${fx?.veil ?? 1}|${fx?.sat ?? 1}`;
}

/** Port of `web/assets/finish_background.js:gaussian`. */
function gaussian(data: Uint8ClampedArray, w: number, h: number, sigma: number): Uint8ClampedArray<ArrayBuffer> {
  const radius = Math.ceil(3 * sigma);
  const kernel: number[] = [];
  let sum = 0;
  for (let i = -radius; i <= radius; i++) {
    const k = Math.exp((-i * i) / (2 * sigma * sigma));
    kernel.push(k);
    sum += k;
  }
  for (let i = 0; i < kernel.length; i++) kernel[i] /= sum;
  const tmp = new Float32Array(w * h * 3);
  const out = new Uint8ClampedArray(new ArrayBuffer(w * h * 4));
  for (let y = 0; y < h; y++) {
    for (let x = 0; x < w; x++) {
      for (let c = 0; c < 3; c++) {
        let value = 0;
        for (let k = -radius; k <= radius; k++) {
          const sx = Math.max(0, Math.min(w - 1, x + k));
          value += data[(y * w + sx) * 4 + c] * kernel[k + radius];
        }
        tmp[(y * w + x) * 3 + c] = value;
      }
    }
  }
  for (let y = 0; y < h; y++) {
    for (let x = 0; x < w; x++) {
      const p = (y * w + x) * 4;
      for (let c = 0; c < 3; c++) {
        let value = 0;
        for (let k = -radius; k <= radius; k++) {
          const sy = Math.max(0, Math.min(h - 1, y + k));
          value += tmp[(sy * w + x) * 3 + c] * kernel[k + radius];
        }
        out[p + c] = value;
      }
      out[p + 3] = 255;
    }
  }
  return out;
}

/** Port of `web/assets/finish_background.js:blurBackdrop`. */
export function blurBackdrop(photo: HTMLCanvasElement, frame: FrameLayoutData): HTMLCanvasElement | null {
  const fx = frame.bgfx;
  if (!fx || fx.kind !== 'blur') return null;
  const kept = entry(photo).canvases;
  const key = keyFor(frame);
  const hit = kept.get(key);
  if (hit) return hit;

  const k = Math.min(1, 768 / Math.max(frame.width, frame.height));
  const w = Math.max(1, Math.round(frame.width * k));
  const h = Math.max(1, Math.round(frame.height * k));
  const scale = Math.max(frame.width / photo.width, frame.height / photo.height);
  const bw = Math.max(frame.width, Math.round(photo.width * scale));
  const bh = Math.max(frame.height, Math.round(photo.height * scale));
  const sw = Math.max(8, Math.floor((bw * k) / 8));
  const sh = Math.max(8, Math.floor((bh * k) / 8));

  const small = makeCanvas(sw, sh);
  const sc = small.getContext('2d');
  if (!sc) return null;
  sc.imageSmoothingQuality = 'low';
  sc.drawImage(photo, 0, 0, sw, sh);
  const sigma = Math.max(0.25, ((fx.sigma ?? 40) * k) / 8);
  const blurred = gaussian(sc.getImageData(0, 0, sw, sh).data, sw, sh, sigma);
  sc.putImageData(new ImageData(blurred, sw, sh), 0, 0);

  const cv = makeCanvas(w, h);
  const ctx = cv.getContext('2d');
  if (!ctx) return null;
  ctx.imageSmoothingQuality = 'high';
  ctx.drawImage(small, -Math.floor((bw - frame.width) / 2) * k, -Math.floor((bh - frame.height) / 2) * k, bw * k, bh * k);
  const pixels = ctx.getImageData(0, 0, w, h);
  const data = pixels.data;
  const sat = fx.sat ?? 1;
  const veil = fx.veil ?? 1;
  for (let i = 0; i < data.length; i += 4) {
    const luma = data[i] * 0.299 + data[i + 1] * 0.587 + data[i + 2] * 0.114;
    for (let c = 0; c < 3; c++) data[i + c] = Math.floor((luma + (data[i + c] - luma) * sat) * veil);
  }
  ctx.putImageData(pixels, 0, 0);
  if (kept.size >= 16) {
    const oldest = kept.keys().next().value;
    if (oldest !== undefined) kept.delete(oldest);
  }
  kept.set(key, cv);
  return cv;
}

/** 空闲时预热其它成片比例的模糊背景，只在边框页可见、不拖滑块时做（Port of `finish_background.js:primeBlur`）。 */
export function primeBlurFrames(
  photo: HTMLCanvasElement,
  frames: readonly FrameLayoutData[],
  active: () => boolean,
): void {
  if (typeof requestIdleCallback !== 'function' || !frames.length || !active()) return;
  const pending = frames.filter((f) => f.bgfx?.kind === 'blur' && !entry(photo).canvases.has(keyFor(f)));
  if (!pending.length) return;
  let index = 0;
  const step = (deadline: IdleDeadline): void => {
    if (!active()) return;
    if (deadline.timeRemaining() < 4) {
      requestIdleCallback(step);
      return;
    }
    const frame = pending[index++];
    if (frame) blurBackdrop(photo, frame);
    if (index < pending.length) requestIdleCallback(step);
  };
  requestIdleCallback(step);
}

/** Port of `web/assets/finish_background.js:paintBackground`. */
export function paintBackground(
  ctx: CanvasRenderingContext2D,
  frame: FrameLayoutData,
  s: number,
  photo: HTMLCanvasElement | null = null,
): void {
  ctx.save();
  ctx.fillStyle = rgb(frame.bg);
  ctx.fillRect(0, 0, frame.width * s, frame.height * s);
  if (frame.bgfx?.kind === 'blur' && photo) {
    const bd = blurBackdrop(photo, frame);
    if (bd) ctx.drawImage(bd, 0, 0, frame.width * s, frame.height * s);
  }
  if (frame.bgfx?.kind === 'glow' && frame.bgfx.falloff) {
    const falloff = frame.bgfx.falloff;
    ctx.translate((frame.width * s) / 2, (frame.height * s) / 2);
    ctx.scale((frame.width * s) / 2, (frame.height * s) / 2);
    const g = ctx.createRadialGradient(0, 0, 0, 0, 0, Math.SQRT2);
    for (let i = 0; i <= 32; i++) {
      const f = i / 32;
      const k = 1 - falloff * f * f;
      g.addColorStop(f, rgb([Math.floor(frame.bg[0] * k), Math.floor(frame.bg[1] * k), Math.floor(frame.bg[2] * k)]));
    }
    ctx.fillStyle = g;
    ctx.fillRect(-1, -1, 2, 2);
  }
  ctx.restore();
  if (frame.grain) paintGrain(ctx, frame, s);
}
