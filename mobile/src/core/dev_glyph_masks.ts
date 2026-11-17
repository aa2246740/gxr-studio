// 只在开发服务器的第 9 项对拍里用：导出 Worker 用动态 import 拿它，生产包里没有这个模块。
//
// export_ink 不交出它混合时用的字形掩模（不改 core）。这里用同一份字体包、同一个 Rust 光栅器把每个字形再画一次：
// inktext_draw 的 char 操作走的就是 export_ink 里 draw_run 用的 render_char_mask_tracked + blend_mask_onto，
// 在全零的 L 画布上以 255 着墨，Pillow 的 BLEND(m, 0, 255) == m，所以画出来的就是那张 8-bit 掩模本身。
// 测试端再用合成门禁核对：成片里每个字形框内的像素都必须恰好等于 blend(底色, 墨色, 这张掩模)，
// 所以这里交的掩模若不是 export_ink 实际混合的那一张，合成门禁会失败。
//
// 交出的二进制：u32 LE 的 JSON 长度、JSON（UTF-8）、各字形掩模字节依次拼接。

import { inktext_draw } from '../wasm-pkg/gxr_wasm.js';

interface Run {
  src: 'frame' | 'logo' | 'stamp';
  font: string;
  px: number;
  text: string;
  xs: number[] | null;
  x: number | null;
  y: number;
  fill: unknown;
  rot: boolean;
  where: string | null;
}

interface Glyph {
  font: string;
  px: number;
  ch: string;
  w: number;
  h: number;
  x_off: number;
  y_off: number;
  offset: number;
  length: number;
  clipped: boolean;
}

type Obj = Record<string, unknown>;

const MARK_FONT = 'atkinson';
const MARK_TEXT = 'GXR';
const MARK_PX = 1000;
const PAD = 8;

function obj(v: unknown): Obj | null {
  return v && typeof v === 'object' && !Array.isArray(v) ? (v as Obj) : null;
}

function num(v: unknown): number | null {
  return typeof v === 'number' && Number.isFinite(v) ? v : null;
}

/** Rust 的 `f64::round_ties_even`。 */
function roundHalfEven(v: number): number {
  const floor = Math.floor(v);
  const diff = v - floor;
  if (diff > 0.5) return floor + 1;
  if (diff < 0.5) return floor;
  return floor % 2 === 0 ? floor : floor + 1;
}

/** 有墨的字：空格和控制字符 draw_run 不画。 */
function inked(text: string): string[] {
  return [...text].filter((ch) => ch !== ' ' && ch.codePointAt(0)! >= 0x20);
}

function textRun(src: Run['src'], item: Obj, fill: unknown, where: string | null): Run | null {
  const font = item.font;
  const px = num(item.px);
  const text = item.text;
  if (typeof font !== 'string' || px === null || typeof text !== 'string') return null;
  const rot = item.rot === true;
  const xs = Array.isArray(rot ? item.xs0 : item.xs) ? ((rot ? item.xs0 : item.xs) as number[]) : null;
  return {
    src,
    font,
    px,
    text,
    xs,
    x: rot ? num(item.gx) : null,
    y: num(rot ? item.gy : item.y) ?? 0,
    fill,
    rot,
    where,
  };
}

/** prepared JSON 里 export_ink 要画的每一段字：边框文字、内置 GXR 标志、日期戳。 */
export function inkRuns(prepared: Obj, layout: Obj): Run[] {
  const runs: Run[] = [];
  const frame = obj(prepared.frame);
  const items = frame && Array.isArray(frame.items) ? frame.items : [];
  const markH = num(obj(layout.mark)?.h);
  for (const raw of items) {
    const item = obj(raw);
    if (!item) continue;
    if (item.k === 'text') {
      const run = textRun('frame', item, item.auto_ink ? { fill: item.fill, auto_ink: item.auto_ink } : item.fill, 'canvas');
      if (run) runs.push(run);
    } else if (item.k === 'logo' && markH !== null && markH > 0) {
      const h = num(item.h) ?? 0;
      const px = Math.max(1, roundHalfEven((MARK_PX * h) / markH));
      runs.push({ src: 'logo', font: MARK_FONT, px, text: MARK_TEXT, xs: null, x: num(item.x), y: num(item.y) ?? 0, fill: item.fill ?? frame?.ink ?? null, rot: false, where: 'canvas' });
    }
  }
  const stamp = obj(prepared.stamp);
  const ops = stamp && Array.isArray(stamp.ops) ? stamp.ops : [];
  const where = typeof prepared.stamp_on === 'string' ? prepared.stamp_on : null;
  for (const raw of ops) {
    const op = obj(raw);
    if (op?.k !== 'text') continue;
    const run = textRun('stamp', op, { ink: stamp?.ink ?? null, glow: stamp?.glow ?? null }, where);
    if (run) runs.push(run);
  }
  return runs;
}

/** 一种字体、一个字号的所有字形画在一行格子里，每格 3·px+2·PAD 见方，字形原点在格内 (px+PAD, px+PAD)。 */
function drawCells(font: string, px: number, chars: string[], fontIndex: string, fontPack: Uint8Array): { pixels: Uint8Array; width: number; cell: number } {
  const size = Math.max(1, px);
  const cell = 3 * size + 2 * PAD;
  const width = cell * chars.length;
  const ops = chars.map((ch, i) => ({ op: 'char', font, px: size, ch, x: i * cell + size + PAD, y: size + PAD, fill: 255 }));
  const img = inktext_draw(new Uint8Array(width * cell), width, cell, 'L', JSON.stringify(ops), fontIndex, fontPack);
  return { pixels: img.pixels(), width, cell };
}

/** 一个格子里非零部分的外框（相对字形原点），以及它是否碰到格子边（碰到就说明格子不够大，测试判失败）。 */
function cutCell(pixels: Uint8Array, width: number, cell: number, index: number, size: number): { w: number; h: number; x: number; y: number; bytes: Uint8Array; clipped: boolean } {
  const x0 = index * cell;
  let left = cell;
  let top = cell;
  let right = -1;
  let bottom = -1;
  for (let y = 0; y < cell; y++) {
    for (let x = 0; x < cell; x++) {
      if (pixels[y * width + x0 + x] !== 0) {
        if (x < left) left = x;
        if (x > right) right = x;
        if (y < top) top = y;
        if (y > bottom) bottom = y;
      }
    }
  }
  if (right < 0) return { w: 0, h: 0, x: 0, y: 0, bytes: new Uint8Array(0), clipped: false };
  const w = right - left + 1;
  const h = bottom - top + 1;
  const bytes = new Uint8Array(w * h);
  for (let y = 0; y < h; y++) bytes.set(pixels.subarray((top + y) * width + x0 + left, (top + y) * width + x0 + left + w), y * w);
  const origin = size + PAD;
  const clipped = left === 0 || top === 0 || right === cell - 1 || bottom === cell - 1;
  return { w, h, x: left - origin, y: top - origin, bytes, clipped };
}

async function sha256(bytes: Uint8Array): Promise<string> {
  const digest = await crypto.subtle.digest('SHA-256', bytes as Uint8Array<ArrayBuffer>);
  return [...new Uint8Array(digest)].map((b) => b.toString(16).padStart(2, '0')).join('');
}

/** 字体包里每个字体的 SHA-256：测试端核对它用来生成 Pillow 掩模的是同一个文件。 */
async function fontDigests(fontIndex: string, fontPack: Uint8Array): Promise<Record<string, string>> {
  const out: Record<string, string> = {};
  let at = 0;
  for (const [name, len] of JSON.parse(fontIndex || '[]') as Array<[string, number]>) {
    out[name] = await sha256(fontPack.subarray(at, at + len));
    at += len;
  }
  return out;
}

/** 这一张 export_ink 画过的字与掩模。`exact` 是 export_ink 自己的标记。 */
export async function glyphMaskReport(prepared: Obj, layout: Obj, exact: boolean, fontIndex: string, fontPack: Uint8Array): Promise<Blob> {
  const runs = inkRuns(prepared, layout);
  const wanted = new Map<string, Set<string>>();
  for (const run of runs) {
    const key = `${run.font}\u0000${run.px}`;
    const set = wanted.get(key) ?? new Set<string>();
    for (const ch of inked(run.text)) set.add(ch);
    wanted.set(key, set);
  }
  const glyphs: Glyph[] = [];
  const parts: Uint8Array[] = [];
  let offset = 0;
  for (const [key, set] of wanted) {
    const [font, pxText] = key.split('\u0000');
    const px = Number(pxText);
    const chars = [...set].sort();
    if (!chars.length) continue;
    const drawn = drawCells(font, px, chars, fontIndex, fontPack);
    chars.forEach((ch, i) => {
      const cut = cutCell(drawn.pixels, drawn.width, drawn.cell, i, Math.max(1, px));
      glyphs.push({ font, px, ch, w: cut.w, h: cut.h, x_off: cut.x, y_off: cut.y, offset, length: cut.bytes.length, clipped: cut.clipped });
      parts.push(cut.bytes);
      offset += cut.bytes.length;
    });
  }
  const head = new TextEncoder().encode(JSON.stringify({ version: 1, exact, runs, glyphs, fonts: await fontDigests(fontIndex, fontPack) }));
  const len = new Uint8Array(4);
  new DataView(len.buffer).setUint32(0, head.length, true);
  return new Blob([len, head, ...(parts as Uint8Array<ArrayBuffer>[])]);
}
