// render/tones.py 的浏览器版：曝光/白平衡增益，以及对比、高光、阴影 + 主曲线合成的 1021 点调子表。
// PCHIP 与 scipy.interpolate.PchipInterpolator 相同（内部点加权调和平均，端点三点公式 + 形状保持）。

export interface Adjust {
  exposure_ev?: number;
  wb?: { warm?: number; tint?: number };
  contrast?: number;
  highlight?: number;
  shadow?: number;
  black_point?: number;
  saturation?: number;
  vibrance?: number;
  vignette?: number;
}

export type CurvePoints = ReadonlyArray<readonly [number, number]>;
export type Curves = Readonly<Partial<Record<'master' | 'r' | 'g' | 'b', CurvePoints | null>>>;

const WARM_EV = 0.35;
const TINT_EV = 0.25;
const SHADOW_HIGHLIGHT_AMOUNT = 0.13;
export const TABLE_STEP = 4;
export const TABLE_SIZE = 255 * TABLE_STEP + 1;

export function channelGains(a: Adjust): [number, number, number] | null {
  const ev = a.exposure_ev ?? 0;
  const warm = (a.wb?.warm ?? 0) / 100;
  const tint = (a.wb?.tint ?? 0) / 100;
  if (ev === 0 && warm === 0 && tint === 0) return null;
  return [2 ** (ev + WARM_EV * warm + TINT_EV * tint), 2 ** ev, 2 ** (ev - WARM_EV * warm + TINT_EV * tint)];
}

export function toneIsIdentity(a: Adjust): boolean {
  return !a.contrast && !a.highlight && !a.shadow;
}

function toneFunction(t: number, a: Adjust): number {
  let y = t;
  if (a.contrast) {
    const k = a.contrast / 100;
    y = y + k * (y * y * (3 - 2 * y) - y);
  }
  if (a.highlight) y = y + (a.highlight / 100) * SHADOW_HIGHLIGHT_AMOUNT * 6.75 * y * y * (1 - y);
  if (a.shadow) y = y + (a.shadow / 100) * SHADOW_HIGHLIGHT_AMOUNT * 6.75 * y * (1 - y) ** 2;
  return Math.min(1, Math.max(0, y));
}

function edgeSlope(h0: number, h1: number, m0: number, m1: number): number {
  let d = ((2 * h0 + h1) * m0 - h0 * m1) / (h0 + h1);
  if (Math.sign(d) !== Math.sign(m0)) d = 0;
  else if (Math.sign(m0) !== Math.sign(m1) && Math.abs(d) > Math.abs(3 * m0)) d = 3 * m0;
  return d;
}

export function pchip(xs: readonly number[], ys: readonly number[]): (x: number) => number {
  const n = xs.length;
  const h: number[] = [];
  const m: number[] = [];
  for (let k = 0; k < n - 1; k++) {
    h.push(xs[k + 1] - xs[k]);
    m.push((ys[k + 1] - ys[k]) / h[k]);
  }
  const d = new Array<number>(n).fill(0);
  if (n === 2) {
    d[0] = m[0];
    d[1] = m[0];
  } else {
    for (let k = 1; k < n - 1; k++) {
      if (m[k - 1] * m[k] <= 0) continue;
      const w1 = 2 * h[k] + h[k - 1];
      const w2 = h[k] + 2 * h[k - 1];
      d[k] = (w1 + w2) / (w1 / m[k - 1] + w2 / m[k]);
    }
    d[0] = edgeSlope(h[0], h[1], m[0], m[1]);
    d[n - 1] = edgeSlope(h[n - 2], h[n - 3], m[n - 2], m[n - 3]);
  }
  return (x: number) => {
    let k = 0;
    while (k < n - 2 && x > xs[k + 1]) k++;
    const t = (x - xs[k]) / h[k];
    const t2 = t * t;
    const t3 = t2 * t;
    return (2 * t3 - 3 * t2 + 1) * ys[k] + (t3 - 2 * t2 + t) * h[k] * d[k] + (-2 * t3 + 3 * t2) * ys[k + 1] + (t3 - t2) * h[k] * d[k + 1];
  };
}

/** render/params.py parse_curve：恰好是恒等直线（每个点 x = y）的曲线等于没有曲线。 */
export function effectiveCurve(curve: CurvePoints | null | undefined): CurvePoints | null {
  if (!curve || curve.length < 2) return null;
  return curve.every(([x, y]) => x === y) ? null : curve;
}

function curveFunction(points: CurvePoints | null | undefined): ((x: number) => number) | null {
  const curve = effectiveCurve(points);
  if (!curve) return null;
  const xs = curve.map((pt) => pt[0]);
  const f = pchip(xs, curve.map((pt) => pt[1]));
  return (x) => Math.min(255, Math.max(0, f(Math.min(xs[xs.length - 1], Math.max(xs[0], x)))));
}

/** render/tones.py display_table：全恒等为 null，仅主曲线 1021 点，RGB 表按 R/G/B 连续存放。 */
export function displayTable(a: Adjust, curves: Curves | null): Float32Array | null {
  const master = curveFunction(curves?.master);
  const channels = [curves?.r, curves?.g, curves?.b].map(curveFunction);
  const separate = channels.some((f) => f !== null);
  if (toneIsIdentity(a) && !master && !separate) return null;
  const count = separate ? 3 : 1;
  const out = new Float32Array(TABLE_SIZE * count);
  for (let i = 0; i < TABLE_SIZE; i++) {
    const x = (255 * i) / (TABLE_SIZE - 1);
    const base = toneFunction(x / 255, a) * 255;
    for (let c = 0; c < count; c++) {
      // 与 GIMP gimp_curve_map_pixels 一致：单通道 → 主曲线，在原显示编码域合表。
      const y = channels[c]?.(base) ?? base;
      out[c * TABLE_SIZE + i] = master?.(y) ?? y;
    }
  }
  return out;
}
