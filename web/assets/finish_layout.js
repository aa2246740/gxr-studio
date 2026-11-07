// 边框和时间戳的版面（纯函数）。src/gxr_studio/finish/layout.py 的 JS 版：算式逐行对应，导出和预览的版面因此相同。
// tests/test_finish_layout_parity.py 用同样的输入逐项对照。改这里的算式，layout.py 要一起改。
// 量字交给调用方（m.advance、m.bbox）：预览用画布的 measureText（finish_fonts.js），测试用 Pillow 量好的表。
// 数字一律是「导出时的像素」：照片是已裁切、已盖日期戳的整张，宽 w 高 h。

export const WEIGHT = { thin: 0.02, medium: 0.04, thick: 0.07 };
export const CANVAS = { '1:1': [1, 1], '4:5': [4, 5], '3:2': [3, 2], '9:16': [9, 16] };
export const COLORS = { white: [255, 255, 255], black: [12, 12, 12], cream: [245, 236, 220] };
export const STAMP_COLORS = { orange: [255, 106, 0], red: [220, 32, 32], yellow: [255, 196, 40], white: [255, 255, 255] };
export const STAMP_SIZE = { small: 0.038, medium: 0.055, large: 0.078 };
export const STAMP_FONT = { seg7: 'dseg', typewriter: 'courier', sans: 'atkinson' };
export const LOGO_SIZE = [2400, 433]; // assets/logo/ricoh.png
export const GLOW_GAIN = 1.15;
export const STAMP_TEXT_MAX = 24;

// Python 的 round() 遇到 .5 取偶数（银行家舍入），Math.round 取大的那个；这里要和 Python 一样
export function pyRound(x) {
  const lo = Math.floor(x);
  const d = x - lo;
  if (d < 0.5) return lo;
  if (d > 0.5) return lo + 1;
  return lo % 2 === 0 ? lo : lo + 1;
}

const idiv = (a, b) => Math.floor(a / b);
const trunc = Math.trunc;

export function needsCjk(text) {
  for (const ch of text) if (ch.codePointAt(0) > 127) return true;
  return false;
}

// 字体的上沿高度（hhea ascender / unitsPerEm）。Pillow 画字用 'la' 锚点：y 是上沿，基线在 y + ascentPx 处。
// 画布按基线画字，所以预览要同样的数。tests/test_finish_layout.py 用 Pillow 核对这几个数。
export const FONT_ASCENT = { atkinson: [950, 1000], inter: [1984, 2048], courier: [1600, 2048], patrick: [1042, 1000], dseg: [1000, 1000] };
export const DSEG_TICK_EM = 0.364; // 数码管字体里「'」（缺字的方块）的宽度；撇号是单独画的，只占这么宽

export function ascentPx(font, px) {
  const [ascender, upm] = FONT_ASCENT[font];
  return Math.ceil(Math.round((ascender * px * 64) / upm) / 64); // FreeType 先按 26.6 定点取整，再向上取整到像素
}

function textItem(x, y, px, font, text, fill, cjk = false) {
  return { k: 'text', x, y, px, font, text, fill: [...fill], cjk };
}

const facePx = (px) => Math.max(8, px); // 边框用的字体不会小于 8 像素

function fit(m, text, font, px, maxW) {
  while (px > 10 && m.advance(font, facePx(px), text, false) > maxW) px -= 1;
  return facePx(px);
}

const margin = (w, h, weight) => Math.max(8, pyRound(Math.max(w, h) * WEIGHT[weight]));

function gallery(w, h, spec) {
  const m = margin(w, h, spec.weight);
  return { width: w + 2 * m, height: h + 2 * m, photo: [m, m], bg: [...COLORS[spec.color]], items: [] };
}

function leicaLines(spec, meta) {
  const lines = [];
  if (spec.show_params && meta.lens) lines.push(meta.lens);
  const second = [meta.aperture, meta.shutter, meta.iso].filter(Boolean).join('  ');
  if (spec.show_params && second) lines.push(second);
  if (spec.show_date && meta.date) lines.push(meta.date);
  if (spec.text) lines.push(spec.text);
  return lines;
}

function leica(w, h, spec, meta, m) {
  const bar = Math.max(64, pyRound(Math.min(w, h) * 0.09));
  const dark = spec.color === 'black';
  const ink = dark ? [245, 245, 245] : [20, 20, 20];
  const muted = dark ? [170, 170, 170] : [90, 90, 90];
  const pad = Math.max(16, idiv(bar, 5));
  const items = [];
  if (spec.show_logo) {
    const lh = trunc(bar * 0.42);
    const lw = Math.max(1, pyRound((LOGO_SIZE[0] * lh) / LOGO_SIZE[1]));
    items.push({ k: 'logo', x: pad, y: h + idiv(bar - lh, 2), w: lw, h: lh });
    const gx = pad + lw + idiv(pad, 2);
    items.push(textItem(gx, h + idiv(bar - trunc(bar * 0.34), 2) - 2, facePx(trunc(bar * 0.34)), 'atkinson', 'GXR', ink));
  }
  const lines = leicaLines(spec, meta);
  if (lines.length) {
    const first = lines[0];
    const second = lines.slice(1).join('  ');
    const px1 = fit(m, first, 'inter', trunc(bar * 0.26), idiv(w, 2));
    const px2 = second ? fit(m, second, 'inter', trunc(bar * 0.22), idiv(w, 2)) : px1;
    const w1 = m.advance('inter', px1, first, false);
    const w2 = second ? m.advance('inter', px2, second, false) : 0;
    const right = w - pad;
    const rule = right - Math.max(w1, w2) - Math.max(8, idiv(bar, 8));
    items.push({ k: 'line', x1: rule, y1: h + pad, x2: rule, y2: h + bar - pad, w: Math.max(1, idiv(bar, 28)), fill: [...muted] });
    items.push(textItem(right - w1, h + trunc(bar * 0.16), px1, 'inter', first, ink));
    if (second) items.push(textItem(right - w2, h + trunc(bar * 0.52), px2, 'inter', second, muted));
  }
  return { width: w, height: h + bar, photo: [0, 0], bg: [...COLORS[spec.color]], items };
}

function hasselblad(w, h, spec, meta, m) {
  const thin = Math.max(10, pyRound(Math.max(w, h) * 0.018));
  const bottom = Math.max(thin * 4, pyRound(Math.min(w, h) * 0.16));
  const outW = w + 2 * thin;
  const outH = h + thin + bottom;
  const dark = spec.color === 'black';
  const ink = dark ? [245, 245, 245] : [24, 24, 24];
  const muted = dark ? [180, 180, 180] : [120, 120, 120];
  const titlePx = facePx(Math.max(16, idiv(bottom, 5)));
  const subPx = facePx(Math.max(13, idiv(bottom, 7)));
  const cx = idiv(outW, 2);
  const y0 = thin + h + trunc(bottom * 0.28);
  const name = 'RICOH GXR';
  const items = [textItem(cx - m.advance('atkinson', titlePx, name, false) / 2, y0, titlePx, 'atkinson', name, ink)];
  const bits = [];
  if (spec.show_params) bits.push(...[meta.lens, meta.aperture, meta.shutter, meta.iso].filter(Boolean));
  if (spec.show_date && meta.date) bits.push(meta.date);
  if (spec.text) bits.push(spec.text);
  const line = bits.join('  ·  ');
  if (line) items.push(textItem(cx - m.advance('inter', subPx, line, false) / 2, y0 + trunc(bottom * 0.28), subPx, 'inter', line, muted));
  return { width: outW, height: outH, photo: [thin, thin], bg: [...COLORS[spec.color]], items };
}

function film(w, h, meta, m) {
  const mg = Math.max(28, pyRound(Math.max(w, h) * 0.045));
  const outW = w + 2 * mg;
  const outH = h + 2 * mg;
  const amber = [232, 164, 74];
  const px = facePx(Math.max(11, idiv(mg, 3)));
  const lens = meta.lens_short || 'A12 50mm';
  const items = [textItem(mg, outH - mg + 6, px, 'courier', `RICOH GXR  ·  ${lens}`, amber)];
  const number = meta.frame_no || '';
  if (number) items.push(textItem(outW - mg - m.advance('courier', px, number, false), 6, px, 'courier', number, amber));
  return { width: outW, height: outH, photo: [mg, mg], bg: [8, 8, 8], items };
}

function instant(w, h, spec, m) {
  const side = Math.max(18, pyRound(Math.max(w, h) * 0.035));
  const bottom = Math.max(side * 4, pyRound(Math.min(w, h) * 0.2));
  const outW = w + 2 * side;
  const outH = h + side + bottom;
  const items = [];
  if (spec.text) {
    const cjk = needsCjk(spec.text);
    const eff = (size) => (cjk ? Math.max(1, size) : facePx(size));
    let px = Math.max(22, idiv(bottom, 4));
    const maxW = Math.max(8, outW - 2 * side);
    while (px > 10 && m.advance('patrick', eff(px), spec.text, cjk) > maxW) px = Math.max(10, trunc(px * 0.85));
    const tw = m.advance('patrick', eff(px), spec.text, cjk);
    items.push(textItem((outW - tw) / 2, side + h + (bottom - eff(px)) / 2, eff(px), 'patrick', spec.text, [40, 40, 40], cjk));
  }
  return { width: outW, height: outH, photo: [side, side], bg: [248, 246, 242], items };
}

// 画布：四边留出和「白边 · 中」一样宽的边，再补到所选的画布比例，照片居中
function canvas(w, h, spec) {
  const mg = margin(w, h, 'medium');
  const innerW = w + 2 * mg;
  const innerH = h + 2 * mg;
  const [rw, rh] = CANVAS[spec.canvas_ratio];
  const target = rw / rh;
  let outW;
  let outH;
  if (innerW / innerH > target) {
    outW = innerW;
    outH = pyRound(innerW / target);
  } else {
    outH = innerH;
    outW = pyRound(innerH * target);
  }
  return { width: outW, height: outH, photo: [idiv(outW - w, 2), idiv(outH - h, 2)], bg: [...COLORS[spec.color]], items: [] };
}

// 样式 none（或不认识的样式）返回 null。meta：{ lens, lens_short, aperture, shutter, iso, date, frame_no }
export function frameLayout(w, h, spec, meta, m) {
  switch (spec.style) {
    case 'gallery': return gallery(w, h, spec);
    case 'leica': return leica(w, h, spec, meta || {}, m);
    case 'hasselblad': return hasselblad(w, h, spec, meta || {}, m);
    case 'film': return film(w, h, meta || {}, m);
    case 'instant': return instant(w, h, spec, m);
    case 'canvas': return canvas(w, h, spec);
    default: return null;
  }
}

// ---- 时间戳 -------------------------------------------------------------------

export const seg7Text = (text) => text.replaceAll(' ', '!'); // DSEG 的 ! 是和数字等宽的空白

function tickItem(x, y, px, fill) {
  const w = Math.max(2, trunc(px * 0.07));
  const h = Math.max(6, trunc(px * 0.42));
  const x0 = x + w;
  const y0 = y + Math.max(0, trunc(px * 0.04));
  return { k: 'poly', pts: [[x0 + 2 * w, y0], [x0 + 3 * w, y0], [x0 + w, y0 + h], [x0, y0 + h]], fill: [...fill] };
}

function stampOps(origin, text, px, font, cjk, ink, m) {
  let [x, y] = origin;
  if (font !== 'dseg' || cjk) return [textItem(x, y, px, font, text, ink, cjk)];
  const drawn = seg7Text(text);
  const ops = [];
  let start = 0;
  while (start < drawn.length) {
    const tick = drawn.indexOf("'", start);
    if (tick < 0) {
      ops.push(textItem(x, y, px, font, drawn.slice(start), ink));
      break;
    }
    const chunk = drawn.slice(start, tick);
    if (chunk) {
      ops.push(textItem(x, y, px, font, chunk, ink));
      x += pyRound(m.advance(font, px, chunk, false));
    }
    ops.push(tickItem(x, y, px, ink));
    x += pyRound(m.advance(font, px, "'", false));
    start = tick + 1;
  }
  return ops;
}

// text 是 stampText() 的结果（空串 = 没有可画的内容）。w、h：照片像素宽高。
export function stampLayout(w, h, spec, text, m) {
  if (!text) return null;
  const font = STAMP_FONT[spec.font];
  const cjk = needsCjk(text);
  const shown = cjk || spec.font !== 'seg7' ? text : seg7Text(text);
  let px = Math.max(12, pyRound(Math.min(w, h) * STAMP_SIZE[spec.size]));
  let box = m.bbox(font, Math.max(1, px), shown, cjk);
  const mg = Math.max(1, pyRound(Math.min(w, h) * 0.035));
  const limitW = Math.max(1, w - 2 * mg);
  const limitH = Math.max(1, h - 2 * mg);
  while ((box[2] - box[0] > limitW || box[3] - box[1] > limitH) && px > 1) {
    const nxt = trunc(px * 0.85);
    px = nxt < px ? nxt : px - 1;
    box = m.bbox(font, Math.max(1, px), shown, cjk);
  }
  const tw = box[2] - box[0];
  const th = box[3] - box[1];
  const x = spec.corner[1] === 'l' ? mg : Math.max(mg, w - mg - tw);
  const y = spec.corner[0] === 't' ? mg : Math.max(mg, h - mg - th);
  const ink = STAMP_COLORS[spec.color];
  const facepx = Math.max(1, px);
  const ops = stampOps([trunc(x - box[0]), trunc(y - box[1])], text, facepx, font, cjk, ink, m);
  const glow = spec.glow && spec.font === 'seg7' ? Math.max(1.2, px * 0.16) : null;
  return { px: facepx, font, rect: [x, y, tw, th], ink: [...ink], glow, ops };
}

// stamp.format_stamp 的 JS 版。when：{ year, month, day, hour, minute } 或 null（没有拍摄时间也没有文件时间）。
const pad2 = (n) => String(n).padStart(2, '0');

export function stampText(spec, when) {
  let moment = when;
  if (spec.date_override) {
    const [year, month, day] = spec.date_override.split('-').map(Number);
    moment = { year, month, day, hour: 0, minute: 0 };
  }
  const parts = [];
  if ((spec.content === 'date' || spec.content === 'date+text') && moment) {
    const yy = pad2(moment.year % 100);
    const mm = pad2(moment.month);
    const dd = pad2(moment.day);
    let text;
    if (spec.date_format === 'yy mm dd') text = `${yy} ${mm} ${dd}`;
    else if (spec.date_format === 'yyyy.mm.dd') text = `${String(moment.year).padStart(4, '0')}.${mm}.${dd}`;
    else if (spec.date_format === "mm dd 'yy") text = `${mm} ${dd} '${yy}`;
    else text = `'${yy} ${mm} ${dd}`;
    if (spec.show_time) text = `${text} ${pad2(moment.hour)}:${pad2(moment.minute)}`;
    parts.push(text);
  }
  if ((spec.content === 'text' || spec.content === 'date+text') && spec.text) parts.push(Array.from(spec.text).slice(0, STAMP_TEXT_MAX).join(''));
  return parts.join(' ');
}

// /api/photos/{id}/meta 的 captured_at（"2026-10-23T17:41:05"，本地时间，没有时区）→ stampText 要的 when
export function momentOf(capturedAt) {
  const hit = /^(\d{4})-(\d{2})-(\d{2})(?:[T ](\d{2}):(\d{2}))?/.exec(capturedAt || '');
  if (!hit) return null;
  return { year: Number(hit[1]), month: Number(hit[2]), day: Number(hit[3]), hour: Number(hit[4] || 0), minute: Number(hit[5] || 0) };
}
