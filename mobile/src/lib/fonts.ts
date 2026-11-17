// 边框与时间戳用的六套开放字体：不含任何中文字体，手机端与桌面共有 `src/gxr_studio/assets/fonts/`。
// 纯函数，不碰 DOM 或 Vite `?url`，Node 测试直接跑。

export type FontKey = 'atkinson' | 'inter' | 'inter_sb' | 'courier' | 'patrick' | 'dseg';

export const FONT_FILES: Record<FontKey, string> = {
  atkinson: 'AtkinsonHyperlegible-Regular.ttf',
  inter: 'Inter-Regular.ttf',
  inter_sb: 'Inter-SemiBold.ttf',
  courier: 'CourierPrime-Regular.ttf',
  patrick: 'PatrickHand-Regular.ttf',
  dseg: 'DSEG7Classic-Regular.ttf',
};

export const FONT_LICENCE_FILES: Record<FontKey, string> = {
  atkinson: 'AtkinsonHyperlegible-OFL.txt',
  inter: 'Inter-OFL.txt',
  inter_sb: 'Inter-SemiBold-OFL.txt',
  courier: 'CourierPrime-OFL.txt',
  patrick: 'PatrickHand-OFL.txt',
  dseg: 'DSEG-LICENSE.txt',
};

export const FONT_FAMILIES: Record<FontKey, string> = {
  atkinson: 'GXR Atkinson',
  inter: 'GXR Inter',
  inter_sb: 'GXR Inter SemiBold',
  courier: 'GXR Courier',
  patrick: 'GXR Patrick',
  dseg: 'GXR DSEG',
};

export const FONT_HHEA: Record<FontKey, { ascender: number; upm: number }> = {
  atkinson: { ascender: 950, upm: 1000 },
  inter: { ascender: 1984, upm: 2048 },
  inter_sb: { ascender: 1984, upm: 2048 },
  courier: { ascender: 1600, upm: 2048 },
  patrick: { ascender: 1042, upm: 1000 },
  dseg: { ascender: 1000, upm: 1000 },
};

export function ascentOf(font: FontKey | string, px: number): number {
  const row = FONT_HHEA[font as FontKey];
  if (!row) return Math.ceil(px * 0.88);
  const { ascender, upm } = row;
  const q = Math.floor((2 * ascender * px * 64 + upm) / (upm * 2));
  return -Math.floor(-q / 64);
}

/** 公开仓库里代替内置 RICOH 图标的 GXR 字体徽标外框（ink-text 第 5 项：Atkinson R=1000 下的 "GXR"）。 */
export const GXR_MARK = {
  w: 1951,
  h: 1240,
  ink_top: 270,
  ink_bottom: 961,
} as const;

export const GXR_MARK_ASCENT = 950;

export const CJK_REFUSE_MESSAGE = '无法显示中文。请改用英文或数字。';

/** 任何 >= 0x2e80 的码点（含 CJK 标点、假名、汉字、全角字符）都拒收，只留 ASCII / 英文和数字。 */
export function hasCjk(text: string): boolean {
  for (const ch of text) {
    const cp = ch.codePointAt(0) ?? 0;
    if (cp >= 0x2e80) return true;
  }
  return false;
}

export function stripCjk(text: string): string {
  return Array.from(text)
    .filter((ch) => (ch.codePointAt(0) ?? 0) < 0x2e80)
    .join('');
}

export function clipText(text: string, max = 24): string {
  return Array.from(text).slice(0, max).join('');
}

export function sanitizeEnglishText(
  raw: unknown,
  max = 24,
): { text: string; rejectedCjk: boolean; overLength: boolean; error: string | null } {
  const str = typeof raw === 'string' ? raw : '';
  const rejectedCjk = hasCjk(str);
  const cleaned = rejectedCjk ? stripCjk(str) : str;
  const overLength = Array.from(cleaned).length > max;
  const text = clipText(cleaned, max);
  return {
    text,
    rejectedCjk,
    overLength,
    error: rejectedCjk ? CJK_REFUSE_MESSAGE : null,
  };
}

export interface FontNeedFrame {
  style?: string;
  logo?: string;
  show_logo?: boolean;
  show_date?: boolean;
  text?: string;
}

export interface FontNeedStamp {
  enabled?: boolean;
  font?: string;
}

const STAMP_FONT_MAP: Record<string, FontKey> = {
  seg7: 'dseg',
  typewriter: 'courier',
  sans: 'atkinson',
};

/** 这一组边框 + 时间戳需要的字体代号（绝不传 `'cjk'`）。照 `finish_fonts.js` 再加徕卡 GXR 徽标要用的 `atkinson`。 */
export function fontsNeeded(
  frame: FontNeedFrame | null | undefined,
  stamp: FontNeedStamp | null | undefined,
): FontKey[] {
  const ids = new Set<FontKey>();
  const style = frame?.style;
  if (
    style === 'leica' ||
    style === 'hasselblad' ||
    style === 'thin_keyline' ||
    style === 'exhibition_label' ||
    style === 'blur_fill' ||
    style === 'postage_stamp'
  ) {
    ids.add('inter_sb');
    ids.add('inter');
  }
  if (style === 'leica') {
    const logo = frame?.logo ?? (frame?.show_logo === false ? 'none' : 'ricoh');
    if (frame?.show_logo !== false && logo !== 'none') ids.add('atkinson');
  }
  if (style === 'cinema' || style === 'slide_mount') {
    ids.add('inter_sb');
    ids.add('courier');
  }
  if (style === 'darkroom') ids.add('patrick');
  if (style === 'film') ids.add('inter_sb');
  if (style === 'instant' && (frame?.text || frame?.show_date)) ids.add('patrick');
  if (stamp?.enabled) ids.add(STAMP_FONT_MAP[stamp.font ?? 'seg7'] ?? 'dseg');
  return [...ids];
}

/** 把多份字体拼成 `gxr-frame` WASM `fonts_from_pack` 要的 `(index_json, pack)`。 */
export function packFonts(entries: ReadonlyArray<readonly [FontKey, Uint8Array]>): { indexJson: string; pack: Uint8Array } {
  let total = 0;
  const index: Array<[FontKey, number]> = [];
  for (const [name, bytes] of entries) {
    index.push([name, bytes.byteLength]);
    total += bytes.byteLength;
  }
  const pack = new Uint8Array(total);
  let offset = 0;
  for (const [, bytes] of entries) {
    pack.set(bytes, offset);
    offset += bytes.byteLength;
  }
  return { indexJson: JSON.stringify(index), pack };
}
