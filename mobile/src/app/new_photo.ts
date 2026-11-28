// 选进来的照片怎么落下：新的一张（还没读过字节，调整全是默认值），以及同名照片（机主 2026-11-28 定）。
// - 同一份（大小一样，头尾各 64 KiB 也一样；不看修改时间）：替换，照旧加 version，调整留着。
// - 原来那张好好的（缩略图好了、文件读得到、没在往显卡里传），新文件又不是同一份：两张都留下，新的叫「名字 (2)」。
// - 原来那张坏了、卡拔了、还在读：照旧替换，「重新选这张」照常能用。
// - 同名的 RAW 和 JPG 先配同一个文件夹里的（webkitRelativePath），否则按拍摄时间配。
// 第一份的 id 还是文件名小写（和以前一样）；另外留下的那份是「文件名小写~大小-头尾摘要」，由文件本身决定。

import { zeroGeometry } from '../lib/crop';
import { captureTimeFromJpeg, captureTimeFromTiff } from '../lib/exif';
import { DEFAULT_FRAME, DEFAULT_STAMP } from '../lib/frame_spec';
import { classify } from '../lib/group';
import { clearCameraRecipe, photoName, releaseLooks, zeroKnobs, type Photo } from './model';

export function newPhoto(id: string, stem: string, lastModified: number, copy?: number): Photo {
  return {
    id,
    stem,
    version: 0,
    dng: null,
    jpg: null,
    diptych: false,
    lastModified,
    captureMs: null,
    status: 'pending',
    gone: false,
    cached: null,
    checked: null,
    bitmap: null,
    neutral: null,
    knobs: zeroKnobs(),
    curve: null,
    rev: 0,
    jpgMeta: null,
    dngMeta: null,
    border: { ...DEFAULT_FRAME },
    stamp: { ...DEFAULT_STAMP },
    frameMeta: null,
    frame: null,
    geometry: zeroGeometry(),
    crop: null,
    choice: null,
    levels: new Map(),
    memory: new Map(),
    lut: null,
    looks: new Map(),
    ...(copy ? { copy } : {}),
  };
}

/** 这次选进来的一张：一对 RAW+JPG 或一个文件，和它所在的文件夹（浏览器没给就是空的）。 */
export interface Arrival {
  stem: string;
  folder: string;
  dng: File | null;
  jpg: File | null;
  lastModified: number;
}

/** 同一个文件夹（或都没有文件夹）里同名的 RAW 或 JPG 不止一个：要按拍摄时间配。 */
export interface Bucket {
  stem: string;
  folder: string;
  dngs: File[];
  jpgs: File[];
  lastModified: number;
}

/** 文件所在的文件夹：webkitRelativePath 去掉文件名。拖进来、普通选文件时是空的。 */
export function folderOf(file: File): string {
  const path = (file as File & { webkitRelativePath?: string }).webkitRelativePath ?? '';
  const cut = path.lastIndexOf('/');
  return cut > 0 ? path.slice(0, cut) : '';
}

/**
 * 按文件夹分开，每个文件夹里照原来的规则（classify）按文件名配 RAW+JPG。同一个文件夹里同一个名字出现了几次
 * （拖进来的几个文件夹没有路径）：第 k 次出现的放进第 k 层各自配，同名的再一起按拍摄时间配（Bucket）。
 */
export function gather(files: readonly File[]): { items: Arrival[]; buckets: Bucket[] } {
  const folders = new Map<string, File[]>();
  for (const file of files) {
    const folder = folderOf(file);
    const list = folders.get(folder);
    if (list) list.push(file);
    else folders.set(folder, [file]);
  }
  const items: Arrival[] = [];
  const buckets: Bucket[] = [];
  for (const [folder, list] of folders) {
    const layers: File[][] = [];
    const seen = new Map<string, number>();
    for (const file of list) {
      const name = file.name.toLowerCase();
      const k = seen.get(name) ?? 0;
      seen.set(name, k + 1);
      (layers[k] ??= []).push(file);
    }
    const found = new Map<string, Bucket>();
    for (const layer of layers) {
      for (const photo of classify(layer).photos) {
        const key = photo.stem.toLowerCase();
        let entry = found.get(key);
        if (!entry) {
          entry = { stem: photo.stem, folder, dngs: [], jpgs: [], lastModified: photo.lastModified };
          found.set(key, entry);
        }
        if (photo.dngIndex !== null) {
          entry.dngs.push(layer[photo.dngIndex]);
          entry.stem = photo.stem;
        }
        if (photo.jpgIndex !== null) entry.jpgs.push(layer[photo.jpgIndex]);
        entry.lastModified = Math.max(entry.lastModified, photo.lastModified);
      }
    }
    for (const entry of found.values()) {
      if (entry.dngs.length > 1 || entry.jpgs.length > 1) buckets.push(entry);
      else items.push({ stem: entry.stem, folder, dng: entry.dngs[0] ?? null, jpg: entry.jpgs[0] ?? null, lastModified: entry.lastModified });
    }
  }
  return { items, buckets };
}

/** 头尾各读这么多字节算摘要。大 DNG 也只读这 128 KiB，不读整份。 */
export const EDGE = 64 * 1024;
/** RAW 和 JPG 的拍摄时间差在这之内算同一次拍的。 */
export const PAIR_MS = 1000;

/** 两路 32 位乘法散列（cyrb53 的做法），合成 64 位，写成 36 进制。不用 crypto.subtle：局域网 http 上没有它。 */
export function digest(...parts: readonly Uint8Array[]): string {
  let h1 = 0xdeadbeef;
  let h2 = 0x41c6ce57;
  for (const bytes of parts) {
    for (let i = 0; i < bytes.length; i += 1) {
      const b = bytes[i];
      h1 = Math.imul(h1 ^ b, 2654435761);
      h2 = Math.imul(h2 ^ b, 1597334677);
    }
  }
  h1 = Math.imul(h1 ^ (h1 >>> 16), 2246822507) ^ Math.imul(h2 ^ (h2 >>> 13), 3266489909);
  h2 = Math.imul(h2 ^ (h2 >>> 16), 2246822507) ^ Math.imul(h1 ^ (h1 >>> 13), 3266489909);
  return `${(h2 >>> 0).toString(36).padStart(7, '0')}${(h1 >>> 0).toString(36).padStart(7, '0')}`;
}

const prints = new WeakMap<File, string>();

/** 文件的指纹：大小加头尾各 64 KiB 的摘要。读不了是 null（不记下，下次再读）。读到的按 File 记住。 */
export async function printOf(file: File): Promise<string | null> {
  const known = prints.get(file);
  if (known) return known;
  try {
    const size = file.size;
    const head = new Uint8Array(await file.slice(0, Math.min(size, EDGE)).arrayBuffer());
    const tail = size > EDGE ? new Uint8Array(await file.slice(Math.max(EDGE, size - EDGE), size).arrayBuffer()) : new Uint8Array(0);
    const print = `${size.toString(36)}-${digest(head, tail)}`;
    prints.set(file, print);
    return print;
  } catch {
    return null;
  }
}

/** 文件现在还读得到（试读 1 个字节，不用记下的指纹）。 */
async function readable(file: File): Promise<boolean> {
  try {
    await file.slice(0, 1).arrayBuffer();
    return true;
  } catch {
    return false;
  }
}

const captures = new WeakMap<File, number | null>();

/** 文件头里的拍摄时间（毫秒）。JPG 读前 128 KiB，DNG 读前 256 KiB，和读缩略图的 Worker 一样。读不了、没有都是 null。 */
export async function captureOf(file: File | null): Promise<number | null> {
  if (!file) return null;
  if (captures.has(file)) return captures.get(file) ?? null;
  let ms: number | null = null;
  try {
    const dng = /\.dng$/i.test(file.name);
    const head = new Uint8Array(await file.slice(0, dng ? 256 * 1024 : 128 * 1024).arrayBuffer());
    ms = (dng ? captureTimeFromTiff(head) : captureTimeFromJpeg(head))?.ms ?? null;
    captures.set(file, ms);
  } catch {
    /* 读不了：不记下 */
  }
  return ms;
}

/** 同一个文件夹（或都没有文件夹）里同名的几份 RAW、JPG：拍摄时间最接近、差不到 1 秒的配成一对，剩下的各自一张。按拍摄时间排。 */
export async function pairByTime(bucket: Bucket): Promise<Arrival[]> {
  const files = [...bucket.dngs, ...bucket.jpgs];
  const times = await Promise.all(files.map((file) => captureOf(file)));
  const at = new Map(files.map((file, i) => [file, times[i]]));
  const pairs: Array<[number, File, File]> = [];
  for (const dng of bucket.dngs) {
    for (const jpg of bucket.jpgs) {
      const a = at.get(dng);
      const b = at.get(jpg);
      if (a != null && b != null && Math.abs(a - b) < PAIR_MS) pairs.push([Math.abs(a - b), dng, jpg]);
    }
  }
  pairs.sort((x, y) => x[0] - y[0]);
  const used = new Set<File>();
  const out: Array<{ ms: number; item: Arrival }> = [];
  const add = (dng: File | null, jpg: File | null) => {
    const ms = at.get((dng ?? jpg) as File) ?? Number.POSITIVE_INFINITY;
    const lastModified = Math.max(dng?.lastModified ?? 0, jpg?.lastModified ?? 0);
    out.push({ ms, item: { stem: bucket.stem, folder: bucket.folder, dng, jpg, lastModified } });
  };
  for (const [, dng, jpg] of pairs) {
    if (used.has(dng) || used.has(jpg)) continue;
    used.add(dng);
    used.add(jpg);
    add(dng, jpg);
  }
  for (const dng of bucket.dngs) if (!used.has(dng)) add(dng, null);
  for (const jpg of bucket.jpgs) if (!used.has(jpg)) add(null, jpg);
  return out.sort((a, b) => a.ms - b.ms).map((row) => row.item);
}

/** 把这次选进来的文件放进这张照片：换了 File 就是新版本，之前在路上的读取结果回来时都作废。（从 boot.ts 的 ingest 原样搬来，加了文件夹。） */
export function takeFiles(photo: Photo, item: Arrival): void {
  if (item.jpg) {
    photo.jpg = item.jpg;
    photo.diptych = false;
  }
  if (item.dng) {
    if (!photo.diptych) photo.dng = item.dng;
    photo.stem = item.stem;
  }
  photo.folder = item.folder || photo.folder || '';
  photo.version += 1;
  photo.gone = false;
  photo.frameMeta = null;
  clearCameraRecipe(photo);
  photo.neutral?.bitmap.close();
  photo.neutral = null;
  releaseLooks(photo);
  photo.lastModified = Math.max(photo.lastModified, item.lastModified);
  if (photo.status !== 'pending') {
    photo.bitmap?.close();
    photo.bitmap = null;
    photo.captureMs = null;
    photo.status = 'pending';
  }
}

/** 另外留下了几张时的那一句。 */
export function keptNote(count: number): string {
  return `有 ${count} 张同名照片另外留下了`;
}

/** 网格格子：同名另外留下的那张左下角加「(N)」，读屏名称后面加「名字 (N)」。fillCell 每次都重写这两处，所以跟在它后面调。 */
export function labelCopy(cell: HTMLElement, photo: Photo): void {
  if (!photo.copy) return;
  const corner = cell.querySelector('.bl');
  if (corner && !corner.querySelector('.cpy')) {
    const tag = document.createElement('span');
    tag.className = 'bdg cpy';
    tag.textContent = `(${photo.copy})`;
    if (corner.childElementCount) tag.style.marginLeft = '3px';
    corner.append(tag);
  }
  const name = photoName(photo);
  const aria = cell.getAttribute('aria-label') ?? '';
  if (!aria.endsWith(name)) cell.setAttribute('aria-label', aria ? `${aria}，${name}` : name);
}

export interface IntakeHost {
  /** 这次的照片（App.byId）。 */
  readonly photos: Map<string, Photo>;
  /** 这张的完整源正往显卡里传（App.uploads 里有它）。 */
  busy(photo: Photo): boolean;
  /** 这次会话的编号：清空照片就换一个，在路上的比对作废。 */
  session(): number;
  /** 换了文件：读缩略图（App.readPhoto）。 */
  read(photo: Photo, session: number): void;
  /** 等过比对的这一次都落定了；`kept` 张同名照片另外留下了。 */
  settled(kept: number): void;
}

type Files = { dng: File | null; jpg: File | null };
type Prints = { dng: string | null; jpg: string | null };
type Side = { files: Files; prints: Prints; readable: boolean };

async function printsOf(files: Files): Promise<Prints> {
  const [dng, jpg] = await Promise.all([files.dng ? printOf(files.dng) : null, files.jpg ? printOf(files.jpg) : null]);
  return { dng, jpg };
}

/** 两边都有的那种文件（RAW、JPG）指纹都一样是同一份；有一样不一样（或读不了）就不是；没有都有的那种是另一半（配对的事）。 */
function relation(a: Side, b: Side): 'same' | 'differs' | 'disjoint' {
  let shared = false;
  for (const kind of ['dng', 'jpg'] as const) {
    if (!a.files[kind] || !b.files[kind]) continue;
    shared = true;
    const pa = a.prints[kind];
    const pb = b.prints[kind];
    if (pa === null || pb === null || pa !== pb) return 'differs';
  }
  return shared ? 'same' : 'disjoint';
}

/**
 * 刚另外留下、缩略图还没读完的那一份（有编号、第一个版本、还在读、卡没拔）。它不是「原来那张还在读」：
 * 接着又选进来一张不是同一份的同名照片时不能拿它来替换，否则刚留下的那张就丢了。同一份（指纹一样）照旧替换，读坏了、卡拔了也照旧替换。
 */
function fresh(photo: Photo): boolean {
  return Boolean(photo.copy) && photo.version === 1 && photo.status === 'pending' && !photo.gone;
}

/** 同名的照片里先挑同一个文件夹的，再挑编号小的（第一份最先）。 */
function best(list: readonly Photo[], item: Arrival): Photo | null {
  const near = (photo: Photo) => ((photo.folder ?? '') === item.folder ? 0 : 1);
  return [...list].sort((a, b) => near(a) - near(b) || (a.copy ?? 1) - (b.copy ?? 1))[0] ?? null;
}

/**
 * 另一半（只有 RAW 的和只有 JPG 的）是不是同一张：同一个文件夹的直接配；不是同一个文件夹（或不知道文件夹）就看拍摄时间，
 * 差不到 1 秒才配（RAW、JPG 分开放在两个文件夹里也能配上）。有一边不知道拍摄时间就照旧按名字配。
 */
async function pairable(item: Arrival, photo: Photo): Promise<boolean> {
  if (item.folder && item.folder === (photo.folder ?? '')) return true;
  const a = await captureOf(item.dng ?? item.jpg);
  const b = photo.captureMs ?? (await captureOf(photo.jpg ?? photo.dng));
  return a === null || b === null || Math.abs(a - b) < PAIR_MS;
}

/**
 * 选进来的照片落到 byId。用不着比对的（没有同名的，或者只有一张同名、它还没读好、坏了、卡拔了或正往显卡里传）当场落下，
 * 时机和以前一样；要比对的排成一队在后面做，每次 await 之后都重新核对会话和同名的照片。等着的这几张算在「正在读取」里。
 */
export class Intake {
  private readonly host: IntakeHost;
  private tail: Promise<void> = Promise.resolve();
  private waitSession = -1;
  private waitCount = 0;
  /** 还有比对没做完的文件名（小写）：后来同名的也排到后面，不抢先落下。 */
  private readonly stems = new Map<string, number>();

  constructor(host: IntakeHost) {
    this.host = host;
  }

  /** 还在等比对的有几张（这次会话的）。 */
  waiting(): number {
    return this.waitSession === this.host.session() ? this.waitCount : 0;
  }

  take(files: readonly File[]): void {
    const session = this.host.session();
    if (this.waitSession !== session) {
      this.waitSession = session;
      this.waitCount = 0;
      this.stems.clear();
    }
    const { items, buckets } = gather(files);
    const touched = new Set<Photo>();
    const later: Arrival[] = [];
    const keys: string[] = [];
    for (const item of items) {
      const key = item.stem.toLowerCase();
      const named = this.stems.has(key) ? null : this.named(key);
      if (named && !named.length) this.place(this.create(key, item, null), item, touched, session);
      else if (named && named.length === 1 && !touched.has(named[0]) && !fresh(named[0]) && !this.healthy(named[0])) this.place(named[0], item, touched, session);
      else {
        later.push(item);
        keys.push(key);
      }
    }
    for (const bucket of buckets) keys.push(bucket.stem.toLowerCase());
    const count = later.length + buckets.reduce((sum, bucket) => sum + Math.max(bucket.dngs.length, bucket.jpgs.length), 0);
    if (!count) return;
    for (const key of keys) this.stems.set(key, (this.stems.get(key) ?? 0) + 1);
    this.waitCount += count;
    const run = this.tail.then(() => this.settle(later, buckets, touched, session, keys, count));
    this.tail = run.catch(() => undefined);
  }

  /** 排在后面的这一批：一张一张比对、落下；出了意外就照以前替换。最后不管怎样都把等着的数减掉，再交给 settled。 */
  private async settle(items: Arrival[], buckets: Bucket[], touched: Set<Photo>, session: number, keys: string[], count: number): Promise<void> {
    let left = count;
    let kept = 0;
    const live = () => this.host.session() === session;
    const one = async (item: Arrival) => {
      try {
        if (await this.decide(item, touched, session)) kept += 1;
      } catch {
        if (live()) this.fallback(item, touched, session);
      }
    };
    try {
      for (const item of items) {
        if (!live()) return;
        await one(item);
        left -= 1;
        this.release(session, 1);
      }
      for (const bucket of buckets) {
        if (!live()) return;
        const share = Math.max(bucket.dngs.length, bucket.jpgs.length);
        const arrivals = await pairByTime(bucket).catch(() => [
          ...bucket.dngs.map((dng) => ({ stem: bucket.stem, folder: bucket.folder, dng, jpg: null, lastModified: dng.lastModified })),
          ...bucket.jpgs.map((jpg) => ({ stem: bucket.stem, folder: bucket.folder, dng: null, jpg, lastModified: jpg.lastModified })),
        ]);
        for (const item of arrivals) {
          if (!live()) return;
          await one(item);
        }
        left -= share;
        this.release(session, share);
      }
    } finally {
      this.release(session, left);
      if (live()) {
        for (const key of keys) {
          const n = (this.stems.get(key) ?? 1) - 1;
          if (n > 0) this.stems.set(key, n);
          else this.stems.delete(key);
        }
        this.host.settled(kept);
      }
    }
  }

  private release(session: number, n: number): void {
    if (n > 0 && this.waitSession === session) this.waitCount = Math.max(0, this.waitCount - n);
  }

  /** 同名（不分大小写）的照片。 */
  private named(key: string): Photo[] {
    return [...this.host.photos.values()].filter((photo) => photo.stem.toLowerCase() === key);
  }

  /** 不看文件也知道的那一半：缩略图好了、卡没拔、没在往显卡里传。 */
  private healthy(photo: Photo): boolean {
    return photo.status === 'ready' && !photo.gone && !this.host.busy(photo);
  }

  /** 还在这次的照片里（没被去掉、没清空）。 */
  private still(photo: Photo): boolean {
    return this.host.photos.get(photo.id) === photo;
  }

  private place(photo: Photo, item: Arrival, touched: Set<Photo>, session: number): void {
    takeFiles(photo, item);
    touched.add(photo);
    this.host.read(photo, session);
    // 有同名的另一份：趁文件还读得到先记下指纹，卡拔了再选回来时也能认出哪个文件是哪一份（printOf 不会失败）。
    if (this.named(item.stem.toLowerCase()).length > 1) for (const file of [photo.dng, photo.jpg]) if (file) void printOf(file);
  }

  /** 新建一张。没有同名的第一份还用文件名小写当 id；另外留下的那份编号取最小的空号，id 带上新文件的指纹。 */
  private create(key: string, item: Arrival, print: string | null): Photo {
    const named = this.named(key);
    let id = key;
    let copy: number | undefined;
    if (this.host.photos.has(key) || named.some((photo) => !photo.copy)) {
      const used = new Set(named.map((photo) => photo.copy ?? 1));
      copy = 2;
      while (used.has(copy)) copy += 1;
      const base = `${key}~${print ?? `n${copy}`}`;
      id = base;
      for (let n = 2; this.host.photos.has(id); n += 1) id = `${base}-${n}`;
    }
    const photo = newPhoto(id, item.stem, item.lastModified, copy);
    this.host.photos.set(id, photo);
    return photo;
  }

  /** 比对出了意外：照以前那样替换同名的（先挑不是这次刚放进来的），没有就新建。 */
  private fallback(item: Arrival, touched: Set<Photo>, session: number): void {
    const key = item.stem.toLowerCase();
    const named = this.named(key);
    const target = best(named.filter((photo) => !touched.has(photo)), item) ?? best(named, item);
    this.place(target ?? this.create(key, item, null), item, touched, session);
  }

  /** 照片这一边：文件、指纹，和文件现在还读不读得到。 */
  private async side(photo: Photo): Promise<Side> {
    const files = { dng: photo.dng, jpg: photo.jpg };
    const [prints, ok] = await Promise.all([printsOf(files), Promise.all([files.dng, files.jpg].map((file) => (file ? readable(file) : true)))]);
    return { files, prints, readable: ok.every(Boolean) };
  }

  /** 比对一张，落下。另外留下了（而且原来有同名的）回 true。 */
  private async decide(item: Arrival, touched: Set<Photo>, session: number): Promise<boolean> {
    const live = () => this.host.session() === session;
    const key = item.stem.toLowerCase();
    const mine: Side = { files: { dng: item.dng, jpg: item.jpg }, prints: await printsOf(item), readable: true };
    if (!live()) return false;
    const theirs = new Map<Photo, Side>();
    let named = this.named(key);
    for (let round = 0; round < 3; round += 1) {
      const todo = named.filter((photo) => !theirs.has(photo));
      if (!todo.length) break;
      const sides = await Promise.all(todo.map((photo) => this.side(photo)));
      if (!live()) return false;
      todo.forEach((photo, i) => theirs.set(photo, sides[i]));
      named = this.named(key);
    }
    const rows = named.filter((photo) => theirs.has(photo)).map((photo) => ({ photo, rel: relation(mine, theirs.get(photo) as Side) }));
    const unreadable = (item.dng !== null && mine.prints.dng === null) || (item.jpg !== null && mine.prints.jpg === null);
    let target: Photo | null = null;
    if (unreadable) target = best(rows.filter((row) => !touched.has(row.photo)).map((row) => row.photo), item);
    target ??= best(rows.filter((row) => row.rel === 'same').map((row) => row.photo), item);
    if (!target) {
      const halves: Photo[] = [];
      for (const row of rows) if (row.rel === 'disjoint' && (await pairable(item, row.photo))) halves.push(row.photo);
      if (!live()) return false;
      target = best(halves.filter((photo) => this.still(photo)), item);
    }
    const sick = rows.filter((row) => !touched.has(row.photo) && !fresh(row.photo) && this.still(row.photo) && (!this.healthy(row.photo) || !(theirs.get(row.photo) as Side).readable));
    target ??= best(sick.map((row) => row.photo), item);
    if (target && this.still(target)) {
      this.place(target, item, touched, session);
      return false;
    }
    const others = this.named(key).length;
    this.place(this.create(key, item, mine.prints.dng ?? mine.prints.jpg), item, touched, session);
    return others > 0;
  }
}
