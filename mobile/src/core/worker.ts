// 模块 Worker：WASM 只在这里加载。主线程把 DNG 的一份拷贝传进来，原件留给显卡引擎。
// 也在这里：读相机配方文件（camdata_*）、读校准数据 zip 并核对、做烘焙表（color_bake）。

import init, { camdata_entries, camdata_menu, camdata_parse, color_bake, dng_describe } from '../wasm-pkg/gxr_wasm.js';
import wasmUrl from '../wasm-pkg/gxr_wasm_bg.wasm?url';
import { checkPack } from '../lib/calib';
import { photoMetaFromTiff } from '../lib/exif';
import { loadPack, savePack } from '../lib/idb';
import { readStoredZip, ZIP_LIMITS } from '../lib/zip';
import type { BakeArgs, CoreOp, CoreReply, CoreRequest } from './protocol';

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
  const menu = camdata_menu(bytes, undefined);
  const entries = camdata_entries(bytes, undefined);
  ok(id, JSON.stringify({ summary, menu }), [own(entries)]);
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
    JSON.stringify({ ok: true, units: checked.units, manifestSha256: checked.manifestSha256, names, saved, ...extra }),
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

async function run(id: number, op: CoreOp, req: CoreRequest): Promise<void> {
  const { buffer, args, file, meta } = req;
  await load();
  if (op === 'describe' && buffer) describe(id, buffer);
  else if (op === 'dat' && buffer) dat(id, buffer);
  else if (op === 'calib-file' && file && meta) await calibFile(id, file, meta);
  else if (op === 'calib-restore') await calibRestore(id);
  else if (op === 'bake' && args) bake(id, args);
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
