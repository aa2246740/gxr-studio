// 边框与时间戳的规格、默认值、归一化和文字摘要（照桌面 `finish.js`、`frame_panel.js`、`stamp_panel.js`、`output_size.js`）。
// 纯函数，不碰 DOM 或 WASM，Node 测试直接跑。

export const TEXT_MAX = 24;
export const LOGO_ID_RE = /^[a-z0-9_-]{1,64}$/;

export type FrameStyle =
  | 'none'
  | 'leica'
  | 'blur_fill'
  | 'gallery'
  | 'hasselblad'
  | 'instant'
  | 'film'
  | 'thin_keyline'
  | 'cinema'
  | 'exhibition_label'
  | 'slide_mount'
  | 'darkroom'
  | 'postage_stamp';

export type FrameColor = 'white' | 'black' | 'cream';
export type FrameWeight = 'thin' | 'medium' | 'thick';
export type OutputRatio = '1:1' | '4:5' | '3:4' | '2:3' | '9:16' | '16:9' | '3:2' | 'original';
export type FilmFmt = '135' | '120' | 'polaroid' | 'instax_square' | 'instax_wide' | 'instax_mini';
export type TableBg = 'light' | 'black';
export type LogoMode = 'auto' | 'orig' | 'mono';
export type LogoSize = 'small' | 'medium' | 'large';

export interface FrameSpec {
  version: 2;
  style: FrameStyle;
  color: FrameColor;
  weight: FrameWeight;
  show_logo: boolean;
  show_params: boolean;
  show_date: boolean;
  text: string;
  output_ratio: OutputRatio;
  fmt: FilmFmt;
  table: TableBg;
  logo: string;
  logo_mode: LogoMode;
  logo_size: LogoSize;
  model: boolean;
}

export type StampContent = 'date' | 'text' | 'date+text';
export type StampDateFormat = "'yy mm dd" | 'yy mm dd' | 'yyyy.mm.dd' | "mm dd 'yy";
export type StampFont = 'seg7' | 'typewriter' | 'sans';
export type StampColor = 'orange' | 'red' | 'yellow' | 'white';
export type StampSize = 'small' | 'medium' | 'large';
export type StampCorner = 'tl' | 'tr' | 'bl' | 'br';
export type StampPlacement = 'photo' | 'border';

export interface StampSpec {
  enabled: boolean;
  content: StampContent;
  date_format: StampDateFormat;
  show_time: boolean;
  date_override: string | null;
  text: string;
  font: StampFont;
  color: StampColor;
  size: StampSize;
  corner: StampCorner;
  glow: boolean;
  placement: StampPlacement;
}

export interface LogoInfo {
  id: string;
  name: string;
  w: number;
  h: number;
  coverage: number;
  opaque: boolean;
  lum: number;
}

export interface FrameMeta {
  lens: string;
  lens_short: string;
  aperture: string;
  shutter: string;
  iso: string;
  date: string;
  frame_no: string;
}

export type Moment = [number, number, number, number, number];

export interface FrameMetaResult {
  frame: FrameMeta;
  taken: string | null;
  file_time: string | null;
  width: number | null;
  height: number | null;
}

export type FrameOptionKey =
  | 'color'
  | 'weight'
  | 'fmt'
  | 'table'
  | 'logo'
  | 'show_params'
  | 'show_date'
  | 'text'
  | 'output_ratio';

export const FRAME_ORDER: readonly FrameStyle[] = [
  'none',
  'leica',
  'blur_fill',
  'gallery',
  'hasselblad',
  'instant',
  'film',
  'thin_keyline',
  'cinema',
  'exhibition_label',
  'slide_mount',
  'darkroom',
  'postage_stamp',
];

export const FRAME_NAMES: Record<FrameStyle, string> = {
  none: '无',
  gallery: '白边',
  leica: '徕卡水印',
  hasselblad: '哈苏水印',
  film: '胶片',
  instant: '拍立得',
  blur_fill: '模糊背景',
  thin_keyline: '极简细线',
  cinema: '电影宽银幕',
  exhibition_label: '展签',
  slide_mount: '幻灯片',
  darkroom: '暗房相纸',
  postage_stamp: '邮票齿孔',
};

export const STYLE_OPTIONS: Record<FrameStyle, readonly FrameOptionKey[]> = {
  none: [],
  leica: ['color', 'logo', 'show_params', 'show_date', 'text', 'output_ratio'],
  blur_fill: ['logo', 'show_params', 'output_ratio'],
  gallery: ['color', 'weight', 'output_ratio'],
  hasselblad: ['color', 'logo', 'show_params', 'show_date', 'text', 'output_ratio'],
  instant: ['fmt', 'show_date', 'text', 'output_ratio'],
  film: ['fmt', 'table', 'output_ratio'],
  thin_keyline: ['color', 'logo', 'output_ratio'],
  cinema: ['logo', 'text', 'output_ratio'],
  exhibition_label: ['color', 'logo', 'text', 'output_ratio'],
  slide_mount: ['color', 'logo', 'output_ratio'],
  darkroom: ['text', 'output_ratio'],
  postage_stamp: ['logo', 'output_ratio'],
};

export const LOGO_STYLES: readonly FrameStyle[] = [
  'leica',
  'hasselblad',
  'thin_keyline',
  'exhibition_label',
  'cinema',
  'slide_mount',
  'postage_stamp',
  'blur_fill',
];

export const COLOR_HEX: Record<FrameColor, string> = {
  white: '#ffffff',
  black: '#0e0e10',
  cream: '#f4efe6',
};

export const COLOR_NAMES: Record<FrameColor, string> = {
  white: '白',
  black: '黑',
  cream: '米白',
};

export const WEIGHT_NAMES: Record<FrameWeight, string> = {
  thin: '细',
  medium: '中',
  thick: '粗',
};

export const INSTANT_FORMATS: ReadonlyArray<[FilmFmt, string]> = [
  ['polaroid', '宝丽来'],
  ['instax_square', '方形'],
  ['instax_wide', '宽幅'],
  ['instax_mini', '迷你'],
];

export const FILM_FORMATS: ReadonlyArray<[FilmFmt, string]> = [
  ['135', '135'],
  ['120', '120'],
];

export const OUTPUT_CHOICES: ReadonlyArray<[OutputRatio, string]> = [
  ['1:1', '1:1 · 方图'],
  ['4:5', '4:5 · Instagram 竖图'],
  ['3:4', '3:4 · 小红书'],
  ['2:3', '2:3 · 竖幅'],
  ['9:16', '9:16 · 快拍 · 短视频'],
  ['16:9', '16:9 · 横屏视频'],
  ['3:2', '3:2 · 横幅'],
  ['original', '照片原比例'],
];

export const OUTPUT_RATIOS: Record<OutputRatio, [number, number] | null> = {
  '1:1': [1, 1],
  '4:5': [4, 5],
  '3:4': [3, 4],
  '2:3': [2, 3],
  '9:16': [9, 16],
  '16:9': [16, 9],
  '3:2': [3, 2],
  original: null,
};

export const STAMP_FONTS: Record<StampFont, string> = {
  seg7: '数码管',
  typewriter: '打字机',
  sans: '无衬线',
};

export const STAMP_COLOR_NAMES: Record<StampColor, string> = {
  orange: '橙',
  red: '红',
  yellow: '黄',
  white: '白',
};

export const STAMP_COLOR_HEX: Record<StampColor, string> = {
  orange: '#ff8a2a',
  red: '#ff3b2f',
  yellow: '#ffd23a',
  white: '#fff4e6',
};

export const STAMP_SIZES: Record<StampSize, string> = {
  small: '小',
  medium: '中',
  large: '大',
};

export const STAMP_CORNERS: Record<StampCorner, string> = {
  tl: '左上',
  tr: '右上',
  bl: '左下',
  br: '右下',
};

export const STAMP_CONTENT: ReadonlyArray<[StampContent, string]> = [
  ['date', '日期'],
  ['text', '文字'],
  ['date+text', '日期和文字'],
];

export const STAMP_FORMATS: readonly StampDateFormat[] = [
  "'yy mm dd",
  'yy mm dd',
  'yyyy.mm.dd',
  "mm dd 'yy",
];

export const DEFAULT_FRAME: FrameSpec = {
  version: 2,
  style: 'none',
  color: 'white',
  weight: 'medium',
  show_logo: true,
  show_params: true,
  show_date: false,
  text: '',
  output_ratio: '3:4',
  fmt: '135',
  table: 'light',
  logo: 'ricoh',
  logo_mode: 'auto',
  logo_size: 'medium',
  model: true,
};

export const DEFAULT_STAMP: StampSpec = {
  enabled: false,
  content: 'date',
  date_format: "'yy mm dd",
  show_time: false,
  date_override: null,
  text: '',
  font: 'seg7',
  color: 'orange',
  size: 'medium',
  corner: 'br',
  glow: true,
  placement: 'photo',
};

const VALID_STYLES = new Set<string>(FRAME_ORDER);
const VALID_COLORS = new Set<string>(['white', 'black', 'cream']);
const VALID_WEIGHTS = new Set<string>(['thin', 'medium', 'thick']);
const VALID_RATIOS = new Set<string>(Object.keys(OUTPUT_RATIOS));
const VALID_TABLES = new Set<string>(['light', 'black']);
const VALID_LOGO_MODES = new Set<string>(['auto', 'orig', 'mono']);
const VALID_LOGO_SIZES = new Set<string>(['small', 'medium', 'large']);
const VALID_STAMP_CONTENT = new Set<string>(['date', 'text', 'date+text']);
const VALID_STAMP_FORMATS = new Set<string>(STAMP_FORMATS);
const VALID_STAMP_FONTS = new Set<string>(['seg7', 'typewriter', 'sans']);
const VALID_STAMP_COLORS = new Set<string>(['orange', 'red', 'yellow', 'white']);
const VALID_STAMP_SIZES = new Set<string>(['small', 'medium', 'large']);
const VALID_STAMP_CORNERS = new Set<string>(['tl', 'tr', 'bl', 'br']);
const VALID_STAMP_PLACEMENTS = new Set<string>(['photo', 'border']);

export function clipText(text: unknown, max = TEXT_MAX): string {
  return Array.from(text == null ? '' : String(text)).slice(0, max).join('');
}

export function isDateString(s: unknown): s is string {
  if (typeof s !== 'string' || !/^\d{4}-\d{2}-\d{2}$/.test(s)) return false;
  const [y, m, d] = s.split('-').map(Number);
  const dt = new Date(Date.UTC(y, m - 1, d));
  return dt.getUTCFullYear() === y && dt.getUTCMonth() === m - 1 && dt.getUTCDate() === d;
}

export function normalizeFrame(f: Partial<FrameSpec> | Record<string, unknown> | null | undefined): FrameSpec {
  if (!f || typeof f !== 'object' || Array.isArray(f)) return { ...DEFAULT_FRAME };
  const keys = Object.keys(DEFAULT_FRAME);
  const raw = f as Record<string, unknown>;
  if (Object.keys(raw).some((k) => !keys.includes(k)) || ('version' in raw && raw.version !== 2)) {
    return { ...DEFAULT_FRAME };
  }
  const style = (raw.style === undefined ? 'none' : raw.style) as string;
  if (!VALID_STYLES.has(style)) return { ...DEFAULT_FRAME };
  const typedStyle = style as FrameStyle;
  const formats: readonly FilmFmt[] =
    typedStyle === 'instant' ? ['polaroid', 'instax_square', 'instax_wide', 'instax_mini'] : ['135', '120'];
  const out: FrameSpec = {
    ...DEFAULT_FRAME,
    color: typedStyle === 'thin_keyline' || typedStyle === 'slide_mount' ? 'cream' : 'white',
    show_date: typedStyle === 'leica' || typedStyle === 'hasselblad',
    fmt: formats[0],
    ...(raw as Partial<FrameSpec>),
    style: typedStyle,
    version: 2,
  };
  if ('logo' in raw && raw.logo !== undefined) {
    if (typeof raw.logo !== 'string' || (raw.logo !== 'ricoh' && raw.logo !== 'none' && !LOGO_ID_RE.test(raw.logo))) {
      return { ...DEFAULT_FRAME };
    }
    out.logo = raw.logo === 'ricoh' && raw.show_logo === false ? 'none' : raw.logo;
  } else {
    out.logo = out.show_logo === false ? 'none' : 'ricoh';
  }
  if (!('show_logo' in raw)) out.show_logo = out.logo !== 'none';
  if (
    !formats.includes(out.fmt) ||
    !VALID_TABLES.has(out.table) ||
    !VALID_COLORS.has(out.color) ||
    !VALID_WEIGHTS.has(out.weight) ||
    !VALID_RATIOS.has(out.output_ratio) ||
    !VALID_LOGO_MODES.has(out.logo_mode) ||
    !VALID_LOGO_SIZES.has(out.logo_size) ||
    typeof out.model !== 'boolean' ||
    typeof out.show_logo !== 'boolean' ||
    typeof out.show_params !== 'boolean' ||
    typeof out.show_date !== 'boolean' ||
    typeof out.text !== 'string' ||
    Array.from(out.text).length > TEXT_MAX
  ) {
    return { ...DEFAULT_FRAME };
  }
  out.show_logo = out.logo !== 'none';
  return out;
}

export function selectFrame(frame: FrameSpec, style: FrameStyle): FrameSpec {
  return normalizeFrame({
    ...frame,
    style,
    color: style === 'thin_keyline' || style === 'slide_mount' ? 'cream' : frame.color,
    fmt: style === 'instant' ? 'polaroid' : '135',
    show_date: style === 'leica' || style === 'hasselblad',
  });
}

export function normalizeStamp(s: Partial<StampSpec> | Record<string, unknown> | null | undefined): StampSpec {
  if (!s || typeof s !== 'object' || Array.isArray(s)) return { ...DEFAULT_STAMP };
  const raw = s as Record<string, unknown>;
  return {
    enabled: typeof raw.enabled === 'boolean' ? raw.enabled : DEFAULT_STAMP.enabled,
    content: VALID_STAMP_CONTENT.has(String(raw.content)) ? (raw.content as StampContent) : DEFAULT_STAMP.content,
    date_format: VALID_STAMP_FORMATS.has(String(raw.date_format))
      ? (raw.date_format as StampDateFormat)
      : DEFAULT_STAMP.date_format,
    show_time: typeof raw.show_time === 'boolean' ? raw.show_time : DEFAULT_STAMP.show_time,
    date_override: isDateString(raw.date_override) ? raw.date_override : null,
    text: typeof raw.text === 'string' ? clipText(raw.text, TEXT_MAX) : DEFAULT_STAMP.text,
    font: VALID_STAMP_FONTS.has(String(raw.font)) ? (raw.font as StampFont) : DEFAULT_STAMP.font,
    color: VALID_STAMP_COLORS.has(String(raw.color)) ? (raw.color as StampColor) : DEFAULT_STAMP.color,
    size: VALID_STAMP_SIZES.has(String(raw.size)) ? (raw.size as StampSize) : DEFAULT_STAMP.size,
    corner: VALID_STAMP_CORNERS.has(String(raw.corner)) ? (raw.corner as StampCorner) : DEFAULT_STAMP.corner,
    glow: typeof raw.glow === 'boolean' ? raw.glow : DEFAULT_STAMP.glow,
    placement: VALID_STAMP_PLACEMENTS.has(String(raw.placement))
      ? (raw.placement as StampPlacement)
      : DEFAULT_STAMP.placement,
  };
}

export function sameFrameSpec(a: FrameSpec, b: FrameSpec): boolean {
  return (
    a.version === b.version &&
    a.style === b.style &&
    a.color === b.color &&
    a.weight === b.weight &&
    a.show_logo === b.show_logo &&
    a.show_params === b.show_params &&
    a.show_date === b.show_date &&
    a.text === b.text &&
    a.output_ratio === b.output_ratio &&
    a.fmt === b.fmt &&
    a.table === b.table &&
    a.logo === b.logo &&
    a.logo_mode === b.logo_mode &&
    a.logo_size === b.logo_size &&
    a.model === b.model
  );
}

export function sameStampSpec(a: StampSpec, b: StampSpec): boolean {
  return (
    a.enabled === b.enabled &&
    a.content === b.content &&
    a.date_format === b.date_format &&
    a.show_time === b.show_time &&
    a.date_override === b.date_override &&
    a.text === b.text &&
    a.font === b.font &&
    a.color === b.color &&
    a.size === b.size &&
    a.corner === b.corner &&
    a.glow === b.glow &&
    a.placement === b.placement
  );
}

export function stampDateText(format: StampDateFormat, iso: string, time = '', showTime = false): string {
  const [Y, M, D] = iso.split('-');
  const yy = Y.slice(2);
  let s: string;
  switch (format) {
    case 'yy mm dd':
      s = `${yy} ${M} ${D}`;
      break;
    case 'yyyy.mm.dd':
      s = `${Y}.${M}.${D}`;
      break;
    case "mm dd 'yy":
      s = `${M} ${D} '${yy}`;
      break;
    default:
      s = `'${yy} ${M} ${D}`;
  }
  if (showTime && time) s += ` ${time}`;
  return s;
}

export function stampSummary(stamp: StampSpec | null | undefined, frame?: FrameSpec | null): string {
  const st = stamp ?? DEFAULT_STAMP;
  const where = st.placement === 'border' && frame?.style === 'instant' ? '拍立得边框上' : '印在照片上';
  return `${where}：${STAMP_COLOR_NAMES[st.color]}色${STAMP_FONTS[st.font]}，${STAMP_CORNERS[st.corner]}角`;
}

export function ratioText(ratio: [number, number] | null | undefined): string {
  if (!ratio) return '';
  const [w, h] = ratio;
  if (w === 191 && h === 100) return '1.91:1';
  if (w === 100 && h === 191) return '1:1.91';
  if (w === 47 && h === 20) return '2.35:1';
  if (w === 20 && h === 47) return '1:2.35';
  if (w === 8 && h === 5) return '16:10';
  if (w === 5 && h === 8) return '10:16';
  return `${w}:${h}`;
}

export function sizeText(size: [number, number], ratio: [number, number] | null | undefined): string {
  return `${size[0]} × ${size[1]} 像素${ratio ? ` · ${ratioText(ratio)}` : ''}`;
}

export function displayRatio(outputRatio: OutputRatio, photoW: number, photoH: number): [number, number] | null {
  const fixed = OUTPUT_RATIOS[outputRatio];
  if (fixed) return fixed;
  if (photoW <= 0 || photoH <= 0) return null;
  const gcd = (a: number, b: number): number => (b === 0 ? a : gcd(b, a % b));
  const g = gcd(Math.round(photoW), Math.round(photoH));
  return [Math.round(photoW / g), Math.round(photoH / g)];
}

/** 解析 `export_meta_photo` 返回的 `YYYY-MM-DDTHH:MM:SS` 为 `gxr-frame` 要的 `[年, 月, 日, 时, 分]`。 */
export function momentFromIso(iso: string | null | undefined): Moment | null {
  if (typeof iso !== 'string') return null;
  const m = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})/.exec(iso);
  if (!m) return null;
  return [Number(m[1]), Number(m[2]), Number(m[3]), Number(m[4]), Number(m[5])];
}

export function styleBgLum(frame: FrameSpec): number {
  const s = frame.style;
  const c = frame.color || 'white';
  if (s === 'cinema') return 8;
  if (s === 'postage_stamp') return 250;
  if (s === 'slide_mount') return c === 'black' ? 30 : 240;
  if (s === 'exhibition_label') return c === 'black' ? 22 : 250;
  if (s === 'thin_keyline') return c === 'black' ? 18 : c === 'cream' ? 238 : 245;
  return c === 'black' ? 18 : 245;
}

export function pickLogoMode(info: Pick<LogoInfo, 'opaque' | 'lum'>, mode: LogoMode, bgLum: number): 'orig' | 'mono' {
  if (info.opaque) return 'orig';
  if (mode === 'orig' || mode === 'mono') return mode;
  return Math.abs(info.lum - bgLum) < 70 ? 'mono' : 'orig';
}

export interface BgFx {
  kind: 'blur' | 'glow';
  sigma?: number;
  veil?: number;
  sat?: number;
  falloff?: number;
}

export type FrameItem =
  | { k: 'photo'; x: number; y: number; w: number; h: number }
  | {
      k: 'text';
      x: number;
      y: number;
      px: number;
      font: string;
      text: string;
      xs: number[];
      fill?: [number, number, number];
      auto_ink?: [[number, number, number], [number, number, number]];
      cjk?: boolean;
      rot?: boolean;
      gx?: number;
      gy?: number;
      tw?: number;
      xs0?: number[];
    }
  | { k: 'poly'; pts: Array<[number, number]>; fill: [number, number, number] }
  | { k: 'carrier'; x: number; y: number; w: number; h: number; c: number; jit: number; seed: number; fill: [number, number, number]; paper: [number, number, number] }
  | { k: 'ellipse'; cx: number; cy: number; r: number; fill: [number, number, number] }
  | { k: 'rect'; x: number; y: number; w: number; h: number; radius?: number; fill?: [number, number, number]; outline?: [number, number, number]; stroke_w?: number }
  | { k: 'shadow'; x: number; y: number; w: number; h: number; radius?: number; alpha: number; blur: number; dx?: number; dy?: number }
  | { k: 'hole'; x: number; y: number; w: number; h: number; radius?: number }
  | { k: 'line'; x1: number; y1: number; x2: number; y2: number; w: number; fill: [number, number, number] }
  | { k: 'logo'; x: number; y: number; w: number; h: number; fill?: [number, number, number] }
  | { k: 'clogo'; id: string; x: number; y: number; w: number; h: number; mode?: 'orig' | 'mono'; ink?: [number, number, number]; auto_ink?: [[number, number, number], [number, number, number]]; auto?: boolean };

export interface FrameLayoutData {
  width: number;
  height: number;
  photo: [number, number];
  bg: [number, number, number];
  items: FrameItem[];
  bgfx?: BgFx | null;
  unit_box?: [number, number, number, number];
  fixed?: boolean;
  grain?: number;
}

export interface StampLayoutData {
  px: number;
  font: string;
  glow: number | null;
  rect: [number, number, number, number];
  ink: [number, number, number];
  ops: Array<
    | {
        k: 'text';
        x: number;
        y: number;
        px: number;
        font: string;
        text: string;
        xs: number[];
        fill: [number, number, number];
        cjk?: boolean;
      }
    | { k: 'poly'; pts: Array<[number, number]>; fill: [number, number, number] }
  >;
}

export interface Deco {
  key: string;
  frame: FrameLayoutData | null;
  stamp: StampLayoutData | null;
  stampOrigin: [number, number] | null;
  ew: number;
  eh: number;
  width: number;
  height: number;
}
