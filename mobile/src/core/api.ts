// 主线程用的核心接口。传给 Worker 的是拷贝，调用方留着原来的 ArrayBuffer 给显卡。

import type { BakeMeta, RawLayout, Scene } from '../../../frontend/src/index';
import type { UnitPack } from '../lib/calib';
import type { PhotoMeta } from '../lib/exif';
import type { ZipFail } from '../lib/zip';
import { Pool } from './pool';
import type { BakeArgs } from './protocol';

export interface Described {
  layout: RawLayout | null;
  reason: string | null;
  scene: Scene | null;
  meta: PhotoMeta;
}

let pool: Pool | null = null;

function workers(): Pool {
  pool ??= new Pool();
  return pool;
}

/**
 * 打开页面就连上描述 Worker。
 * 拖到第一次点开照片才连的话，开发服务器会在那时整页刷新，刚选的文件就没了。
 */
export function warmCore(): Promise<void> {
  return workers()
    .request('ping', null, [])
    .then(() => undefined, () => undefined);
}

function isLayout(value: unknown): value is RawLayout {
  if (!value || typeof value !== 'object') return false;
  const row = value as Record<string, unknown>;
  return typeof row.bps === 'number' && Array.isArray(row.strips) && Array.isArray(row.crop) && Array.isArray(row.wb);
}

function isScene(value: unknown): value is Scene {
  if (!value || typeof value !== 'object') return false;
  const row = value as Record<string, unknown>;
  return Array.isArray(row.A) && Array.isArray(row.b);
}

function metaOf(value: unknown): PhotoMeta {
  const row = (value && typeof value === 'object' ? value : {}) as Record<string, unknown>;
  return {
    lens: typeof row.lens === 'string' ? row.lens : null,
    iso: typeof row.iso === 'number' && Number.isFinite(row.iso) ? row.iso : null,
  };
}

/** 解析一份 DNG。`bytes` 仍归调用方；Worker 拿到的是拷贝。 */
export async function describeDng(bytes: ArrayBuffer): Promise<Described> {
  const copy = bytes.slice(0);
  const { json } = await workers().request('describe', copy, [copy]);
  const data = JSON.parse(json) as { layout?: unknown; reason?: unknown; scene?: unknown; meta?: unknown; error?: { message?: string } };
  if (data.error) throw new Error(data.error.message || '读不了这张照片');
  return {
    layout: isLayout(data.layout) ? data.layout : null,
    reason: typeof data.reason === 'string' ? data.reason : null,
    scene: isScene(data.scene) ? data.scene : null,
    meta: metaOf(data.meta),
  };
}

export interface DatRead {
  summary: string;
  menu: string;
  /** 24 × 552 字节：单元 0..2，位置 1..8。 */
  entries: Uint8Array;
}

/** 解析相机配方文件。坏文件抛核心的原话。 */
export async function readDat(bytes: ArrayBuffer): Promise<DatRead> {
  const { json, buffers } = await workers().request('dat', bytes, [bytes]);
  const data = JSON.parse(json) as { summary: string; menu: string };
  return { summary: data.summary, menu: data.menu, entries: new Uint8Array(buffers[0] ?? new ArrayBuffer(0)) };
}

export type CalibRead =
  | { ok: true; units: UnitPack[]; manifestSha256: string; files: Map<string, Uint8Array>; saved: boolean }
  | { ok: false; zip: ZipFail | null; message: string | null; detail: string };

export type CalibRestored = (CalibRead & { name: string; savedAt: number }) | null;

function calibOf(json: string, buffers: ArrayBuffer[]): CalibRead & { name?: string; savedAt?: number; none?: boolean } {
  const data = JSON.parse(json) as {
    ok: boolean;
    zip?: ZipFail;
    message?: string;
    detail?: string;
    units?: UnitPack[];
    manifestSha256?: string;
    names?: string[];
    saved?: boolean;
    name?: string;
    savedAt?: number;
    none?: boolean;
  };
  const extra = { name: data.name, savedAt: data.savedAt, none: data.none };
  if (!data.ok) return { ok: false, zip: data.zip ?? null, message: data.message ?? null, detail: data.detail ?? '', ...extra };
  const files = new Map<string, Uint8Array>();
  (data.names ?? []).forEach((name, i) => files.set(name, new Uint8Array(buffers[i] ?? new ArrayBuffer(0))));
  return { ok: true, units: data.units ?? [], manifestSha256: data.manifestSha256 ?? '', files, saved: data.saved !== false, ...extra };
}

/**
 * 导入校准数据 zip：File 交给 Worker。Worker 先查大小，再读、核对清单和每个 SHA-256，通过后在 Worker 里存进 IndexedDB。
 * 主线程不读也不复制整包。
 */
export async function importCalib(file: File, savedAt: number): Promise<CalibRead> {
  const { json, buffers } = await workers().request('calib-file', null, [], null, { file, meta: { name: file.name, savedAt } });
  return calibOf(json, buffers);
}

/** 打开页面时读回 IndexedDB 里的那份并重新核对（也在 Worker 里）。没有存过返回 null。 */
export async function restoreCalib(): Promise<CalibRestored> {
  const { json, buffers } = await workers().request('calib-restore', null, []);
  const read = calibOf(json, buffers);
  if (read.none) return null;
  return { ...read, name: read.name ?? '', savedAt: read.savedAt ?? 0 };
}

export interface Bake {
  meta: BakeMeta;
  data: ArrayBuffer;
}

/** 在 Worker 里跑 color_bake。失败抛核心的原话。 */
export async function bakeIn(args: BakeArgs): Promise<Bake> {
  const transfer = [args.entry, args.stock, args.ctx, args.ring];
  const { json, buffers } = await workers().request('bake', null, transfer, args);
  const meta = JSON.parse(json) as BakeMeta;
  return { meta: { nx: meta.nx, ny: meta.ny, origin: meta.origin, step: meta.step }, data: buffers[0] ?? new ArrayBuffer(0) };
}
