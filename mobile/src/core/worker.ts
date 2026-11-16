// 模块 Worker：WASM 只在这里加载。主线程把 DNG 的一份拷贝传进来，原件留给显卡引擎。
// 也在这里：读相机配方文件（camdata_*）、读校准数据 zip 并核对、做烘焙表（color_bake）。

import init, {
  camdata_entries,
  camdata_parse,
  color_bake,
  color_calib_check,
  color_calib_pack,
  color_listing,
  color_resolve,
  dng_describe,
  export_meta_photo,
  frame_layout,
  stamp_layout,
} from '../wasm-pkg/gxr_wasm.js';
import wasmUrl from '../wasm-pkg/gxr_wasm_bg.wasm?url';
import { checkPack } from '../lib/calib';
import { photoMetaFromTiff } from '../lib/exif';
import { FONT_URLS } from '../lib/font_assets';
import { packFonts, type FontKey } from '../lib/fonts';
import { loadPack, savePack } from '../lib/idb';
import {
  cleanLogoName,
  cropImageData,
  hasWhiteBackground,
  isOpaqueImageData,
  keyWhiteImageData,
  LOGO_ERR,
  LOGO_LIMITS,
  logoStats,
  sniffImageMagic,
  trimBounds,
  type RgbaPixels,
} from '../lib/logo_key';
import { shotSize } from '../lib/photo_size';
import { readStoredZip, ZIP_LIMITS } from '../lib/zip';
import type {
  BakeArgs,
  CalibCheckArgs,
  CoreOp,
  CoreReply,
  CoreRequest,
  FrameLayoutArgs,
  FrameMetaArgs,
  LogoSaveArgs,
  ResolveArgs,
} from './protocol';

const fontCache = new Map<FontKey, Uint8Array>();

let ready: Promise<unknown> | null = null;

function load(): Promise<unknown> {
  ready ??= init({ module_or_path: wasmUrl });
  return ready;
}

/** 核心的错误是 JSON 文本（kind、message）；拿出 message，拿不出就原样。 */
function coreMessage(err: unknown): string {
  const text = typeof err === 'string' ? err : err instanceof Error ? err.message : String(err);
  try {
    const parsed = JSON.parse(text) as { error?: { message?: unknown }; message?: unknown };
    const message = parsed.error?.message ?? parsed.message;
    if (typeof message === 'string') return message;
  } catch {
    /* 不是 JSON，就用原文。 */
  }
  return text;
}

function ok(id: number, json: string, buffers: ArrayBuffer[] = []): void {
  const reply: CoreReply = { id, ok: true, json, buffers };
  self.postMessage(reply, { transfer: buffers });
}

function failed(id: number, err: unknown): void {
  const reply: CoreReply = { id, ok: false, message: coreMessage(err) };
  self.postMessage(reply);
}

function own(view: Uint8Array): ArrayBuffer {
  const copy = new Uint8Array(view.length);
  copy.set(view);
  return copy.buffer;
}

function describe(id: number, buffer: ArrayBuffer): void {
  const bytes = new Uint8Array(buffer);
  const text = dng_describe(bytes);
  let data: Record<string, unknown>;
  try {
    data = JSON.parse(text) as Record<string, unknown>;
  } catch {
    ok(id, text);
    return;
  }
  // 镜头和 ISO：配方要按镜头单元和 ISO 选模型（桌面 meta.read_dng_meta 的同一组标签）。
  data.meta = photoMetaFromTiff(bytes);
  ok(id, JSON.stringify(data));
}

function dat(id: number, buffer: ArrayBuffer): void {
  const bytes = new Uint8Array(buffer);
  const summary = camdata_parse(bytes, undefined);
  // 配方列表照桌面 CameraData.listing（内置、文件里的 Looks、相机里的用户配方，各带旋钮）。
  const listing = color_listing(bytes, undefined);
  const entries = camdata_entries(bytes, undefined);
  // DAT 字节原样还回去：之后每次 color_resolve 都要用（主线程不再复制一份）。
  ok(id, JSON.stringify({ summary, listing }), [own(entries), buffer]);
}

/** 一个配方选择 → 552 字节条目（旋钮已经套上，和桌面 CameraData.resolve 一样）。 */
function resolve(id: number, args: ResolveArgs): void {
  const text = color_resolve(new Uint8Array(args.dat), new TextEncoder().encode(args.recipe), undefined);
  ok(id, text);
}

/** 校准包对这个单元、这个 ISO 的 model_for 检查（gxr-cli color-calib-check --dir 的同一段代码）。 */
function calibCheck(id: number, args: CalibCheckArgs): void {
  const index: Record<string, [number, number]> = {};
  let size = 0;
  args.names.forEach((name, i) => {
    index[name] = [size, args.blobs[i].byteLength];
    size += args.blobs[i].byteLength;
  });
  const blob = new Uint8Array(size);
  args.blobs.forEach((part, i) => blob.set(new Uint8Array(part), index[args.names[i]][0]));
  const text = color_calib_check(
    new Uint8Array(args.manifest),
    new TextEncoder().encode(JSON.stringify(index)),
    blob,
    new Uint8Array(args.stock),
    args.unit,
    args.iso,
  );
  ok(id, text);
}

/** manifest 单独，其余文件拼成一块，加 `{名字: [偏移, 长度]}` 索引（color_calib_pack 的输入）。 */
function packBlob(entries: Map<string, Uint8Array>): { manifest: Uint8Array; index: Uint8Array; blob: Uint8Array } {
  const index: Record<string, [number, number]> = {};
  let size = 0;
  for (const [name, data] of entries) {
    if (name === 'manifest.json') continue;
    index[name] = [size, data.length];
    size += data.length;
  }
  const blob = new Uint8Array(size);
  for (const [name, data] of entries) if (name !== 'manifest.json') blob.set(data, index[name][0]);
  return { manifest: entries.get('manifest.json') ?? new Uint8Array(), index: new TextEncoder().encode(JSON.stringify(index)), blob };
}

/** 核对一份 zip；`save` 给了就在这里（Worker 里）存进 IndexedDB，主线程不复制整包。 */
async function checkZip(id: number, buffer: ArrayBuffer, extra: Record<string, unknown>, save: { name: string; savedAt: number } | null): Promise<void> {
  const zip = readStoredZip(new Uint8Array(buffer));
  if (!zip.ok) {
    ok(id, JSON.stringify({ ok: false, zip: zip.reason, detail: zip.detail }));
    return;
  }
  const checked = await checkPack(zip.entries);
  if (!checked.ok) {
    ok(id, JSON.stringify({ ok: false, message: checked.message, detail: checked.detail }));
    return;
  }
  // 存之前先跑核心的包级检查（gxr-cli color-calib-check --dir 不带 --unit 的同一段代码）：版本、单元、每个单元
  // 的文件清单和 SHA-256。旧版本、单元为空或不认得的包在这里就拒收，不会存下来再显示「已导入」（Sol r3）。
  let units: Array<{ unit: string }>;
  try {
    const { manifest, index, blob } = packBlob(zip.entries);
    const reply = JSON.parse(color_calib_pack(manifest, index, blob)) as { units: string[] };
    units = reply.units.map((unit) => ({ unit }));
  } catch (err) {
    ok(id, JSON.stringify({ ok: false, message: coreMessage(err), detail: 'pack' }));
    return;
  }
  // 只有真的写进 IndexedDB 才算存下了：事务失败、或者已经有更新的版本都不算（Sol r2）。
  let saved = true;
  if (save) {
    try {
      saved = await savePack({ zip: buffer, name: save.name, savedAt: save.savedAt });
    } catch {
      saved = false;
    }
  }
  const names = [...zip.entries.keys()];
  ok(
    id,
    JSON.stringify({ ok: true, units, manifestSha256: checked.manifestSha256, names, saved, ...extra }),
    names.map((name) => own(zip.entries.get(name) ?? new Uint8Array())),
  );
}

/** 导入：先看大小，超过上限一个字节都不读（Sol r1）。 */
async function calibFile(id: number, file: File, meta: { name: string; savedAt: number }): Promise<void> {
  if (file.size > ZIP_LIMITS.bytes) {
    ok(id, JSON.stringify({ ok: false, zip: 'too_big', detail: 'file' }));
    return;
  }
  const buffer = await file.arrayBuffer();
  await checkZip(id, buffer, {}, meta);
}

/** 打开页面时：IndexedDB 里存的那份也在 Worker 里读、重新核对。 */
async function calibRestore(id: number): Promise<void> {
  const stored = await loadPack();
  if (!stored) {
    ok(id, JSON.stringify({ ok: false, none: true }));
    return;
  }
  await checkZip(id, stored.zip, { name: stored.name, savedAt: stored.savedAt }, null);
}

function bake(id: number, args: BakeArgs): void {
  const made = color_bake(
    new Uint8Array(args.entry),
    new Uint8Array(args.stock),
    new TextEncoder().encode(args.menu),
    new Uint8Array(args.ctx),
    new Uint8Array(args.ring),
    new TextEncoder().encode(args.params),
  );
  const meta = made.meta;
  const bytes = made.bytes();
  ok(id, meta, [own(bytes)]);
}

async function headSlice(file: File | null): Promise<Uint8Array | null> {
  if (!file) return null;
  try {
    return new Uint8Array(await file.slice(0, 1 << 20).arrayBuffer());
  } catch {
    return null;
  }
}

async function frameMeta(id: number, args: FrameMetaArgs): Promise<void> {
  const [jpgHead, dngHead] = await Promise.all([headSlice(args.jpg), headSlice(args.dng)]);
  const size = shotSize(jpgHead, dngHead);
  const mtime = args.mtimeMs ?? undefined;
  const tz = args.tzOffsetMin ?? 0;
  const raw = JSON.parse(export_meta_photo(jpgHead, dngHead, args.name, mtime, tz)) as {
    frame?: Record<string, string>;
    taken?: string | null;
    file_time?: string | null;
  };
  ok(
    id,
    JSON.stringify({
      frame: raw.frame ?? {},
      taken: raw.taken ?? null,
      file_time: raw.file_time ?? null,
      width: size?.width ?? null,
      height: size?.height ?? null,
    }),
  );
}

async function ensureFontBytes(keys: readonly FontKey[]): Promise<Array<readonly [FontKey, Uint8Array]>> {
  const unique = [...new Set(keys)].filter((k): k is FontKey => k in FONT_URLS);
  return Promise.all(
    unique.map(async (key) => {
      let bytes = fontCache.get(key);
      if (!bytes) {
        const res = await fetch(FONT_URLS[key], { credentials: 'same-origin' });
        if (!res.ok) throw new Error(`font_${key}_${res.status}`);
        bytes = new Uint8Array(await res.arrayBuffer());
        fontCache.set(key, bytes);
      }
      return [key, bytes] as const;
    }),
  );
}

async function frameLayoutOp(id: number, args: FrameLayoutArgs): Promise<void> {
  const loaded = await ensureFontBytes(args.fontKeys);
  const { indexJson, pack } = packFonts(loaded);
  if (args.cards) {
    const cards: Record<string, unknown> = {};
    for (const card of args.cards) {
      try {
        cards[card.id] = JSON.parse(frame_layout(args.metaJson, card.specJson, indexJson, pack));
      } catch {
        cards[card.id] = null;
      }
    }
    ok(id, JSON.stringify({ cards }));
    return;
  }
  const frame = args.frameSpecJson ? JSON.parse(frame_layout(args.metaJson, args.frameSpecJson, indexJson, pack)) : null;
  const stamp =
    args.stampSpecJson && args.stampMetaJson
      ? JSON.parse(stamp_layout(args.stampMetaJson, args.stampSpecJson, indexJson, pack))
      : null;
  ok(id, JSON.stringify({ frame, stamp }));
}

function bitmapToRgba(bmp: ImageBitmap, width: number, height: number): RgbaPixels {
  const canvas = new OffscreenCanvas(width, height);
  try {
    const ctx = canvas.getContext('2d', { willReadFrequently: true });
    if (!ctx) throw new Error(LOGO_ERR.unreadable);
    ctx.imageSmoothingQuality = 'high';
    ctx.drawImage(bmp, 0, 0, width, height);
    const data = ctx.getImageData(0, 0, width, height);
    return { width, height, data: data.data };
  } finally {
    canvas.width = 0;
    canvas.height = 0;
  }
}

function resizeRgba(rgba: RgbaPixels, outW: number, outH: number): RgbaPixels {
  const srcCanvas = new OffscreenCanvas(rgba.width, rgba.height);
  const dstCanvas = new OffscreenCanvas(outW, outH);
  try {
    const sctx = srcCanvas.getContext('2d');
    if (!sctx) throw new Error(LOGO_ERR.unreadable);
    sctx.putImageData(new ImageData(new Uint8ClampedArray(rgba.data), rgba.width, rgba.height), 0, 0);
    const dctx = dstCanvas.getContext('2d', { willReadFrequently: true });
    if (!dctx) throw new Error(LOGO_ERR.unreadable);
    dctx.imageSmoothingQuality = 'high';
    dctx.drawImage(srcCanvas, 0, 0, outW, outH);
    const out = dctx.getImageData(0, 0, outW, outH);
    return { width: outW, height: outH, data: out.data };
  } finally {
    srcCanvas.width = 0;
    srcCanvas.height = 0;
    dstCanvas.width = 0;
    dstCanvas.height = 0;
  }
}

async function decodeLogoFile(file: File): Promise<{ bmp: ImageBitmap } | { error: string }> {
  if (file.size > LOGO_LIMITS.bytes) return { error: LOGO_ERR.tooBig };
  let magic: 'PNG' | 'JPEG' | 'WEBP' | null = null;
  try {
    const head = new Uint8Array(await file.slice(0, 16).arrayBuffer());
    magic = sniffImageMagic(head);
  } catch {
    return { error: LOGO_ERR.unreadable };
  }
  if (!magic) return { error: LOGO_ERR.unreadable };
  try {
    const bmp = await createImageBitmap(file, {
      imageOrientation: 'from-image',
      colorSpaceConversion: 'default',
      premultiplyAlpha: 'none',
    });
    if (
      bmp.width <= 0 ||
      bmp.height <= 0 ||
      bmp.width > LOGO_LIMITS.dim ||
      bmp.height > LOGO_LIMITS.dim ||
      bmp.width * bmp.height > LOGO_LIMITS.pixels
    ) {
      bmp.close();
      return { error: LOGO_ERR.unreadable };
    }
    return { bmp };
  } catch {
    return { error: LOGO_ERR.unreadable };
  }
}

async function logoPreview(id: number, file: File): Promise<void> {
  const decoded = await decodeLogoFile(file);
  if ('error' in decoded) {
    ok(id, JSON.stringify({ ok: false, error: decoded.error }));
    return;
  }
  const { bmp } = decoded;
  try {
    const origW = bmp.width;
    const origH = bmp.height;
    const scale = Math.min(1, 480 / Math.max(origW, origH));
    const pw = Math.max(1, Math.round(origW * scale));
    const ph = Math.max(1, Math.round(origH * scale));
    const rgba = bitmapToRgba(bmp, pw, ph);
    const whiteBg = hasWhiteBackground(rgba);
    const origOpaque = isOpaqueImageData(rgba);
    const buf = own(new Uint8Array(rgba.data.buffer, rgba.data.byteOffset, rgba.data.byteLength));
    ok(id, JSON.stringify({ ok: true, width: pw, height: ph, origW, origH, whiteBg, origOpaque }), [buf]);
  } finally {
    bmp.close();
  }
}

async function logoSave(id: number, file: File, args: LogoSaveArgs): Promise<void> {
  if (!Number.isFinite(args.tol) || !Number.isInteger(args.tol) || args.tol < 0 || args.tol > 60) {
    ok(id, JSON.stringify({ ok: false, error: LOGO_ERR.badTol }));
    return;
  }
  const decoded = await decodeLogoFile(file);
  if ('error' in decoded) {
    ok(id, JSON.stringify({ ok: false, error: decoded.error }));
    return;
  }
  const { bmp } = decoded;
  let rgba: RgbaPixels;
  try {
    rgba = bitmapToRgba(bmp, bmp.width, bmp.height);
  } finally {
    bmp.close();
  }
  const origOpaque = isOpaqueImageData(rgba);
  if (args.keyWhite && origOpaque) {
    rgba = keyWhiteImageData(rgba, args.tol);
  }
  const box = trimBounds(rgba);
  if (!box || Math.min(box[2], box[3]) < LOGO_LIMITS.minSide) {
    ok(id, JSON.stringify({ ok: false, error: LOGO_ERR.tooSmall }));
    return;
  }
  let trimmed = cropImageData(rgba, box);
  const longEdge = Math.max(trimmed.width, trimmed.height);
  if (longEdge > LOGO_LIMITS.storeLongEdge) {
    const scale = LOGO_LIMITS.storeLongEdge / longEdge;
    const nw = Math.max(1, Math.round(trimmed.width * scale));
    const nh = Math.max(1, Math.round(trimmed.height * scale));
    trimmed = resizeRgba(trimmed, nw, nh);
  }
  const stats = logoStats(trimmed);
  const name = cleanLogoName(args.name, file.name);
  const buf = own(new Uint8Array(trimmed.data.buffer, trimmed.data.byteOffset, trimmed.data.byteLength));
  ok(
    id,
    JSON.stringify({
      ok: true,
      name,
      w: trimmed.width,
      h: trimmed.height,
      coverage: stats.coverage,
      opaque: stats.opaque,
      lum: stats.lum,
    }),
    [buf],
  );
}

async function run(id: number, op: CoreOp, req: CoreRequest): Promise<void> {
  const { buffer, args, file, meta } = req;
  await load();
  if (op === 'describe' && buffer) describe(id, buffer);
  else if (op === 'dat' && buffer) dat(id, buffer);
  else if (op === 'calib-file' && file && meta) await calibFile(id, file, meta);
  else if (op === 'calib-restore') await calibRestore(id);
  else if (op === 'bake' && args) bake(id, args);
  else if (op === 'resolve' && req.resolve) resolve(id, req.resolve);
  else if (op === 'calib-check' && req.check) calibCheck(id, req.check);
  else if (op === 'frame-meta' && req.frameMeta) await frameMeta(id, req.frameMeta);
  else if (op === 'frame-layout' && req.frameLayout) await frameLayoutOp(id, req.frameLayout);
  else if (op === 'logo-preview' && file) await logoPreview(id, file);
  else if (op === 'logo-save' && file && req.logoSave) await logoSave(id, file, req.logoSave);
  else throw new Error('未知请求');
}

self.onmessage = (event: MessageEvent<CoreRequest>) => {
  const { id, op } = event.data;
  if (op === 'ping') {
    ok(id, '{}');
    return;
  }
  run(id, op, event.data).catch((err: unknown) => failed(id, err));
};
