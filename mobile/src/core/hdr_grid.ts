// 预览用的增益网格。拟合只走已有的 WASM：hdrtrack_begin → hdrtrack_to_grid → hdr_luma → hdr_gain。
// 不解码 hdrtrack_finish 的增益 JPEG。

import type { GainMeta } from '../../../frontend/src/hdr_math';

export const PREVIEW_FACTOR = 4;
export const PREVIEW_LONG_EDGE = 1600;

export interface HdrTrackBegun {
  readonly active: boolean;
  readonly height: number;
  readonly width: number;
  readonly state: string;
  layer_bytes(): Uint8Array;
}

export interface HdrToGrid {
  readonly gh: number;
  readonly gw: number;
  clip_bytes(): Uint8Array;
  raw_y_bytes(): Uint8Array;
}

export interface HdrLumaOut {
  readonly height: number;
  readonly width: number;
  bytes(): Uint8Array;
}

export interface HdrGainOut {
  readonly height: number;
  readonly width: number;
  readonly report: string;
  codes(): Uint8Array;
}

/** 预览网格要用的四个导出。测试传入已初始化的模块。 */
export interface HdrGridWasm {
  hdrtrack_begin(rgb: Uint8Array, w: number, h: number, hasDng: boolean, dng: Uint8Array, dngName: string, hasJpg: boolean, jpg: Uint8Array, params: string): HdrTrackBegun;
  hdrtrack_to_grid(layer: Float32Array, w: number, h: number, outW: number, outH: number, factor: number): HdrToGrid;
  hdr_luma(rgb: Uint8Array, height: number, width: number, factor: number): HdrLumaOut;
  hdr_gain(sdr: Float32Array, raw: Float32Array, clip: Uint8Array, height: number, width: number, params: string): HdrGainOut;
}

export type PreviewGrid =
  | { ok: true; width: number; height: number; codes: Uint8Array; meta: GainMeta }
  | { ok: false; reason: string };

export function rgbaToRgb(rgba: Uint8Array, width: number, height: number): Uint8Array {
  const rgb = new Uint8Array(width * height * 3);
  const pixels = width * height;
  for (let i = 0; i < pixels; i++) {
    rgb[i * 3] = rgba[i * 4] ?? 0;
    rgb[i * 3 + 1] = rgba[i * 4 + 1] ?? 0;
    rgb[i * 3 + 2] = rgba[i * 4 + 2] ?? 0;
  }
  return rgb;
}

/** 长边收到 PREVIEW_LONG_EDGE 以内。块平均，四舍五入到整数码值。 */
export function downscaleRgba(src: Uint8Array, width: number, height: number, edge = PREVIEW_LONG_EDGE): { rgba: Uint8Array; width: number; height: number } {
  const long = Math.max(width, height);
  if (long <= edge || width < 1 || height < 1) return { rgba: src, width, height };
  const scale = edge / long;
  const dw = Math.max(1, Math.round(width * scale));
  const dh = Math.max(1, Math.round(height * scale));
  const out = new Uint8Array(dw * dh * 4);
  for (let y = 0; y < dh; y++) {
    const y0 = Math.floor((y * height) / dh);
    const y1 = Math.max(y0 + 1, Math.floor(((y + 1) * height) / dh));
    for (let x = 0; x < dw; x++) {
      const x0 = Math.floor((x * width) / dw);
      const x1 = Math.max(x0 + 1, Math.floor(((x + 1) * width) / dw));
      let r = 0;
      let g = 0;
      let b = 0;
      let a = 0;
      let n = 0;
      for (let yy = y0; yy < y1; yy++) {
        for (let xx = x0; xx < x1; xx++) {
          const i = (yy * width + xx) * 4;
          r += src[i] ?? 0;
          g += src[i + 1] ?? 0;
          b += src[i + 2] ?? 0;
          a += src[i + 3] ?? 0;
          n += 1;
        }
      }
      const o = (y * dw + x) * 4;
      out[o] = Math.round(r / n);
      out[o + 1] = Math.round(g / n);
      out[o + 2] = Math.round(b / n);
      out[o + 3] = Math.round(a / n);
    }
  }
  return { rgba: out, width: dw, height: dh };
}

function f32(bytes: Uint8Array): Float32Array {
  const copy = bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength);
  return new Float32Array(copy);
}

function reasonOf(err: unknown): string {
  const text = err instanceof Error ? err.message : String(err);
  try {
    const parsed = JSON.parse(text) as { error?: { kind?: unknown } };
    if (typeof parsed.error?.kind === 'string') return parsed.error.kind;
  } catch {
    /* 不是核心的 JSON。 */
  }
  return 'hdr_unsupported';
}

function metaOf(report: string): GainMeta | null {
  try {
    const parsed = JSON.parse(report) as { meta?: Partial<GainMeta> };
    const meta = parsed.meta;
    if (!meta) return null;
    const num = (value: unknown, fallback: number) => (typeof value === 'number' && Number.isFinite(value) ? value : fallback);
    return {
      gain_min: num(meta.gain_min, 0),
      gain_max: num(meta.gain_max, 1),
      gamma: num(meta.gamma, 1),
      offset_sdr: num(meta.offset_sdr, 1 / 64),
      offset_hdr: num(meta.offset_hdr, 1 / 64),
      cap_min: num(meta.cap_min, 0),
      cap_max: num(meta.cap_max, 1),
    };
  } catch {
    return null;
  }
}

/**
 * 同一张预览尺寸的 SDR（uint8 RGB）和 DNG。失败（对不齐、拍摄信息不符、拟合不了）返回 reason，不抛。
 * 名字固定写成 preview.dng，不把真实文件名送进核心。
 */
export function fitPreviewGrid(mod: HdrGridWasm, rgb: Uint8Array, width: number, height: number, dng: Uint8Array, jpg: Uint8Array | null): PreviewGrid {
  if (width < 1 || height < 1 || rgb.length !== width * height * 3 || dng.length === 0) return { ok: false, reason: 'bad_input' };
  let begun: HdrTrackBegun;
  try {
    begun = mod.hdrtrack_begin(rgb, width, height, true, dng, 'preview.dng', jpg !== null, jpg ?? new Uint8Array(0), '');
  } catch (err) {
    return { ok: false, reason: reasonOf(err) };
  }
  const stateText = begun.state;
  if (!begun.active) {
    let reason = 'hdr_unsupported';
    try {
      const state = JSON.parse(stateText) as { problem?: unknown };
      if (typeof state.problem === 'string' && state.problem) reason = state.problem;
    } catch {
      /* 状态不是 JSON 时用总称。 */
    }
    return { ok: false, reason };
  }
  const layerW = begun.width;
  const layerH = begun.height;
  const layer = f32(begun.layer_bytes());
  let grid: HdrToGrid;
  let luma: HdrLumaOut;
  try {
    grid = mod.hdrtrack_to_grid(layer, layerW, layerH, width, height, PREVIEW_FACTOR);
    luma = mod.hdr_luma(rgb, height, width, PREVIEW_FACTOR);
  } catch (err) {
    return { ok: false, reason: reasonOf(err) };
  }
  const clip = grid.clip_bytes();
  const raw = f32(grid.raw_y_bytes());
  const gh = luma.height;
  const gw = luma.width;
  const sdr = f32(luma.bytes());
  let gain: HdrGainOut;
  try {
    gain = mod.hdr_gain(sdr, raw, clip, gh, gw, '');
  } catch (err) {
    return { ok: false, reason: reasonOf(err) };
  }
  const meta = metaOf(gain.report);
  const gridW = gain.width;
  const gridH = gain.height;
  const codes = gain.codes();
  if (!meta) return { ok: false, reason: 'hdr_unsupported' };
  return { ok: true, width: gridW, height: gridH, codes, meta };
}
