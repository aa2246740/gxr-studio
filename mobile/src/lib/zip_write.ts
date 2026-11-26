// 电脑上一批下载打成一个 ZIP（2026-11-26）：Safari 一次点击只放行 1 个下载，Chrome 最多 10 个。
// 不压缩（stored，方法 0），名字按 UTF-8（通用标志第 11 位），没有 ZIP64。ZIP 是 Blob 的各段拼起来的：
// 本地头 + 文件 Blob + … + 中央目录 + 结束记录，文件字节不拷进一整块大缓冲。
// 每个文件的 CRC-32 在它从导出 Worker 回来时就分片算（每片不超过 SLICE_MS），点「下载」时只拼 Blob，
// 这样点击里能同步触发下载，主线程每个文件也不会卡超过 50 ms。

/** 一包最多几个条目、多少字节（16 位条目数和 32 位偏移的上限以内，留了余量）。超过就拆成几包。 */
export const ZIP_MAX_ENTRIES = 65_000;
export const ZIP_MAX_BYTES = 3_500_000_000;

const LOCAL_SIZE = 30;
const CENTRAL_SIZE = 46;
const EOCD_SIZE = 22;
/** 拆包时每个条目按这么多额外字节估：两份头加两份名字（名字最多 255 个字，UTF-8 每字最多 4 字节）。 */
const ENTRY_OVERHEAD = LOCAL_SIZE + CENTRAL_SIZE + 2 * 1024;
const FLAG_UTF8 = 0x0800;
/** 「需要的版本」2.0；「生成的系统」3（Unix），这样外部属性里的 0644 权限 unzip / ditto 都认。 */
const VERSION_NEEDED = 20;
const VERSION_MADE_BY = (3 << 8) | 20;
const UNIX_FILE_0644 = (0o100644 << 16) >>> 0;

/** 每片最多算这么久就让出主线程。 */
const SLICE_MS = 8;
const BLOCK = 1 << 20;

const TABLE = (() => {
  const t = new Uint32Array(256 * 8);
  for (let n = 0; n < 256; n++) {
    let c = n;
    for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
    t[n] = c >>> 0;
  }
  for (let n = 0; n < 256; n++) {
    for (let s = 1; s < 8; s++) {
      const prev = t[(s - 1) * 256 + n];
      t[s * 256 + n] = (prev >>> 8) ^ t[prev & 0xff];
    }
  }
  return t;
})();

/** 接着算 CRC-32（slice-by-8）。`state` 是没取反的中间值，从 0xffffffff 开始。 */
function crcStep(state: number, b: Uint8Array): number {
  const t = TABLE;
  let c = state;
  let i = 0;
  const n = b.length;
  for (; i + 8 <= n; i += 8) {
    const a = c ^ (b[i] | (b[i + 1] << 8) | (b[i + 2] << 16) | (b[i + 3] << 24));
    c =
      t[1792 + (a & 0xff)] ^
      t[1536 + ((a >>> 8) & 0xff)] ^
      t[1280 + ((a >>> 16) & 0xff)] ^
      t[1024 + (a >>> 24)] ^
      t[768 + b[i + 4]] ^
      t[512 + b[i + 5]] ^
      t[256 + b[i + 6]] ^
      t[b[i + 7]];
  }
  for (; i < n; i++) c = t[(c ^ b[i]) & 0xff] ^ (c >>> 8);
  return c >>> 0;
}

/** zlib 的 CRC-32（和 lib/zip.ts 的 crc32 结果一样，快几倍）。 */
export function crc32(bytes: Uint8Array): number {
  return (crcStep(0xffffffff, bytes) ^ 0xffffffff) >>> 0;
}

interface CrcJob {
  file: Blob;
  bytes: Uint8Array;
  at: number;
  state: number;
}

const known = new WeakMap<Blob, number>();
const jobs: CrcJob[] = [];
let timer: ReturnType<typeof setTimeout> | null = null;

/** 量出来的数（开发服务器的测试读）：最长的一片多少毫秒，点击时补算了几个文件。 */
export const crcStats = { maxSliceMs: 0, finishedAtClick: 0, files: 0 };

function now(): number {
  return typeof performance !== 'undefined' ? performance.now() : Date.now();
}

function schedule(): void {
  if (timer === null && jobs.length) timer = setTimeout(pump, 0);
}

function done(job: CrcJob): void {
  known.set(job.file, (job.state ^ 0xffffffff) >>> 0);
  crcStats.files += 1;
}

function pump(): void {
  timer = null;
  const start = now();
  while (jobs.length && now() - start < SLICE_MS) {
    const job = jobs[0];
    const end = Math.min(job.bytes.length, job.at + BLOCK);
    job.state = crcStep(job.state, job.bytes.subarray(job.at, end));
    job.at = end;
    if (end >= job.bytes.length) {
      jobs.shift();
      done(job);
    }
  }
  crcStats.maxSliceMs = Math.max(crcStats.maxSliceMs, now() - start);
  schedule();
}

/**
 * 文件刚做好：记下它的字节，空闲时分片算 CRC。`bytes` 必须就是 `file` 里的字节（`new File([bytes])` 复制了一份，
 * 这份原件只留到 CRC 算完）。
 */
export function trackCrc(file: Blob, bytes: ArrayBuffer | Uint8Array): void {
  if (known.has(file) || jobs.some((j) => j.file === file)) return;
  const view = bytes instanceof Uint8Array ? bytes : new Uint8Array(bytes);
  if (view.length !== file.size) return;
  jobs.push({ file, bytes: view, at: 0, state: 0xffffffff });
  schedule();
}

/** 这个文件的 CRC。还没算完就在这里同步算完（点击里用，剩下的通常很少）；从没记过的文件是 null。 */
export function crcOf(file: Blob): number | null {
  const hit = known.get(file);
  if (hit !== undefined) return hit;
  const index = jobs.findIndex((j) => j.file === file);
  if (index < 0) return null;
  const [job] = jobs.splice(index, 1);
  job.state = crcStep(job.state, job.bytes.subarray(job.at));
  job.at = job.bytes.length;
  done(job);
  crcStats.finishedAtClick += 1;
  return known.get(file) ?? null;
}

export interface ZipEntry {
  name: string;
  file: Blob;
  crc: number;
}

function dosTime(when: Date): { time: number; date: number } {
  const year = Math.min(2107, Math.max(1980, when.getFullYear()));
  return {
    time: (when.getHours() << 11) | (when.getMinutes() << 5) | (when.getSeconds() >> 1),
    date: ((year - 1980) << 9) | ((when.getMonth() + 1) << 5) | when.getDate(),
  };
}

/**
 * 拼一个不压缩的 ZIP。空的、超过 ZIP_MAX_ENTRIES 个条目或 ZIP_MAX_BYTES 字节的都拒绝（抛 RangeError），
 * 调用方先用 `zipParts` 拆好。条目的字节就是原来那个 Blob，不改不复制。
 */
export function zipBlob(entries: readonly ZipEntry[], when: Date = new Date()): Blob {
  if (!entries.length) throw new RangeError('zip: no entries');
  if (entries.length > ZIP_MAX_ENTRIES) throw new RangeError('zip: too many entries');
  const enc = new TextEncoder();
  const { time, date } = dosTime(when);
  const parts: BlobPart[] = [];
  const central: Uint8Array<ArrayBuffer>[] = [];
  let offset = 0;
  for (const entry of entries) {
    const name = enc.encode(entry.name);
    if (!name.length || name.length > 0xffff) throw new RangeError('zip: bad name');
    const size = entry.file.size;
    const local = new Uint8Array(LOCAL_SIZE + name.length);
    const lv = new DataView(local.buffer);
    lv.setUint32(0, 0x04034b50, true);
    lv.setUint16(4, VERSION_NEEDED, true);
    lv.setUint16(6, FLAG_UTF8, true);
    lv.setUint16(8, 0, true);
    lv.setUint16(10, time, true);
    lv.setUint16(12, date, true);
    lv.setUint32(14, entry.crc >>> 0, true);
    lv.setUint32(18, size, true);
    lv.setUint32(22, size, true);
    lv.setUint16(26, name.length, true);
    lv.setUint16(28, 0, true);
    local.set(name, LOCAL_SIZE);

    const cd = new Uint8Array(CENTRAL_SIZE + name.length);
    const cv = new DataView(cd.buffer);
    cv.setUint32(0, 0x02014b50, true);
    cv.setUint16(4, VERSION_MADE_BY, true);
    cv.setUint16(6, VERSION_NEEDED, true);
    cv.setUint16(8, FLAG_UTF8, true);
    cv.setUint16(10, 0, true);
    cv.setUint16(12, time, true);
    cv.setUint16(14, date, true);
    cv.setUint32(16, entry.crc >>> 0, true);
    cv.setUint32(20, size, true);
    cv.setUint32(24, size, true);
    cv.setUint16(28, name.length, true);
    cv.setUint32(38, UNIX_FILE_0644, true);
    cv.setUint32(42, offset, true);
    cd.set(name, CENTRAL_SIZE);

    parts.push(local, entry.file);
    central.push(cd);
    offset += local.length + size;
    if (offset > ZIP_MAX_BYTES) throw new RangeError('zip: too big');
  }
  const cdSize = central.reduce((sum, c) => sum + c.length, 0);
  const end = new Uint8Array(EOCD_SIZE);
  const ev = new DataView(end.buffer);
  ev.setUint32(0, 0x06054b50, true);
  ev.setUint16(8, entries.length, true);
  ev.setUint16(10, entries.length, true);
  ev.setUint32(12, cdSize, true);
  ev.setUint32(16, offset, true);
  return new Blob([...parts, ...central, end], { type: 'application/zip' });
}

/** 按顺序拆成几包：每包不超过 `maxEntries` 个文件、大约 `maxBytes` 字节（头和名字按上限估）。返回每包的下标。 */
export function zipParts(sizes: readonly number[], maxEntries = ZIP_MAX_ENTRIES, maxBytes = ZIP_MAX_BYTES): number[][] {
  const out: number[][] = [];
  let cur: number[] = [];
  let bytes = EOCD_SIZE;
  const most = Math.max(1, Math.min(ZIP_MAX_ENTRIES, Math.floor(maxEntries)));
  sizes.forEach((size, i) => {
    const add = size + ENTRY_OVERHEAD;
    if (cur.length && (cur.length >= most || bytes + add > maxBytes)) {
      out.push(cur);
      cur = [];
      bytes = EOCD_SIZE;
    }
    cur.push(i);
    bytes += add;
  });
  if (cur.length) out.push(cur);
  return out;
}

/** ZIP 的文件名，只用 ASCII：`GXR-export-20261126-2340-11.zip`（本地时间，文件个数）。 */
export function zipName(count: number, when: Date = new Date()): string {
  const p = (n: number) => String(n).padStart(2, '0');
  const day = `${when.getFullYear()}${p(when.getMonth() + 1)}${p(when.getDate())}`;
  return `GXR-export-${day}-${p(when.getHours())}${p(when.getMinutes())}-${count}.zip`;
}

/**
 * 同一个 ZIP 里名字不能重复（解压时后一个会盖掉前一个）：重名的第二个起改成 `名字 (2).jpg`，和浏览器重名下载的改法一样。
 * 名字不重复时原样不动。
 */
export function uniqueNames(names: readonly string[]): string[] {
  const taken = new Set(names);
  const seen = new Set<string>();
  return names.map((name) => {
    if (!seen.has(name)) {
      seen.add(name);
      return name;
    }
    const dot = name.lastIndexOf('.');
    const stem = dot > 0 ? name.slice(0, dot) : name;
    const ext = dot > 0 ? name.slice(dot) : '';
    let n = 2;
    while (taken.has(`${stem} (${n})${ext}`)) n += 1;
    const next = `${stem} (${n})${ext}`;
    taken.add(next);
    seen.add(next);
    return next;
  });
}

/** 这几个文件的 ZIP（名字和字节与逐个下载时一样）。有文件的 CRC 不知道（不是导出记下的）就是 null。 */
export function zipOf(files: readonly File[], when: Date = new Date()): { blob: Blob; name: string } | null {
  if (files.length < 2) return null;
  const names = uniqueNames(files.map((f) => f.name));
  const entries: ZipEntry[] = [];
  for (const [i, file] of files.entries()) {
    const crc = crcOf(file);
    if (crc === null) return null;
    entries.push({ name: names[i], file, crc });
  }
  return { blob: zipBlob(entries, when), name: zipName(files.length, when) };
}
