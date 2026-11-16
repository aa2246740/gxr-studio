// 把 Rust/WASM 算好的版面画到 Canvas2D 上（大图的叠加层和边框卡片共用，照桌面 `web/assets/finish_draw.js`）。
// 坐标手动乘 `s`，不用 `ctx.scale`；内置标志用 Atkinson 在标定框里画「GXR」，不引用任何理光数据。

import { ascentOf, GXR_MARK, GXR_MARK_ASCENT } from '../lib/fonts';
import { pickLogoMode, type Deco, type FrameItem, type FrameLayoutData, type StampLayoutData } from '../lib/frame_spec';
import { fontCss, fontReady } from './font_loader';
import { paintBackground } from './finish_bg';
import { paintCarrier } from './finish_texture';
import { logoStore } from './logo_store';

const GLOW_GAIN = 2.0;
type TextItem = Extract<FrameItem, { k: 'text' }>;
type ClogoItem = Extract<FrameItem, { k: 'clogo' }>;
type PolyItem = Extract<FrameItem, { k: 'poly' }>;
type RectItem = Extract<FrameItem, { k: 'rect' }>;
type StampOp = StampLayoutData['ops'][number];
type StampTextOp = Extract<StampOp, { k: 'text' }>;

const rgb = (c: readonly [number, number, number]): string => `rgb(${c[0]}, ${c[1]}, ${c[2]})`;

let tintCanvas: HTMLCanvasElement | null = null;
let scratch: HTMLCanvasElement | null = null;

function textInk(ctx: CanvasRenderingContext2D, item: TextItem | StampTextOp, s: number): [number, number, number] {
  if (!('auto_ink' in item) || !item.auto_ink) return item.fill ?? [22, 22, 26];
  const left = Math.max(0, Math.floor(((item.xs[0] || 0) - 6) * s));
  const top = Math.max(0, Math.floor(item.y * s));
  const right = Math.min(ctx.canvas.width, Math.ceil(((item.xs[item.xs.length - 1] || 0) + item.px + 6) * s));
  const bottom = Math.min(ctx.canvas.height, Math.ceil((item.y + item.px) * s));
  if (right <= left || bottom <= top) return item.auto_ink[0];
  const data = ctx.getImageData(left, top, right - left, bottom - top).data;
  let luma = 0;
  for (let i = 0; i < data.length; i += 4) luma += 0.299 * data[i] + 0.587 * data[i + 1] + 0.114 * data[i + 2];
  return item.auto_ink[luma / (data.length / 4) > 150 ? 1 : 0];
}

/** Port of `web/assets/finish_draw.js:drawText`. */
function drawText(ctx: CanvasRenderingContext2D, item: TextItem | StampTextOp, dx: number, dy: number, s: number): void {
  if (!fontReady(item.font as Parameters<typeof fontReady>[0])) return;
  ctx.font = fontCss(item.font as Parameters<typeof fontCss>[0], item.px * s);
  ctx.fillStyle = rgb(textInk(ctx, item, s));
  const baseline = dy + (item.y + ascentOf(item.font, item.px)) * s;
  const chars = Array.from(item.text);
  chars.forEach((ch, i) => {
    const cp = ch.codePointAt(0) ?? 0;
    if (ch !== ' ' && cp >= 0x20 && cp < 0x2e80) {
      ctx.fillText(ch, dx + item.xs[i] * s, baseline);
    }
  });
}

function drawBuiltInMark(
  ctx: CanvasRenderingContext2D,
  item: Extract<FrameItem, { k: 'logo' }>,
  frame: FrameLayoutData,
  s: number,
): void {
  if (!fontReady('atkinson')) return;
  const darkBg = 0.299 * frame.bg[0] + 0.587 * frame.bg[1] + 0.114 * frame.bg[2] < 128;
  const ink: [number, number, number] = item.fill ?? (darkBg ? [244, 244, 246] : [22, 22, 26]);
  const scaleMark = (item.h * s) / GXR_MARK.h;
  const px = 1000 * scaleMark;
  const baseline = item.y * s + GXR_MARK_ASCENT * scaleMark;
  ctx.save();
  ctx.font = fontCss('atkinson', px);
  ctx.fillStyle = rgb(ink);
  ctx.textBaseline = 'alphabetic';
  ctx.textAlign = 'center';
  ctx.fillText('GXR', (item.x + item.w / 2) * s, baseline);
  ctx.restore();
}

/** Port of `web/assets/finish_draw.js:drawClogo`. */
function drawClogo(ctx: CanvasRenderingContext2D, item: ClogoItem, s: number, onLogo: (() => void) | null): void {
  const img = logoStore.canvas(item.id, onLogo);
  if (!img) return;
  const info = logoStore.info(item.id);
  const opaque = Boolean(info?.opaque);
  let mode = opaque ? 'orig' : item.mode || 'orig';
  let ink: [number, number, number] = item.ink || [22, 22, 26];
  if (item.auto_ink && !opaque) {
    const x0 = Math.max(0, Math.floor(item.x * s));
    const y0 = Math.max(0, Math.floor(item.y * s));
    const x1 = Math.min(ctx.canvas.width, Math.ceil((item.x + item.w) * s));
    const y1 = Math.min(ctx.canvas.height, Math.ceil((item.y + item.h) * s));
    let bgL = 128;
    if (x1 > x0 && y1 > y0) {
      const data = ctx.getImageData(x0, y0, x1 - x0, y1 - y0).data;
      let sum = 0;
      for (let i = 0; i < data.length; i += 4) sum += 0.299 * data[i] + 0.587 * data[i + 1] + 0.114 * data[i + 2];
      bgL = sum / (data.length / 4);
    }
    ink = item.auto_ink[bgL > 150 ? 1 : 0];
    if (item.auto && info) mode = pickLogoMode(info, 'auto', bgL);
  }
  const dx = item.x * s;
  const dy = item.y * s;
  const dw = item.w * s;
  const dh = item.h * s;
  if (dw <= 0 || dh <= 0) return;
  if (mode === 'mono' && !opaque) {
    const tw = Math.max(1, Math.round(dw));
    const th = Math.max(1, Math.round(dh));
    tintCanvas ??= document.createElement('canvas');
    tintCanvas.width = tw;
    tintCanvas.height = th;
    const tc = tintCanvas.getContext('2d');
    if (!tc) return;
    tc.clearRect(0, 0, tw, th);
    tc.globalCompositeOperation = 'source-over';
    tc.imageSmoothingQuality = 'high';
    tc.drawImage(img, 0, 0, tw, th);
    tc.globalCompositeOperation = 'source-in';
    tc.fillStyle = rgb(ink);
    tc.fillRect(0, 0, tw, th);
    ctx.imageSmoothingQuality = 'high';
    ctx.drawImage(tintCanvas, dx, dy, dw, dh);
  } else {
    ctx.imageSmoothingQuality = 'high';
    ctx.drawImage(img, dx, dy, dw, dh);
  }
}

function drawPoly(ctx: CanvasRenderingContext2D, item: Pick<PolyItem, 'pts' | 'fill'>, dx: number, dy: number, s: number): void {
  ctx.fillStyle = rgb(item.fill);
  ctx.beginPath();
  item.pts.forEach(([x, y], i) => {
    if (i) ctx.lineTo(dx + x * s, dy + y * s);
    else ctx.moveTo(dx + x * s, dy + y * s);
  });
  ctx.closePath();
  ctx.fill();
}

function rectPath(ctx: CanvasRenderingContext2D, item: Pick<RectItem, 'x' | 'y' | 'w' | 'h' | 'radius'>, s: number, inset = 0): void {
  ctx.beginPath();
  const x = (item.x + inset) * s;
  const y = (item.y + inset) * s;
  const w = (item.w - 2 * inset) * s;
  const h = (item.h - 2 * inset) * s;
  if (w <= 0 || h <= 0) return;
  const r = Math.max(0, Math.min((item.radius || 0) - inset, Math.min(w, h) / s / 2)) * s;
  ctx.roundRect(x, y, w, h, r);
}

function rotatedText(ctx: CanvasRenderingContext2D, item: TextItem, s: number): void {
  if (!fontReady(item.font as Parameters<typeof fontReady>[0]) || item.gx === undefined || item.gy === undefined || !item.tw || !item.xs0) {
    return;
  }
  const cv = document.createElement('canvas');
  cv.width = Math.max(1, Math.ceil(item.tw * s));
  cv.height = Math.max(1, Math.ceil((ascentOf(item.font, item.px) + Math.ceil(item.px * 0.4)) * s));
  const sc = cv.getContext('2d');
  if (!sc) return;
  sc.fontKerning = 'none';
  sc.textBaseline = 'alphabetic';
  drawText(sc, { ...item, y: 0, xs: item.xs0 }, 0, 0, s);
  ctx.save();
  ctx.translate(item.gx * s, (item.gy + item.tw) * s);
  ctx.rotate(-Math.PI / 2);
  ctx.drawImage(cv, 0, 0);
  ctx.restore();
}

/** Port of `web/assets/finish_draw.js:drawFrameItems`. */
function drawFrameItems(
  ctx: CanvasRenderingContext2D,
  frame: FrameLayoutData,
  s: number,
  onLogo: (() => void) | null,
  photo: CanvasImageSource | null,
): void {
  for (const item of frame.items) {
    if (item.k === 'photo') {
      if (photo) ctx.drawImage(photo, item.x * s, item.y * s, item.w * s, item.h * s);
      else ctx.clearRect(item.x * s, item.y * s, item.w * s, item.h * s);
    } else if (item.k === 'text') {
      if (item.rot) rotatedText(ctx, item, s);
      else drawText(ctx, item, 0, 0, s);
    } else if (item.k === 'poly') {
      drawPoly(ctx, item, 0, 0, s);
    } else if (item.k === 'carrier') {
      paintCarrier(ctx, item, s);
    } else if (item.k === 'ellipse') {
      ctx.beginPath();
      ctx.ellipse(item.cx * s, item.cy * s, item.r * s, item.r * s, 0, 0, 2 * Math.PI);
      ctx.fillStyle = rgb(item.fill);
      ctx.fill();
    } else if (item.k === 'rect') {
      if (item.fill) {
        rectPath(ctx, item, s);
        ctx.fillStyle = rgb(item.fill);
        ctx.fill();
      }
      if (item.outline) {
        const sw = item.stroke_w || 1;
        rectPath(ctx, item, s, sw / 2);
        ctx.strokeStyle = rgb(item.outline);
        ctx.lineWidth = sw * s;
        ctx.stroke();
      }
    } else if (item.k === 'shadow') {
      if (!item.alpha) continue;
      ctx.save();
      ctx.shadowColor = `rgba(0,0,0,${item.alpha})`;
      ctx.shadowBlur = 2 * item.blur * s;
      ctx.shadowOffsetX = (item.dx || 0) * s;
      ctx.shadowOffsetY = (item.dy || 0) * s;
      rectPath(ctx, item, s);
      ctx.fillStyle = 'rgb(0,0,0)';
      ctx.fill();
      ctx.restore();
    } else if (item.k === 'hole') {
      ctx.save();
      rectPath(ctx, item, s);
      ctx.clip();
      paintBackground(ctx, frame, s);
      ctx.restore();
    } else if (item.k === 'line') {
      ctx.strokeStyle = rgb(item.fill);
      ctx.lineWidth = item.w * s;
      ctx.beginPath();
      ctx.moveTo(item.x1 * s, item.y1 * s);
      ctx.lineTo(item.x2 * s, item.y2 * s);
      ctx.stroke();
    } else if (item.k === 'logo') {
      drawBuiltInMark(ctx, item, frame, s);
    } else if (item.k === 'clogo') {
      drawClogo(ctx, item, s, onLogo);
    }
  }
}

function paintOps(ctx: CanvasRenderingContext2D, stamp: StampLayoutData, dx: number, dy: number, s: number): void {
  for (const op of stamp.ops) {
    if (op.k === 'text') drawText(ctx, op, dx, dy, s);
    else drawPoly(ctx, op, dx, dy, s);
  }
}

/** Port of `web/assets/finish_draw.js:drawStamp`. */
function drawStamp(ctx: CanvasRenderingContext2D, stamp: StampLayoutData, ox: number, oy: number, s: number): void {
  const dx = ox * s;
  const dy = oy * s;
  const first = stamp.ops[0];
  const canGlow = Boolean(
    stamp.glow &&
      'filter' in ctx &&
      (!first || first.k !== 'text' || fontReady(stamp.font as Parameters<typeof fontReady>[0])),
  );
  if (stamp.glow && canGlow) {
    const sigma = stamp.glow * s;
    const pad = Math.ceil(sigma * 3) + 2;
    const [rx, ry, rw, rh] = stamp.rect;
    const x0 = Math.floor(dx + rx * s) - pad;
    const y0 = Math.floor(dy + ry * s) - pad;
    const w = Math.max(1, Math.ceil(rw * s) + 2 * pad);
    const h = Math.max(1, Math.ceil(rh * s) + 2 * pad);
    scratch ??= document.createElement('canvas');
    scratch.width = w;
    scratch.height = h;
    const sc = scratch.getContext('2d');
    if (sc) {
      paintOps(sc, stamp, dx - x0, dy - y0, s);
      ctx.save();
      ctx.filter = `blur(${sigma}px)`;
      ctx.drawImage(scratch, x0, y0);
      ctx.globalAlpha = GLOW_GAIN - 1;
      ctx.drawImage(scratch, x0, y0);
      ctx.restore();
    }
  }
  paintOps(ctx, stamp, dx, dy, s);
}

/** Port of `web/assets/finish_draw.js:drawDecor`. */
export function drawDecor(
  ctx: CanvasRenderingContext2D,
  deco: Deco,
  s: number,
  options: {
    photo?: CanvasImageSource | null;
    backdrop?: HTMLCanvasElement | null;
    onLogo?: (() => void) | null;
  } = {},
): void {
  const { photo = null, backdrop = null, onLogo = null } = options;
  const { frame, stamp } = deco;
  const [px, py] = frame ? frame.photo : [0, 0];
  ctx.imageSmoothingQuality = 'high';
  if (frame) paintBackground(ctx, frame, s, backdrop);
  else if (photo) ctx.drawImage(photo, 0, 0, deco.ew * s, deco.eh * s);
  ctx.fontKerning = 'none';
  ctx.textBaseline = 'alphabetic';
  ctx.textAlign = 'left';
  if (frame) drawFrameItems(ctx, frame, s, onLogo, photo);
  if (stamp) {
    const [sx, sy] = deco.stampOrigin ?? [px, py];
    drawStamp(ctx, stamp, sx, sy, s);
  }
}
