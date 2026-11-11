import * as plain from './finish_frame_styles.js';
import * as objects from './finish_frame_objects.js';
import * as newStyles from './finish_frame_new.js';
import { LOGO_STYLES, getLogoMap } from './finish_frame_core.js';
// 边框和时间戳的版面（纯函数）。src/gxr_studio/finish/layout.py 的 JS 版：算式逐行对应，导出和预览的版面因此相同。
// tests/test_finish_layout_parity.py 用同样的输入逐项对照。改这里的算式，layout.py 要一起改。
// 字的宽度、每个字的位置、墨迹框都由字体表里的整数（步进宽度、字形外框，src/gxr_studio/finish/sfnt.py 从字体文件读出，
// /api/finish/assets/<字体>.json 给浏览器）和整数算式得出：不用画布量字，也不管 Pillow 的排版引擎。表由调用方给（m.upm、m.glyph、m.ascent）。
// 数字一律是「导出时的像素」：照片是已裁切、已盖日期戳的整张，宽 w 高 h。

export const OUTPUT_RATIOS = { '1:1': [1, 1], '4:5': [4, 5], '3:4': [3, 4], '2:3': [2, 3], '9:16': [9, 16], '16:9': [16, 9], '3:2': [3, 2] };
const CROP_RATIOS = { '3:2': [3, 2], '4:3': [4, 3], '1:1': [1, 1], '4:5': [5, 4], '16:9': [16, 9], '16:10': [8, 5], '1.91:1': [191, 100], '2.35:1': [47, 20], 'xpan-65-24': [65, 24] };
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

export function cropRatio(preset, w, h) {
  const ratio = CROP_RATIOS[preset];
  return ratio ? (w >= h ? [...ratio] : [ratio[1], ratio[0]]) : null;
}

export function ratioSize(w, h, rw, rh) {
  const multiple = Math.max(Math.ceil(w / rw), Math.ceil(h / rh));
  return [multiple * rw, multiple * rh];
}

export function resolveLogo(spec, logos = null) {
  if (!LOGO_STYLES.includes(spec.style)) return ['none', null];
  const raw = spec.logo || (spec.show_logo === false ? 'none' : 'ricoh');
  if (raw === 'none' || spec.show_logo === false) return ['none', null];
  if (raw === 'ricoh') return ['ricoh', null];
  const map = logos || getLogoMap();
  const custom = map ? map[raw] : null;
  if (!custom) return ['ricoh', null];
  return ['custom', custom];
}

export function frameLayout(w, h, spec, meta, m, logos = null) {
  if (spec.style === 'none') return null;
  const ratio = OUTPUT_RATIOS[spec.output_ratio ?? '3:4'] || null;
  const added = { thin_keyline: newStyles.thinKeyline, exhibition_label: newStyles.exhibitionLabel, blur_fill: newStyles.blurFill, cinema: newStyles.cinema, slide_mount: newStyles.slideMount, darkroom: newStyles.darkroom, postage_stamp: newStyles.postageStamp };
  const build = plain[spec.style] || objects[spec.style] || added[spec.style];
  const [logoKind, customLogo] = resolveLogo(spec, logos);
  const effective = {
    ...spec,
    fmt: spec.fmt || (spec.style === 'instant' ? 'polaroid' : '135'),
    table: spec.table || 'light',
    logo_mode: spec.logo_mode || 'auto',
    logo_size: spec.logo_size || 'medium',
    model: spec.model !== false,
    _logoKind: logoKind,
    _customLogo: customLogo,
  };
  return build ? build(w, h, effective, meta || {}, m, ratio) : null;
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
  const bottom = frame.unit_box[1] + frame.unit_box[3];
  const margin = Math.max(2, Math.min(pyRound(Math.min(w, h) * 0.035), Math.floor((bottom - top) / 8)));
  const cjk = needsCjk(text);
  const shown = spec.font === 'seg7' && !cjk ? seg7Text(text) : text;
  let px = base.px;
  let box; let glow; let pad; let tw; let th;
  for (;;) {
    box = inkBox(m, base.font, px, shown, cjk);
    glow = base.glow !== null ? Math.max(1.2, px * 0.16) : null;
    pad = glow !== null ? Math.max(margin, Math.ceil(3 * glow) + 2) : margin;
    tw = box[2] - box[0]; th = box[3] - box[1];
    if (tw <= w - 2 * pad && th <= bottom - top - 2 * pad) break;
    if (px === 1) return null;
    px = Math.max(1, trunc(px * 0.85));
  }
  const x = frame.photo[0] + (spec.corner[1] === 'l' ? pad : w - pad - tw);
  const y = spec.corner[0] === 't' ? top + pad : bottom - pad - th;
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
