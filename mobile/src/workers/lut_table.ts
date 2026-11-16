// 短命 Worker：每次只做一张 LUT 的 GPU 3D 表（lut_table），做完传回主线程并汇报 WASM 内存高水位，随后由主线程立刻 terminate()。

import init, { lut_table, type InitOutput } from '../wasm-pkg/gxr_wasm.js';
import wasmUrl from '../wasm-pkg/gxr_wasm_bg.wasm?url';

export interface LutTableRequest {
  cube: ArrayBuffer;
  spec: string;
}

export type LutTableReply =
  | {
      ok: true;
      metaJson: string;
      isDisplay: boolean;
      data: ArrayBuffer;
      wasmBytes: number;
    }
  | {
      ok: false;
      reason: string;
      message: string;
      wasmBytes: number;
    };

let wasmReady: Promise<InitOutput> | null = null;
let wasmOut: InitOutput | null = null;

function load(): Promise<InitOutput> {
  wasmReady ??= init({ module_or_path: wasmUrl }).then((out) => {
    wasmOut = out;
    return out;
  });
  return wasmReady;
}

function coreMessage(err: unknown): string {
  const text = typeof err === 'string' ? err : err instanceof Error ? err.message : String(err);
  try {
    const parsed = JSON.parse(text) as { error?: { message?: unknown }; message?: unknown };
    const message = parsed.error?.message ?? parsed.message;
    if (typeof message === 'string') return message;
  } catch {
    /* 原文 */
  }
  return text;
}

self.onmessage = async (event: MessageEvent<LutTableRequest>) => {
  const { cube, spec } = event.data;
  try {
    const out = await load();
    const table = lut_table(new Uint8Array(cube), spec);
    const reason = table.reason();
    const metaJson = table.meta_json();
    const isDisplay = table.is_display;
    const wasmBytes = wasmOut?.memory.buffer.byteLength ?? out.memory.buffer.byteLength;
    if (reason) {
      table.free();
      const reply: LutTableReply = { ok: false, reason, message: reason, wasmBytes };
      self.postMessage(reply);
      return;
    }
    const bytes = table.bytes();
    const data = bytes.buffer as ArrayBuffer;
    const reply: LutTableReply = { ok: true, metaJson, isDisplay, data, wasmBytes };
    self.postMessage(reply, { transfer: [data] });
  } catch (err) {
    const wasmBytes = wasmOut?.memory.buffer.byteLength ?? 0;
    const reply: LutTableReply = {
      ok: false,
      reason: 'core',
      message: coreMessage(err),
      wasmBytes,
    };
    self.postMessage(reply);
  }
};
