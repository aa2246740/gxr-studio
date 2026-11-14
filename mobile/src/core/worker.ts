// 模块 Worker：WASM 只在这里加载。主线程把 DNG 的一份拷贝传进来，原件留给显卡引擎。
// 也在这里：读相机配方文件（camdata_*）、读校准数据 zip 并核对、做烘焙表（color_bake）。

import init, { camdata_entries, camdata_menu, camdata_parse, color_bake, dng_describe } from '../wasm-pkg/gxr_wasm.js';
import wasmUrl from '../wasm-pkg/gxr_wasm_bg.wasm?url';
import { checkPack } from '../lib/calib';
import { photoMetaFromTiff } from '../lib/exif';
import { readStoredZip } from '../lib/zip';
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

async function calib(id: number, buffer: ArrayBuffer): Promise<void> {
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
  const names = [...zip.entries.keys()];
  ok(
    id,
    JSON.stringify({ ok: true, units: checked.units, manifestSha256: checked.manifestSha256, names }),
    names.map((name) => own(zip.entries.get(name) ?? new Uint8Array())),
  );
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

async function run(id: number, op: CoreOp, buffer: ArrayBuffer | null, args: BakeArgs | null): Promise<void> {
  await load();
  if (op === 'describe' && buffer) describe(id, buffer);
  else if (op === 'dat' && buffer) dat(id, buffer);
  else if (op === 'calib' && buffer) await calib(id, buffer);
  else if (op === 'bake' && args) bake(id, args);
  else throw new Error('未知请求');
}

self.onmessage = (event: MessageEvent<CoreRequest>) => {
  const { id, op, buffer, args } = event.data;
  if (op === 'ping') {
    ok(id, '{}');
    return;
  }
  run(id, op, buffer ?? null, args ?? null).catch((err: unknown) => failed(id, err));
};
