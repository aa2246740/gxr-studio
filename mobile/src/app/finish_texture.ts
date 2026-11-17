// 预览用的确定性纸张纤维颗粒与手工搓边裁片轮廓（照桌面 `web/assets/finish_texture.js`）。
// 仅保留浏览器 Canvas2D 预览所需的绘制逻辑，导出走 Rust/WASM。

import type { FrameItem, FrameLayoutData } from '../lib/frame_spec';

type CarrierItem = Extract<FrameItem, { k: 'carrier' }>;

const grains = new Map<string, HTMLCanvasElement>();
const contours = new Map<string, [Array<[number, number]>, Array<[number, number]>]>();
const rgb = (c: readonly [number, number, number]): string => `rgb(${c[0]},${c[1]},${c[2]})`;

function random(seed: number): () => number {
  let state = seed >>> 0;
  return () => {
    state = (Math.imul(state, 1664525) + 1013904223) >>> 0;
    return (state + 0.5) / 4294967296;
  };
}

function normal(rng: () => number): number {
  return Math.sqrt(-2 * Math.log(rng())) * Math.cos(2 * Math.PI * rng());
}

function remember<V>(
  map: Map<string, V>,
  key: string,
  value: V,
  onEvict?: (evicted: V) => void,
): V {
  if (map.size >= 8) {
    const oldest = map.keys().next().value;
    if (oldest !== undefined) {
      const evicted = map.get(oldest);
      if (evicted !== undefined) onEvict?.(evicted);
      map.delete(oldest);
    }
  }
  map.set(key, value);
  return value;
}

export function clearTextureCaches(): void {
  for (const cv of grains.values()) {
    cv.width = 0;
    cv.height = 0;
  }
  grains.clear();
  contours.clear();
}

/** Port of `web/assets/finish_texture.js:paintGrain`. */
export function paintGrain(ctx: CanvasRenderingContext2D, frame: FrameLayoutData, s: number): void {
  const grain = frame.grain ?? 0;
  if (!grain) return;
  const w = Math.max(1, Math.ceil(frame.width * s));
  const h = Math.max(1, Math.ceil(frame.height * s));
  const key = `${w}x${h}|${s}|${grain}`;
  let cv = grains.get(key);
  if (!cv) {
    cv = document.createElement('canvas');
    cv.width = w;
    cv.height = h;
    const sc = cv.getContext('2d');
    if (!sc) return;
    const pixels = sc.createImageData(w, h);
    const rng = random(42);
    const sigma = 255 * grain * Math.min(1, s);
    const lowW = Math.max(4, Math.ceil(w / 6));
    const lowH = Math.max(4, Math.ceil(h / 6));
    const low = Float32Array.from({ length: lowW * lowH }, () => normal(rng) * 40 * grain * 0.8);
    for (let y = 0; y < h; y++) {
      for (let x = 0; x < w; x++) {
        const i = (y * w + x) * 4;
        const noise =
          normal(rng) * sigma + low[Math.min(lowH - 1, Math.floor(y / 6)) * lowW + Math.min(lowW - 1, Math.floor(x / 6))];
        for (let c = 0; c < 3; c++) pixels.data[i + c] = Math.floor(frame.bg[c] + noise);
        pixels.data[i + 3] = 255;
      }
    }
    sc.putImageData(pixels, 0, 0);
    remember(grains, key, cv, (oldCv) => {
      oldCv.width = 0;
      oldCv.height = 0;
    });
  }
  ctx.drawImage(cv, 0, 0);
}

function wobble(n: number, rng: () => number, wavelength: number, notches: number, notchW: number): Float64Array {
  const out = new Float64Array(n);
  [1, 0.55, 0.3].forEach((amp, k) => {
    const wl = wavelength / (1 + 1.7 * k);
    const phase = rng() * 6.28;
    for (let i = 0; i < n; i++) out[i] += (amp * Math.sin((2 * Math.PI * i) / wl + phase)) / 1.85;
  });
  for (let j = 0; j < notches; j++) {
    const pos = rng() * n;
    const wid = (0.5 + rng() * 0.8) * notchW;
    const amp = (rng() < 0.5 ? -1 : 1) * (0.7 + rng() * 0.7);
    for (let i = 0; i < n; i++) out[i] += amp * Math.exp(-(((i - pos) / wid) ** 2));
  }
  for (let i = 0; i < n; i++) out[i] = Math.max(-1.4, Math.min(1.4, out[i] + normal(rng) * 0.05));
  return out;
}

function carrier(item: CarrierItem): [Array<[number, number]>, Array<[number, number]>] {
  const { w, h, c, jit, seed } = item;
  const key = `${w}x${h}|${c}|${jit}|${seed}`;
  const cached = contours.get(key);
  if (cached) return cached;
  const short = Math.min(w, h);
  const rng = random(seed);
  const wob: Record<string, Float64Array> = {};
  const gaps: Record<string, number[]> = {};
  for (const side of ['t', 'r', 'b', 'l']) {
    const len = side === 't' || side === 'b' ? w : h;
    wob[side] = wobble(Math.floor(len / 2) + 4, rng, 0.35 * short, 4, 0.04 * short);
  }
  for (const side of ['t', 'r', 'b', 'l']) {
    const arr = wobble(wob[side].length, rng, 0.2 * short, 3, 0.03 * short);
    gaps[side] = Array.from(arr, (v) => Math.max(0, Math.min(3.2, v * 1.4 + 1.2)));
  }
  const outer: Array<[number, number]> = [];
  const inner: Array<[number, number]> = [];
  const sides: Array<[string, number]> = [
    ['t', w],
    ['r', h],
    ['b', w],
    ['l', h],
  ];
  for (const [side, length] of sides) {
    for (let i = 0; i <= Math.floor(length / 2); i++) {
      const d = c + 0.55 * jit * wob[side][i] + 0.45 * jit;
      const g = gaps[side][i];
      const a = 2 * i;
      if (side === 't') {
        outer.push([a, -d]);
        inner.push([a, -g]);
      } else if (side === 'r') {
        outer.push([w + d, a]);
        inner.push([w + g, a]);
      } else if (side === 'b') {
        outer.push([w - a, h + d]);
        inner.push([w - a, h + g]);
      } else {
        outer.push([-d, h - a]);
        inner.push([-g, h - a]);
      }
    }
  }
  return remember(contours, key, [outer, inner]);
}

/** Port of `web/assets/finish_texture.js:paintCarrier`. */
export function paintCarrier(ctx: CanvasRenderingContext2D, item: CarrierItem, s: number): void {
  const rings = carrier(item);
  rings.forEach((points, index) => {
    ctx.fillStyle = rgb(index ? item.paper : item.fill);
    ctx.beginPath();
    points.forEach(([x, y], i) => {
      const px = (item.x + x) * s;
      const py = (item.y + y) * s;
      if (i) ctx.lineTo(px, py);
      else ctx.moveTo(px, py);
    });
    ctx.closePath();
    ctx.fill();
  });
}
