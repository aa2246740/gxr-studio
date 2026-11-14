// 读 Mac 上导出的校准数据 zip。只认不压缩（stored）的条目；压缩、加密、ZIP64、名字带路径的都拒绝。
// 坏数据返回原因，不抛。所有偏移和长度都先查边界再读。

export type ZipFail = 'not_zip' | 'too_big' | 'compressed' | 'broken';

export type ZipResult = { ok: true; entries: Map<string, Uint8Array> } | { ok: false; reason: ZipFail; detail: string };

export const ZIP_LIMITS = {
  /** 整个文件。校准包只有几十 KB，留足余量。 */
  bytes: 64 << 20,
  entries: 64,
  entry: 64 << 20,
} as const;

const EOCD = 0x06054b50;
const CENTRAL = 0x02014b50;
const LOCAL = 0x04034b50;
const EOCD_SIZE = 22;
const CENTRAL_SIZE = 46;
const LOCAL_SIZE = 30;

let crcTable: Uint32Array | null = null;

function table(): Uint32Array {
  if (crcTable) return crcTable;
  const out = new Uint32Array(256);
  for (let n = 0; n < 256; n++) {
    let c = n;
    for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
    out[n] = c >>> 0;
  }
  crcTable = out;
  return out;
}

/** zlib 的 CRC-32。 */
export function crc32(bytes: Uint8Array): number {
  const t = table();
  let c = 0xffffffff;
  for (let i = 0; i < bytes.length; i++) c = t[(c ^ bytes[i]) & 0xff] ^ (c >>> 8);
  return (c ^ 0xffffffff) >>> 0;
}

function u16(b: Uint8Array, at: number): number {
  return b[at] | (b[at + 1] << 8);
}

function u32(b: Uint8Array, at: number): number {
  return (b[at] | (b[at + 1] << 8) | (b[at + 2] << 16)) + b[at + 3] * 0x1000000;
}

function fail(reason: ZipFail, detail: string): ZipResult {
  return { ok: false, reason, detail };
}

/** 名字只能是一层的文件名：没有 `/`、`\`、`..`，不是空的，也没有控制字符。 */
export function plainName(name: string): boolean {
  if (!name || name === '.' || name === '..' || name.length > 255) return false;
  for (let i = 0; i < name.length; i++) {
    const code = name.charCodeAt(i);
    if (code < 32 || code === 0x7f || name[i] === '/' || name[i] === '\\') return false;
  }
  return true;
}

function decodeName(bytes: Uint8Array): string | null {
  try {
    return new TextDecoder('utf-8', { fatal: true }).decode(bytes);
  } catch {
    return null;
  }
}

function findEnd(b: Uint8Array): number {
  const lowest = Math.max(0, b.length - EOCD_SIZE - 0xffff);
  for (let at = b.length - EOCD_SIZE; at >= lowest; at--) {
    if (u32(b, at) === EOCD && at + EOCD_SIZE + u16(b, at + 20) === b.length) return at;
  }
  return -1;
}

/** 读整个 zip。条目字节是原数组的视图，不复制。 */
export function readStoredZip(bytes: Uint8Array, limits: { bytes: number; entries: number; entry: number } = ZIP_LIMITS): ZipResult {
  try {
    if (bytes.length > limits.bytes) return fail('too_big', 'file');
    if (bytes.length < EOCD_SIZE) return fail('not_zip', 'short');
    const end = findEnd(bytes);
    if (end < 0) return fail('not_zip', 'no end record');
    const disk = u16(bytes, end + 4);
    const cdDisk = u16(bytes, end + 6);
    const onDisk = u16(bytes, end + 8);
    const count = u16(bytes, end + 10);
    const cdSize = u32(bytes, end + 12);
    const cdAt = u32(bytes, end + 16);
    if (disk !== 0 || cdDisk !== 0 || onDisk !== count) return fail('broken', 'multi-disk');
    if (count === 0xffff || cdSize === 0xffffffff || cdAt === 0xffffffff) return fail('broken', 'zip64');
    if (count > limits.entries) return fail('too_big', 'entries');
    if (cdAt + cdSize > end) return fail('broken', 'central directory outside file');
    const entries = new Map<string, Uint8Array>();
    let at = cdAt;
    for (let i = 0; i < count; i++) {
      if (at + CENTRAL_SIZE > cdAt + cdSize || u32(bytes, at) !== CENTRAL) return fail('broken', 'central entry');
      const flags = u16(bytes, at + 8);
      const method = u16(bytes, at + 10);
      const crc = u32(bytes, at + 16);
      const packed = u32(bytes, at + 20);
      const size = u32(bytes, at + 24);
      const nameLen = u16(bytes, at + 28);
      const extraLen = u16(bytes, at + 30);
      const commentLen = u16(bytes, at + 32);
      const localAt = u32(bytes, at + 42);
      const next = at + CENTRAL_SIZE + nameLen + extraLen + commentLen;
      if (next > cdAt + cdSize) return fail('broken', 'central entry length');
      const name = decodeName(bytes.subarray(at + CENTRAL_SIZE, at + CENTRAL_SIZE + nameLen));
      if (name === null || !plainName(name)) return fail('broken', 'name');
      if (flags & 1) return fail('broken', 'encrypted');
      if (method !== 0) return fail('compressed', name);
      if (packed === 0xffffffff || size === 0xffffffff) return fail('broken', 'zip64');
      if (packed !== size) return fail('broken', `size ${name}`);
      if (size > limits.entry) return fail('too_big', name);
      if (entries.has(name)) return fail('broken', `duplicate ${name}`);
      if (localAt + LOCAL_SIZE > cdAt || u32(bytes, localAt) !== LOCAL) return fail('broken', `local header ${name}`);
      if (u16(bytes, localAt + 8) !== 0) return fail('compressed', name);
      const dataAt = localAt + LOCAL_SIZE + u16(bytes, localAt + 26) + u16(bytes, localAt + 28);
      if (dataAt + size > cdAt) return fail('broken', `data ${name}`);
      const data = bytes.subarray(dataAt, dataAt + size);
      if (crc32(data) !== crc) return fail('broken', `crc ${name}`);
      entries.set(name, data);
      at = next;
    }
    return { ok: true, entries };
  } catch {
    return fail('broken', 'unreadable');
  }
}
