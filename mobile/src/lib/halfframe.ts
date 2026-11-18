// 半格双联 JPG 检测：与桌面 halfframe.py 同规则（1/8 尺度上检查未转正 4288×2848 双联黑缝 2088–2199 / 2120–2167）。

import { jpegFrame, type Frame } from './photo_size.ts';

const PREFIX = 128 * 1024;
const SIZE_PREFIX = 1024 * 1024;

export const FULL_WIDTH = 4288;
export const FULL_HEIGHT = 2848;
export const SCALE = 8;
export const DRAFT_WIDTH = FULL_WIDTH / SCALE;
export const DRAFT_HEIGHT = FULL_HEIGHT / SCALE;
export const LUMA_THRESHOLD = 12;

export const SEAMS: readonly [number, number][] = [
  [2088, 2199],
  [2120, 2167],
];

const SEAM_SLICES: readonly [number, number][] = SEAMS.map(([x0, x1]) => [
  Math.floor((x0 + SCALE - 1) / SCALE),
  Math.floor((x1 - (SCALE - 1)) / SCALE),
]);

function sliceMaxLuma(
  rgba: Uint8Array | Uint8ClampedArray,
  width: number,
  height: number,
  index: number,
  axis: 'col' | 'row',
): number {
  let best = 0;
  if (axis === 'col') {
    for (let y = 0; y < height; y += 1) {
      const off = (y * width + index) * 4;
      const luma = Math.round(0.299 * rgba[off] + 0.587 * rgba[off + 1] + 0.114 * rgba[off + 2]);
      if (luma > best) best = luma;
      if (best > LUMA_THRESHOLD) return best;
    }
    return best;
  }
  const rowBase = index * width * 4;
  for (let x = 0; x < width; x += 1) {
    const off = rowBase + x * 4;
    const luma = Math.round(0.299 * rgba[off] + 0.587 * rgba[off + 1] + 0.114 * rgba[off + 2]);
    if (luma > best) best = luma;
    if (best > LUMA_THRESHOLD) return best;
  }
  return best;
}

/**
 * 纯函数检查 1/8 尺度 RGBA 像素（536×356 横向按列，或经 EXIF 90°/270° 转正后的 356×536 按行）是否匹配已知双联黑缝。
 */
export function isDiptychRgba(
  rgba: Uint8Array | Uint8ClampedArray,
  width: number,
  height: number,
  axis: 'col' | 'row' = 'col',
): boolean {
  const wantW = axis === 'row' ? DRAFT_HEIGHT : DRAFT_WIDTH;
  const wantH = axis === 'row' ? DRAFT_WIDTH : DRAFT_HEIGHT;
  if (width !== wantW || height !== wantH || rgba.length < width * height * 4) return false;
  for (const [c0, c1] of SEAM_SLICES) {
    if (c0 <= 0 || c1 + 1 >= DRAFT_WIDTH) continue;
    let seamBlack = true;
    for (let c = c0; c <= c1; c += 1) {
      if (sliceMaxLuma(rgba, width, height, c, axis) > LUMA_THRESHOLD) {
        seamBlack = false;
        break;
      }
    }
    if (!seamBlack) continue;
    if (
      sliceMaxLuma(rgba, width, height, c0 - 1, axis) > LUMA_THRESHOLD &&
      sliceMaxLuma(rgba, width, height, c1 + 1, axis) > LUMA_THRESHOLD
    ) {
      return true;
    }
  }
  return false;
}

async function readRange(blob: Blob, start: number, length: number): Promise<Uint8Array | null> {
  const end = Math.min(blob.size, start + length);
  if (end <= start) return new Uint8Array();
  try {
    return new Uint8Array(await blob.slice(start, end).arrayBuffer());
  } catch {
    return null;
  }
}

async function readFrameFromBlob(blob: Blob): Promise<Frame | null> {
  const head = await readRange(blob, 0, PREFIX);
  let frame = head ? jpegFrame(head) : null;
  if (!frame && blob.size > PREFIX) {
    const wide = await readRange(blob, 0, SIZE_PREFIX);
    if (wide) frame = jpegFrame(wide);
  }
  return frame;
}

function unrotatedDimsMatch(frame: Frame): { ok: boolean; rot90: boolean } {
  const rot90 = frame.orientation >= 5 && frame.orientation <= 8;
  const ok = rot90
    ? frame.width === FULL_HEIGHT && frame.height === FULL_WIDTH
    : frame.width === FULL_WIDTH && frame.height === FULL_HEIGHT;
  return { ok, rot90 };
}

/**
 * 判断相机 JPG Blob 是否为半格双联。使用 1/8 尺度 `createImageBitmap(..., { resizeWidth, resizeHeight })`，用完立刻关闭位图。
 */
export async function isDiptychBlob(blob: Blob, frame?: Frame | null): Promise<boolean> {
  if (typeof createImageBitmap !== 'function' || typeof OffscreenCanvas !== 'function') return false;
  let rot90 = false;
  const resolved = frame ?? (await readFrameFromBlob(blob));
  if (resolved) {
    const match = unrotatedDimsMatch(resolved);
    if (!match.ok) return false;
    rot90 = match.rot90;
  } else {
    let probe: ImageBitmap | null = null;
    try {
      probe = await createImageBitmap(blob);
      if (probe.width === FULL_WIDTH && probe.height === FULL_HEIGHT) rot90 = false;
      else if (probe.width === FULL_HEIGHT && probe.height === FULL_WIDTH) rot90 = true;
      else return false;
    } catch {
      return false;
    } finally {
      probe?.close();
    }
  }
  const rw = rot90 ? DRAFT_HEIGHT : DRAFT_WIDTH;
  const rh = rot90 ? DRAFT_WIDTH : DRAFT_HEIGHT;
  let bm: ImageBitmap | null = null;
  try {
    try {
      bm = await createImageBitmap(blob, { resizeWidth: rw, resizeHeight: rh });
    } catch {
      bm = await createImageBitmap(blob);
    }
    const validFull = (bm.width === FULL_WIDTH && bm.height === FULL_HEIGHT && !rot90) || (bm.width === FULL_HEIGHT && bm.height === FULL_WIDTH && rot90);
    if (!((bm.width === rw && bm.height === rh) || validFull)) return false;
    const canvas = new OffscreenCanvas(rw, rh);
    const ctx = canvas.getContext('2d');
    if (!ctx) return false;
    ctx.drawImage(bm, 0, 0, rw, rh);
    const rgba = ctx.getImageData(0, 0, rw, rh).data;
    return isDiptychRgba(rgba, rw, rh, rot90 ? 'row' : 'col');
  } catch {
    return false;
  } finally {
    bm?.close();
  }
}
