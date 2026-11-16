// 裁剪和拉直的状态、预设比例、参考线偏好与纯几何计算。不碰 DOM 或 WASM，Node 单元测试直接跑。
// 数学照桌面 `web/assets/geom.js` 和 `web/assets/crop.js`；提交规则永远由 Worker 里的 WASM `geom_crop_write` / `geom_geometry_write` 定。

import type { Frame } from './photo_size';

export type Orientation = 'landscape' | 'portrait';
export type Handle = 'tl' | 'tr' | 'bl' | 'br' | 't' | 'b' | 'l' | 'r';
export type GuideId = 'off' | 'thirds' | 'grid' | 'golden' | 'spiral' | 'diagonal';

export interface CropState {
  preset: string;
  rect: [number, number, number, number];
  angle_deg: number;
}

export interface GeometryState {
  straighten: number;
}

export interface Rect {
  x: number;
  y: number;
  w: number;
  h: number;
}

export interface CanvasDims {
  w: number;
  h: number;
}

export interface CropPresetSpec {
  id: string;
  label: string;
  orient?: Orientation;
}

export const MAX_ANGLE = 45;
const EPS = 1e-9;
const MIN_SIDE = 0.02;

/** 03e 比例条（顺序与设计稿一致；id 和文字照桌面 `crop.js` PRESET_GROUPS）。 */
export const PRESETS: ReadonlyArray<CropPresetSpec> = [
  { id: 'original', label: '原始' },
  { id: 'free', label: '自由' },
  { id: '1:1', label: '1:1' },
  { id: '4:5', label: '4:5', orient: 'portrait' },
  { id: '3:2', label: '3:2' },
  { id: '16:9', label: '16:9' },
  { id: 'xpan-65-24', label: 'XPan 65:24' },
  { id: '4:3', label: '小红书 3:4', orient: 'portrait' },
];

/** 能横竖切换的比例（`crop.js` ORIENTABLE，去掉了 03e 不放的 `1.91:1` 也没关系，集合里保留同步）。 */
export const ORIENTABLE = new Set(['3:2', '4:3', '16:9', '4:5', '1.91:1', 'xpan-65-24']);

/** 桌面 `crop.js` GUIDES。 */
export const GUIDES: ReadonlyArray<readonly [GuideId, string]> = [
  ['off', '关'],
  ['thirds', '三等分'],
  ['grid', '网格'],
  ['golden', '黄金比例'],
  ['spiral', '黄金螺旋'],
  ['diagonal', '对角线'],
];

/** 和桌面同一个键：参考线偏好记在 localStorage。 */
export const GUIDE_PREFS_KEY = 'gxr-studio.crop-guides';

export interface GuidePrefs {
  guide: GuideId;
  spiral: number;
  fade: boolean;
}

interface Store {
  getItem(key: string): string | null;
  setItem(key: string, value: string): void;
}

function store(): Store | null {
  try {
    return (globalThis as { localStorage?: Store }).localStorage ?? null;
  } catch {
    return null;
  }
}

export function loadGuidePrefs(from: Store | null = store()): GuidePrefs {
  let saved: Record<string, unknown> = {};
  try {
    const raw = JSON.parse(from?.getItem(GUIDE_PREFS_KEY) || '{}');
    if (raw && typeof raw === 'object') saved = raw as Record<string, unknown>;
  } catch {
    saved = {};
  }
  const guide = GUIDES.some(([id]) => id === saved.guide) ? (saved.guide as GuideId) : 'thirds';
  const spiral = typeof saved.spiral === 'number' && Number.isInteger(saved.spiral) && saved.spiral >= 0 && saved.spiral < 4 ? saved.spiral : 0;
  const fade = typeof saved.fade === 'boolean' ? saved.fade : true;
  return { guide, spiral, fade };
}

export function saveGuidePrefs(patch: Partial<GuidePrefs>, to: Store | null = store(), from: Store | null = to): GuidePrefs {
  const next = { ...loadGuidePrefs(from), ...patch };
  try {
    to?.setItem(GUIDE_PREFS_KEY, JSON.stringify(next));
  } catch {
    /* 写不进去就算了 */
  }
  return next;
}

export function guideLabel(id: GuideId): string {
  return (GUIDES.find(([gid]) => gid === id) ?? GUIDES[1])[1];
}

export function clamp(v: number, lo: number, hi: number): number {
  return Math.min(hi, Math.max(lo, v));
}

export function zeroGeometry(): GeometryState {
  return { straighten: 0 };
}

export function cloneCrop(crop: CropState | null): CropState | null {
  return crop ? { preset: crop.preset, rect: [crop.rect[0], crop.rect[1], crop.rect[2], crop.rect[3]], angle_deg: crop.angle_deg } : null;
}

export function sameCropState(a: CropState | null, b: CropState | null): boolean {
  if (a === b) return true;
  if (!a || !b) return false;
  return (
    a.preset === b.preset &&
    a.angle_deg === b.angle_deg &&
    a.rect[0] === b.rect[0] &&
    a.rect[1] === b.rect[1] &&
    a.rect[2] === b.rect[2] &&
    a.rect[3] === b.rect[3]
  );
}

export function rectFromArray(a: readonly [number, number, number, number] | readonly number[] | null | undefined): Rect {
  if (!Array.isArray(a) || a.length !== 4) return { x: 0, y: 0, w: 1, h: 1 };
  return { x: a[0], y: a[1], w: a[2], h: a[3] };
}

export function rectToArray(r: Rect): [number, number, number, number] {
  return [r.x, r.y, r.w, r.h];
}

/** 桌面 sidecar 的 `{ straighten, vertical, horizontal, distortion, rotate90, flip }`；全为 0 时返回 null。 */
export function geometrySidecar(straighten: number): Record<string, unknown> | null {
  if (!straighten) return null;
  return { straighten, vertical: 0, horizontal: 0, distortion: 0, rotate90: 0, flip: false };
}

/** 交给 Core WASM `geom_crop_write` / `geom_geometry_write` 的 sidecar 子集。 */
export function sideSubset(photo: {
  frame: Frame | null;
  geometry: GeometryState;
  crop: CropState | null;
}): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  if (photo.frame) out.frame = { orientation: photo.frame.orientation, width: photo.frame.width, height: photo.frame.height };
  const geom = geometrySidecar(photo.geometry.straighten);
  out.geometry = geom;
  out.crop = photo.crop ? { preset: photo.crop.preset, rect: [...photo.crop.rect], angle_deg: photo.crop.angle_deg } : null;
  return out;
}

/** 横向比例数值（宽 ÷ 高 ≥ 1）。原比例、自由返回 null。 */
export function landscapeRatio(id: string): number | null {
  if (id === 'xpan-65-24') return 65 / 24;
  const m = /^(\d+(?:\.\d+)?):(\d+(?:\.\d+)?)$/.exec(id || '');
  if (!m) return null;
  const a = Number(m[1]);
  const b = Number(m[2]);
  return Math.max(a / b, b / a);
}

/** 横向最简整数比；原比例、自由返回 null。 */
export function landscapePair(id: string): [number, number] | null {
  if (id === 'xpan-65-24') return [65, 24];
  const m = /^(\d+(?:\.\d+)?):(\d+(?:\.\d+)?)$/.exec(id || '');
  if (!m) return null;
  let a = Math.round(Number(m[1]) * 100);
  let b = Math.round(Number(m[2]) * 100);
  const gcd = (x: number, y: number): number => (y ? gcd(y, x % y) : x);
  const g = gcd(a, b);
  a = Math.trunc(a / g);
  b = Math.trunc(b / g);
  return a >= b ? [a, b] : [b, a];
}

export function cropIsPortrait(rect: Rect, canvas: CanvasDims | null): boolean {
  if (canvas && canvas.w > 0 && canvas.h > 0) return rect.w * canvas.w < rect.h * canvas.h;
  return false;
}

export function lockRatio(preset: string, rect: Rect, canvas: CanvasDims): number | null {
  if (preset === 'free') return null;
  const L = landscapeRatio(preset);
  if (L === null) return canvas.w / canvas.h;
  if (!ORIENTABLE.has(preset)) return L;
  return cropIsPortrait(rect, canvas) ? 1 / L : L;
}

export function presetLabel(preset: string, rect: Rect | null, canvas: CanvasDims | null): string {
  if (preset === '4:3') {
    if (rect && canvas && cropIsPortrait(rect, canvas)) return '小红书 3:4';
    return '4:3';
  }
  const hit = PRESETS.find((p) => p.id === preset);
  return hit ? hit.label : preset;
}

export function presetMatchesSpec(spec: CropPresetSpec, preset: string, rect: Rect | null, canvas: CanvasDims | null): boolean {
  if (spec.id !== preset) return false;
  if (spec.id === '4:3' && spec.orient === 'portrait') {
    return rect && canvas ? cropIsPortrait(rect, canvas) : true;
  }
  return true;
}

/** 拉直 `angleDeg` 后未裁切画面中以中心为基准的最大合格框（`geom.js` centeredMaxRect）。 */
export function centeredMaxRect(canvas: CanvasDims, ratio: number | null, angleDeg = 0, pair: [number, number] | null = null): Rect {
  const W = canvas.w;
  const H = canvas.h;
  const a = ratio === null ? W / H : ratio;
  const t = (Math.abs(angleDeg) * Math.PI) / 180;
  const c = Math.cos(t);
  const s = Math.sin(t);
  const q = 1 / a;
  let w = Math.min(W, W / (c + q * s), H / (s + q * c));
  let h = Math.min(H, w / a);
  if (pair && Math.abs(angleDeg) > 1e-9) {
    const [n, d] = a >= 1 ? pair : [pair[1], pair[0]];
    const k = Math.min(Math.floor(Math.floor(w + 1e-9) / n), Math.floor(Math.floor(h + 0.5) / d));
    if (k >= 1) {
      w = k * n;
      h = k * d;
    }
  }
  return { x: (W - w) / 2 / W, y: (H - h) / 2 / H, w: w / W, h: h / H };
}

export function insideStraightened(r: Rect, canvas: CanvasDims, angleDeg: number): boolean {
  if (r.x < -EPS || r.y < -EPS || r.x + r.w > 1 + EPS || r.y + r.h > 1 + EPS) return false;
  const W = canvas.w;
  const H = canvas.h;
  const t = (angleDeg * Math.PI) / 180;
  const c = Math.cos(t);
  const s = Math.sin(t);
  const tol = 1e-6;
  const x0 = r.x * W - W / 2;
  const y0 = r.y * H - H / 2;
  const x1 = x0 + r.w * W;
  const y1 = y0 + r.h * H;
  for (const [dx, dy] of [
    [x0, y0],
    [x1, y0],
    [x0, y1],
    [x1, y1],
  ] as const) {
    if (Math.abs(dx * c + dy * s) > W / 2 + tol || Math.abs(-dx * s + dy * c) > H / 2 + tol) return false;
  }
  return true;
}

/** 收进拉直后的原画面：先挪位置，挪不进去再按中心等比缩小，保持长宽比。 */
export function fitStraightened(rect: Rect, canvas: CanvasDims, angleDeg: number): { rect: Rect; changed: boolean } {
  if (insideStraightened(rect, canvas, angleDeg)) return { rect: { ...rect }, changed: false };
  const cx0 = rect.x + rect.w / 2;
  const cy0 = rect.y + rect.h / 2;
  const cap = Math.min(1, 1 / Math.max(EPS, rect.w), 1 / Math.max(EPS, rect.h));
  const at = (t: number, k: number): Rect => {
    const cx = cx0 + (0.5 - cx0) * t;
    const cy = cy0 + (0.5 - cy0) * t;
    const kk = Math.min(k, cap);
    const w = rect.w * kk;
    const h = rect.h * kk;
    return { x: cx - w / 2, y: cy - h / 2, w, h };
  };
  const ok = (r: Rect) => insideStraightened(r, canvas, angleDeg);
  if (ok(at(1, 1))) {
    let lo = 0;
    let hi = 1;
    for (let i = 0; i < 40; i += 1) {
      const mid = (lo + hi) / 2;
      if (ok(at(mid, 1))) hi = mid;
      else lo = mid;
    }
    return { rect: at(hi, 1), changed: true };
  }
  let lo = 0;
  let hi = 1;
  for (let i = 0; i < 40; i += 1) {
    const mid = (lo + hi) / 2;
    if (ok(at(1, mid))) lo = mid;
    else hi = mid;
  }
  return { rect: at(1, lo), changed: true };
}

export function moveRect(start: Rect, dx: number, dy: number): Rect {
  return { ...start, x: clamp(start.x + dx, 0, 1 - start.w), y: clamp(start.y + dy, 0, 1 - start.h) };
}

/** 拖四角或四边调整框大小；`lock !== null` 时锁定像素宽高比（边拖时沿对边垂直居中缩放）。 */
export function resizeRect(start: Rect, handle: Handle, pointer: { x: number; y: number }, canvas: CanvasDims, lock: number | null): Rect {
  const aspectPx = canvas.w / canvas.h;
  const minForRatio = (ratio: number): { w: number; h: number } => {
    // 保持 (w * canvas.w) / (h * canvas.h) === ratio 且 w >= MIN_SIDE && h >= MIN_SIDE
    const wFromH = (MIN_SIDE * ratio) / aspectPx;
    if (wFromH <= MIN_SIDE) {
      return { w: MIN_SIDE, h: (MIN_SIDE * aspectPx) / ratio };
    }
    return { w: wFromH, h: MIN_SIDE };
  };
  const lockH = (w: number, ratio: number): { w: number; h: number } => {
    const mins = minForRatio(ratio);
    const ww = Math.max(mins.w, w);
    return { w: ww, h: (ww * aspectPx) / ratio };
  };
  const lockW = (h: number, ratio: number): { w: number; h: number } => {
    const mins = minForRatio(ratio);
    const hh = Math.max(mins.h, h);
    return { w: (hh * ratio) / aspectPx, h: hh };
  };

  if (handle === 'l' || handle === 'r') {
    const ax = handle === 'l' ? start.x + start.w : start.x;
    const rawW = handle === 'l' ? ax - pointer.x : pointer.x - ax;
    if (lock === null) {
      const w = Math.max(MIN_SIDE, rawW);
      return { x: handle === 'l' ? ax - w : ax, y: start.y, w, h: start.h };
    }
    const { w, h } = lockH(rawW, lock);
    const cy = start.y + start.h / 2;
    return { x: handle === 'l' ? ax - w : ax, y: cy - h / 2, w, h };
  }
  if (handle === 't' || handle === 'b') {
    const ay = handle === 't' ? start.y + start.h : start.y;
    const rawH = handle === 't' ? ay - pointer.y : pointer.y - ay;
    if (lock === null) {
      const h = Math.max(MIN_SIDE, rawH);
      return { x: start.x, y: handle === 't' ? ay - h : ay, w: start.w, h };
    }
    const { w, h } = lockW(rawH, lock);
    const cx = start.x + start.w / 2;
    return { x: cx - w / 2, y: handle === 't' ? ay - h : ay, w, h };
  }

  const left = handle.includes('l');
  const top = handle.includes('t');
  const ax = left ? start.x + start.w : start.x;
  const ay = top ? start.y + start.h : start.y;
  const rawW = left ? ax - pointer.x : pointer.x - ax;
  const rawH = top ? ay - pointer.y : pointer.y - ay;
  if (lock === null) {
    const w = Math.max(MIN_SIDE, rawW);
    const h = Math.max(MIN_SIDE, rawH);
    return { x: left ? ax - w : ax, y: top ? ay - h : ay, w, h };
  }
  const { w, h } = Math.abs(rawW) >= (Math.abs(rawH) * lock) / aspectPx ? lockH(rawW, lock) : lockW(rawH, lock);
  return { x: left ? ax - w : ax, y: top ? ay - h : ay, w, h };
}

export function croppedPixelSize(rect: Rect, canvas: CanvasDims): { w: number; h: number } {
  return {
    w: Math.max(1, Math.round(rect.w * canvas.w)),
    h: Math.max(1, Math.round(rect.h * canvas.h)),
  };
}

const PHI = (1 + Math.sqrt(5)) / 2;
const GOLD_SMALL = 1 - 1 / PHI;
const GOLD_BIG = 1 / PHI;

function spiralPath(k: number, ar: number): string {
  const sx = k & 1 ? -1 : 1;
  const sy = k & 2 ? -1 : 1;
  const px = (sx > 0 ? GOLD_BIG : GOLD_SMALL) * ar;
  const py = sy > 0 ? GOLD_SMALL : GOLD_BIG;
  let d = '';
  for (let u = -9; u <= 0.6 + 1e-9; u += 0.02) {
    const r = 0.9 * PHI ** u;
    const th = (u * Math.PI) / 2;
    const x = px + sx * r * Math.cos(th);
    const y = py + sy * r * Math.sin(th);
    d += `${d ? 'L' : 'M'}${x.toFixed(4)} ${y.toFixed(4)}`;
  }
  return d;
}

/** 构图参考线几何（照桌面 `crop.js` guideShapes）。 */
export function guideShapes(
  guide: GuideId,
  spiral: number,
  ar: number,
): { lines: Array<[number, number, number, number]>; path: string } {
  const lines: Array<[number, number, number, number]> = [];
  const at = (fracs: number[]) => {
    for (const f of fracs) {
      lines.push([f * ar, 0, f * ar, 1]);
      lines.push([0, f, ar, f]);
    }
  };
  switch (guide) {
    case 'thirds':
      at([1 / 3, 2 / 3]);
      break;
    case 'golden':
      at([GOLD_SMALL, GOLD_BIG]);
      break;
    case 'diagonal':
      lines.push([0, 0, ar, 1], [ar, 0, 0, 1]);
      break;
    case 'grid':
      at([0.25, 0.5, 0.75]);
      break;
    case 'spiral':
      return { lines, path: spiralPath(spiral, ar) };
    case 'off':
    default:
      break;
  }
  return { lines, path: '' };
}
