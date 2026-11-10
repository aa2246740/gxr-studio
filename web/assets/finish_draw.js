// 把 finish_layout.js 算好的版面画到 Canvas2D 上（大图的叠加层和边框卡片共用）。
// 版面里的数字是导出时的像素；s 是「画布像素 / 导出像素」。画的时候不用 ctx.scale，坐标自己乘 s，
// 这样模糊半径等按设备像素算的量不会被变换带偏。

import { ascentOf, fontCss, fontReady, logoImage } from './finish_fonts.js';
import { GLOW_GAIN } from './finish_layout.js';
import { paintBackground } from './finish_background.js';
import { paintCarrier } from './finish_texture.js';

const rgb = (c) => `rgb(${c[0]}, ${c[1]}, ${c[2]})`;

// 字体还没下载好时先不画这行字（等字体到了会再画一遍），免得闪一下后备字体
const drawable = (item) => item.cjk || fontReady(item.font);

// 一个字一个字地画在版面算好的列上（item.xs），和导出一样：字距、连字、画布自己的字距都影响不到位置。
// 空格和控制字符不画（和 fonts.py 的 draw_run 一样）。
function drawText(ctx, item, dx, dy, s) {
  if (!drawable(item)) return;
  ctx.font = fontCss(item.font, item.px * s, item.cjk);
  ctx.fillStyle = rgb(textInk(ctx,item,s));
  const baseline = dy + (item.y + ascentOf(item.font, item.px, item.cjk)) * s;
  Array.from(item.text).forEach((ch, i) => {
    if (ch !== ' ' && ch.codePointAt(0) >= 0x20) {
      ctx.font = fontCss(item.font, item.px * s, item.cjk || ch.codePointAt(0) >= 0x2e80);
      ctx.fillText(ch, dx + item.xs[i] * s, baseline);
    }
  });
}

function textInk(ctx,item,s) {
  if(!item.auto_ink) return item.fill;
  const left=Math.max(0,Math.floor(((item.xs[0]||0)-6)*s)),top=Math.max(0,Math.floor(item.y*s));
  const right=Math.min(ctx.canvas.width,Math.ceil(((item.xs.at(-1)||0)+item.px+6)*s));
  const bottom=Math.min(ctx.canvas.height,Math.ceil((item.y+item.px)*s));
  if(right<=left||bottom<=top) return item.auto_ink[0];
  const data=ctx.getImageData(left,top,right-left,bottom-top).data;
  let luma=0;
  for(let i=0;i<data.length;i+=4) luma+=0.299*data[i]+0.587*data[i+1]+0.114*data[i+2];
  return item.auto_ink[luma/(data.length/4)>150?1:0];
}

function drawPoly(ctx, item, dx, dy, s) {
  ctx.fillStyle = rgb(item.fill);
  ctx.beginPath();
  item.pts.forEach(([x, y], i) => (i ? ctx.lineTo(dx + x * s, dy + y * s) : ctx.moveTo(dx + x * s, dy + y * s)));
  ctx.closePath();
  ctx.fill();
}

function rectPath(ctx, item, s, inset = 0) {
  ctx.beginPath();
  const x=(item.x+inset)*s,y=(item.y+inset)*s,w=(item.w-2*inset)*s,h=(item.h-2*inset)*s;
  if (w<=0 || h<=0) return;
  ctx.roundRect(x,y,w,h,Math.max(0,Math.min((item.radius||0)-inset,Math.min(w,h)/s/2))*s);
}

function rotatedText(ctx,item,s) {
  if (!drawable(item)) return;
  const cv=document.createElement('canvas');
  cv.width=Math.ceil(item.tw*s);
  cv.height=Math.ceil((ascentOf(item.font,item.px,false)+Math.ceil(item.px*0.4))*s);
  const sc=cv.getContext('2d');
  sc.fontKerning='none';sc.textBaseline='alphabetic';
  drawText(sc,{...item,y:0,xs:item.xs0},0,0,s);
  ctx.save();ctx.translate(item.gx*s,(item.gy+item.tw)*s);ctx.rotate(-Math.PI/2);
  ctx.drawImage(cv,0,0);ctx.restore();
}

function drawFrameItems(ctx, frame, s, onLogo, photo) {
  for (const item of frame.items) {
    if (item.k === 'photo') {
      if (photo) ctx.drawImage(photo,item.x*s,item.y*s,item.w*s,item.h*s);
      else ctx.clearRect(item.x*s,item.y*s,item.w*s,item.h*s);
    } else if (item.k === 'text') {
      if (item.rot) rotatedText(ctx,item,s);
      else drawText(ctx, item, 0, 0, s);
    } else if (item.k === 'poly') drawPoly(ctx,item,0,0,s);
    else if (item.k === 'carrier') paintCarrier(ctx,item,s);
    else if (item.k === 'ellipse') {
      ctx.beginPath();ctx.ellipse(item.cx*s,item.cy*s,item.r*s,item.r*s,0,0,2*Math.PI);
      ctx.fillStyle=rgb(item.fill);ctx.fill();
    }
    else if (item.k === 'rect') {
      if (item.fill) { rectPath(ctx,item,s);ctx.fillStyle=rgb(item.fill);ctx.fill(); }
      if (item.outline) {
        const sw=item.stroke_w||1;
        rectPath(ctx,item,s,sw/2);ctx.strokeStyle=rgb(item.outline);ctx.lineWidth=sw*s;ctx.stroke();
      }
    } else if (item.k === 'shadow') {
      if (!item.alpha) continue;
      ctx.save();
      ctx.shadowColor=`rgba(0,0,0,${item.alpha})`;
      ctx.shadowBlur=2*item.blur*s;ctx.shadowOffsetX=(item.dx||0)*s;ctx.shadowOffsetY=(item.dy||0)*s;
      rectPath(ctx,item,s);ctx.fillStyle='rgb(0,0,0)';ctx.fill();ctx.restore();
    } else if (item.k === 'hole') {
      ctx.save();rectPath(ctx,item,s);ctx.clip();paintBackground(ctx,frame,s);ctx.restore();
    } else if (item.k === 'line') {
      ctx.strokeStyle = rgb(item.fill);ctx.lineWidth = item.w * s;ctx.beginPath();
      ctx.moveTo(item.x1 * s, item.y1 * s);ctx.lineTo(item.x2 * s, item.y2 * s);ctx.stroke();
    } else if (item.k === 'logo') {
      const img = logoImage(onLogo);
      if (img) {ctx.imageSmoothingQuality = 'high';ctx.drawImage(img,item.x*s,item.y*s,item.w*s,item.h*s);}
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
export function drawDecor(ctx, deco, s, { photo = null, backdrop = photo, onLogo = null } = {}) {
  const { frame, stamp } = deco;
  const [px, py] = frame ? frame.photo : [0, 0];
  ctx.imageSmoothingQuality = 'high';
  if (frame) paintBackground(ctx,frame,s,backdrop);
  else if (photo) ctx.drawImage(photo,0,0,deco.ew*s,deco.eh*s);
  ctx.fontKerning = 'none';
  ctx.textBaseline = 'alphabetic';
  ctx.textAlign = 'left';
  if (frame) drawFrameItems(ctx, frame, s, onLogo, photo);
  if (stamp) {
    const [sx, sy] = deco.stampOrigin || [px, py];
    drawStamp(ctx, stamp, sx, sy, s);
  }
}
