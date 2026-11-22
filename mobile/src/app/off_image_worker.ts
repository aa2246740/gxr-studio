// 短命关机图 Worker：单张处理（解码为 sRGB RGB → Rust WASM 铺满居中裁切 + 4:2:2 DRI 40 编码 + DAT 260 严格校验），
// 用完立即由主线程 terminate() 释放 WASM 线性内存。

import init, { off_image_check, off_image_make, type InitOutput } from '../wasm-pkg/gxr_wasm.js';
import wasmUrl from '../wasm-pkg/gxr_wasm_bg.wasm?url';

export interface OffImageErrorItem {
  code: string;
  message: string;
}

export type OffImageRequest =
  | { op: 'blob'; blob: Blob; corruptTail?: boolean }
  | { op: 'rgb'; rgb: ArrayBuffer; width: number; height: number; corruptTail?: boolean };

export type OffImageReply =
  | {
      ok: true;
      file: ArrayBuffer;
      bytes: number;
      quality: number;
      width: number;
      height: number;
      dat_state: string;
      errors: OffImageErrorItem[];
      wasmBytes: number;
    }
  | {
      ok: false;
      code: string;
      message: string;
      dat_state: string;
      errors: OffImageErrorItem[];
      wasmBytes: number;
    };

let ready: Promise<InitOutput> | null = null;
let wasm: InitOutput | null = null;

function load(): Promise<InitOutput> {
  ready ??= init({ module_or_path: wasmUrl }).then((out) => {
    wasm = out;
    return out;
  });
  return ready;
}

function parseErrorMessage(err: unknown): { code: string; message: string } {
  const text = typeof err === 'string' ? err : err instanceof Error ? err.message : String(err);
  try {
    const parsed = JSON.parse(text) as { error?: { kind?: unknown; message?: unknown }; message?: unknown };
    const code = typeof parsed.error?.kind === 'string' ? parsed.error.kind : 'core';
    const message =
      typeof parsed.error?.message === 'string'
        ? parsed.error.message
        : typeof parsed.message === 'string'
          ? parsed.message
          : text;
    return { code, message };
  } catch {
    return { code: 'core', message: text || '生成关机图失败。' };
  }
}

/** 按 EXIF 方向转正、sRGB 色彩并把透明通道铺在黑底上，输出紧凑 RGB 字节。 */
async function decodeBlobToRgb(blob: Blob): Promise<{ rgb: Uint8Array; width: number; height: number }> {
  if (typeof createImageBitmap !== 'function' || typeof OffscreenCanvas === 'undefined') {
    throw new Error('DECODE_NEEDED');
  }
  const bitmap = await createImageBitmap(blob, {
    imageOrientation: 'from-image',
    colorSpaceConversion: 'default',
    premultiplyAlpha: 'default',
  });
  const width = bitmap.width;
  const height = bitmap.height;
  let rgba: Uint8ClampedArray;
  try {
    const canvas = new OffscreenCanvas(width, height);
    const ctx = canvas.getContext('2d', { colorSpace: 'srgb', willReadFrequently: true });
    if (!ctx) throw new Error('DECODE_NEEDED');
    ctx.fillStyle = '#000000';
    ctx.fillRect(0, 0, width, height);
    ctx.drawImage(bitmap, 0, 0);
    rgba = ctx.getImageData(0, 0, width, height, { colorSpace: 'srgb' }).data;
  } finally {
    bitmap.close();
  }
  const rgb = new Uint8Array(width * height * 3);
  for (let i = 0, j = 0; i < rgba.length; i += 4, j += 3) {
    rgb[j] = rgba[i];
    rgb[j + 1] = rgba[i + 1];
    rgb[j + 2] = rgba[i + 2];
  }
  return { rgb, width, height };
}

function post(msg: OffImageReply, transfer: Transferable[] = []): void {
  self.postMessage(msg, { transfer });
}

self.onmessage = async (event: MessageEvent<OffImageRequest>) => {
  const req = event.data;
  try {
    let rgb: Uint8Array;
    let width: number;
    let height: number;
    if (req.op === 'blob') {
      try {
        const decoded = await decodeBlobToRgb(req.blob);
        rgb = decoded.rgb;
        width = decoded.width;
        height = decoded.height;
      } catch {
        post({
          ok: false,
          code: 'decode_needed',
          message: '图片无法直接解码。',
          dat_state: 'not_checked',
          errors: [],
          wasmBytes: wasm?.memory.buffer.byteLength ?? 0,
        });
        return;
      }
    } else {
      rgb = new Uint8Array(req.rgb);
      width = req.width;
      height = req.height;
    }

    const out = await load();
    const made = off_image_make(rgb, width, height);
    let bytes: Uint8Array;
    let meta: { bytes: number; quality: number; width: number; height: number; dat_state: string };
    try {
      bytes = made.file();
      meta = JSON.parse(made.json()) as { bytes: number; quality: number; width: number; height: number; dat_state: string };
    } finally {
      made.free();
    }

    if (req.corruptTail && bytes.length > 2) {
      bytes[bytes.length - 1] ^= 0xff;
    }

    const chk = JSON.parse(off_image_check(bytes)) as {
      ok: boolean;
      dat_state: string;
      errors: OffImageErrorItem[];
    };
    const wasmBytes = wasm?.memory.buffer.byteLength ?? out.memory.buffer.byteLength;
    if (!chk.ok) {
      post({
        ok: false,
        code: chk.errors[0]?.code ?? 'check_failed',
        message: chk.errors[0]?.message ?? '关机图校验未通过。',
        dat_state: chk.dat_state,
        errors: chk.errors,
        wasmBytes,
      });
      return;
    }

    const copy = new Uint8Array(bytes.byteLength);
    copy.set(bytes);
    const file = copy.buffer;
    post(
      {
        ok: true,
        file,
        bytes: copy.byteLength,
        quality: meta.quality,
        width: meta.width,
        height: meta.height,
        dat_state: chk.dat_state,
        errors: chk.errors,
        wasmBytes,
      },
      [file],
    );
  } catch (err) {
    const parsed = parseErrorMessage(err);
    post({
      ok: false,
      code: parsed.code,
      message: parsed.message,
      dat_state: 'error',
      errors: [{ code: parsed.code, message: parsed.message }],
      wasmBytes: wasm?.memory.buffer.byteLength ?? 0,
    });
  }
};
