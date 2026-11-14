// 只读 JPEG APP1 / TIFF IFD 里的拍摄时间，以及 DNG 内嵌预览的位置。
// 坏数据返回 null，不抛。调用方只把文件开头的一小段传进来。

export interface CaptureTime {
  ms: number;
}

export interface ByteRange {
  offset: number;
  length: number;
}

const DATE = /^(\d{4}):(\d{2}):(\d{2}) (\d{2}):(\d{2}):(\d{2})$/;
const MAX_IFD = 8;
const MAX_ENTRIES = 256;
const MAX_EMBEDDED = 8 << 20;

function u16(bytes: Uint8Array, offset: number, le: boolean): number | null {
  if (offset < 0 || offset + 2 > bytes.length) return null;
  const a = bytes[offset];
  const b = bytes[offset + 1];
  return le ? a + b * 256 : b + a * 256;
}

function u32(bytes: Uint8Array, offset: number, le: boolean): number | null {
  if (offset < 0 || offset + 4 > bytes.length) return null;
  const a = bytes[offset];
  const b = bytes[offset + 1];
  const c = bytes[offset + 2];
  const d = bytes[offset + 3];
  return le ? a + b * 256 + c * 65536 + d * 16777216 : d + c * 256 + b * 65536 + a * 16777216;
}

function ascii(bytes: Uint8Array, offset: number, count: number): string | null {
  if (count <= 0 || count > 64 || offset < 0 || offset + count > bytes.length) return null;
  let text = '';
  for (let i = 0; i < count; i++) {
    const code = bytes[offset + i];
    if (code === 0) break;
    if (code < 32 || code > 126) return null;
    text += String.fromCharCode(code);
  }
  return text;
}

/** EXIF 的 `YYYY:MM:DD HH:MM:SS`，亚秒最多取 3 位。溢出的日期（比如 2 月 31 日）不算。 */
export function captureMillis(text: string, sub: string | null): number | null {
  const match = DATE.exec(text);
  if (!match) return null;
  const year = Number(match[1]);
  const month = Number(match[2]);
  const day = Number(match[3]);
  const hour = Number(match[4]);
  const minute = Number(match[5]);
  const second = Number(match[6]);
  if (month < 1 || month > 12 || day < 1 || day > 31 || hour > 23 || minute > 59 || second > 60) return null;
  let millis = 0;
  if (sub) {
    const digits = sub.replace(/\D/g, '').slice(0, 3);
    if (digits) millis = Number(digits.padEnd(3, '0'));
    if (!Number.isFinite(millis)) return null;
  }
  const date = new Date(year, month - 1, day, hour, minute, second, millis);
  if (date.getFullYear() !== year || date.getMonth() !== month - 1 || date.getDate() !== day) return null;
  const ms = date.getTime();
  return Number.isFinite(ms) ? ms : null;
}

interface Found {
  original: string | null;
  subOriginal: string | null;
  dateTime: string | null;
  embedded: ByteRange | null;
  /** 缩小尺寸、JPEG 压缩的单条带（GXR 的 DNG 把 640×480 预览放在第二个 SubIFD 里）。 */
  strips: ByteRange[];
}

function typeSize(type: number): number | null {
  if (type === 1 || type === 2 || type === 6 || type === 7) return 1;
  if (type === 3 || type === 8) return 2;
  if (type === 4 || type === 9) return 4;
  if (type === 5 || type === 10) return 8;
  return null;
}

function inlineValue(bytes: Uint8Array, entry: number, size: number, count: number, le: boolean): { offset: number } | null {
  const inline = size * count;
  if (inline <= 4) return { offset: entry + 8 };
  const pointed = u32(bytes, entry + 8, le);
  if (pointed === null) return null;
  return { offset: pointed };
}

function readAsciiTag(bytes: Uint8Array, entry: number, count: number, le: boolean): string | null {
  const place = inlineValue(bytes, entry, 1, count, le);
  if (!place) return null;
  return ascii(bytes, place.offset, count);
}

function noteEmbedded(found: Found, offset: number | null, length: number | null): void {
  const prev = found.embedded;
  if (prev && prev.offset >= 0 && prev.length > 0) return;
  found.embedded = { offset: offset ?? prev?.offset ?? -1, length: length ?? prev?.length ?? 0 };
}

function walk(bytes: Uint8Array, le: boolean, ifd: number, depth: number, found: Found): void {
  if (depth > MAX_IFD || ifd < 0 || ifd + 2 > bytes.length) return;
  const count = u16(bytes, ifd, le);
  if (count === null || count > MAX_ENTRIES) return;
  const exifAt: number[] = [];
  const subAt: number[] = [];
  let subfile: number | null = null;
  let compression: number | null = null;
  let photometric: number | null = null;
  let stripOffset: number | null = null;
  let stripLength: number | null = null;
  for (let i = 0; i < count; i++) {
    const entry = ifd + 2 + i * 12;
    if (entry + 12 > bytes.length) return;
    const tag = u16(bytes, entry, le);
    const type = u16(bytes, entry + 2, le);
    const n = u32(bytes, entry + 4, le);
    if (tag === null || type === null || n === null) return;
    const size = typeSize(type);
    if (size === null || n > MAX_ENTRIES) continue;
    if (tag === 0x0132 && type === 2) found.dateTime = readAsciiTag(bytes, entry, n, le);
    if (tag === 0x9003 && type === 2) found.original = readAsciiTag(bytes, entry, n, le);
    if (tag === 0x9291 && type === 2) found.subOriginal = readAsciiTag(bytes, entry, n, le);
    if (tag === 0x8769 && type === 4 && n >= 1) {
      const place = inlineValue(bytes, entry, 4, n, le);
      const next = place ? u32(bytes, place.offset, le) : null;
      if (next !== null) exifAt.push(next);
    }
    if (tag === 0x014a && type === 4 && n >= 1 && n <= 8) {
      const place = inlineValue(bytes, entry, 4, n, le);
      if (place) {
        for (let s = 0; s < n; s++) {
          const next = u32(bytes, place.offset + s * 4, le);
          if (next !== null) subAt.push(next);
        }
      }
    }
    if (tag === 0x0201 && type === 4 && n === 1) noteEmbedded(found, u32(bytes, entry + 8, le), null);
    if (tag === 0x0202 && type === 4 && n === 1) noteEmbedded(found, null, u32(bytes, entry + 8, le));
    if (n === 1 && (type === 3 || type === 4)) {
      const value = type === 3 ? u16(bytes, entry + 8, le) : u32(bytes, entry + 8, le);
      if (tag === 0x00fe) subfile = value;
      if (tag === 0x0103) compression = value;
      if (tag === 0x0106) photometric = value;
      if (tag === 0x0111) stripOffset = value;
      if (tag === 0x0117) stripLength = value;
    }
  }
  const reduced = subfile !== null && (subfile & 1) === 1;
  const jpeg = compression === 6 || compression === 7;
  const picture = photometric !== 32803 && photometric !== 34892;
  if (reduced && jpeg && picture && stripOffset !== null && stripLength !== null && stripLength > 0 && found.strips.length < MAX_IFD) {
    found.strips.push({ offset: stripOffset, length: stripLength });
  }
  for (const next of exifAt) walk(bytes, le, next, depth + 1, found);
  for (const next of subAt) walk(bytes, le, next, depth + 1, found);
  const nextPtr = ifd + 2 + count * 12;
  const next = u32(bytes, nextPtr, le);
  if (next) walk(bytes, le, next, depth + 1, found);
}

function fromTiff(bytes: Uint8Array): Found | null {
  if (bytes.length < 8) return null;
  const le = bytes[0] === 0x49 && bytes[1] === 0x49;
  const be = bytes[0] === 0x4d && bytes[1] === 0x4d;
  if (!le && !be) return null;
  if (u16(bytes, 2, le) !== 42) return null;
  const first = u32(bytes, 4, le);
  if (first === null) return null;
  const found: Found = { original: null, subOriginal: null, dateTime: null, embedded: null, strips: [] };
  walk(bytes, le, first, 0, found);
  return found;
}

function timeOf(found: Found): CaptureTime | null {
  const original = found.original ? captureMillis(found.original, found.subOriginal) : null;
  if (original !== null) return { ms: original };
  const plain = found.dateTime ? captureMillis(found.dateTime, null) : null;
  return plain === null ? null : { ms: plain };
}

/** JPEG 开头一段里的 APP1。没有 Exif、截断、标记坏了，都返回 null。 */
export function captureTimeFromJpeg(bytes: Uint8Array): CaptureTime | null {
  try {
    if (bytes.length < 4 || bytes[0] !== 0xff || bytes[1] !== 0xd8) return null;
    let i = 2;
    while (i + 4 <= bytes.length) {
      if (bytes[i] !== 0xff) return null;
      while (i < bytes.length && bytes[i] === 0xff) i += 1;
      if (i >= bytes.length) return null;
      const marker = bytes[i];
      i += 1;
      if (marker === 0xd9 || marker === 0xda) return null;
      const length = u16(bytes, i, false);
      if (length === null || length < 2 || i + length > bytes.length) return null;
      if (marker === 0xe1 && length >= 8) {
        const head = ascii(bytes, i + 2, 6);
        if (head === 'Exif') {
          const tiff = bytes.subarray(i + 8, i + length);
          const found = fromTiff(tiff);
          if (found) {
            const time = timeOf(found);
            if (time) return time;
          }
        }
      }
      i += length;
    }
    return null;
  } catch {
    return null;
  }
}

/** DNG / TIFF 开头一段。日期在 IFD 里；不在这段里就当没有。 */
export function captureTimeFromTiff(bytes: Uint8Array): CaptureTime | null {
  try {
    const found = fromTiff(bytes);
    return found ? timeOf(found) : null;
  } catch {
    return null;
  }
}

/** 内嵌预览 JPEG 的文件偏移。长度或偏移不可信时返回 null，调用方再去读那一段。 */
export function embeddedJpegRange(bytes: Uint8Array): ByteRange | null {
  try {
    const found = fromTiff(bytes);
    if (!found) return null;
    const candidates = [found.embedded, ...found.strips].filter(
      (range): range is ByteRange =>
        range !== null && range.offset > 0 && range.length > 0 && range.length <= MAX_EMBEDDED && range.offset <= 0xffffffff - range.length,
    );
    if (!candidates.length) return null;
    // 有几份时取最大的那份：缩略图要够清楚。
    return candidates.reduce((best, range) => (range.length > best.length ? range : best));
  } catch {
    return null;
  }
}

/**
 * DNG 的结构检查，在 Worker 里做，不看有没有显卡。返回 null 表示结构可用，否则是原因（只给日志和测试看）。
 * `bytes` 是文件开头一段，`fileSize` 是整个文件的大小。IFD 落在这一段之外、但没超出文件时不判坏：
 * 读不到就不下结论，留给 WASM 的完整描述去判。
 */
export function checkDng(bytes: Uint8Array, fileSize: number): string | null {
  try {
    if (bytes.length < 8 || fileSize < 8) return 'too short';
    const le = bytes[0] === 0x49 && bytes[1] === 0x49;
    const be = bytes[0] === 0x4d && bytes[1] === 0x4d;
    if (!le && !be) return 'byte order';
    if (u16(bytes, 2, le) !== 42) return 'magic';
    const first = u32(bytes, 4, le);
    if (first === null || first < 8 || first + 2 > fileSize) return 'ifd0 offset';
    const ifd0 = readIfd(bytes, fileSize, le, first);
    if (typeof ifd0 === 'string') return ifd0;
    if (!ifd0) return null;
    if (!ifd0.tags.has(0xc612)) return 'no DNGVersion';
    for (const sub of ifd0.subs) {
      if (sub < 8 || sub + 2 > fileSize) return 'subifd offset';
      const row = readIfd(bytes, fileSize, le, sub);
      if (typeof row === 'string') return row;
    }
    return null;
  } catch {
    return 'unreadable';
  }
}

/** 读一个 IFD 的条目。超出文件返回原因；落在开头这段之外返回 null（不下结论）。 */
function readIfd(bytes: Uint8Array, fileSize: number, le: boolean, at: number): { tags: Set<number>; subs: number[] } | string | null {
  if (at + 2 > bytes.length) return null;
  const count = u16(bytes, at, le);
  if (count === null || count === 0 || count > MAX_ENTRIES) return 'ifd entry count';
  const end = at + 2 + count * 12 + 4;
  if (end > fileSize) return 'truncated ifd';
  if (end > bytes.length) return null;
  const tags = new Set<number>();
  const subs: number[] = [];
  for (let i = 0; i < count; i++) {
    const entry = at + 2 + i * 12;
    const tag = u16(bytes, entry, le);
    const type = u16(bytes, entry + 2, le);
    const n = u32(bytes, entry + 4, le);
    if (tag === null || type === null || n === null) return 'truncated ifd';
    const size = type === 13 ? 4 : typeSize(type);
    if (size === null) return 'entry type';
    const total = size * n;
    if (total > 4) {
      const pointed = u32(bytes, entry + 8, le);
      if (pointed === null || pointed + total > fileSize) return 'value outside file';
    }
    tags.add(tag);
    if (tag === 0x014a && (type === 4 || type === 13) && n >= 1 && n <= 8) {
      const base = total > 4 ? u32(bytes, entry + 8, le) : entry + 8;
      for (let k = 0; base !== null && k < n; k++) {
        const sub = u32(bytes, base + k * 4, le);
        if (sub !== null) subs.push(sub);
      }
    }
  }
  return { tags, subs };
}
