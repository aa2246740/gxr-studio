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
// g250 单侧紧邻暗景时，暗侧半幅至少 1/20（5%，约 4,646/92,916 像素）高于阈值，与桌面 halfframe.py 同规则。
export const DARK_HALF_MIN_DIVISOR = 20;

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

function darkHalfHasContent(
  rgba: Uint8Array | Uint8ClampedArray,
  width: number,
  height: number,
  start: number,
  end: number,
  axis: 'col' | 'row',
): boolean {
  const sliceLen = axis === 'col' ? height : width;
  const total = (end - start) * sliceLen;
  const minLit = Math.ceil(total / DARK_HALF_MIN_DIVISOR);
  let lit = 0;
  if (axis === 'col') {
    for (let y = 0; y < height; y += 1) {
      const rowOff = y * width;
      for (let x = start; x < end; x += 1) {
        const off = (rowOff + x) * 4;
        if (Math.round(0.299 * rgba[off] + 0.587 * rgba[off + 1] + 0.114 * rgba[off + 2]) > LUMA_THRESHOLD) {
          lit += 1;
          if (lit >= minLit) return true;
        }
      }
    }
    return false;
  }
  for (let y = start; y < end; y += 1) {
    const rowBase = y * width * 4;
    for (let x = 0; x < width; x += 1) {
      const off = rowBase + x * 4;
      if (Math.round(0.299 * rgba[off] + 0.587 * rgba[off + 1] + 0.114 * rgba[off + 2]) > LUMA_THRESHOLD) {
        lit += 1;
        if (lit >= minLit) return true;
      }
    }
  }
  return false;
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
      if (darkHalfHasContent(rgba, width, height, start, end, resolvedAxis)) return true;
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

// ---------- 半格导出模式、单格切分、连续编号第 1 张判定与 g258 拍摄配方标签 ----------

export const HALF_WIDTH = 2088;
export const SEAM_X0 = 2088;
export const SEAM_X1 = 2199;
export const RIGHT_X0 = 2200;
export const STRIP_X0_JPG = 1100;
export const VALID_HALFFRAME_ORIENTATIONS: ReadonlySet<number> = new Set([1, 3, 6, 8]);

export type HalfframeMode = 'diptych' | 'singles' | 'both';
export type HalfframeRole = 'first' | 'second';
export type HalfframeSource = 'first-strip' | 'diptych-first' | 'diptych-second';

/** 桌面 web/assets/halfframe.js 的导出选项文案。 */
export const HALFFRAME_MODES: Array<[HalfframeMode, string]> = [
  ['diptych', '双联一张'],
  ['singles', '两张单格'],
  ['both', '都要'],
];

/** 桌面 web/assets/halfframe.js 的 PAIR_EXPORT_NOTE。 */
export const PAIR_EXPORT_NOTE = '双联和相机拼的一样：第 1 张在左（竖拿时在上），中间黑缝 112 像素。';

const SIZE_EDGES: Record<string, number | null> = {
  original: null,
  large: 3000,
  medium: 2048,
  small: 1600,
};

export function normalizedHalfframeMode(raw: unknown): HalfframeMode {
  return raw === 'singles' || raw === 'both' ? raw : 'diptych';
}

function fitEdge(w: number, h: number, edge: number | null): [number, number] {
  if (!edge || Math.max(w, h) <= edge) return [w, h];
  const k = edge / Math.max(w, h);
  return [Math.max(1, Math.round(w * k)), Math.max(1, Math.round(h * k))];
}

/** 桌面 web/assets/halfframe.js 的 singleHalfSize。 */
export function singleHalfSize(frame: { width: number; height: number } | null | undefined, sizeKey = 'original'): [number, number] {
  const portrait = frame && frame.width && frame.height ? frame.height > frame.width : false;
  const base: [number, number] = portrait ? [FULL_HEIGHT, HALF_WIDTH] : [HALF_WIDTH, FULL_HEIGHT];
  return fitEdge(base[0], base[1], SIZE_EDGES[sizeKey] ?? null);
}

/** 桌面 web/assets/halfframe.js 的 pairExportSizeText。 */
export function pairExportSizeText(
  frame: { width: number; height: number } | null | undefined,
  diptychSize: [number, number] | null | undefined,
  sizeKey: string,
  mode: HalfframeMode,
): string {
  const dip =
    diptychSize ??
    (frame && frame.width && frame.height
      ? fitEdge(frame.width, frame.height, SIZE_EDGES[sizeKey] ?? null)
      : fitEdge(FULL_WIDTH, FULL_HEIGHT, SIZE_EDGES[sizeKey] ?? null));
  const single = singleHalfSize(frame, sizeKey);
  if (mode === 'singles') return `${single[0]} × ${single[1]} 像素 · 2 个文件`;
  if (mode === 'both') return `${dip[0]} × ${dip[1]} + ${single[0]} × ${single[1]} 像素 · 3 个文件`;
  return `${dip[0]} × ${dip[1]} 像素 · 1 个文件`;
}

/** 一批照片按指定半格导出模式会产出几个文件。 */
export function halfframeFileCount(photos: ReadonlyArray<{ diptych?: boolean }>, mode: HalfframeMode | undefined): number {
  const m = normalizedHalfframeMode(mode);
  const perDiptych = m === 'both' ? 3 : m === 'singles' ? 2 : 1;
  let total = 0;
  for (const photo of photos) total += photo.diptych ? perDiptych : 1;
  return total;
}

/**
 * 从已按 EXIF Orientation 转正的双联 RGB 切出第 1 格或第 2 格（排除 112px 黑缝）。
 * g250 规则（ori 1/6 → 传感器左半为第 1 格，ori 3/8 → 传感器右半为第 1 格）转正后等价于：
 * - 横向 4288×2848：第 1 格为左列 0..2087，第 2 格为右列 2200..4287，产出 2088×2848；
 * - 竖向 2848×4288：第 1 格为上行 0..2087，第 2 格为下行 2200..4287，产出 2848×2088。
 */
export function sliceDiptychHalfRgb(
  rgb: Uint8Array,
  width: number,
  height: number,
  role: HalfframeRole,
): { data: Uint8Array; width: number; height: number } {
  if (width === FULL_WIDTH && height === FULL_HEIGHT) {
    const outW = HALF_WIDTH;
    const outH = FULL_HEIGHT;
    const x0 = role === 'first' ? 0 : RIGHT_X0;
    const out = new Uint8Array(outW * outH * 3);
    const rowBytes = outW * 3;
    const srcStride = width * 3;
    const xByte = x0 * 3;
    for (let y = 0; y < outH; y += 1) {
      const srcOff = y * srcStride + xByte;
      out.set(rgb.subarray(srcOff, srcOff + rowBytes), y * rowBytes);
    }
    return { data: out, width: outW, height: outH };
  }
  if (width === FULL_HEIGHT && height === FULL_WIDTH) {
    const outW = FULL_HEIGHT;
    const outH = HALF_WIDTH;
    const y0 = role === 'first' ? 0 : RIGHT_X0;
    const rowBytes = outW * 3;
    const start = y0 * rowBytes;
    const end = start + outH * rowBytes;
    return { data: rgb.slice(start, end), width: outW, height: outH };
  }
  throw new Error(`unexpected diptych RGB dimensions: ${width}x${height}`);
}

/**
 * 照桌面 halfframe_compose.camera_jpg_singles：从第 1 张相机 JPG（已按 EXIF 转正）切出中间半格条带。
 * 传感器列 1100..3187 关于 4288 对称（1100 + 2088 + 1100 = 4288），因此 ori 1/3/6/8 转正后均为：
 * - 横向 4288×2848：列 1100..3187，产出 2088×2848；
 * - 竖向 2848×4288：行 1100..3187，产出 2848×2088。
 */
export function sliceFirstHalfStripRgb(
  rgb: Uint8Array,
  width: number,
  height: number,
): { data: Uint8Array; width: number; height: number } {
  if (width === FULL_WIDTH && height === FULL_HEIGHT) {
    const outW = HALF_WIDTH;
    const outH = FULL_HEIGHT;
    const out = new Uint8Array(outW * outH * 3);
    const rowBytes = outW * 3;
    const srcStride = width * 3;
    const xByte = STRIP_X0_JPG * 3;
    for (let y = 0; y < outH; y += 1) {
      const srcOff = y * srcStride + xByte;
      out.set(rgb.subarray(srcOff, srcOff + rowBytes), y * rowBytes);
    }
    return { data: out, width: outW, height: outH };
  }
  if (width === FULL_HEIGHT && height === FULL_WIDTH) {
    const outW = FULL_HEIGHT;
    const outH = HALF_WIDTH;
    const rowBytes = outW * 3;
    const start = STRIP_X0_JPG * rowBytes;
    const end = start + outH * rowBytes;
    return { data: rgb.slice(start, end), width: outW, height: outH };
  }
  throw new Error(`unexpected first-half RGB dimensions: ${width}x${height}`);
}

function writeU16(bytes: Uint8Array, off: number, value: number, le: boolean): void {
  if (le) {
    bytes[off] = value & 0xff;
    bytes[off + 1] = (value >> 8) & 0xff;
  } else {
    bytes[off] = (value >> 8) & 0xff;
    bytes[off + 1] = value & 0xff;
  }
}

/**
 * 从相机 JPEG 的 APP1 Exif IFD0 移除 Copyright（0x8298）标签，保留其余全部 EXIF（机身、镜头、曝光、时间等）。
 * 第 1 张不在库里时用于第 1 格单格导出，避免把第 2 张的拍摄配方误标给第 1 格。
 */
export function stripExifCopyrightFromJpeg(bytes: Uint8Array): Uint8Array {
  if (bytes.length < 4 || bytes[0] !== 0xff || bytes[1] !== 0xd8) return bytes;
  let out: Uint8Array | null = null;
  let pos = 2;
  while (pos + 4 <= bytes.length) {
    if (bytes[pos] !== 0xff) break;
    while (pos < bytes.length && bytes[pos] === 0xff) pos += 1;
    if (pos >= bytes.length) break;
    const marker = bytes[pos++];
    if (marker === 0xda || marker === 0xd9) break;
    if (marker === 0x01 || (marker >= 0xd0 && marker <= 0xd7)) continue;
    const segLen = readU16(bytes, pos, false);
    if (segLen === null || segLen < 2 || pos + segLen > bytes.length) break;
    const payloadStart = pos + 2;
    const payloadEnd = pos + segLen;
    if (
      marker === 0xe1 &&
      payloadEnd - payloadStart >= 14 &&
      bytes[payloadStart] === 0x45 &&
      bytes[payloadStart + 1] === 0x78 &&
      bytes[payloadStart + 2] === 0x69 &&
      bytes[payloadStart + 3] === 0x66 &&
      bytes[payloadStart + 4] === 0x00 &&
      bytes[payloadStart + 5] === 0x00
    ) {
      const tiffBase = payloadStart + 6;
      const le = bytes[tiffBase] === 0x49 && bytes[tiffBase + 1] === 0x49;
      const be = bytes[tiffBase] === 0x4d && bytes[tiffBase + 1] === 0x4d;
      if ((le || be) && readU16(bytes, tiffBase + 2, le) === 42) {
        const ifdOff = readU32(bytes, tiffBase + 4, le);
        const ifdPos = ifdOff !== null ? tiffBase + ifdOff : -1;
        const count = ifdPos >= tiffBase ? readU16(bytes, ifdPos, le) : null;
        if (count !== null && count > 0 && ifdPos + 2 + count * 12 <= payloadEnd) {
          let hasCopyright = false;
          for (let i = 0; i < count; i += 1) {
            if (readU16(bytes, ifdPos + 2 + i * 12, le) === 0x8298) {
              hasCopyright = true;
              break;
            }
          }
          if (hasCopyright) {
            out ??= bytes.slice();
            let kept = 0;
            for (let i = 0; i < count; i += 1) {
              const src = ifdPos + 2 + i * 12;
              if (readU16(out, src, le) === 0x8298) continue;
              const dst = ifdPos + 2 + kept * 12;
              if (dst !== src) out.copyWithin(dst, src, src + 12);
              kept += 1;
            }
            const oldTail = ifdPos + 2 + count * 12;
            const newTail = ifdPos + 2 + kept * 12;
            const hasNext = oldTail + 4 <= payloadEnd;
            if (hasNext) out.copyWithin(newTail, oldTail, oldTail + 4);
            out.fill(0, newTail + (hasNext ? 4 : 0), oldTail + (hasNext ? 4 : 0));
            writeU16(out, ifdPos, kept, le);
          }
        }
      }
    }
    pos = payloadEnd;
  }
  return out ?? bytes;
}

// --- 连续编号相邻第 1 张（与桌面 halfframe_pairs.py 同规则） ---

const MPF_WIDTH = 640;
const MPF_HEIGHT = 424;
const MPF_LEFT_MAX = 163;
const MPF_RIGHT_MIN = 476;
const TRAILING_DIGITS = /(\d+)$/;
const CONFLICT_SUFFIX = /_[0-9a-f]{6,}$/;

function stripConflictSuffix(stem: string): string {
  const stripped = stem.replace(CONFLICT_SUFFIX, '');
  return stripped !== stem && TRAILING_DIGITS.test(stripped) ? stripped : stem;
}

/** 桌面 halfframe_pairs.neighbor_names：生成 ±delta 邻居文件名干（最多两个，去重保序）。 */
export function neighborNames(name: string | null | undefined, delta: number): string[] {
  if (!name) return [];
  const base = name.replace(/\.[^./\\]+$/, '');
  const stem = stripConflictSuffix(base);
  const m = TRAILING_DIGITS.exec(stem);
  if (!m) return [];
  const digits = m[1];
  const target = Number.parseInt(digits, 10) + delta;
  if (!Number.isFinite(target) || target < 0) return [];
  const prefix = stem.slice(0, m.index);
  const padded = `${prefix}${String(target).padStart(digits.length, '0')}`;
  const plain = `${prefix}${target}`;
  return padded === plain ? [padded] : [padded, plain];
}

/** 纯函数检查 MPF 640×424 预览 RGBA 是否符合 g250 第 1 张（两侧 0..163、476..639 全黑 ≤12，中间有内容）。 */
export function isFirstLikeRgba(rgba: Uint8Array | Uint8ClampedArray, width: number, height: number): boolean {
  if (width !== MPF_WIDTH || height !== MPF_HEIGHT || rgba.length < width * height * 4) return false;
  let midLit = false;
  for (let y = 0; y < height; y += 1) {
    const rowOff = y * width;
    for (let x = 0; x <= MPF_LEFT_MAX; x += 1) {
      const off = (rowOff + x) * 4;
      if (Math.round(0.299 * rgba[off] + 0.587 * rgba[off + 1] + 0.114 * rgba[off + 2]) > LUMA_THRESHOLD) return false;
    }
    for (let x = MPF_RIGHT_MIN; x < width; x += 1) {
      const off = (rowOff + x) * 4;
      if (Math.round(0.299 * rgba[off] + 0.587 * rgba[off + 1] + 0.114 * rgba[off + 2]) > LUMA_THRESHOLD) return false;
    }
    if (!midLit) {
      for (let x = MPF_LEFT_MAX + 1; x < MPF_RIGHT_MIN; x += 1) {
        const off = (rowOff + x) * 4;
        if (Math.round(0.299 * rgba[off] + 0.587 * rgba[off + 1] + 0.114 * rgba[off + 2]) > LUMA_THRESHOLD) {
          midLit = true;
          break;
        }
      }
    }
  }
  return midLit;
}

function readU16(bytes: Uint8Array, off: number, le: boolean): number | null {
  if (off < 0 || off + 2 > bytes.length) return null;
  return le ? bytes[off] + (bytes[off + 1] << 8) : (bytes[off] << 8) + bytes[off + 1];
}

function readU32(bytes: Uint8Array, off: number, le: boolean): number | null {
  if (off < 0 || off + 4 > bytes.length) return null;
  return le
    ? (bytes[off] | (bytes[off + 1] << 8) | (bytes[off + 2] << 16) | (bytes[off + 3] << 24)) >>> 0
    : ((bytes[off] << 24) | (bytes[off + 1] << 16) | (bytes[off + 2] << 8) | bytes[off + 3]) >>> 0;
}

/** 解析 JPEG 头部 APP2 MPF 段，找出第 2 帧（640×424 预览）在文件中的字节范围。 */
export function extractMpfSecondRange(head: Uint8Array): { offset: number; size: number } | null {
  if (head.length < 4 || head[0] !== 0xff || head[1] !== 0xd8) return null;
  let pos = 2;
  while (pos + 4 <= head.length) {
    if (head[pos] !== 0xff) return null;
    while (pos < head.length && head[pos] === 0xff) pos += 1;
    if (pos >= head.length) return null;
    const marker = head[pos++];
    if (marker === 0xda || marker === 0xd9) break;
    if (marker === 0x01 || (marker >= 0xd0 && marker <= 0xd7)) continue;
    const segLen = readU16(head, pos, false);
    if (segLen === null || segLen < 2 || pos + segLen > head.length) break;
    const payloadStart = pos + 2;
    const payloadEnd = pos + segLen;
    if (
      marker === 0xe2 &&
      payloadEnd - payloadStart >= 16 &&
      head[payloadStart] === 0x4d &&
      head[payloadStart + 1] === 0x50 &&
      head[payloadStart + 2] === 0x46 &&
      head[payloadStart + 3] === 0x00
    ) {
      const mpfBase = payloadStart + 4;
      const le = head[mpfBase] === 0x49 && head[mpfBase + 1] === 0x49;
      const be = head[mpfBase] === 0x4d && head[mpfBase + 1] === 0x4d;
      if (!le && !be) return null;
      if (readU16(head, mpfBase + 2, le) !== 42) return null;
      const ifdOff = readU32(head, mpfBase + 4, le);
      if (ifdOff === null) return null;
      const ifdPos = mpfBase + ifdOff;
      const count = readU16(head, ifdPos, le);
      if (count === null) return null;
      for (let i = 0; i < count; i += 1) {
        const entryPos = ifdPos + 2 + i * 12;
        if (entryPos + 12 > payloadEnd) break;
        const tag = readU16(head, entryPos, le);
        const bytesCount = readU32(head, entryPos + 4, le);
        const valOff = readU32(head, entryPos + 8, le);
        if (tag === 0xb002 && bytesCount !== null && bytesCount >= 32 && valOff !== null) {
          const entry1 = mpfBase + valOff + 16;
          if (entry1 + 16 > payloadEnd) return null;
          const size = readU32(head, entry1 + 4, le);
          const dataOff = readU32(head, entry1 + 8, le);
          if (size !== null && dataOff !== null && size > 0 && dataOff > 0) {
            return { offset: mpfBase + dataOff, size };
          }
        }
      }
    }
    pos = payloadEnd;
  }
  return null;
}

/** 浏览器侧判定相机 JPG Blob 是否为 g250 第 1 张（4288×2848 且 MPF 640×424 预览两侧全黑）。 */
export async function isFirstLikeBlob(blob: Blob): Promise<boolean> {
  if (typeof createImageBitmap !== 'function' || typeof OffscreenCanvas !== 'function') return false;
  const head = await readRange(blob, 0, PREFIX);
  if (!head) return false;
  const frame = jpegFrame(head);
  if (!frame || !unrotatedDimsMatch(frame).ok) return false;
  const range = extractMpfSecondRange(head);
  if (!range || range.offset + range.size > blob.size) return false;
  const slice = blob.slice(range.offset, range.offset + range.size, 'image/jpeg');
  let bm: ImageBitmap | null = null;
  try {
    bm = await createImageBitmap(slice, { imageOrientation: 'none', colorSpaceConversion: 'none', premultiplyAlpha: 'none' });
    if (bm.width !== MPF_WIDTH || bm.height !== MPF_HEIGHT) return false;
    const canvas = new OffscreenCanvas(MPF_WIDTH, MPF_HEIGHT);
    const ctx = canvas.getContext('2d');
    if (!ctx) return false;
    ctx.drawImage(bm, 0, 0, MPF_WIDTH, MPF_HEIGHT);
    const rgba = ctx.getImageData(0, 0, MPF_WIDTH, MPF_HEIGHT).data;
    return isFirstLikeRgba(rgba, MPF_WIDTH, MPF_HEIGHT);
  } catch {
    return false;
  } finally {
    bm?.close();
  }
}

const firstLikeCache = new WeakMap<Blob, Promise<boolean>>();
const orientationCache = new WeakMap<Blob, Promise<number | null>>();

export function cachedFirstLikeBlob(blob: Blob): Promise<boolean> {
  let pending = firstLikeCache.get(blob);
  if (!pending) {
    pending = isFirstLikeBlob(blob).catch(() => false);
    firstLikeCache.set(blob, pending);
  }
  return pending;
}

function cachedOrientation(blob: Blob): Promise<number | null> {
  let pending = orientationCache.get(blob);
  if (!pending) {
    pending = readFrameFromBlob(blob).then((f) => f?.orientation ?? null, () => null);
    orientationCache.set(blob, pending);
  }
  return pending;
}

export interface HalfframeNeighborCandidate {
  id: string;
  stem: string;
  diptych?: boolean;
  jpg: File | null;
  frame?: { orientation: number } | null;
  /** 所在的文件夹（知道的话）和拍摄时间：同名的第 1 张不止一张时用来挑（同一个文件夹的先，再挑拍摄时间最近的）。 */
  folder?: string;
  captureMs?: number | null;
}

/**
 * 找半格双联对应的第 1 张原片：与桌面 halfframe_pairs 同规则（编号 -1、非双联、EXIF 方向一致且在 {1,3,6,8}、
 * MPF 预览为 first-like）。供照片信息与单格导出共用。
 */
export async function findFirstHalfNeighbor<T extends HalfframeNeighborCandidate>(
  photo: T,
  photos: readonly T[],
  checkFirstLike: (blob: Blob) => Promise<boolean> = cachedFirstLikeBlob,
): Promise<T | null> {
  if (!photo.diptych || !photo.jpg) return null;
  const prevNames = new Set(neighborNames(photo.stem, -1).map((s) => s.toLowerCase()));
  if (!prevNames.size) return null;
  // 同名的另外留下了几张（2026-11-28）：同一个文件夹的先，再按拍摄时间离这张最近的排（和配 RAW+JPG 同一条规则）。只有一张时和以前一样。
  const near = (p: T) => ((p.folder ?? '') === (photo.folder ?? '') ? 0 : 1);
  const gap = (p: T) => (photo.captureMs != null && p.captureMs != null ? Math.abs(p.captureMs - photo.captureMs) : Number.POSITIVE_INFINITY);
  const candidates = photos
    .filter((p) => p.id !== photo.id && !p.diptych && p.jpg !== null && prevNames.has(p.stem.toLowerCase()))
    .sort((a, b) => near(a) - near(b) || gap(a) - gap(b));
  if (!candidates.length) return null;
  const dipOri = photo.frame?.orientation ?? (await cachedOrientation(photo.jpg));
  if (dipOri === null || !VALID_HALFFRAME_ORIENTATIONS.has(dipOri)) return null;
  for (const cand of candidates) {
    if (!cand.jpg) continue;
    const candOri = cand.frame?.orientation ?? (await cachedOrientation(cand.jpg));
    if (candOri !== dipOri) continue;
    if (await checkFirstLike(cand.jpg)) return cand;
  }
  return null;
}

// EXIF Copyright GXR1 拍摄配方标签解析与展示已独立到 recipe_tags.ts，此处保留转发以免改动旧调用方。
export { formatShootingRecipeTag, parseRecipeTag, type ParsedRecipeTag } from './recipe_tags.ts';
