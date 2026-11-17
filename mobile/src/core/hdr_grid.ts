// 预览用的增益网格。拟合走 hdr_chain（和导出同一条 WASM 链），不在这里重写数学。
// 不解码 hdrtrack_finish 的增益 JPEG。

import type { GainMeta } from '../../../frontend/src/hdr_math';
import { runHdrChain, type HdrChainWasm } from './hdr_chain';

export const PREVIEW_FACTOR = 4;
export const PREVIEW_LONG_EDGE = 1600;

/** 预览网格要用的导出。测试传入已初始化的模块。 */
export type HdrGridWasm = HdrChainWasm;

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

/**
 * 没有裁切、没有变形时的预览网格：基图和当前 SDR 是同一张。
 * 名字固定写成 preview.dng，不把真实文件名送进核心。
 */
export function fitPreviewGrid(mod: HdrGridWasm, rgb: Uint8Array, width: number, height: number, dng: Uint8Array, jpg: Uint8Array | null): PreviewGrid {
  return runHdrChain(mod, {
    baseRgb: rgb,
    baseWidth: width,
    baseHeight: height,
    sdrRgb: rgb,
    sdrWidth: width,
    sdrHeight: height,
    dng,
    jpg,
    dngName: 'preview.dng',
    geometryJson: null,
    cropJson: null,
    box: null,
    frameLayoutJson: null,
    stamp: null,
    factor: PREVIEW_FACTOR,
  });
}
