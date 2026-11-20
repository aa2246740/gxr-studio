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

const SEAM_G250 = 'g250';
const SEAM_LEGACY48 = 'legacy48';

const SEAM_SLICES: readonly [string, number, number][] = [
  [SEAM_G250, Math.floor((SEAMS[0][0] + SCALE - 1) / SCALE), Math.floor((SEAMS[0][1] - (SCALE - 1)) / SCALE)],
  [SEAM_LEGACY48, Math.floor((SEAMS[1][0] + SCALE - 1) / SCALE), Math.floor((SEAMS[1][1] - (SCALE - 1)) / SCALE)],
];

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

export type DiptychAxis = 'col' | 'row';

export interface DraftLayout {
  draftWidth: number;
  draftHeight: number;
  axis: DiptychAxis;
}

/**
 * 根据解码后位图的实际宽高确定 1/8 检测尺寸与扫描方向（不依赖头信息预测的旋转方向）。
 */
export function draftLayoutForBitmap(width: number, height: number): DraftLayout | null {
  if ((width === DRAFT_WIDTH && height === DRAFT_HEIGHT) || (width === FULL_WIDTH && height === FULL_HEIGHT)) {
    return { draftWidth: DRAFT_WIDTH, draftHeight: DRAFT_HEIGHT, axis: 'col' };
  }
  if ((width === DRAFT_HEIGHT && height === DRAFT_WIDTH) || (width === FULL_HEIGHT && height === FULL_WIDTH)) {
    return { draftWidth: DRAFT_HEIGHT, draftHeight: DRAFT_WIDTH, axis: 'row' };
  }
  return null;
}

/**
 * 纯函数检查 1/8 尺度 RGBA 像素（536×356 横向按列，或 356×536 竖向按行）是否匹配已知双联黑缝。
 */
export function isDiptychRgba(
  rgba: Uint8Array | Uint8ClampedArray,
  width: number,
  height: number,
  axis?: DiptychAxis,
): boolean {
  const resolvedAxis: DiptychAxis | null =
    axis ??
    (width === DRAFT_WIDTH && height === DRAFT_HEIGHT
      ? 'col'
      : width === DRAFT_HEIGHT && height === DRAFT_WIDTH
        ? 'row'
        : null);
  if (!resolvedAxis) return false;
  const wantW = resolvedAxis === 'row' ? DRAFT_HEIGHT : DRAFT_WIDTH;
  const wantH = resolvedAxis === 'row' ? DRAFT_WIDTH : DRAFT_HEIGHT;
  if (width !== wantW || height !== wantH || rgba.length < width * height * 4) return false;
  for (const [kind, c0, c1] of SEAM_SLICES) {
    if (c0 <= 0 || c1 + 1 >= DRAFT_WIDTH) continue;
    let seamBlack = true;
    for (let c = c0; c <= c1; c += 1) {
      if (sliceMaxLuma(rgba, width, height, c, resolvedAxis) > LUMA_THRESHOLD) {
        seamBlack = false;
        break;
      }
    }
    if (!seamBlack) continue;
    const leftEdge = sliceMaxLuma(rgba, width, height, c0 - 1, resolvedAxis) > LUMA_THRESHOLD;
    const rightEdge = sliceMaxLuma(rgba, width, height, c1 + 1, resolvedAxis) > LUMA_THRESHOLD;
    if (leftEdge && rightEdge) return true;
    if (kind === SEAM_G250 && (leftEdge || rightEdge)) {
      const start = leftEdge ? c1 + 1 : 0;
      const end = leftEdge ? DRAFT_WIDTH : c0;
      for (let c = start; c < end; c += 1) {
        if (sliceMaxLuma(rgba, width, height, c, resolvedAxis) > LUMA_THRESHOLD) return true;
      }
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
  let hintRot90 = false;
  const resolved = frame ?? (await readFrameFromBlob(blob));
  if (resolved) {
    const match = unrotatedDimsMatch(resolved);
    if (!match.ok) return false;
    hintRot90 = match.rot90;
  } else {
    let probe: ImageBitmap | null = null;
    try {
      probe = await createImageBitmap(blob);
      const probeLayout = draftLayoutForBitmap(probe.width, probe.height);
      if (!probeLayout) return false;
      hintRot90 = probeLayout.axis === 'row';
    } catch {
      return false;
    } finally {
      probe?.close();
    }
  }
  const rw = hintRot90 ? DRAFT_HEIGHT : DRAFT_WIDTH;
  const rh = hintRot90 ? DRAFT_WIDTH : DRAFT_HEIGHT;
  let bm: ImageBitmap | null = null;
  try {
    try {
      bm = await createImageBitmap(blob, { resizeWidth: rw, resizeHeight: rh });
    } catch {
      bm = await createImageBitmap(blob);
    }
    const layout = draftLayoutForBitmap(bm.width, bm.height);
    if (!layout) return false;
    const canvas = new OffscreenCanvas(layout.draftWidth, layout.draftHeight);
    const ctx = canvas.getContext('2d');
    if (!ctx) return false;
    ctx.drawImage(bm, 0, 0, layout.draftWidth, layout.draftHeight);
    const rgba = ctx.getImageData(0, 0, layout.draftWidth, layout.draftHeight).data;
    return isDiptychRgba(rgba, layout.draftWidth, layout.draftHeight, layout.axis);
  } catch {
    return false;
  } finally {
    bm?.close();
  }
}
