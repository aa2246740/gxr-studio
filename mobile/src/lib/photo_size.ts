// 照片的宽高，和桌面图库存的一样（library/store.py：`photo_meta(jpg).width or photo_meta(dng).width`）。
// JPG：帧头（SOF）的宽高，按 EXIF 方向转正（meta.read_jpeg_meta）。DNG：第一个 NewSubFileType 为 0 的子 IFD 的
// DefaultCropSize，没有就用它的宽高；没有这样的子 IFD 用 IFD0 的宽高；再按 IFD0 的方向转正（meta.read_dng_meta）。
// 只读文件头；坏数据返回 null，不抛。导出时交给 export_prepare 的 shot（标签里的比例文字要用）。

export interface Size {
  width: number;
  height: number;
}

export interface Frame {
  orientation: number;
  width: number;
  height: number;
}

const MAX_ENTRIES = 512;

function u16(b: Uint8Array, at: number, le: boolean): number | null {
  if (at < 0 || at + 2 > b.length) return null;
  return le ? b[at] + b[at + 1] * 256 : b[at] * 256 + b[at + 1];
}

function u32(b: Uint8Array, at: number, le: boolean): number | null {
  if (at < 0 || at + 4 > b.length) return null;
  const [p, q, r, s] = le ? [b[at], b[at + 1], b[at + 2], b[at + 3]] : [b[at + 3], b[at + 2], b[at + 1], b[at]];
  return p + q * 256 + r * 65536 + s * 16777216;
}

const TYPE_SIZE: Record<number, number> = { 1: 1, 3: 2, 4: 4, 5: 8, 7: 1, 9: 4, 10: 8, 13: 4, 16: 8 };

interface Entry {
  type: number;
  count: number;
  /** 值所在的偏移（放得下 4 字节就在条目里）。 */
  at: number;
}

/** 一个 IFD 的条目，按标签号。越界就停，不抛。 */
function readIfd(b: Uint8Array, offset: number, le: boolean): Map<number, Entry> | null {
  const n = u16(b, offset, le);
  if (n === null || n > MAX_ENTRIES) return null;
  const out = new Map<number, Entry>();
  for (let i = 0; i < n; i++) {
    const e = offset + 2 + i * 12;
    const tag = u16(b, e, le);
    const type = u16(b, e + 2, le);
    const count = u32(b, e + 4, le);
    if (tag === null || type === null || count === null) return out;
    const size = TYPE_SIZE[type];
    if (!size) continue;
    const total = size * count;
    const at = total <= 4 ? e + 8 : u32(b, e + 8, le);
    if (at === null || at + total > b.length) continue;
    if (!out.has(tag)) out.set(tag, { type, count, at });
  }
  return out;
}

/** 第 `i` 个整数值（SHORT / LONG / BYTE），有理数取 int(分子 / 分母)，和 Pillow 的 int(IFDRational) 一样截断。 */
function num(b: Uint8Array, e: Entry | undefined, i: number, le: boolean): number | null {
  if (!e || i >= e.count) return null;
  if (e.type === 3) return u16(b, e.at + i * 2, le);
  if (e.type === 4 || e.type === 13) return u32(b, e.at + i * 4, le);
  if (e.type === 1 || e.type === 7) return b[e.at + i] ?? null;
  if (e.type === 5) {
    const p = u32(b, e.at + i * 8, le);
    const q = u32(b, e.at + i * 8 + 4, le);
    if (p === null || q === null || q === 0) return null;
    return Math.trunc(p / q);
  }
  return null;
}

/** `meta._orientation`：1..8 之外都当 1。 */
function orientation(value: number | null): number {
  return value !== null && value >= 1 && value <= 8 ? value : 1;
}

/** `meta._oriented`。 */
export function oriented(width: number, height: number, orient: number): Size {
  return orient >= 5 && orient <= 8 ? { width: height, height: width } : { width, height };
}

function tiffHeader(b: Uint8Array): { le: boolean; ifd0: number } | null {
  if (b.length < 8) return null;
  const le = b[0] === 0x49 && b[1] === 0x49;
  if (!le && !(b[0] === 0x4d && b[1] === 0x4d)) return null;
  if (u16(b, 2, le) !== 42) return null;
  const ifd0 = u32(b, 4, le);
  return ifd0 === null ? null : { le, ifd0 };
}

const SOF = new Set([0xc0, 0xc1, 0xc2, 0xc3, 0xc5, 0xc6, 0xc7, 0xc9, 0xca, 0xcb, 0xcd, 0xce, 0xcf]);

/** 相机 JPG：方向标签（1..8）加按它转正后的 SOF 宽高（桌面 side.frame 的形状）。 */
export function jpegFrame(b: Uint8Array): Frame | null {
  try {
    if (b.length < 4 || b[0] !== 0xff || b[1] !== 0xd8) return null;
    let orient = 1;
    let i = 2;
    while (i + 4 <= b.length) {
      if (b[i] !== 0xff) return null;
      while (i < b.length && b[i] === 0xff) i += 1;
      const marker = b[i];
      i += 1;
      if (marker === 0xd9 || marker === 0xda) return null;
      if (marker === 0x01 || (marker >= 0xd0 && marker <= 0xd7)) continue;
      const length = u16(b, i, false);
      if (length === null || length < 2 || i + length > b.length) return null;
      if (marker === 0xe1 && length >= 16 && b[i + 2] === 0x45 && b[i + 3] === 0x78 && b[i + 4] === 0x69 && b[i + 5] === 0x66) {
        const tiff = b.subarray(i + 8, i + length);
        const head = tiffHeader(tiff);
        const ifd = head ? readIfd(tiff, head.ifd0, head.le) : null;
        if (head && ifd) orient = orientation(num(tiff, ifd.get(274), 0, head.le));
      }
      if (SOF.has(marker)) {
        const h = u16(b, i + 3, false);
        const w = u16(b, i + 5, false);
        if (h === null || w === null || !h || !w) return null;
        const size = oriented(w, h, orient);
        return { orientation: orient, width: size.width, height: size.height };
      }
      i += length;
    }
    return null;
  } catch {
    return null;
  }
}

/** 相机 JPG：SOF 的宽高，按 APP1 里 IFD0 的方向转正。 */
export function jpegSize(b: Uint8Array): Size | null {
  const frame = jpegFrame(b);
  return frame ? { width: frame.width, height: frame.height } : null;
}

/** DNG：IFD0 的方向（1..8）加转正后的 DefaultCropSize（或宽高），和桌面 side.frame 一样。 */
export function dngFrame(b: Uint8Array): Frame | null {
  try {
    const head = tiffHeader(b);
    if (!head) return null;
    const { le } = head;
    const ifd0 = readIfd(b, head.ifd0, le);
    if (!ifd0) return null;
    let width = num(b, ifd0.get(256), 0, le);
    let height = num(b, ifd0.get(257), 0, le);
    const subs = ifd0.get(330);
    for (let k = 0; subs && k < Math.min(subs.count, 64); k++) {
      const off = num(b, subs, k, le);
      const sub = off === null ? null : readIfd(b, off, le);
      if (!sub) continue;
      const kind = num(b, sub.get(254), 0, le) ?? 0;
      const sw = num(b, sub.get(256), 0, le);
      const sh = num(b, sub.get(257), 0, le);
      if (kind !== 0 || !sw || !sh) continue;
      const crop = sub.get(50720);
      const cw = crop && crop.count === 2 ? num(b, crop, 0, le) : null;
      const ch = crop && crop.count === 2 ? num(b, crop, 1, le) : null;
      if (cw !== null && ch !== null) {
        width = cw;
        height = ch;
      } else {
        width = sw;
        height = sh;
      }
      break;
    }
    if (!width || !height) return null;
    const orient = orientation(num(b, ifd0.get(274), 0, le));
    const size = oriented(width, height, orient);
    return { orientation: orient, width: size.width, height: size.height };
  } catch {
    return null;
  }
}

/** DNG：第一个整幅子 IFD 的 DefaultCropSize（或宽高），按 IFD0 的方向转正。 */
export function dngSize(b: Uint8Array): Size | null {
  const frame = dngFrame(b);
  return frame ? { width: frame.width, height: frame.height } : null;
}

/** 桌面图库的规则：先看相机 JPG（读不了就当没有），没有再看 DNG。都没有是 null。 */
export function shotFrame(jpg: Uint8Array | null, dng: Uint8Array | null): Frame | null {
  return (jpg ? jpegFrame(jpg) : null) ?? (dng ? dngFrame(dng) : null);
}

/** 桌面图库的规则：先看相机 JPG（读不了就当没有），没有再看 DNG。都没有是 null（shot 里不写）。 */
export function shotSize(jpg: Uint8Array | null, dng: Uint8Array | null): Size | null {
  const frame = shotFrame(jpg, dng);
  return frame ? { width: frame.width, height: frame.height } : null;
}
