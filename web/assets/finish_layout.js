// 边框和时间戳的版面（纯函数）。src/gxr_studio/finish/layout.py 的 JS 版：算式逐行对应，导出和预览的版面因此相同。
// tests/test_finish_layout_parity.py 用同样的输入逐项对照。改这里的算式，layout.py 要一起改。
// 字的宽度、每个字的位置、墨迹框都由字体表里的整数（步进宽度、字形外框，src/gxr_studio/finish/sfnt.py 从字体文件读出，
// /api/finish/assets/<字体>.json 给浏览器）和整数算式得出：不用画布量字，也不管 Pillow 的排版引擎。表由调用方给（m.upm、m.glyph、m.ascent）。
// 数字一律是「导出时的像素」：照片是已裁切、已盖日期戳的整张，宽 w 高 h。

export const WEIGHT = { thin: 0.02, medium: 0.04, thick: 0.07 };
export const OUTPUT_RATIOS = { '1:1': [1, 1], '4:5': [4, 5], '3:4': [3, 4], '2:3': [2, 3], '9:16': [9, 16], '16:9': [16, 9], '3:2': [3, 2] };
const CROP_RATIOS = { '3:2': [3, 2], '4:3': [4, 3], '1:1': [1, 1], '4:5': [5, 4], '16:9': [16, 9], '16:10': [8, 5], '1.91:1': [191, 100], '2.35:1': [47, 20], 'xpan-65-24': [65, 24] };
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

// Pillow 画字用 'la' 锚点：y 是字体上沿，基线在 y + ascentPx 处；画布按基线画字，预览要同样的数。
// FreeType 的办法：hhea ascender 先四舍五入到 1/64 像素，再向上取整到像素（tests/test_finish_fonts.py 对 Pillow 逐字号核对）。
export function ascentPx(ascender, upm, px) {
  return Math.ceil(Math.floor((2 * ascender * px * 64 + upm) / (2 * upm)) / 64);
}

// 每个字符前面的步进之和（字体单位），最后一项是整行的步进。逐个码点，和 Python 的 for ch in text 一样
function pens(m, font, chars, cjk) {
  const out = [0];
  for (const ch of chars) out.push(out[out.length - 1] + m.glyph(font, ch, cjk)[0]);
  return out;
}

// 整行的宽度（像素，小数）：步进之和 × 字号 / upm。没有字距，没有取整。
export function advance(m, font, px, text, cjk = false) {
  const p = pens(m, font, Array.from(text), cjk);
  return (p[p.length - 1] * px) / m.upm(font, cjk);
}

// 每个字画在哪一列像素：起点 x 加上前面各字的步进，四舍五入到整像素。预览和导出都一个字一个字地画在这些位置上。
export function glyphXs(m, font, px, text, x, cjk = false) {
  const upm = m.upm(font, cjk);
  return pens(m, font, Array.from(text), cjk)
    .slice(0, -1)
    .map((pen) => Math.floor(x + (pen * px) / upm + 0.5));
}

// 一行字的墨迹框 [左, 上, 右, 下]（整像素，起点是 'la' 锚点）。横向把整行的步进算进去（左边不大于 0，右边不小于步进）。
export function inkBox(m, font, px, text, cjk = false) {
  const upm = m.upm(font, cjk);
  const asc = m.ascent(font, px, cjk);
  const chars = Array.from(text);
  const p = pens(m, font, chars, cjk);
  let left = 0;
  let right = (p[p.length - 1] * px) / upm;
  let up = 0;
  let down = 0;
  chars.forEach((ch, i) => {
    const box = m.glyph(font, ch, cjk)[1];
    if (!box) return;
    left = Math.min(left, ((p[i] + box[0]) * px) / upm);
    right = Math.max(right, ((p[i] + box[2]) * px) / upm);
    up = Math.max(up, (box[3] * px) / upm);
    down = Math.max(down, (-box[1] * px) / upm);
  });
  return [Math.floor(left), Math.floor(asc - up), Math.ceil(right), Math.ceil(asc + down)];
}

// 一行字：x 是起点，xs 是每个字的整像素列（glyphXs），y 是整像素行（字体上沿所在的那一行）
function textItem(m, x, y, px, font, text, fill, cjk = false) {
  return { k: 'text', x, y: Math.floor(y + 0.5), px, font, text, fill: [...fill], cjk, xs: glyphXs(m, font, px, text, x, cjk) };
}

const facePx = (px) => Math.max(8, px); // 边框用的字体不会小于 8 像素

function fit(m, text, font, px, maxW, floor = 10) {
  while (px > floor && advance(m, font, facePx(px), text, false) > maxW) px -= 1;
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
    const gxrPx = facePx(trunc(bar * 0.34));
    if (gx + advance(m, 'atkinson', gxrPx, 'GXR') <= w) items.push(textItem(m, gx, h + idiv(bar - trunc(bar * 0.34), 2) - 2, gxrPx, 'atkinson', 'GXR', ink));
  }
  const lines = leicaLines(spec, meta);
  if (lines.length) {
    const first = lines[0];
    const second = lines.slice(1).join('  ');
    const px1 = fit(m, first, 'inter', trunc(bar * 0.26), idiv(w, 2), 8);
    const px2 = second ? fit(m, second, 'inter', trunc(bar * 0.22), idiv(w, 2), 8) : px1;
    const w1 = advance(m, 'inter', px1, first);
    const w2 = second ? advance(m, 'inter', px2, second) : 0;
    const right = w - pad;
    const rule = right - Math.max(w1, w2) - Math.max(8, idiv(bar, 8));
    items.push({ k: 'line', x1: rule, y1: h + pad, x2: rule, y2: h + bar - pad, w: Math.max(1, idiv(bar, 28)), fill: [...muted] });
    items.push(textItem(m, Math.max(0, right - w1), h + trunc(bar * 0.16), px1, 'inter', first, ink));
    if (second) items.push(textItem(m, Math.max(0, right - w2), h + trunc(bar * 0.52), px2, 'inter', second, muted));
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
  const items = [textItem(m, Math.max(0, cx - advance(m, 'atkinson', titlePx, name) / 2), y0, titlePx, 'atkinson', name, ink)];
  const bits = [];
  if (spec.show_params) bits.push(...[meta.lens, meta.aperture, meta.shutter, meta.iso].filter(Boolean));
  if (spec.show_date && meta.date) bits.push(meta.date);
  if (spec.text) bits.push(spec.text);
  let line = bits.join('  ·  ');
  const maxW = outW - 2 * thin;
  // 一行参数比照片还宽时先缩字号（最小 8 像素）；还放不下就从最后一项开始去掉，直到放得下（至少留一项）
  while (bits.length > 1 && advance(m, 'inter', fit(m, line, 'inter', subPx, maxW, 8), line) > maxW) {
    bits.pop();
    line = bits.join('  ·  ');
  }
  if (line) {
    const fitted = fit(m, line, 'inter', subPx, maxW, 8);
    items.push(textItem(m, Math.max(0, cx - advance(m, 'inter', fitted, line) / 2), y0 + trunc(bottom * 0.28), fitted, 'inter', line, muted));
  }
  return { width: outW, height: outH, photo: [thin, thin], bg: [...COLORS[spec.color]], items };
}

function film(w, h, meta, m) {
  const mg = Math.max(28, pyRound(Math.max(w, h) * 0.045));
  const outW = w + 2 * mg;
  const outH = h + 2 * mg;
  const amber = [232, 164, 74];
  const px = facePx(Math.max(11, idiv(mg, 3)));
  const lens = meta.lens_short || 'A12 50mm';
  const items = [textItem(m, mg, outH - mg + 6, px, 'courier', `RICOH GXR  ·  ${lens}`, amber)];
  const number = meta.frame_no || '';
  if (number) items.push(textItem(m, Math.max(0, outW - mg - advance(m, 'courier', px, number)), 6, px, 'courier', number, amber));
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
    while (px > 10 && advance(m, 'patrick', eff(px), spec.text, cjk) > maxW) px = Math.max(10, trunc(px * 0.85));
    const tw = advance(m, 'patrick', eff(px), spec.text, cjk);
    items.push(textItem(m, Math.max(0, (outW - tw) / 2), side + h + (bottom - eff(px)) / 2, eff(px), 'patrick', spec.text, [40, 40, 40], cjk));
  }
  return { width: outW, height: outH, photo: [side, side], bg: [248, 246, 242], items };
}

// 样式 none（或不认识的样式）返回 null。meta：{ lens, lens_short, aperture, shutter, iso, date, frame_no }
function baseFrame(w, h, spec, meta, m) {
  switch (spec.style) {
    case 'gallery': return gallery(w, h, spec);
    case 'leica': return leica(w, h, spec, meta || {}, m);
    case 'hasselblad': return hasselblad(w, h, spec, meta || {}, m);
    case 'film': return film(w, h, meta || {}, m);
    case 'instant': return instant(w, h, spec, m);
    default: return null;
  }
}

export function cropRatio(preset, w, h) {
  const ratio = CROP_RATIOS[preset];
  return ratio ? (w >= h ? [...ratio] : [ratio[1], ratio[0]]) : null;
}

export function ratioSize(w, h, rw, rh) {
  const multiple = Math.max(Math.ceil(w / rw), Math.ceil(h / rh));
  return [multiple * rw, multiple * rh];
}

function contentSize(layout, m) {
  let ex = 0;
  let ey = 0;
  for (const item of layout.items) {
    let left, top, right, bottom;
    if (item.k === 'text') {
      const box = inkBox(m, item.font, item.px, item.text, item.cjk);
      [left, top, right, bottom] = [item.x + box[0], item.y + box[1], item.x + box[2], item.y + box[3]];
    } else if (item.k === 'line') {
      const stroke = idiv(item.w + 1, 2);
      [left, top, right, bottom] = [item.x1 - stroke, item.y1 - stroke, item.x2 + stroke, item.y2 + stroke];
    } else {
      [left, top, right, bottom] = [item.x, item.y, item.x + item.w, item.y + item.h];
    }
    ex = Math.max(ex, Math.ceil(-left), Math.ceil(right - layout.width));
    ey = Math.max(ey, Math.ceil(-top), Math.ceil(bottom - layout.height));
  }
  return [layout.width + 2 * ex, layout.height + 2 * ey];
}

export function frameLayout(w, h, spec, meta, m) {
  const layout = baseFrame(w, h, spec, meta, m);
  const ratio = OUTPUT_RATIOS[spec.output_ratio ?? '3:4'];
  if (!layout || !ratio) return layout;
  const [width, height] = ratioSize(...contentSize(layout, m), ...ratio);
  const dx = idiv(width - layout.width, 2);
  const dy = idiv(height - layout.height, 2);
  const items = layout.items.map((original) => {
    const item = { ...original };
    for (const key of ['x', 'x1', 'x2']) if (key in item) item[key] += dx;
    for (const key of ['y', 'y1', 'y2']) if (key in item) item[key] += dy;
    if (item.xs) item.xs = item.xs.map((x) => x + dx);
    return item;
  });
  return { ...layout, width, height, photo: [layout.photo[0] + dx, layout.photo[1] + dy], items };
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
  if (font !== 'dseg' || cjk) return [textItem(m, x, y, px, font, text, ink, cjk)];
  const drawn = seg7Text(text);
  const ops = [];
  let start = 0;
  while (start < drawn.length) {
    const tick = drawn.indexOf("'", start);
    if (tick < 0) {
      ops.push(textItem(m, x, y, px, font, drawn.slice(start), ink));
      break;
    }
    const chunk = drawn.slice(start, tick);
    if (chunk) {
      ops.push(textItem(m, x, y, px, font, chunk, ink));
      x += pyRound(advance(m, font, px, chunk));
    }
    ops.push(tickItem(x, y, px, ink));
    x += pyRound(advance(m, font, px, "'"));
    start = tick + 1;
  }
  return ops;
}

// text 是 stampText() 的结果（空串 = 没有可画的内容）。w、h：照片像素宽高。
export function stampLayout(w, h, spec, text, m, frame = null) {
  if (!text) return null;
  if (frame) return borderStampLayout(w, h, spec, text, m, frame);
  const font = STAMP_FONT[spec.font];
  const cjk = needsCjk(text);
  const shown = cjk || spec.font !== 'seg7' ? text : seg7Text(text);
  let px = Math.max(12, pyRound(Math.min(w, h) * STAMP_SIZE[spec.size]));
  let box = inkBox(m, font, Math.max(1, px), shown, cjk);
  const mg = Math.max(1, pyRound(Math.min(w, h) * 0.035));
  const limitW = Math.max(1, w - 2 * mg);
  const limitH = Math.max(1, h - 2 * mg);
  while ((box[2] - box[0] > limitW || box[3] - box[1] > limitH) && px > 1) {
    const nxt = trunc(px * 0.85);
    px = nxt < px ? nxt : px - 1;
    box = inkBox(m, font, Math.max(1, px), shown, cjk);
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

// 拍立得底边，坐标属于整张成片。与 layout.py 的 _border_stamp_layout 逐项对应。
function borderStampLayout(w, h, spec, text, m, frame) {
  const base = stampLayout(w, h, spec, text, m);
  if (!base) return null;
  let top = frame.photo[1] + h;
  for (const item of frame.items) {
    if (item.k === 'text') {
      const box = inkBox(m, item.font, item.px, item.text, item.cjk);
      top = Math.max(top, item.y + box[3] + 1);
    }
  }
  const margin = Math.max(2, Math.min(pyRound(Math.min(w, h) * 0.035), Math.floor((frame.height - top) / 8)));
  const cjk = needsCjk(text);
  const shown = spec.font === 'seg7' && !cjk ? seg7Text(text) : text;
  let px = base.px;
  let box; let glow; let pad; let tw; let th;
  for (;;) {
    box = inkBox(m, base.font, px, shown, cjk);
    glow = base.glow !== null ? Math.max(1.2, px * 0.16) : null;
    pad = glow !== null ? Math.max(margin, Math.ceil(3 * glow) + 2) : margin;
    tw = box[2] - box[0]; th = box[3] - box[1];
    if (tw <= w - 2 * pad && th <= frame.height - top - 2 * pad) break;
    if (px === 1) return null;
    px = Math.max(1, trunc(px * 0.85));
  }
  const x = frame.photo[0] + (spec.corner[1] === 'l' ? pad : w - pad - tw);
  const y = spec.corner[0] === 't' ? top + pad : frame.height - pad - th;
  const ops = stampOps([x - box[0], y - box[1]], text, px, base.font, cjk, base.ink, m);
  return { px, font: base.font, rect: [x, y, tw, th], ink: [...base.ink], glow, ops };
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
