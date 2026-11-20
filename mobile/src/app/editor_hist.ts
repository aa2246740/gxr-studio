// 编辑器悬浮直方图画布绘制与底栏已修改圆点判定。

import { curveJson } from '../lib/export_request';
import { histPoint, histShape } from '../lib/hist';
import { el } from './dom';
import type { EditedFlags } from './editor_bar';
import type { Photo } from './model';

export interface HistWidget {
  root: HTMLElement;
  canvas: HTMLCanvasElement;
  clipLow: HTMLElement;
  clipHigh: HTMLElement;
}

export function createHistWidget(): HistWidget {
  const root = el('div', 'hist hist-float');
  const canvas = document.createElement('canvas');
  canvas.width = 408;
  canvas.height = 112;
  const clipLow = el('span', 'clip l');
  const clipHigh = el('span', 'clip r');
  clipLow.setAttribute('aria-label', '暗部压死');
  clipHigh.setAttribute('aria-label', '高光溢出');
  root.append(canvas, clipLow, clipHigh);
  return { root, canvas, clipLow, clipHigh };
}

export function drawHistCanvas(
  widget: HistWidget | null,
  counts: Uint32Array,
  samples: number,
  version: number,
): void {
  if (!widget) return;
  const { canvas, clipLow, clipHigh } = widget;
  const ctx = canvas.getContext('2d');
  if (!ctx) return;
  const shape = histShape(counts, samples);
  const w = canvas.width;
  const h = canvas.height;
  ctx.clearRect(0, 0, w, h);
  const area = (values: Float64Array, color: string) => {
    ctx.beginPath();
    ctx.moveTo(0, h);
    for (let i = 0; i < 256; i++) {
      const [x, y] = histPoint(i, values[i], w, h);
      ctx.lineTo(x, y);
    }
    ctx.lineTo(w, h);
    ctx.closePath();
    ctx.fillStyle = color;
    ctx.fill();
  };
  ctx.globalCompositeOperation = 'screen';
  area(shape.r, 'rgba(255,70,70,.62)');
  area(shape.g, 'rgba(60,220,110,.62)');
  area(shape.b, 'rgba(70,130,255,.7)');
  ctx.globalCompositeOperation = 'source-over';
  ctx.beginPath();
  for (let i = 0; i < 256; i++) {
    const [x, y] = histPoint(i, shape.l[i], w, h);
    if (i === 0) ctx.moveTo(x, y);
    else ctx.lineTo(x, y);
  }
  ctx.strokeStyle = 'rgba(255,255,255,.85)';
  ctx.lineWidth = 2;
  ctx.stroke();
  canvas.dataset.version = String(version);
  clipLow.classList.toggle('lit', shape.low);
  clipHigh.classList.toggle('lit', shape.high);
}

export function editedFlagsOf(photo: Photo): EditedFlags {
  const c = photo.crop;
  const hasCrop =
    c !== null && (c.preset !== 'original' || c.rect[2] < 0.999 || c.rect[3] < 0.999 || c.rect[0] > 0.001 || c.rect[1] > 0.001);
  return {
    recipe: photo.choice !== null || photo.lut !== null,
    adjust: Object.values(photo.knobs).some((v) => v !== 0) || curveJson(photo.curve) !== null,
    crop: hasCrop || photo.geometry.straighten !== 0,
    more: photo.border.style !== 'none' || photo.stamp.enabled,
  };
}

/** 未套用边框/时间戳装饰时把 GPU 画布贴合照片实际宽高比，避免引擎的灰底黑边盖住舞台纯黑背景。 */
export function fitGpuCanvas(
  stage: HTMLElement | null,
  pv: HTMLElement | null,
  gpu: HTMLCanvasElement | null,
  size: { w: number; h: number } | null,
): boolean {
  if (!stage || !pv || !gpu || pv.dataset.decor === '1') return false;
  const sw = stage.clientWidth;
  const sh = stage.clientHeight;
  if (sw <= 0 || sh <= 0 || !size || size.w <= 0 || size.h <= 0) {
    const had = Boolean(gpu.style.width || gpu.style.height);
    gpu.style.removeProperty('width');
    gpu.style.removeProperty('height');
    return had;
  }
  const scale = Math.min(sw / size.w, sh / size.h);
  const wPx = `${Math.max(1, Math.round(size.w * scale))}px`;
  const hPx = `${Math.max(1, Math.round(size.h * scale))}px`;
  if (gpu.style.width === wPx && gpu.style.height === hPx) return false;
  gpu.style.width = wPx;
  gpu.style.height = hPx;
  return true;
}
