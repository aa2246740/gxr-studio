// 主线程用的核心接口。传给 Worker 的是拷贝，调用方留着原来的 ArrayBuffer 给显卡。

import type { BakeMeta, RawLayout, Scene } from '../../../frontend/src/index';
import type { UnitPack } from '../lib/calib';
import type { CropState } from '../lib/crop';
import type { PhotoMeta } from '../lib/exif';
import type { FontKey } from '../lib/fonts';
import type { FrameLayoutData, FrameMetaResult, StampLayoutData } from '../lib/frame_spec';
import type { LutPatchFields, LutSnapshotRow } from '../lib/idb';
import type { RgbaPixels } from '../lib/logo_key';
import type { LutRecord } from '../lib/lut';
import type { Frame } from '../lib/photo_size';
import type { ZipFail } from '../lib/zip';

import { Pool } from './pool';
import type { BakeArgs, CalibCheckArgs, FrameLayoutArgs, FrameMetaArgs, LogoSaveArgs } from './protocol';

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
  /** `camdata_parse` 的摘要（代数等）。 */
  summary: string;
  /** `color_listing`：桌面 CameraData.listing 的配方和旋钮。 */
  listing: string;
  /** 24 × 552 字节：单元 0..2，位置 1..8（原厂 04 从这里取）。 */
  entries: Uint8Array;
  /** DAT 原来的字节（Worker 读完还回来的），之后 resolve 用。 */
  bytes: ArrayBuffer;
  /** 同一次读回来的 GXRFX.BIN。单文件卡是 null。 */
  library?: ArrayBuffer | null;
}

/** DAT 字节 → 同一次读入的 GXRFX.BIN。导出只快照 DAT 字节，靠这个找回库。 */
const libraries = new Map<string, ArrayBuffer | null>();

function datKey(bytes: ArrayBuffer): string {
  const view = new Uint8Array(bytes);
  let hash = 2166136261;
  for (let i = 0; i < view.length; i += 1) hash = Math.imul(hash ^ view[i], 16777619);
  return `${view.length}:${hash >>> 0}`;
}

export function rememberLibrary(dat: ArrayBuffer, library: ArrayBuffer | null): void {
  libraries.set(datKey(dat), library);
}

export function libraryFor(dat: ArrayBuffer | null): ArrayBuffer | null {
  if (!dat || dat.byteLength === 0) return null;
  return libraries.get(datKey(dat)) ?? null;
}

/** 解析相机配方文件。`library` 是同卡的 GXRFX.BIN。坏文件抛核心的原话。 */
export async function readDat(bytes: ArrayBuffer, library?: ArrayBuffer | null): Promise<DatRead> {
  const transfer: Transferable[] = [bytes];
  if (library) transfer.push(library);
  // 库字节放在 resolve.library：dat 这一支和 resolve 共用已经能传过去的那一格。
  const { json, buffers } = await workers().request('dat', bytes, transfer, null, {
    resolve: { dat: bytes, recipe: '', library: library ?? null },
  });
  const data = JSON.parse(json) as { summary: string; listing: string };
  const datBytes = buffers[1] ?? new ArrayBuffer(0);
  const libraryBack = buffers[2] ?? null;
  rememberLibrary(datBytes, libraryBack);
  return {
    summary: data.summary,
    listing: data.listing,
    entries: new Uint8Array(buffers[0] ?? new ArrayBuffer(0)),
    bytes: datBytes,
    library: libraryBack,
  };
}

export interface Resolved {
  entry: Uint8Array;
  entrySha256: string;
  levels: Record<string, number>;
}

function fromHex(text: string): Uint8Array {
  const out = new Uint8Array(text.length >> 1);
  for (let i = 0; i < out.length; i++) out[i] = parseInt(text.slice(i * 2, i * 2 + 2), 16);
  return out;
}

/** `color_resolve`：这份 DAT 上的一个配方选择 → 552 字节条目（旋钮已经套上）。`dat` 和 `library` 是拷贝。失败抛核心的原话。 */
export async function resolveRecipe(dat: ArrayBuffer, recipe: string, library?: ArrayBuffer | null): Promise<Resolved> {
  const transfer: Transferable[] = [dat];
  if (library) transfer.push(library);
  const { json } = await workers().request('resolve', null, transfer, null, { resolve: { dat, recipe, library: library ?? null } });
  const data = JSON.parse(json) as { entry: string; entry_sha256: string; levels: Record<string, number> };
  return { entry: fromHex(data.entry), entrySha256: data.entry_sha256, levels: data.levels };
}

export interface CalibPick {
  iso: number;
  calibrated: boolean;
  model_iso: number;
  ctx: string;
  ring: string;
  rows: number;
}

/** `color_calib_check`：校准包对这个单元、这个 ISO 的 model_for 检查。字节都是拷贝。失败抛桌面的原话。 */
export async function calibCheck(args: CalibCheckArgs): Promise<CalibPick> {
  const transfer = [args.manifest, args.stock, ...args.blobs];
  const { json } = await workers().request('calib-check', null, transfer, null, { check: args });
  return JSON.parse(json) as CalibPick;
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
  /** 核心给的整份 meta（多一个 `input_coefficients`）。引擎只看 `meta`；对拍要拿整份。 */
  fullMeta: Record<string, unknown>;
}

/** 在 Worker 里跑 color_bake。失败抛核心的原话。 */
export async function bakeIn(args: BakeArgs): Promise<Bake> {
  const transfer = [args.entry, args.stock, args.ctx, args.ring];
  const { json, buffers } = await workers().request('bake', null, transfer, args);
  const fullMeta = JSON.parse(json) as BakeMeta & Record<string, unknown>;
  return {
    meta: { nx: fullMeta.nx, ny: fullMeta.ny, origin: fullMeta.origin, step: fullMeta.step },
    data: buffers[0] ?? new ArrayBuffer(0),
    fullMeta,
  };
}

export async function readFrameMeta(args: FrameMetaArgs): Promise<FrameMetaResult> {
  const { json } = await workers().request('frame-meta', null, [], null, { frameMeta: args });
  return JSON.parse(json) as FrameMetaResult;
}

export async function computeFrameLayout(
  args: Omit<FrameLayoutArgs, 'cards'>,
): Promise<{ frame: FrameLayoutData | null; stamp: StampLayoutData | null }> {
  const { json } = await workers().request('frame-layout', null, [], null, { frameLayout: args });
  return JSON.parse(json) as { frame: FrameLayoutData | null; stamp: StampLayoutData | null };
}

export async function computeCardLayouts(
  metaJson: string,
  fontKeys: FontKey[],
  cards: Array<{ id: string; specJson: string }>,
): Promise<Record<string, FrameLayoutData | null>> {
  const { json } = await workers().request('frame-layout', null, [], null, {
    frameLayout: {
      metaJson,
      frameSpecJson: null,
      stampMetaJson: null,
      stampSpecJson: null,
      fontKeys,
      cards,
    },
  });
  const parsed = JSON.parse(json) as { cards?: Record<string, FrameLayoutData | null> };
  return parsed.cards ?? {};
}

export type LogoPreviewReply =
  | {
      ok: true;
      imgData: RgbaPixels;
      origW: number;
      origH: number;
      whiteBg: boolean;
      origOpaque: boolean;
    }
  | { ok: false; error: string };

export async function previewLogo(file: File): Promise<LogoPreviewReply> {
  const { json, buffers } = await workers().request('logo-preview', null, [], null, { file });
  const data = JSON.parse(json) as
    | { ok: false; error: string }
    | { ok: true; width: number; height: number; origW: number; origH: number; whiteBg: boolean; origOpaque: boolean };
  if (!data.ok) return { ok: false, error: data.error };
  const rgba = new Uint8ClampedArray(buffers[0] ?? new ArrayBuffer(0));
  return {
    ok: true,
    imgData: { width: data.width, height: data.height, data: rgba },
    origW: data.origW,
    origH: data.origH,
    whiteBg: data.whiteBg,
    origOpaque: data.origOpaque,
  };
}

export type LogoSaveReply =
  | {
      ok: true;
      name: string;
      w: number;
      h: number;
      coverage: number;
      opaque: boolean;
      lum: number;
      rgba: ArrayBuffer;
    }
  | { ok: false; error: string };

export async function prepareLogo(file: File, args: LogoSaveArgs): Promise<LogoSaveReply> {
  const { json, buffers } = await workers().request('logo-save', null, [], null, { file, logoSave: args });
  const data = JSON.parse(json) as
    | { ok: false; error: string }
    | { ok: true; name: string; w: number; h: number; coverage: number; opaque: boolean; lum: number };
  if (!data.ok) return { ok: false, error: data.error };
  return {
    ok: true,
    name: data.name,
    w: data.w,
    h: data.h,
    coverage: data.coverage,
    opaque: data.opaque,
    lum: data.lum,
    rgba: buffers[0] ?? new ArrayBuffer(0),
  };
}

export interface GeomWriteReply {
  frame: Frame | null;
  geometry: {
    straighten: number;
    vertical: number;
    horizontal: number;
    distortion: number;
    rotate90: number;
    flip: boolean;
  } | null;
  crop: CropState | null;
  describe: { preset: string; rect: [number, number, number, number]; angle: number };
}

export interface FinalBoxReply {
  box: [number, number, number, number];
  valid: [number, number, number, number];
  header: string;
}

export interface OutputSizesReply {
  photo: [number, number];
  crop_preset: string | null;
  ratio: [number, number] | null;
  sizes: {
    original: [number, number];
    large: [number, number];
    medium: [number, number];
    small: [number, number];
  };
}

/** 裁切写入规则（`gxr-finish/src/crop_edit.rs`），只在 Worker 里跑 WASM。 */
export async function geomCropWrite(side: Record<string, unknown>, request: Record<string, unknown>): Promise<GeomWriteReply> {
  const { json } = await workers().request('geom-crop-write', null, [], null, {
    geom: { side: JSON.stringify(side), payload: JSON.stringify(request) },
  });
  return JSON.parse(json) as GeomWriteReply;
}

/** 几何写入规则（改拉直时按桌面 `render/edits.py` 重收裁切框），只在 Worker 里跑 WASM。 */
export async function geomGeometryWrite(side: Record<string, unknown>, geometry: Record<string, unknown> | null): Promise<GeomWriteReply> {
  const { json } = await workers().request('geom-geometry-write', null, [], null, {
    geom: { side: JSON.stringify(side), payload: JSON.stringify(geometry) },
  });
  return JSON.parse(json) as GeomWriteReply;
}

/** 成片裁切框（`gpu/box.py:final_box`），与导出和显卡预览同一份 WASM。 */
export async function geomFinalBox(w: number, h: number, geometry: Record<string, unknown> | null, crop: CropState | null): Promise<FinalBoxReply> {
  const { json } = await workers().request('geom-final-box', null, [], null, {
    finalBox: { w, h, geometry: JSON.stringify(geometry ?? {}), crop: JSON.stringify(crop) },
  });
  return JSON.parse(json) as FinalBoxReply;
}

/** 成品像素行，走 Worker 里的 `export_output_sizes`（传当前框的 `rect` 和 `preset`）。 */
export async function geomOutputSizes(
  w: number,
  h: number,
  side: Record<string, unknown>,
  rect: [number, number, number, number] | null,
  preset: string | null,
): Promise<OutputSizesReply> {
  const { json } = await workers().request('export-output-sizes', null, [], null, {
    sizes: {
      w,
      h,
      side: JSON.stringify(side),
      rect: rect ? JSON.stringify(rect) : '',
      preset: preset ?? undefined,
    },
  });
  return JSON.parse(json) as OutputSizesReply;
}

export type LutImportReply =
  | { ok: true; duplicate: boolean; record: LutRecord; totalBytes: number }
  | { ok: false; reason: string; message: string };

export interface LutLibraryMeta {
  records: LutRecord[];
  order: string[];
  totalBytes: number;
  watermark: number;
}

/** 在 Worker 里解析、识别并保存一个 .cube LUT（主线程不读文件字节）。 */
export async function importLutFile(file: File, filename: string, version: number): Promise<LutImportReply> {
  const { json } = await workers().request('lut-import', null, [], null, {
    lutImport: { file, filename, version },
  });
  return JSON.parse(json) as LutImportReply;
}

/** 打开页面时在 Worker 里从 IndexedDB 读回 LUT 目录元数据（不含大文件字节）。 */
export async function restoreLutLibrary(): Promise<LutLibraryMeta> {
  const { json } = await workers().request('lut-restore', null, []);
  return JSON.parse(json) as LutLibraryMeta;
}

/** 在 Worker 里更新 LUT 元数据（改名、输入色彩空间等）。 */
export async function patchLut(
  cubeId: string,
  patch: LutPatchFields,
  version: number,
): Promise<{ ok: true; record: LutRecord } | { ok: false; reason: 'not_found' | 'stale' }> {
  const { json } = await workers().request('lut-patch', null, [], null, {
    lutPatch: { cubeId, patch, version },
  });
  return JSON.parse(json) as { ok: true; record: LutRecord } | { ok: false; reason: 'not_found' | 'stale' };
}

/** 在 Worker 里从 IndexedDB 删除一条 LUT（先写删除后再清内存）。 */
export async function deleteLut(cubeId: string, version: number): Promise<boolean> {
  const { json } = await workers().request('lut-delete', null, [], null, {
    lutDelete: { cubeId, version },
  });
  const parsed = JSON.parse(json) as { ok?: boolean };
  return parsed.ok === true;
}

/** 从 IndexedDB 读出原始 .cube 字节（供导出或建 GPU 3D 表用）。 */
export async function readLutBlob(cubeId: string): Promise<ArrayBuffer | null> {
  const { json, buffers } = await workers().request('lut-blob', null, [], null, {
    lutBlob: { cubeId },
  });
  const parsed = JSON.parse(json) as { ok?: boolean };
  return parsed.ok && buffers[0] ? buffers[0] : null;
}

/** 在单个 IndexedDB readonly 事务里原子读取一组 LUT 的记录与 .cube 字节（P2-02）。 */
export async function readLutBatchSnapshot(cubeIds: readonly string[]): Promise<LutSnapshotRow[]> {
  const unique = [...new Set(cubeIds)];
  if (!unique.length) return [];
  const { json, buffers } = await workers().request('lut-snapshot', null, [], null, {
    lutSnapshot: { cubeIds: unique },
  });
  const parsed = JSON.parse(json) as { items?: Array<{ cubeId: string; record: LutRecord | null; hasBlob: boolean }> };
  let bi = 0;
  return (parsed.items ?? []).map((item) => ({
    cubeId: item.cubeId,
    record: item.record,
    blob: item.hasBlob ? (buffers[bi++] ?? null) : null,
  }));
}


