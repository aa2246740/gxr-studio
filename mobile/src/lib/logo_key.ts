// 上传标志的本机预览与去白底：照桌面 `web/assets/logo_key.js` 与 `finish/logo_store.py` 实现。
// 纯图像算法不碰 DOM（drawCheckerPreview 仅在浏览器里调），Node 测试可以直接测去白底、裁边和统计。

function clipText(text: string, max: number): string {
  return Array.from(text).slice(0, max).join('');
}

export const LOGO_LIMITS = {
  bytes: 5 * 1024 * 1024,
  pixels: 25_000_000,
  dim: 16_384,
  minSide: 64,
  storeLongEdge: 1200,
  maxLogos: 12,
  nameMax: 20,
} as const;

export const LOGO_ERR = {
  tooBig: '文件太大了（超过 5 MB）。请换一张小一点的。',
  unreadable: '这个文件打不开。请换一张 PNG、JPG 或 WebP 图片。',
  tooSmall: '图片太小了：标志至少要 64 像素高。',
  limit: '已经保存了 12 个标志。删除一个，再上传新的。',
  badName: '请输入标志名称（最多 20 个字）。',
  badTol: '容差必须是 0 到 60 的整数。',
} as const;

export const LOGO_WARN = {
  whiteBg: '这张图有白色背景。去掉后，标志放在彩色或深色边框上才好看。',
  opaque: '这张图是不透明的，标志可能带着背景。可以打开「去掉白底」。',
} as const;

export interface RgbaPixels {
  width: number;
  height: number;
  data: Uint8ClampedArray;
}

export function sniffImageMagic(bytes: Uint8Array | null | undefined): 'PNG' | 'JPEG' | 'WEBP' | null {
  if (!bytes || bytes.length < 12) return null;
  if (
    bytes[0] === 0x89 &&
    bytes[1] === 0x50 &&
    bytes[2] === 0x4e &&
    bytes[3] === 0x47 &&
    bytes[4] === 0x0d &&
    bytes[5] === 0x0a &&
    bytes[6] === 0x1a &&
    bytes[7] === 0x0a
  ) {
    return 'PNG';
  }
  if (bytes[0] === 0xff && bytes[1] === 0xd8 && bytes[2] === 0xff) return 'JPEG';
  if (
    bytes[0] === 0x52 &&
    bytes[1] === 0x49 &&
    bytes[2] === 0x46 &&
    bytes[3] === 0x46 &&
    bytes[8] === 0x57 &&
    bytes[9] === 0x45 &&
    bytes[10] === 0x42 &&
    bytes[11] === 0x50
  ) {
    return 'WEBP';
  }
  return null;
}

export function isOpaqueImageData(imgData: RgbaPixels): boolean {
  const d = imgData.data;
  for (let i = 3; i < d.length; i += 4) if (d[i] < 250) return false;
  return true;
}

export function hasWhiteBackground(imgData: RgbaPixels): boolean {
  if (!isOpaqueImageData(imgData)) return false;
  const { width: w, height: h, data: d } = imgData;
  if (w < 1 || h < 1) return false;
  const coords = [
    [0, 0],
    [w - 1, 0],
    [0, h - 1],
    [w - 1, h - 1],
  ];
  return coords.every(([x, y]) => {
    const o = (y * w + x) * 4;
    return Math.min(d[o], d[o + 1], d[o + 2]) >= 238;
  });
}

export function validateLogoTol(tol: number): number {
  if (!Number.isFinite(tol) || !Number.isInteger(tol) || tol < 0 || tol > 60) {
    throw new RangeError(LOGO_ERR.badTol);
  }
  return tol;
}

export function keyWhiteImageData(imgData: RgbaPixels, tol = 30): RgbaPixels {
  const clamped = validateLogoTol(tol);
  const { width: w, height: h, data: src } = imgData;
  const n = w * h;
  const near = new Uint8Array(n);
  for (let i = 0; i < n; i++) {
    const o = i * 4;
    if (255 - Math.min(src[o], src[o + 1], src[o + 2]) <= clamped) near[i] = 1;
  }
  const seen = new Uint8Array(n);
  const queue = new Int32Array(n);
  let head = 0;
  let tail = 0;
  const push = (idx: number) => {
    if (near[idx] && !seen[idx]) {
      seen[idx] = 1;
      queue[tail++] = idx;
    }
  };
  for (let x = 0; x < w; x++) {
    push(x);
    push((h - 1) * w + x);
  }
  for (let y = 0; y < h; y++) {
    push(y * w);
    push(y * w + (w - 1));
  }
  while (head < tail) {
    const idx = queue[head++];
    const x = idx % w;
    const y = (idx - x) / w;
    if (x > 0) push(idx - 1);
    if (x + 1 < w) push(idx + 1);
    if (y > 0) push(idx - w);
    if (y + 1 < h) push(idx + w);
  }
  const out = new Uint8ClampedArray(n * 4);
  for (let y = 0; y < h; y++) {
    const y0 = Math.max(0, y - 1);
    const y1 = Math.min(h - 1, y + 1);
    for (let x = 0; x < w; x++) {
      const idx = y * w + x;
      const o = idx * 4;
      out[o] = src[o];
      out[o + 1] = src[o + 1];
      out[o + 2] = src[o + 2];
      if (seen[idx]) {
        out[o + 3] = 0;
        continue;
      }
      const x0 = Math.max(0, x - 1);
      const x1 = Math.min(w - 1, x + 1);
      let hasBg = false;
      for (let yy = y0; yy <= y1 && !hasBg; yy++) {
        const row = yy * w;
        for (let xx = x0; xx <= x1; xx++) {
          if (seen[row + xx]) {
            hasBg = true;
            break;
          }
        }
      }
      if (hasBg) {
        const whiteFrac = Math.max(0, Math.min(255, 255 - Math.min(src[o], src[o + 1], src[o + 2])));
        const prod16 = ((whiteFrac * 255) << 16) >> 16;
        out[o + 3] = Math.max(0, Math.min(255, Math.floor(prod16 / 160)));
      } else {
        out[o + 3] = 255;
      }
    }
  }
  return { width: w, height: h, data: out };
}

export function trimBounds(imgData: RgbaPixels, thresh = 8): [number, number, number, number] | null {
  const { width: w, height: h, data: d } = imgData;
  let minX = w;
  let minY = h;
  let maxX = -1;
  let maxY = -1;
  for (let y = 0; y < h; y++) {
    for (let x = 0; x < w; x++) {
      if (d[(y * w + x) * 4 + 3] > thresh) {
        if (x < minX) minX = x;
        if (x > maxX) maxX = x;
        if (y < minY) minY = y;
        if (y > maxY) maxY = y;
      }
    }
  }
  if (maxX < minX || maxY < minY) return null;
  return [minX, minY, maxX - minX + 1, maxY - minY + 1];
}

export function cropImageData(imgData: RgbaPixels, box: [number, number, number, number]): RgbaPixels {
  const [bx, by, bw, bh] = box;
  const out = new Uint8ClampedArray(bw * bh * 4);
  for (let y = 0; y < bh; y++) {
    const srcStart = ((by + y) * imgData.width + bx) * 4;
    out.set(imgData.data.subarray(srcStart, srcStart + bw * 4), y * bw * 4);
  }
  return { width: bw, height: bh, data: out };
}

export function logoStats(imgData: RgbaPixels): { coverage: number; opaque: boolean; lum: number } {
  const { width: w, height: h, data: d } = imgData;
  const total = Math.max(1, w * h);
  let opaque = true;
  let alphaSum = 0;
  let lumSum = 0;
  for (let i = 0; i < d.length; i += 4) {
    const a8 = d[i + 3];
    if (a8 < 250) opaque = false;
    const a = a8 / 255;
    alphaSum += a;
    const luma = 0.299 * d[i] + 0.587 * d[i + 1] + 0.114 * d[i + 2];
    lumSum += luma * a;
  }
  const coverage = Math.round((alphaSum / total) * 1e6) / 1e6;
  const lum = Math.round((lumSum / Math.max(1e-6, alphaSum)) * 1e3) / 1e3;
  return { coverage, opaque, lum };
}

export function stemOf(filename: string): string {
  const base = String(filename || '').replace(/\\/g, '/').split('/').pop() || '';
  const dot = base.lastIndexOf('.');
  const raw = dot > 0 ? base.slice(0, dot) : base;
  return clipText(raw.trim(), LOGO_LIMITS.nameMax);
}

export function cleanLogoName(rawName: string, fallback = ''): string {
  const clean = (s: string) =>
    Array.from(s)
      .filter((ch) => (ch.codePointAt(0) ?? 0) >= 0x20)
      .join('')
      .trim();
  let candidate = clean(rawName);
  if (!candidate && fallback) candidate = clean(stemOf(fallback));
  if (!candidate) candidate = '我的标志';
  return clipText(candidate, LOGO_LIMITS.nameMax);
}

export function drawCheckerPreview(
  canvas: HTMLCanvasElement,
  imgData: RgbaPixels | null,
  options: { cropToInk?: boolean } = {},
): void {
  const ctx = canvas.getContext('2d');
  if (!ctx) return;
  const cw = canvas.width;
  const ch = canvas.height;
  const cell = 12;
  for (let y = 0; y < ch; y += cell) {
    for (let x = 0; x < cw; x += cell) {
      ctx.fillStyle = (x / cell + y / cell) & 1 ? '#2a2a2e' : '#36363c';
      ctx.fillRect(x, y, cell, cell);
    }
  }
  if (!imgData) return;
  const box = options.cropToInk ? trimBounds(imgData) : [0, 0, imgData.width, imgData.height];
  if (!box) return;
  const [bx, by, bw, bh] = box;
  const tmp = document.createElement('canvas');
  try {
    tmp.width = imgData.width;
    tmp.height = imgData.height;
    const tctx = tmp.getContext('2d');
    if (!tctx) return;
    tctx.putImageData(new ImageData(new Uint8ClampedArray(imgData.data), imgData.width, imgData.height), 0, 0);
    const pad = 20;
    const availW = Math.max(1, cw - 2 * pad);
    const availH = Math.max(1, ch - 2 * pad);
    const scale = Math.min(availW / bw, availH / bh);
    const dw = bw * scale;
    const dh = bh * scale;
    ctx.imageSmoothingQuality = 'high';
    ctx.drawImage(tmp, bx, by, bw, bh, (cw - dw) / 2, (ch - dh) / 2, dw, dh);
  } finally {
    tmp.width = 0;
    tmp.height = 0;
  }
}
