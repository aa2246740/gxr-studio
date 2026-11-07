// 边框和时间戳在浏览器里画字用的字体：和导出用的是同一批文件（/api/finish/assets/），第一次用到时才下载。
// 量字交给画布的 measureText，给 finish_layout.js 当 Metrics（advance、bbox）。
// bbox 照 Pillow 的 textbbox 的约定：横向是 [min(0, 字形左沿), max(字宽, 字形右沿)]，纵向是字形上沿到基线（或字形下沿）。

import { ascentPx, DSEG_TICK_EM } from './finish_layout.js';

const FILES = {
  atkinson: 'AtkinsonHyperlegible-Regular.ttf',
  inter: 'Inter-Regular.ttf',
  courier: 'CourierPrime-Regular.ttf',
  patrick: 'PatrickHand-Regular.ttf',
  dseg: 'DSEG7Classic-Regular.ttf',
};
const FAMILY = { atkinson: 'GXR Atkinson', inter: 'GXR Inter', courier: 'GXR Courier', patrick: 'GXR Patrick', dseg: 'GXR DSEG' };
// 导出时中文用系统字体，顺序和 stamp.py 的 _CJK_FONTS 一样
const CJK = '"PingFang SC", "Hiragino Sans GB", "STHeiti", sans-serif';
const EPS = 0.02; // 字形边界的浮点误差，向上取整之前先减掉

const loading = new Map(); // 字体代号 → Promise
const ready = new Set();
let logo = null; // { img, promise }

export const fontReady = (id) => ready.has(id);

export function loadFont(id) {
  if (!loading.has(id)) {
    loading.set(id, (async () => {
      const res = await fetch(`/api/finish/assets/${FILES[id]}`, { credentials: 'same-origin' });
      if (!res.ok) throw new Error(`http_${res.status}`);
      const face = new FontFace(FAMILY[id], await res.arrayBuffer());
      await face.load();
      document.fonts.add(face);
      ready.add(id);
    })().catch((e) => {
      loading.delete(id); // 下次再试
      throw e;
    }));
  }
  return loading.get(id);
}

// 标志图片：返回已加载的图（还没好返回 null，好了以后 onReady 会被调用一次）
export function logoImage(onReady) {
  if (!logo) {
    const img = new Image();
    logo = { img, loaded: false, waiting: [] };
    img.addEventListener('load', () => {
      logo.loaded = true;
      for (const fn of logo.waiting.splice(0)) fn();
    });
    img.addEventListener('error', () => { logo = null; });
    img.src = '/api/finish/assets/ricoh.png';
  }
  if (logo.loaded) return logo.img;
  if (onReady) logo.waiting.push(onReady);
  return null;
}

// 这一组边框 + 时间戳要画哪些字体（代号）。中文自定义文字用系统字体，不在其中。
export function fontsNeeded(frame, stamp, stampTextIsCjk = false) {
  const ids = new Set();
  const style = frame && frame.style;
  if (style === 'leica' || style === 'hasselblad') ids.add('atkinson').add('inter');
  if (style === 'film') ids.add('courier');
  if (style === 'instant' && frame.text) ids.add('patrick');
  if (stamp && stamp.enabled && !stampTextIsCjk) ids.add({ seg7: 'dseg', typewriter: 'courier', sans: 'atkinson' }[stamp.font] || 'dseg');
  return [...ids];
}

export function fontCss(font, px, cjk) {
  return `${px}px ${cjk ? CJK : `"${FAMILY[font]}", sans-serif`}`;
}

let probe = null;
function context() {
  if (!probe) probe = document.createElement('canvas').getContext('2d');
  return probe;
}

// 中文字体的上沿高度：画布问字体自己（Pillow 用的也是字体的 hhea 上沿，取整到像素）
function cjkAscent(ctx, px) {
  const m = ctx.measureText('国');
  return Math.ceil((m.fontBoundingBoxAscent ?? px * 0.88) - EPS);
}

export function ascentOf(font, px, cjk) {
  if (!cjk) return ascentPx(font, px);
  const ctx = context();
  ctx.font = fontCss(font, px, true);
  return cjkAscent(ctx, px);
}

// 数码管字体没有「'」：撇号单独画，只占一个固定的宽度（Pillow 量出来的是缺字方块的宽度）
function textWidth(ctx, font, px, text) {
  if (font !== 'dseg' || !text.includes("'")) return ctx.measureText(text).width;
  const parts = text.split("'");
  let width = (parts.length - 1) * DSEG_TICK_EM * px;
  for (const part of parts) if (part) width += ctx.measureText(part).width;
  return width;
}

export function canvasMetrics() {
  const ctx = context();
  const set = (font, px, cjk) => {
    ctx.font = fontCss(font, px, cjk);
    ctx.textBaseline = 'alphabetic';
    ctx.textAlign = 'left';
    ctx.fontKerning = 'normal';
  };
  return {
    advance(font, px, text, cjk) {
      set(font, px, cjk);
      return textWidth(ctx, font, px, text);
    },
    bbox(font, px, text, cjk) {
      set(font, px, cjk);
      const clean = font === 'dseg' ? text.replaceAll("'", '') : text;
      const m = ctx.measureText(clean);
      const width = textWidth(ctx, font, px, text);
      const asc = cjk ? cjkAscent(ctx, px) : ascentPx(font, px);
      return [
        Math.min(0, Math.floor(-m.actualBoundingBoxLeft + EPS)),
        Math.floor(asc - m.actualBoundingBoxAscent + EPS),
        Math.max(Math.ceil(width - EPS), Math.ceil(m.actualBoundingBoxRight - EPS)),
        Math.ceil(asc + Math.max(0, m.actualBoundingBoxDescent) - EPS),
      ];
    },
  };
}
