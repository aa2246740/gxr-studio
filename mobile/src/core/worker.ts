// 模块 Worker：WASM 只在这里加载。主线程把 DNG 的一份拷贝传进来，原件留给显卡引擎。
// 也在这里：读相机配方文件（camdata_*）、读校准数据 zip 并核对、做烘焙表（color_bake）。

import init, { camdata_entries, camdata_parse, color_bake, color_calib_check, color_calib_pack, color_listing, color_resolve, dng_describe, lut_identify, lut_parse } from '../wasm-pkg/gxr_wasm.js';
import wasmUrl from '../wasm-pkg/gxr_wasm_bg.wasm?url';
import { checkPack } from '../lib/calib';
import { photoMetaFromTiff } from '../lib/exif';
import { deleteLutRecord, loadLutBatchSnapshot, loadLutBlob, loadLutLibraryMeta, loadPack, patchLutRecord, saveLut, savePack } from '../lib/idb';
import { LUT_ERRORS, MAX_CUBE_BYTES, buildLutRecord, decodeCubeUtf8, readLutHeader, sha256Hex, type IdentifyReport, type LutRecord, type ParsedCubeSummary } from '../lib/lut';
import { readStoredZip, ZIP_LIMITS } from '../lib/zip';
import type { BakeArgs, CalibCheckArgs, CoreOp, CoreReply, CoreRequest, LutBlobArgs, LutDeleteArgs, LutImportArgs, LutPatchArgs, LutSnapshotArgs, ResolveArgs } from './protocol';

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

async function sha256Buffer(buffer: ArrayBuffer): Promise<string> {
  if (typeof crypto !== 'undefined' && crypto.subtle) {
    const digest = await crypto.subtle.digest('SHA-256', buffer);
    return [...new Uint8Array(digest)].map((b) => b.toString(16).padStart(2, '0')).join('');
  }
  return sha256Hex(new Uint8Array(buffer));
}

async function lutImport(id: number, args: LutImportArgs): Promise<void> {
  if (args.file.size > MAX_CUBE_BYTES) {
    ok(id, JSON.stringify({ ok: false, reason: 'too_big', message: LUT_ERRORS.cube_too_big }));
    return;
  }
  let buffer: ArrayBuffer;
  try {
    buffer = await args.file.arrayBuffer();
  } catch {
    ok(id, JSON.stringify({ ok: false, reason: 'unreadable', message: LUT_ERRORS.cube_unreadable }));
    return;
  }
  const bytes = new Uint8Array(buffer);
  const text = decodeCubeUtf8(bytes);
  if (text === null) {
    ok(id, JSON.stringify({ ok: false, reason: 'unreadable', message: LUT_ERRORS.cube_unreadable }));
    return;
  }
  let parsed: ParsedCubeSummary;
  let identified: IdentifyReport;
  try {
    parsed = JSON.parse(lut_parse(bytes)) as ParsedCubeSummary;
    identified = JSON.parse(lut_identify(bytes, args.filename)) as IdentifyReport;
  } catch (err) {
    const msg = coreMessage(err);
    const isUtf8 = /utf-?8/i.test(msg);
    ok(
      id,
      JSON.stringify({
        ok: false,
        reason: isUtf8 ? 'unreadable' : 'parse',
        message: isUtf8 || !msg ? LUT_ERRORS.cube_unreadable : msg,
      }),
    );
    return;
  }
  const cubeId = await sha256Buffer(buffer);
  const header = readLutHeader(text);
  const record = buildLutRecord({
    cubeId,
    filename: args.filename,
    byteLength: bytes.byteLength,
    parsed,
    identified,
    header,
    savedAt: args.version,
  });
  const saved = await saveLut(record, buffer, args.version);
  if (!saved.ok) {
    ok(
      id,
      JSON.stringify({
        ok: false,
        reason: saved.reason,
        message: saved.reason === 'full' ? LUT_ERRORS.library_full : '',
      }),
    );
    return;
  }
  ok(
    id,
    JSON.stringify({
      ok: true,
      duplicate: saved.duplicate,
      record: saved.record,
      totalBytes: saved.totalBytes,
    }),
  );
}

async function lutRestore(id: number): Promise<void> {
  const meta = await loadLutLibraryMeta();
  ok(id, JSON.stringify(meta));
}

async function lutPatch(id: number, args: LutPatchArgs): Promise<void> {
  const res = await patchLutRecord(args.cubeId, args.patch, args.version);
  ok(id, JSON.stringify(res));
}

async function lutDelete(id: number, args: LutDeleteArgs): Promise<void> {
  const deleted = await deleteLutRecord(args.cubeId, args.version);
  ok(id, JSON.stringify({ ok: deleted }));
}

async function lutBlob(id: number, args: LutBlobArgs): Promise<void> {
  const blob = await loadLutBlob(args.cubeId);
  if (!blob) {
    ok(id, JSON.stringify({ ok: false }));
    return;
  }
  ok(id, JSON.stringify({ ok: true }), [blob]);
}

async function lutSnapshot(id: number, args: LutSnapshotArgs): Promise<void> {
  const rows = await loadLutBatchSnapshot(args.cubeIds);
  const items: Array<{ cubeId: string; record: LutRecord | null; hasBlob: boolean }> = [];
  const buffers: ArrayBuffer[] = [];
  for (const row of rows) {
    if (row.blob) buffers.push(row.blob);
    items.push({ cubeId: row.cubeId, record: row.record, hasBlob: row.blob !== null });
  }
  ok(id, JSON.stringify({ items }), buffers);
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
  else if (op === 'lut-import' && req.lutImport) await lutImport(id, req.lutImport);
  else if (op === 'lut-restore') await lutRestore(id);
  else if (op === 'lut-patch' && req.lutPatch) await lutPatch(id, req.lutPatch);
  else if (op === 'lut-delete' && req.lutDelete) await lutDelete(id, req.lutDelete);
  else if (op === 'lut-blob' && req.lutBlob) await lutBlob(id, req.lutBlob);
  else if (op === 'lut-snapshot' && req.lutSnapshot) await lutSnapshot(id, req.lutSnapshot);
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
