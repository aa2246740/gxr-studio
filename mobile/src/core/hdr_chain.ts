// 预览和导出共用的 HDR 增益链。顺序和 export_prepare / export_complete 里的 Rust 一样：
// 在未裁切、未变形的基图上 hdrtrack_begin，再按几何调用 hdrtrack_warp / hdrtrack_box，
// 然后是日期戳和边框（blur_fill 得到 frame_layout），最后 hdrtrack_to_grid、hdr_luma、hdr_gain。
// 这里不重写这些运算，只调用已经导出的 WASM。不解码 hdrtrack_finish 的增益 JPEG。

import type { GainMeta } from '../../../frontend/src/hdr_math';

export interface TrackOut {
  readonly active: boolean;
  readonly height: number;
  readonly width: number;
  readonly state: string;
  layer_bytes(): Uint8Array;
}

export interface GridOut {
  readonly gh: number;
  readonly gw: number;
  clip_bytes(): Uint8Array;
  raw_y_bytes(): Uint8Array;
}

export interface LumaOut {
  readonly height: number;
  readonly width: number;
  bytes(): Uint8Array;
}

export interface GainOut {
  readonly height: number;
  readonly width: number;
  readonly report: string;
  codes(): Uint8Array;
}

/** 这条链要的 WASM。测试可以传入已初始化的模块。 */
export interface HdrChainWasm {
  hdrtrack_begin(rgb: Uint8Array, w: number, h: number, hasDng: boolean, dng: Uint8Array, dngName: string, hasJpg: boolean, jpg: Uint8Array, params: string): TrackOut;
  hdrtrack_warp(layer: Float32Array, state: string, geom: string): TrackOut;
  hdrtrack_box(layer: Float32Array, state: string, x: number, y: number, w: number, h: number): TrackOut;
  hdrtrack_stamp(layer: Float32Array, state: string, mask: Uint8Array, w: number, h: number): TrackOut;
  hdrtrack_frame(layer: Float32Array, state: string, layout: string): TrackOut;
  hdrtrack_to_grid(layer: Float32Array, w: number, h: number, outW: number, outH: number, factor: number): GridOut;
  hdr_luma(rgb: Uint8Array, height: number, width: number, factor: number): LumaOut;
  hdr_gain(sdr: Float32Array, raw: Float32Array, clip: Uint8Array, height: number, width: number, params: string): GainOut;
  geom_final_box?(w: number, h: number, geometry: string, crop: string): string;
}

export interface PixelBox {
  x: number;
  y: number;
  w: number;
  h: number;
}

export interface HdrChainRequest {
  baseRgb: Uint8Array;
  baseWidth: number;
  baseHeight: number;
  sdrRgb: Uint8Array;
  sdrWidth: number;
  sdrHeight: number;
  dng: Uint8Array;
  jpg: Uint8Array | null;
  dngName: string;
  geometryJson: string | null;
  cropJson: string | null;
  box: PixelBox | null;
  frameLayoutJson: string | null;
  stamp: { mask: Uint8Array; width: number; height: number } | null;
  factor: number;
}

export type HdrChainResult =
  | { ok: true; width: number; height: number; codes: Uint8Array; meta: GainMeta }
  | { ok: false; reason: string };

const IDENTITY = '{"straighten":0,"vertical":0,"horizontal":0,"distortion":0,"rotate90":0,"flip":false}';

interface LiveTrack {
  width: number;
  height: number;
  state: string;
  layer: Float32Array;
}

function f32(bytes: Uint8Array): Float32Array {
  const copy = new ArrayBuffer(bytes.byteLength);
  new Uint8Array(copy).set(bytes);
  return new Float32Array(copy);
}

export function reasonOf(err: unknown): string {
  const text = err instanceof Error ? err.message : String(err);
  try {
    const parsed = JSON.parse(text) as { error?: { kind?: unknown } };
    if (typeof parsed.error?.kind === 'string') return parsed.error.kind;
  } catch {
    /* 不是核心的 JSON。 */
  }
  return 'hdr_unsupported';
}

function problemOf(stateText: string): string {
  try {
    const state = JSON.parse(stateText) as { problem?: unknown };
    if (typeof state.problem === 'string' && state.problem) return state.problem;
  } catch {
    /* 状态不是 JSON 时用总称。 */
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

/** 和 Geometry::is_identity 同一个判断，用来决定要不要调用 hdrtrack_warp。变形本身在 WASM 里。 */
export function geometryIsIdentity(json: string | null): boolean {
  if (!json || json === 'null' || json.trim() === '' || json === '{}') return true;
  let value: Record<string, unknown>;
  try {
    value = JSON.parse(json) as Record<string, unknown>;
  } catch {
    return false;
  }
  const num = (key: string) => (typeof value[key] === 'number' ? (value[key] as number) : 0);
  const turn = ((num('rotate90') % 4) + 4) % 4;
  return Math.abs(num('straighten')) < 1e-9 && num('vertical') === 0 && num('horizontal') === 0 && num('distortion') === 0 && turn === 0 && value.flip !== true;
}

function adopt(result: TrackOut): LiveTrack | { reason: string } {
  const active = result.active;
  const width = result.width;
  const height = result.height;
  const state = result.state;
  const bytes = result.layer_bytes();
  if (!active) return { reason: problemOf(state) };
  return { width, height, state, layer: f32(bytes) };
}

function fullFrame(box: PixelBox, width: number, height: number): boolean {
  return box.x === 0 && box.y === 0 && box.w === width && box.h === height;
}

function boxFromFinal(mod: HdrChainWasm, req: HdrChainRequest): PixelBox | { reason: string } | null {
  if (req.box) return req.box;
  if (!req.cropJson || !mod.geom_final_box) return null;
  try {
    const text = mod.geom_final_box(req.baseWidth, req.baseHeight, req.geometryJson && !geometryIsIdentity(req.geometryJson) ? req.geometryJson : IDENTITY, req.cropJson);
    const parsed = JSON.parse(text) as { box?: number[] };
    const box = parsed.box;
    if (!box || box.length !== 4 || box.some((n) => !Number.isInteger(n))) return { reason: 'bad_rect' };
    return { x: box[0] ?? 0, y: box[1] ?? 0, w: box[2] ?? 0, h: box[3] ?? 0 };
  } catch (err) {
    return { reason: reasonOf(err) };
  }
}

/**
 * 跑完整条链。`baseRgb` 是未裁切、未变形的基图，`sdrRgb` 是要对齐的当前画面（预览用自己的 factor）。
 * 失败返回 reason，不抛。
 */
export function runHdrChain(mod: HdrChainWasm, req: HdrChainRequest): HdrChainResult {
  if (req.baseWidth < 1 || req.baseHeight < 1 || req.sdrWidth < 1 || req.sdrHeight < 1) return { ok: false, reason: 'bad_input' };
  if (req.baseRgb.length !== req.baseWidth * req.baseHeight * 3 || req.sdrRgb.length !== req.sdrWidth * req.sdrHeight * 3) return { ok: false, reason: 'bad_input' };
  if (req.dng.length === 0 || req.factor < 1) return { ok: false, reason: 'bad_input' };
  let begun: TrackOut;
  try {
    begun = mod.hdrtrack_begin(
      req.baseRgb,
      req.baseWidth,
      req.baseHeight,
      true,
      req.dng,
      req.dngName,
      req.jpg !== null,
      req.jpg ?? new Uint8Array(0),
      '',
    );
  } catch (err) {
    return { ok: false, reason: reasonOf(err) };
  }
  let track = adopt(begun);
  if ('reason' in track) return { ok: false, reason: track.reason };
  if (req.geometryJson && !geometryIsIdentity(req.geometryJson)) {
    try {
      track = adopt(mod.hdrtrack_warp(track.layer, track.state, req.geometryJson));
    } catch (err) {
      return { ok: false, reason: reasonOf(err) };
    }
    if ('reason' in track) return { ok: false, reason: track.reason };
  }
  const box = boxFromFinal(mod, req);
  if (box && 'reason' in box) return { ok: false, reason: box.reason };
  if (box && !fullFrame(box, track.width, track.height)) {
    try {
      track = adopt(mod.hdrtrack_box(track.layer, track.state, box.x, box.y, box.w, box.h));
    } catch (err) {
      return { ok: false, reason: reasonOf(err) };
    }
    if ('reason' in track) return { ok: false, reason: track.reason };
  }
  if (req.stamp && req.stamp.mask.length > 0) {
    try {
      track = adopt(mod.hdrtrack_stamp(track.layer, track.state, req.stamp.mask, req.stamp.width, req.stamp.height));
    } catch (err) {
      return { ok: false, reason: reasonOf(err) };
    }
    if ('reason' in track) return { ok: false, reason: track.reason };
  }
  const layout = req.frameLayoutJson;
  if (layout && layout !== 'null') {
    try {
      track = adopt(mod.hdrtrack_frame(track.layer, track.state, layout));
    } catch (err) {
      return { ok: false, reason: reasonOf(err) };
    }
    if ('reason' in track) return { ok: false, reason: track.reason };
  }
  let grid: GridOut;
  let luma: LumaOut;
  try {
    grid = mod.hdrtrack_to_grid(track.layer, track.width, track.height, req.sdrWidth, req.sdrHeight, req.factor);
    luma = mod.hdr_luma(req.sdrRgb, req.sdrHeight, req.sdrWidth, req.factor);
  } catch (err) {
    return { ok: false, reason: reasonOf(err) };
  }
  const clip = grid.clip_bytes();
  const raw = f32(grid.raw_y_bytes());
  const gh = luma.height;
  const gw = luma.width;
  const sdr = f32(luma.bytes());
  let gain: GainOut;
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
