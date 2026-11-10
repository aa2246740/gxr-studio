// 边框和时间戳用的字体：和导出用的是同一批文件（/api/finish/assets/），第一次用到时才下载（页面空闲时也会先下好）。
// 每个字体两样东西：字体文件（画字用）和版面表（量字用）。版面表是服务器从字体文件里读出来的整数
// （步进宽度、字形外框、上沿高度，/api/finish/assets/<字体>.json），和导出读的是同一份；版面（finish_layout.js）只用它，
// 不用画布量字，所以不受这台电脑的字体渲染、字距、排版引擎影响。画布只负责把字画出来。
// 中文回退字体读不到表：按 1000 像素的字号用画布量一次（当作 upm = 1000），这一项只能和系统字体一样准。

import { ascentPx } from './finish_layout.js';

const FILES = {
  atkinson: 'AtkinsonHyperlegible-Regular.ttf',
  inter: 'Inter-Regular.ttf',
  inter_sb: 'Inter-SemiBold.ttf',
  courier: 'CourierPrime-Regular.ttf',
  patrick: 'PatrickHand-Regular.ttf',
  dseg: 'DSEG7Classic-Regular.ttf',
};
const FAMILY = { atkinson: 'GXR Atkinson', inter: 'GXR Inter', inter_sb: 'GXR Inter SemiBold', courier: 'GXR Courier', patrick: 'GXR Patrick', dseg: 'GXR DSEG' };
// 导出时中文用系统字体，顺序和 stamp.py 的 _CJK_FONTS 一样
const CJK = '"PingFang SC", "Hiragino Sans GB", "STHeiti", sans-serif';
const CJK_PX = 1000; // 和 fonts.py 的 CJK_REFERENCE_PX 一样
const EPS = 0.02; // 画布量出来的浮点误差，向上取整之前先减掉

const loading = new Map(); // 字体代号 → Promise（字体文件）
const loadingTable = new Map(); // 字体代号 → Promise（版面表）
const ready = new Set();
const tables = new Map(); // 字体代号 → { upm, ascender, notdef, glyphs }
let logo = null; // { img, loaded, waiting }

export const fontReady = (id) => ready.has(id);
export const tableReady = (id) => tables.has(id);

// 列式的表（fonts.py 的 FontTable.to_json）→ { upm, ascender, notdef: [步进, 外框 | null], glyphs: Map(码点 → 同样的) }
function build(json) {
  const box = (v) => (v[2] > v[0] && v[3] > v[1] ? v : null); // 没有墨迹的字形写四个 0
  const glyphs = new Map();
  json.cps.forEach((cp, i) => glyphs.set(cp, [json.adv[i], box(json.box.slice(4 * i, 4 * i + 4))]));
  return { upm: json.upm, ascender: json.ascender, notdef: [json.notdef[0], box(json.notdef.slice(1))], glyphs };
}

// 装上一份表（loadTable 下载完就调它；node 里的测试直接给）
export function useTable(id, json) {
  tables.set(id, build(json));
}

export function loadTable(id) {
  if (!loadingTable.has(id)) {
    loadingTable.set(id, (async () => {
      const res = await fetch(`/api/finish/assets/${id}.json`, { credentials: 'same-origin' });
      if (!res.ok) throw new Error(`http_${res.status}`);
      useTable(id, await res.json());
    })().catch((e) => {
      loadingTable.delete(id); // 下次再试
      throw e;
    }));
  }
  return loadingTable.get(id);
}

function loadFace(id) {
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

// 字体文件和版面表都到了才算好
export function loadFont(id) {
  return Promise.all([loadTable(id), loadFace(id)]);
}

// 页面空闲时把六个字体先下好：第一次点边框、日期戳时不用等网络
export function warmUp() {
  for (const id of Object.keys(FILES)) loadFont(id).catch(() => {});
}
if (typeof requestIdleCallback === 'function') requestIdleCallback(warmUp);

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

// 这一组边框 + 时间戳要用哪些字体（代号）。中文自定义文字用系统字体，不在其中。
export function fontsNeeded(frame, stamp, stampTextIsCjk = false) {
  const ids = new Set();
  const style = frame && frame.style;
  if (['leica', 'hasselblad', 'thin_keyline', 'exhibition_label', 'blur_fill', 'postage_stamp'].includes(style)) ids.add('inter_sb').add('inter');
  if (style === 'cinema' || style === 'slide_mount') ids.add('inter_sb').add('courier');
  if (style === 'darkroom') ids.add('patrick');
  if (style === 'film') ids.add('inter_sb');
  if (style === 'instant' && (frame.text || frame.show_date)) ids.add('patrick');
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

// 中文字体按 1000 像素量一个字：步进取整，外框向外取整（和 Pillow 的 getbbox 一样）
const cjkGlyphs = new Map();
function cjkGlyph(ch) {
  if (!cjkGlyphs.has(ch)) {
    const ctx = context();
    ctx.font = `${CJK_PX}px ${CJK}`;
    const m = ctx.measureText(ch);
    const ink = m.actualBoundingBoxRight + m.actualBoundingBoxLeft > 0 && m.actualBoundingBoxAscent + m.actualBoundingBoxDescent > 0;
    const box = ink
      ? [Math.floor(-m.actualBoundingBoxLeft), -Math.ceil(m.actualBoundingBoxDescent), Math.ceil(m.actualBoundingBoxRight), Math.ceil(m.actualBoundingBoxAscent)]
      : null;
    cjkGlyphs.set(ch, [Math.round(m.width), box]);
  }
  return cjkGlyphs.get(ch);
}

// 中文字体的上沿高度：画布问字体自己（Pillow 用的也是字体的 hhea 上沿，取整到像素）
function cjkAscent(px) {
  const ctx = context();
  ctx.font = fontCss('patrick', px, true);
  return Math.ceil((ctx.measureText('国').fontBoundingBoxAscent ?? px * 0.88) - EPS);
}

function table(font) {
  const t = tables.get(font);
  if (!t) throw new Error(`版面表还没到：${font}`);
  return t;
}

// finish_layout.js 要的 Metrics：{ upm, glyph, ascent }。内置字体全从表里读；字体表还没到就抛错（调用方先 tableReady）。
export const metrics = {
  upm: (font, cjk) => (cjk ? CJK_PX : table(font).upm),
  glyph: (font, ch, cjk) => {
    if (cjk) return cjkGlyph(ch);
    const t = table(font);
    return t.glyphs.get(ch.codePointAt(0)) || t.notdef;
  },
  ascent: (font, px, cjk) => {
    if (cjk) return cjkAscent(px);
    const t = table(font);
    return ascentPx(t.ascender, t.upm, px);
  },
};

export const ascentOf = (font, px, cjk) => metrics.ascent(font, px, cjk);
