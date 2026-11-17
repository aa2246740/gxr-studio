// HDR 增益的纯计算。不碰 WebGPU，测试可以直接引进来。

export interface GainMeta {
  gain_min: number;
  gain_max: number;
  gamma: number;
  offset_sdr: number;
  offset_hdr: number;
  cap_min: number;
  cap_max: number;
}

export interface GainTexture {
  width: number;
  height: number;
  codes: Uint8Array;
}

export type HdrOutputMode =
  | { hdr: false }
  | { hdr: true; gain: GainTexture; meta: GainMeta; headroom?: number | null };

/** headroom 是线性倍数（1 = SDR 白）。没有时用 cap_max，权重为 1。 */
export function gainWeight(meta: GainMeta, headroom: number | null | undefined): number {
  const ratio = headroom == null || !(headroom > 0) ? null : headroom;
  const H = ratio == null ? meta.cap_max : Math.log2(ratio);
  const span = meta.cap_max - meta.cap_min;
  if (!(span > 0)) return H >= meta.cap_max ? 1 : 0;
  return Math.min(1, Math.max(0, (H - meta.cap_min) / span));
}

export function f16ToF32(h: number): number {
  const sign = (h & 0x8000) !== 0 ? -1 : 1;
  const exp = (h & 0x7c00) >> 10;
  const frac = h & 0x03ff;
  if (exp === 0) return sign * 2 ** -14 * (frac / 1024);
  if (exp === 31) return frac === 0 ? sign * Infinity : Number.NaN;
  return sign * 2 ** (exp - 15) * (1 + frac / 1024);
}

export function presentMap(
  cw: number,
  ch: number,
  fw: number,
  fh: number,
  gray: number,
  shadowMax: number,
  highlightMin: number,
  overlay: { shadow: boolean; highlight: boolean },
  aspect: number | null,
): Float32Array {
  let sx = Math.max(fw / cw, fh / ch);
  let sy = sx;
  let ox = (cw - fw / sx) / 2;
  let oy = (ch - fh / sy) / 2;
  if (aspect !== null) {
    const dw = Math.min(cw, ch * aspect);
    const dh = dw / aspect;
    sx = fw / dw;
    sy = fh / dh;
    ox = (cw - dw) / 2;
    oy = (ch - dh) / 2;
  }
  return new Float32Array([
    sx, ox, oy, gray,
    fw, fh, sy, 0,
    shadowMax + 0.5, highlightMin - 0.5, overlay.shadow ? 1 : 0, overlay.highlight ? 1 : 0,
  ]);
}
