// 透视（垂直、水平）和畸变：画布大小、原点和有效区，float64，与 finish/geometry.py 逐式对应。
// 画布大小必须与 Python 一致（_expanded_bounds），所以这里用 JS 的 double 算；逐像素的反向映射在 geo.wgsl 里用 f32 算。

export interface Warp {
  straighten: number;
  vertical: number;
  horizontal: number;
  distortion: number;
}

export interface Lens {
  radial: number;
  sv: number;
  sh: number;
  cos: number;
  sin: number;
  halfDiag: number;
}

export interface Bounds {
  minx: number;
  miny: number;
  w: number;
  h: number;
}

/** 透视或畸变会改变画布（Geometry.expands）。 */
export function expands(g: Warp): boolean {
  return g.vertical !== 0 || g.horizontal !== 0 || g.distortion !== 0;
}

// math.radians(x) = x × (π / 180)；先乘后除会差 1 ulp
const RAD = Math.PI / 180;

function sliderS(slider: number): number {
  const focal = 1 / Math.tan(13.5 * RAD);
  return Math.tan((slider / 100) * (30 * RAD)) / focal;
}

/** _lens：滑块 → 径向系数、两个透视系数、拉直的 cos / sin、半对角线。 */
export function lens(g: Warp, width: number, height: number): Lens {
  const th = g.straighten * RAD;
  return {
    radial: -(g.distortion / 100) * 0.35,
    sv: sliderS(g.vertical),
    sh: -sliderS(g.horizontal),
    cos: Math.cos(th),
    sin: Math.sin(th),
    halfDiag: Math.hypot(width, height) / 2,
  };
}

/** numpy.linspace(0, stop, n)：最后一个点正好是 stop。 */
function linspace(stop: number, n: number): number[] {
  const step = stop / (n - 1);
  const out = Array.from({ length: n }, (_, i) => i * step);
  out[n - 1] = stop;
  return out;
}

/** _expanded_bounds：画布原点与宽高。边缘 240 个点正向映射，留 1.5 像素，下取整 / 上取整。 */
export function expandedBounds(g: Warp, w: number, h: number): Bounds {
  const { radial, sv, sh, cos: c, sin: s, halfDiag } = lens(g, w, h);
  const xs = [...linspace(w - 1, 80), ...linspace(w - 1, 80), ...new Array<number>(40).fill(0), ...new Array<number>(40).fill(w - 1)];
  const ys = [...new Array<number>(80).fill(0), ...new Array<number>(80).fill(h - 1), ...linspace(h - 1, 40), ...linspace(h - 1, 40)];
  let minx = Infinity;
  let miny = Infinity;
  let maxx = -Infinity;
  let maxy = -Infinity;
  for (let i = 0; i < xs.length; i++) {
    let cx = xs[i] - (w - 1) / 2;
    let cy = ys[i] - (h - 1) / 2;
    const radius = Math.hypot(cx, cy) / halfDiag;
    const factor = 1 + radial * radius * radius;
    cx *= factor;
    cy *= factor;
    let nx = cx / (w / 2);
    let ny = cy / (h / 2);
    const denom = 1 + sv * ny + sh * nx;
    nx /= denom;
    ny /= denom;
    cx = nx * (w / 2);
    cy = ny * (h / 2);
    const fx = c * cx - s * cy;
    const fy = s * cx + c * cy;
    if (fx < minx) minx = fx;
    if (fx > maxx) maxx = fx;
    if (fy < miny) miny = fy;
    if (fy > maxy) maxy = fy;
  }
  const x0 = Math.floor(minx - 1.5);
  const y0 = Math.floor(miny - 1.5);
  return { minx: x0, miny: y0, w: Math.max(1, Math.ceil(maxx + 1.5) - x0), h: Math.max(1, Math.ceil(maxy + 1.5) - y0) };
}

/** 一个画布（旋转之前）：原图 w×h 经过几何之后。没有透视、畸变时画布还是原图大小，原点 0。 */
export interface Canvas extends Bounds {
  expanded: boolean;
}

export function canvasOf(g: Warp, w: number, h: number): Canvas {
  if (!expands(g)) return { minx: 0, miny: 0, w, h, expanded: false };
  return { ...expandedBounds(g, w, h), expanded: true };
}

/** _expanded_coords 在一个输出像素 (X, Y)（画布整数坐标）上的反向映射；返回原图里的取样位置。 */
export function sourceAt(g: Warp, w: number, h: number, canvas: Bounds, X: number, Y: number, l: Lens = lens(g, w, h)): [number, number] {
  const ox = canvas.minx + X + 0.5;
  const oy = canvas.miny + Y + 0.5;
  const rx = l.cos * ox + l.sin * oy;
  const ry = -l.sin * ox + l.cos * oy;
  let nx = rx / (w / 2);
  let ny = ry / (h / 2);
  const denom = 1 - l.sh * nx - l.sv * ny;
  nx /= denom;
  ny /= denom;
  const cx = nx * (w / 2);
  const cy = ny * (h / 2);
  const rOut = Math.hypot(cx, cy) / l.halfDiag;
  let rSrc = rOut;
  if (Math.abs(l.radial) >= 1e-8) {
    for (let i = 0; i < 8; i++) {
      const f = l.radial * rSrc ** 3 + rSrc - rOut;
      const df = 3 * l.radial * rSrc ** 2 + 1;
      rSrc -= f / (Math.abs(df) < 1e-6 ? 1 : df);
    }
  }
  const scale = rOut > 1e-8 ? rSrc / rOut : 1;
  return [cx * scale + (w - 1) / 2, cy * scale + (h - 1) / 2];
}

/** _coverage：取样位置落在原图里（边缘留 1e-3 像素）。 */
export function covered(w: number, h: number, sx: number, sy: number): boolean {
  return sx >= -1e-3 && sy >= -1e-3 && sx <= w - 1 + 1e-3 && sy <= h - 1 + 1e-3;
}

/** 旋转 90° 次数 → 旋转后画布的宽高。 */
export function rotatedSize(canvas: Bounds, turns: number): { w: number; h: number } {
  return turns % 2 ? { w: canvas.h, h: canvas.w } : { w: canvas.w, h: canvas.h };
}

// ---- 有效区（normalized_valid_rect）-------------------------------------------------------
// Python 先在整张画布上算蒙版，再缩到长边 240 取样。这里只算那 ≤240×240 个取样点上的覆盖，结果相同，代价是几万次反向映射。

const SAMPLE_EDGE = 240;

/** Python 的 round：对半取偶。 */
function roundEven(x: number): number {
  const f = Math.floor(x);
  const d = x - f;
  if (d < 0.5) return f;
  if (d > 0.5) return f + 1;
  return f % 2 === 0 ? f : f + 1;
}

/** numpy：np.linspace(0, n - 1, count).astype(int32)（向零取整）。 */
function sampleIndex(n: number, count: number): number[] {
  return linspace(n - 1, count).map((v) => Math.trunc(v));
}

/** max_area_rect：完全落在有效格子里、面积最大的轴对齐矩形，返回 [x, y, w, h]。 */
export function maxAreaRect(valid: Uint8Array, width: number, height: number): [number, number, number, number] {
  const heights = new Int32Array(width);
  let best: [number, number, number, number] = [0, 0, 0, 0];
  let bestArea = 0;
  const stack: number[] = [];
  for (let i = 0; i < height; i++) {
    for (let j = 0; j < width; j++) heights[j] = valid[i * width + j] ? heights[j] + 1 : 0;
    stack.length = 0;
    stack.push(-1);
    for (let j = 0; j <= width; j++) {
      const cur = j < width ? heights[j] : 0;
      while (stack[stack.length - 1] !== -1 && heights[stack[stack.length - 1]] > cur) {
        const bar = heights[stack.pop()!];
        const left = stack[stack.length - 1];
        const rw = j - left - 1;
        if (bar * rw > bestArea) {
          bestArea = bar * rw;
          best = [left + 1, i - bar + 1, rw, bar];
        }
      }
      stack.push(j);
    }
  }
  return best;
}

/**
 * warp_valid → normalized_valid_rect：几何变换（含旋转、翻转）之后，完全落在照片里的最大矩形，归一化到画布。
 * w、h 是原图（颜色层）尺寸。返回 [x, y, w, h]（0–1）。
 */
export function validRect(g: Warp & { rotate90: number; flip: boolean }, w: number, h: number): [number, number, number, number] {
  const turns = ((g.rotate90 % 4) + 4) % 4;
  const canvas = canvasOf(g, w, h);
  const { w: rw, h: rh } = rotatedSize(canvas, turns);
  const l = lens(g, w, h);
  // 旋转、翻转之后的画布格子 (i 行, j 列) → 旋转之前的画布像素 (X, Y)（np.rot90 与 np.fliplr 的逆映射）
  const cover = (i: number, j: number): boolean => {
    const col = g.flip ? rw - 1 - j : j;
    let X = col;
    let Y = i;
    if (turns === 1) {
      X = canvas.w - 1 - i;
      Y = col;
    } else if (turns === 2) {
      X = canvas.w - 1 - col;
      Y = canvas.h - 1 - i;
    } else if (turns === 3) {
      X = i;
      Y = canvas.h - 1 - col;
    }
    const [sx, sy] = canvas.expanded ? sourceAt(g, w, h, canvas, X, Y, l) : straightAt(g, w, h, X, Y);
    return covered(w, h, sx, sy);
  };
  const long = Math.max(rh, rw);
  if (long <= SAMPLE_EDGE) {
    const grid = new Uint8Array(rw * rh);
    let all = true;
    for (let i = 0; i < rh; i++) for (let j = 0; j < rw; j++) all = (grid[i * rw + j] = cover(i, j) ? 1 : 0) !== 0 && all;
    if (all) return [0, 0, 1, 1];
    const [x, y, bw, bh] = maxAreaRect(grid, rw, rh);
    return bw <= 0 || bh <= 0 ? [0, 0, 1, 1] : [x / rw, y / rh, bw / rw, bh / rh];
  }
  const scale = SAMPLE_EDGE / long;
  const sh = Math.max(2, roundEven(rh * scale));
  const sw = Math.max(2, roundEven(rw * scale));
  const rows = sampleIndex(rh, sh);
  const cols = sampleIndex(rw, sw);
  const grid = new Uint8Array(sw * sh);
  for (let a = 0; a < sh; a++) for (let b = 0; b < sw; b++) grid[a * sw + b] = cover(rows[a], cols[b]) ? 1 : 0;
  const [x, y, bw, bh] = maxAreaRect(grid, sw, sh);
  if (bw <= 0 || bh <= 0) return [0, 0, 1, 1];
  const x0 = Math.trunc((x / sw) * rw) + 1;
  const y0 = Math.trunc((y / sh) * rh) + 1;
  const x1 = Math.max(x0 + 1, Math.trunc(((x + bw) / sw) * rw) - 1);
  const y1 = Math.max(y0 + 1, Math.trunc(((y + bh) / sh) * rh) - 1);
  return [x0 / rw, y0 / rh, (x1 - x0) / rw, (y1 - y0) / rh];
}

/** _straighten_coords：只有拉直时，输出像素 (X, Y) 在原图里的位置。 */
function straightAt(g: Warp, w: number, h: number, X: number, Y: number): [number, number] {
  if (Math.abs(g.straighten) < 1e-9) return [X, Y];
  const th = g.straighten * RAD;
  const c = Math.cos(th);
  const s = Math.sin(th);
  const cx = (w - 1) / 2;
  const cy = (h - 1) / 2;
  const dx = X - cx;
  const dy = Y - cy;
  return [c * dx + s * dy + cx, -s * dx + c * dy + cy];
}
