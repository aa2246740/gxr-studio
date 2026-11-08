// 把 finish_layout.js 算好的版面画到 Canvas2D 上（大图的叠加层和边框卡片共用）。
// 版面里的数字是导出时的像素；s 是「画布像素 / 导出像素」。画的时候不用 ctx.scale，坐标自己乘 s，
// 这样模糊半径等按设备像素算的量不会被变换带偏。

import { ascentOf, fontCss, fontReady, logoImage } from './finish_fonts.js';
import { GLOW_GAIN } from './finish_layout.js';

const rgb = (c) => `rgb(${c[0]}, ${c[1]}, ${c[2]})`;

// 字体还没下载好时先不画这行字（等字体到了会再画一遍），免得闪一下后备字体
const drawable = (item) => item.cjk || fontReady(item.font);

// 一个字一个字地画在版面算好的列上（item.xs），和导出一样：字距、连字、画布自己的字距都影响不到位置。
// 空格和控制字符不画（和 fonts.py 的 draw_run 一样）。
function drawText(ctx, item, dx, dy, s) {
  if (!drawable(item)) return;
  ctx.font = fontCss(item.font, item.px * s, item.cjk);
  ctx.fillStyle = rgb(item.fill);
  const baseline = dy + (item.y + ascentOf(item.font, item.px, item.cjk)) * s;
  Array.from(item.text).forEach((ch, i) => {
    if (ch !== ' ' && ch.codePointAt(0) >= 0x20) ctx.fillText(ch, dx + item.xs[i] * s, baseline);
  });
}

function drawPoly(ctx, item, dx, dy, s) {
  ctx.fillStyle = rgb(item.fill);
  ctx.beginPath();
  item.pts.forEach(([x, y], i) => (i ? ctx.lineTo(dx + x * s, dy + y * s) : ctx.moveTo(dx + x * s, dy + y * s)));
  ctx.closePath();
  ctx.fill();
}

function drawFrameItems(ctx, frame, s, onLogo) {
  for (const item of frame.items) {
    if (item.k === 'text') {
      drawText(ctx, item, 0, 0, s);
    } else if (item.k === 'line') {
      ctx.strokeStyle = rgb(item.fill);
      ctx.lineWidth = item.w * s;
      ctx.beginPath();
      ctx.moveTo(item.x1 * s, item.y1 * s);
      ctx.lineTo(item.x2 * s, item.y2 * s);
      ctx.stroke();
    } else if (item.k === 'logo') {
      const img = logoImage(onLogo);
      if (img) {
        ctx.imageSmoothingQuality = 'high';
        ctx.drawImage(img, item.x * s, item.y * s, item.w * s, item.h * s);
      }
    }
  }
}

function paintOps(ctx, stamp, dx, dy, s) {
  for (const op of stamp.ops) {
    if (op.k === 'text') drawText(ctx, op, dx, dy, s);
    else drawPoly(ctx, op, dx, dy, s);
  }
}

let scratch = null;

// 日期戳：先画发光（文字的模糊影子，颜色同字，透明度乘 GLOW_GAIN），再画字。ox、oy 是照片在整张成片里的位置。
function drawStamp(ctx, stamp, ox, oy, s) {
  const dx = ox * s;
  const dy = oy * s;
  if (stamp.glow && 'filter' in ctx && (stamp.ops[0].cjk || fontReady(stamp.font))) {
    const sigma = stamp.glow * s;
    const pad = Math.ceil(sigma * 3) + 2;
    const [rx, ry, rw, rh] = stamp.rect;
    const x0 = Math.floor(dx + rx * s) - pad;
    const y0 = Math.floor(dy + ry * s) - pad;
    const w = Math.ceil(rw * s) + 2 * pad;
    const h = Math.ceil(rh * s) + 2 * pad;
    scratch = scratch || document.createElement('canvas');
    scratch.width = w;
    scratch.height = h;
    const sc = scratch.getContext('2d');
    paintOps(sc, stamp, dx - x0, dy - y0, s);
    ctx.save();
    ctx.filter = `blur(${sigma}px)`;
    ctx.drawImage(scratch, x0, y0);
    ctx.globalAlpha = GLOW_GAIN - 1;
    ctx.drawImage(scratch, x0, y0);
    ctx.restore();
  }
  paintOps(ctx, stamp, dx, dy, s);
}

// deco：{ frame, stamp, ew, eh }（stage_finish.js 的 decorOf）。ctx 已经清好；photo 给了就连底色和照片一起画（卡片），
// 不给就只画边框上的字、线、标志和日期戳（大图：照片和底色是别的元素）。
export function drawDecor(ctx, deco, s, { photo = null, onLogo = null } = {}) {
  const { frame, stamp } = deco;
  const [px, py] = frame ? frame.photo : [0, 0];
  if (photo) {
    if (frame) {
      ctx.fillStyle = rgb(frame.bg);
      ctx.fillRect(0, 0, Math.ceil(frame.width * s), Math.ceil(frame.height * s));
    }
    ctx.imageSmoothingQuality = 'high';
    ctx.drawImage(photo, px * s, py * s, deco.ew * s, deco.eh * s);
  }
  ctx.fontKerning = 'none';
  ctx.textBaseline = 'alphabetic';
  ctx.textAlign = 'left';
  if (frame) drawFrameItems(ctx, frame, s, onLogo);
  if (stamp) {
    const [sx, sy] = deco.stampOrigin || [px, py];
    drawStamp(ctx, stamp, sx, sy, s);
  }
}
