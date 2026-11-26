// LUT 纯函数：12 个色彩空间、命名与声明识别、文件头署名读取、输入色彩空间选择建议、出图规格与令牌（照桌面 lut/*.py 实现，不碰 DOM/IDB/WASM）。

import { thisDevice, type DeviceEnv } from './device.ts';

export const MAX_CUBE_BYTES = 64 << 20;
/** 浏览器本地 LUT 库总大小上限（IndexedDB 与读取前的唯一常量）。 */
export const MAX_LUT_LIBRARY_BYTES = 256 << 20;

export function lutFootnote(env?: DeviceEnv): string {
  return `LUT 只存在${thisDevice('上', env)}，直接套在照片上，不写入相机。`;
}

export const LUT_ERRORS = {
  cube_too_big: '无法读取 LUT。文件过大。',
  cube_unreadable: '无法读取 LUT。请改用 .cube 文件。',
  cube_not_found: '找不到这个 LUT。请重新导入。',
  lut_needs_raw: '这个 LUT 需要 RAW 文件',
  lut_space_required: '请选择输入色彩空间',
  library_full: 'LUT 太多了。删掉几个再导入。',
  duplicate: '已经导入过',
  bad_name: '无法保存。名称无效。',
} as const;

export function ignoredFilesMessage(count: number): string {
  return `${count} 个文件不是 .cube，没有导入。`;
}

export type SpaceKind = 'display' | 'log';
export type GamutId = 'rec709' | 'rec2020' | 'vgamut' | 'sgamut3' | 'sgamut3cine';
export type RangeKind = 'full' | 'legal';

export type SpaceId =
  | 'srgb'
  | 'rec709'
  | 'gamma24'
  | 'flog'
  | 'flog2'
  | 'llog'
  | 'llog709'
  | 'vlog'
  | 'slog3'
  | 'slog3-sgamut3'
  | 'applelog'
  | 'linear';

export interface SpaceInfo {
  id: SpaceId;
  label: string;
  shortLabel: string;
  kind: SpaceKind;
  gamut: GamutId;
}

/** `lut/service.py` SPACE_LABELS + `lut/spaces.py` SPACES / GAMUT。 */
export const SPACES: readonly SpaceInfo[] = [
  { id: 'srgb', label: '普通照片 / 网页颜色（sRGB）', shortLabel: 'sRGB', kind: 'display', gamut: 'rec709' },
  { id: 'rec709', label: '视频显示（Rec.709）', shortLabel: 'Rec.709', kind: 'display', gamut: 'rec709' },
  { id: 'gamma24', label: '视频显示（Gamma 2.4）', shortLabel: 'Gamma 2.4', kind: 'display', gamut: 'rec709' },
  { id: 'flog', label: '富士 F-Log', shortLabel: '富士 F-Log', kind: 'log', gamut: 'rec2020' },
  { id: 'flog2', label: '富士 F-Log2', shortLabel: '富士 F-Log2', kind: 'log', gamut: 'rec2020' },
  { id: 'llog', label: '徕卡 L-Log', shortLabel: '徕卡 L-Log', kind: 'log', gamut: 'rec2020' },
  { id: 'llog709', label: '徕卡 L-Log（Rec.709 色域）', shortLabel: '徕卡 L-Log（Rec.709 色域）', kind: 'log', gamut: 'rec709' },
  { id: 'vlog', label: '松下 V-Log', shortLabel: '松下 V-Log', kind: 'log', gamut: 'vgamut' },
  { id: 'slog3', label: '索尼 S-Log3', shortLabel: '索尼 S-Log3', kind: 'log', gamut: 'sgamut3cine' },
  { id: 'slog3-sgamut3', label: '索尼 S-Log3 / S-Gamut3', shortLabel: '索尼 S-Log3 / S-Gamut3', kind: 'log', gamut: 'sgamut3' },
  { id: 'applelog', label: '苹果 Log', shortLabel: '苹果 Log', kind: 'log', gamut: 'rec2020' },
  { id: 'linear', label: '线性光（高级）', shortLabel: '线性光（高级）', kind: 'log', gamut: 'rec709' },
] as const;

export const DISPLAY_SPACES: readonly SpaceInfo[] = SPACES.filter((s) => s.kind === 'display');

export const SPACE_GROUPS: ReadonlyArray<{ kind: SpaceKind; label: string }> = [
  { kind: 'display', label: '普通照片与视频显示' },
  { kind: 'log', label: '相机 Log 与高级空间' },
];

export const OUTPUT_GAMUTS: ReadonlyArray<readonly [GamutId | '', string]> = [
  ['', '默认'],
  ['rec709', 'Rec.709'],
  ['rec2020', 'Rec.2020'],
];

export type InputChoiceSource = 'declared' | 'guessed' | 'fallback' | 'manual' | 'required';

export const SOURCE_TAGS: Record<Exclude<InputChoiceSource, 'required'>, string> = {
  declared: '文件已写明',
  guessed: '推测',
  fallback: '默认',
  manual: '已手选',
};

const SPACE_BY_ID = new Map<string, SpaceInfo>(SPACES.map((s) => [s.id, s]));

export function isValidSpace(space: unknown): space is SpaceId {
  return typeof space === 'string' && SPACE_BY_ID.has(space);
}

export function isDisplaySpace(space: string | null | undefined): boolean {
  return space === 'srgb' || space === 'rec709' || space === 'gamma24';
}

export function spaceInfo(space: string | null | undefined): SpaceInfo | null {
  return typeof space === 'string' ? SPACE_BY_ID.get(space) ?? null : null;
}

export function spaceLabel(space: string | null | undefined): string {
  return spaceInfo(space)?.label ?? '';
}

export function spaceShortLabel(space: string | null | undefined): string {
  return spaceInfo(space)?.shortLabel ?? '';
}

/** 只有明确是 Log / 高级输入空间才需要 RAW；没有选空间时还不允许选用。 */
export function lutNeedsRaw(inputSpace: string | null | undefined): boolean {
  if (!inputSpace) return false;
  return !isDisplaySpace(inputSpace);
}

/** `lut/library.py` label()：压缩空白、去首尾，限制 1–255 字符，禁止控制字符。 */
export function cleanLutLabel(value: unknown, field = 'LUT 名称'): string {
  if (typeof value !== 'string') throw new Error(LUT_ERRORS.bad_name);
  const cleaned = value.replace(/\s+/g, ' ').trim();
  const len = Array.from(cleaned).length;
  if (len < 1 || len > 255) throw new Error(`${field}需为 1–255 个字符。`);
  for (let i = 0; i < cleaned.length; i++) {
    const code = cleaned.charCodeAt(i);
    if (code < 32 || code === 127) throw new Error(LUT_ERRORS.bad_name);
  }
  return cleaned;
}

/** `lut/library.py` file_parts()：只作显示名，不作磁盘路径。 */
export function fileParts(filename: string): { name: string; group: string | null; skip: boolean } {
  const parts = filename
    .replace(/\\/g, '/')
    .split('/')
    .filter((p, i) => p !== '' && !(i > 0 && p === '.'));
  const skip = parts.includes('__MACOSX') || parts.some((p) => p.startsWith('._'));
  const name = parts.length ? parts[parts.length - 1] : '';
  const group = parts.length > 1 && parts[0] !== '/' && parts[0] !== '.' && parts[0] !== '..' ? parts[0] : null;
  return { name, group, skip };
}

export function isCubeFilename(filename: string): boolean {
  return filename.toLowerCase().endsWith('.cube');
}

export function classifyLutImports<T extends { name: string }>(
  files: readonly T[],
): { cubes: Array<{ file: T; name: string; group: string | null }>; ignored: number; skipped: number } {
  const cubes: Array<{ file: T; name: string; group: string | null }> = [];
  let ignored = 0;
  let skipped = 0;
  for (const file of files) {
    const { name, group, skip } = fileParts(file.name);
    if (skip) {
      skipped += 1;
      continue;
    }
    if (!isCubeFilename(name)) {
      ignored += 1;
      continue;
    }
    cubes.push({ file, name, group });
  }
  return { cubes, ignored, skipped };
}

/** `lut/cube.py` comment_start()：不把引号 TITLE 里的 `#` 当注释。 */
export function commentStart(line: string): number {
  if (!line.trimStart().startsWith('TITLE')) return line.indexOf('#');
  let quote = '';
  let escaped = false;
  for (let i = 0; i < line.length; i++) {
    const ch = line[i];
    if (escaped) {
      escaped = false;
    } else if (ch === '\\' && quote !== "'") {
      escaped = true;
    } else if (quote) {
      if (ch === quote) quote = '';
    } else if (ch === '"' || ch === "'") {
      quote = ch;
    } else if (ch === '#') {
      return i;
    }
  }
  return -1;
}

export interface LutHeader {
  source: string;
  author: string;
  license: string;
  comments: string[];
}

const META_KEYS: Record<string, 'source' | 'author' | 'license'> = {
  source: 'source',
  来源: 'source',
  url: 'source',
  website: 'source',
  author: 'author',
  作者: 'author',
  creator: 'author',
  copyright: 'author',
  license: 'license',
  licence: 'license',
  许可: 'license',
};

const CC_RE = /CC[ -]BY|creativecommons\.org\/licenses|Creative Commons/i;
const HEADER_FIELD_RE = /^([^:=]+)[:=] ?(.*)$/;

/** 按 UTF-8（去 BOM）读取 `.cube` 文本；非 UTF-8 返回 null。 */
export function decodeCubeUtf8(bytes: Uint8Array): string | null {
  try {
    const text = new TextDecoder('utf-8', { fatal: true }).decode(bytes);
    return text.charCodeAt(0) === 0xfeff ? text.slice(1) : text;
  } catch {
    return null;
  }
}

/** `lut/metadata.py` read_header()：提取署名与其余注释供选择建议。 */
export function readLutHeader(text: string): LutHeader {
  const values: Record<'source' | 'author' | 'license', string[]> = { source: [], author: [], license: [] };
  const comments: string[] = [];
  let exact: { source: string; author: string; license: string } | null = null;
  for (const line of text.split(/\r\n|\n|\r/)) {
    const start = commentStart(line);
    if (start < 0) continue;
    const comment = line.slice(start + 1).trimStart();
    if (comment.startsWith('GXR-STUDIO-METADATA: ')) {
      try {
        const raw = JSON.parse(comment.slice('GXR-STUDIO-METADATA: '.length)) as unknown;
        if (raw && typeof raw === 'object' && !Array.isArray(raw)) {
          const obj = raw as Record<string, unknown>;
          const keys = Object.keys(obj);
          if (
            keys.length === 3 &&
            ['source', 'author', 'license'].every(
              (k) => typeof obj[k] === 'string' && (obj[k] as string).length <= 8192,
            )
          ) {
            exact = {
              source: obj.source as string,
              author: obj.author as string,
              license: obj.license as string,
            };
          }
        }
      } catch {
        /* 坏的元数据行忽略 */
      }
      continue;
    }
    const match = HEADER_FIELD_RE.exec(comment);
    const key = match ? META_KEYS[match[1].trim().toLowerCase()] : undefined;
    if (key && match) {
      values[key].push(match[2]);
    } else if (CC_RE.test(comment)) {
      values.license.push(comment);
    } else {
      comments.push(comment);
    }
  }
  return {
    source: exact ? exact.source : values.source.join('\n'),
    author: exact ? exact.author : values.author.join('\n'),
    license: exact ? exact.license : values.license.join('\n'),
    comments,
  };
}

// ---- `lut/identify.py` + `lut/page.py` ----------------------------------------

const PATTERNS: ReadonlyArray<readonly [string, SpaceId]> = [
  ['s[-_ ]?log3[-_ ]s[-_ ]?gamut3(?![a-z0-9]|cine)', 'slog3-sgamut3'],
  ['f[-_ ]?log2', 'flog2'],
  ['f[-_ ]?log', 'flog'],
  ['l[-_ ]?log709', 'llog709'],
  ['l[-_ ]?log', 'llog'],
  ['v[-_ ]?log', 'vlog'],
  ['s[-_ ]?log3', 'slog3'],
  ['apple[-_ ]?log', 'applelog'],
  ['srgb', 'srgb'],
  ['(?:rec|bt)[ ._-]*709', 'rec709'],
  ['gamma[ _-]*2[._ ]?4|bt[ ._-]*1886', 'gamma24'],
  ['linear', 'linear'],
];

const FIELD_RE = /(?:^|;)\s*(input|output)(?:[ _-]*(?:colou?r[ _-]*space|space|transfer(?:[ _-]*function)?|gamma))?\b\s*[:=]?\s*/gi;
const SPACE_HINT_RE = /(?<![a-z0-9])(?:(?:apple|red|[afvslcd])[-_ ]?)?(?:log[a-z0-9]*|linear[a-z0-9]*|gamma[a-z0-9]*)/i;
const ATTR_PREFIX_RE = /^(?:source|author|license|来源|作者|许可)\s*:/i;

function stemOf(filename: string): string {
  const base = filename.split(/[\\/]/).pop() ?? filename;
  const dot = base.lastIndexOf('.');
  return dot <= 0 ? base : base.slice(0, dot);
}

function findSpaces(input: string): Record<string, string> {
  const cleaned = input.replace(/(?:rec[ ._-]*709|srgb)\s+(?:primaries|gamut)/gi, '');
  const found: Record<string, string> = {};
  const occupied: Array<[number, number]> = [];
  for (const [pattern, space] of PATTERNS) {
    // 兼容不含 lookbehind-conflict 的模式；JS 支持 lookbehind。
    const re = new RegExp(`(?<![a-z0-9])(?:${pattern})(?![a-z0-9])`, 'gi');
    for (const match of cleaned.matchAll(re)) {
      const start = match.index ?? 0;
      const end = start + match[0].length;
      if (occupied.some(([s, e]) => s <= start && start < e)) continue;
      found[space] = match[0];
      occupied.push([start, end]);
    }
  }
  return found;
}

function namedInput(name: string, title: string): Record<string, string> {
  const named = [title, stemOf(name)];
  const marked: Record<string, string> = {};
  for (const text of named) {
    const match = /\b(?:input|from)[ :=_-]+(.+)/i.exec(text);
    if (match) {
      const domain = match[1].split(/[;]|\bto\b|_to_|->|[_ -]+(?:style|look|target)[ :=_-]+/i, 2)[0];
      Object.assign(marked, findSpaces(domain));
    }
  }
  const dest = named[1].split(/_to_/i);
  const suffix = /.+[_ ]((?:v|f|l|s)[- ]?log\d?|apple[- ]?log)(?:[_ ](?:srgb|rec[ ._-]*709|\d+))*$/i.exec(dest[dest.length - 1] ?? '');
  if (dest.length > 1 && suffix) {
    Object.assign(marked, findSpaces(suffix[1]));
  }
  if (Object.keys(marked).length) return marked;
  const directional: Record<string, string> = {};
  for (const text of named) {
    const parts = text.split(/(?:\bto\b|_to_|->)/i);
    if (parts.length > 1) Object.assign(directional, findSpaces(parts[0]));
  }
  if (Object.keys(directional).length) return directional;
  const found: Record<string, string> = {};
  for (const text of named) Object.assign(found, findSpaces(text));
  const logs: Record<string, string> = {};
  for (const [space, word] of Object.entries(found)) {
    if (!isDisplaySpace(space)) logs[space] = word;
  }
  return Object.keys(logs).length ? logs : found;
}

export function guessSpace(name: string, title = ''): [SpaceId | null, string | null] {
  const found = namedInput(name, title);
  const keys = Object.keys(found);
  if (keys.length === 1) {
    const space = keys[0] as SpaceId;
    return [space, `文件名/标题里有“${found[space]}”`];
  }
  return [null, null];
}

export interface SpaceEvidence {
  keyword?: string | null;
  content?: 'display' | 'log' | null;
  input_declarations?: string[];
  output?: Record<string, string>;
  black?: number | null;
  white?: number | null;
  middle?: number | null;
  mid_slope?: number | null;
  channel_spread?: number | null;
}

export interface InputChoice {
  default_input_space: SpaceId | null;
  default_input_source: InputChoiceSource;
  default_input_note: string;
}

export interface ChoiceInput {
  input_space: SpaceId | null;
  input_space_state: string;
  input_space_manual: boolean;
  filename: string;
  title: string;
  comments: readonly string[];
  space_evidence: SpaceEvidence;
}

/** `lut/page.py` input_choice()：给出默认输入空间、来源标签（manual / declared / guessed / fallback / required）和提示语。 */
export function computeInputChoice(item: ChoiceInput): InputChoice {
  const space = item.input_space;
  const evidence = item.space_evidence ?? {};
  const declarations = evidence.input_declarations ?? [];
  if (item.input_space_manual && space) {
    return {
      default_input_space: space,
      default_input_source: 'manual',
      default_input_note: '使用你选择的输入色彩空间。',
    };
  }
  if (space && declarations.length > 0) {
    return {
      default_input_space: space,
      default_input_source: 'declared',
      default_input_note: '按 LUT 文件写明的输入色彩空间选择。',
    };
  }
  if (space) {
    return {
      default_input_space: space,
      default_input_source: 'guessed',
      default_input_note: '根据文件名、标题或注释推测，请核对 LUT 的说明。',
    };
  }
  return undeclaredChoice(item, evidence, declarations);
}

function undeclaredChoice(
  item: ChoiceInput,
  evidence: SpaceEvidence,
  declarations: readonly string[],
): InputChoice {
  if (item.input_space_state === 'conflict') {
    return {
      default_input_space: null,
      default_input_source: 'required',
      default_input_note: '文件里的颜色信息不一致，请按 LUT 的说明选择。',
    };
  }
  if (declarations.length > 0) {
    return {
      default_input_space: null,
      default_input_source: 'required',
      default_input_note: '暂时无法识别文件写明的输入颜色，请按 LUT 的说明选择。',
    };
  }
  const naming = `${item.filename || ''} ${item.title || ''}`;
  if (SPACE_HINT_RE.test(naming) && !guessSpace(item.filename || '', item.title || '')[0]) {
    return {
      default_input_space: null,
      default_input_source: 'required',
      default_input_note: '无法确定这个 LUT 的输入颜色，请按 LUT 的说明选择。',
    };
  }
  const comments: string[] = [];
  for (const text of item.comments ?? []) {
    FIELD_RE.lastIndex = 0;
    let hasOutput = false;
    let m: RegExpExecArray | null;
    while ((m = FIELD_RE.exec(text)) !== null) {
      if (m[1].toLowerCase() === 'output') {
        hasOutput = true;
        break;
      }
    }
    if (!hasOutput && !ATTR_PREFIX_RE.test(text)) comments.push(text);
  }
  const guesses = new Set<SpaceId>();
  for (const text of comments) {
    const [g] = guessSpace('', text);
    if (g) guesses.add(g);
  }
  if (guesses.size === 1) {
    const space = [...guesses][0];
    const kind: SpaceKind = isDisplaySpace(space) ? 'display' : 'log';
    if (evidence.content == null || evidence.content === kind) {
      return {
        default_input_space: space,
        default_input_source: 'guessed',
        default_input_note: '根据文件注释推测，请核对 LUT 的说明。',
      };
    }
  }
  const combined = [item.filename || '', item.title || '', ...comments].join(' ');
  if (guesses.size > 0 || evidence.content === 'log' || SPACE_HINT_RE.test(combined)) {
    return {
      default_input_space: null,
      default_input_source: 'required',
      default_input_note: '无法确定这个 LUT 的输入颜色，请按 LUT 的说明选择。',
    };
  }
  return {
    default_input_space: 'srgb',
    default_input_source: 'fallback',
    default_input_note: '文件没有写明输入颜色，先用创意 LUT 常用的 sRGB（Rec.709 色域）。请核对 LUT 的说明。',
  };
}

// ---- 记录、出图规格与令牌 ----------------------------------------------------

export interface LutTargetSpec {
  input_space: SpaceId;
  output_space: SpaceId;
  input_gamut: GamutId | null;
  output_gamut: GamutId | null;
  input_range: RangeKind;
  output_range: RangeKind;
}

export interface LutRecord {
  cube_id: string;
  name: string;
  filename: string;
  title: string;
  kind: string;
  size_1d: number;
  size_3d: number;
  bytes: number;
  version: number;
  starred: boolean;
  source: string;
  author: string;
  license: string;
  comments: string[];
  input_space: SpaceId | null;
  output_space: SpaceId;
  input_gamut: GamutId | null;
  output_gamut: GamutId | null;
  input_range: RangeKind;
  output_range: RangeKind;
  input_space_state: string;
  input_space_manual: boolean;
  input_space_guess: SpaceId | null;
  guess_reason: string | null;
  guess_note: string | null;
  space_evidence: SpaceEvidence;
  output_space_state: string;
  technical: boolean;
  savedAt: number;
}

export interface ParsedCubeSummary {
  title?: string | null;
  kind?: string;
  size_1d?: number;
  size_3d?: number;
}

export interface IdentifyReport {
  input_space?: SpaceId | null;
  input_space_state?: string;
  input_space_manual?: boolean;
  input_space_guess?: SpaceId | null;
  guess_reason?: string | null;
  guess_note?: string | null;
  space_evidence?: SpaceEvidence;
  output_space?: SpaceId;
  output_space_state?: string;
  technical?: boolean;
}

/** 构造存入 IndexedDB / 内存目录的记录（对齐 `lut/library.py` _record）。 */
export function buildLutRecord(params: {
  cubeId: string;
  filename: string;
  byteLength: number;
  parsed: ParsedCubeSummary;
  identified: IdentifyReport;
  header: LutHeader;
  savedAt: number;
}): LutRecord {
  const title = typeof params.parsed.title === 'string' ? params.parsed.title : '';
  const stem = stemOf(params.filename);
  const rawName = Array.from(title || stem || 'LUT').slice(0, 255).join('');
  let name: string;
  try {
    name = cleanLutLabel(rawName);
  } catch {
    name = cleanLutLabel(Array.from(stem || 'LUT').slice(0, 255).join('') || 'LUT');
  }
  const inputSpace = isValidSpace(params.identified.input_space) ? params.identified.input_space : null;
  const outputSpace = isValidSpace(params.identified.output_space) ? params.identified.output_space : 'srgb';
  const inputGuess = isValidSpace(params.identified.input_space_guess) ? params.identified.input_space_guess : null;
  const size1d = Number(params.parsed.size_1d ?? 0);
  const size3d = Number(params.parsed.size_3d ?? 0);
  const kind =
    typeof params.parsed.kind === 'string' && params.parsed.kind
      ? params.parsed.kind
      : [size1d > 0 ? '1D' : '', size3d > 0 ? '3D' : ''].filter(Boolean).join('+');
  return {
    cube_id: params.cubeId,
    name,
    filename: params.filename,
    title,
    kind,
    size_1d: size1d,
    size_3d: size3d,
    bytes: params.byteLength,
    version: 1,
    starred: false,
    source: params.header.source,
    author: params.header.author,
    license: params.header.license,
    comments: [...params.header.comments],
    input_space: inputSpace,
    output_space: outputSpace,
    input_gamut: null,
    output_gamut: null,
    input_range: 'full',
    output_range: 'full',
    input_space_state: params.identified.input_space_state ?? (inputSpace ? 'guessed' : 'unknown'),
    input_space_manual: Boolean(params.identified.input_space_manual),
    input_space_guess: inputGuess,
    guess_reason: params.identified.guess_reason ?? null,
    guess_note: params.identified.guess_note ?? (inputSpace ? null : LUT_ERRORS.lut_space_required),
    space_evidence: params.identified.space_evidence ?? {},
    output_space_state: params.identified.output_space_state ?? 'unknown',
    technical: Boolean(params.identified.technical),
    savedAt: params.savedAt,
  };
}

/** 交给 WASM `lut_table` / `export_prepare` 的色彩空间 JSON 规格；没有输入空间时返回 null。 */
export function lutTargetSpec(record: Pick<LutRecord, 'input_space' | 'output_space' | 'input_gamut' | 'output_gamut' | 'input_range' | 'output_range'>): LutTargetSpec | null {
  if (!record.input_space) return null;
  return {
    input_space: record.input_space,
    output_space: record.output_space ?? 'srgb',
    input_gamut: record.input_gamut ?? null,
    output_gamut: record.output_gamut ?? null,
    input_range: record.input_range ?? 'full',
    output_range: record.output_range ?? 'full',
  };
}

/**
 * 照片缓存与显卡 LUT 纹理共用的出图令牌（`lut/library.py` `_token` / `render_token`）：
 * 删除后得到 `<cube_id>:deleted` 的摘要，不会和重新导入复用的记录冲突。
 */
export function lutRenderToken(
  record: Pick<LutRecord, 'cube_id' | 'input_space' | 'output_space' | 'input_gamut' | 'output_gamut' | 'input_range' | 'output_range'>,
): string {
  const spec = {
    input_gamut: record.input_gamut ?? null,
    input_range: record.input_range ?? 'full',
    input_space: record.input_space ?? null,
    output_gamut: record.output_gamut ?? null,
    output_range: record.output_range ?? 'full',
    output_space: record.output_space ?? 'srgb',
  };
  const doc = JSON.stringify({ cube: record.cube_id, spec });
  return sha256Hex(new TextEncoder().encode(doc)).slice(0, 16);
}

export function deletedLutToken(cubeId: string): string {
  return sha256Hex(new TextEncoder().encode(`${cubeId}:deleted`)).slice(0, 16);
}

// ---- 轻量同步 SHA-256（令牌与小串用；大文件在 Worker 里走 Web Crypto） ---------------

const K = new Uint32Array([
  0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4, 0xab1c5ed5,
  0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3, 0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174,
  0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
  0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967,
  0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13, 0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85,
  0xa2bfe8a1, 0xa81a664b, 0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
  0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
  0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208, 0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2,
]);

export function sha256Hex(data: Uint8Array): string {
  const len = data.length;
  const blocks = Math.ceil((len + 9) / 64) * 64;
  const buf = new Uint8Array(blocks);
  buf.set(data);
  buf[len] = 0x80;
  const bitsHi = Math.floor(len / 0x20000000);
  const bitsLo = (len << 3) >>> 0;
  const view = new DataView(buf.buffer);
  view.setUint32(blocks - 8, bitsHi, false);
  view.setUint32(blocks - 4, bitsLo, false);

  let h0 = 0x6a09e667;
  let h1 = 0xbb67ae85;
  let h2 = 0x3c6ef372;
  let h3 = 0xa54ff53a;
  let h4 = 0x510e527f;
  let h5 = 0x9b05688c;
  let h6 = 0x1f83d9ab;
  let h7 = 0x5be0cd19;
  const w = new Uint32Array(64);
  const rotr = (x: number, n: number) => (x >>> n) | (x << (32 - n));

  for (let off = 0; off < blocks; off += 64) {
    for (let i = 0; i < 16; i++) w[i] = view.getUint32(off + i * 4, false);
    for (let i = 16; i < 64; i++) {
      const s0 = rotr(w[i - 15], 7) ^ rotr(w[i - 15], 18) ^ (w[i - 15] >>> 3);
      const s1 = rotr(w[i - 2], 17) ^ rotr(w[i - 2], 19) ^ (w[i - 2] >>> 10);
      w[i] = (w[i - 16] + s0 + w[i - 7] + s1) >>> 0;
    }
    let a = h0;
    let b = h1;
    let c = h2;
    let d = h3;
    let e = h4;
    let f = h5;
    let g = h6;
    let h = h7;
    for (let i = 0; i < 64; i++) {
      const S1 = rotr(e, 6) ^ rotr(e, 11) ^ rotr(e, 25);
      const ch = (e & f) ^ (~e & g);
      const temp1 = (h + S1 + ch + K[i] + w[i]) >>> 0;
      const S0 = rotr(a, 2) ^ rotr(a, 13) ^ rotr(a, 22);
      const maj = (a & b) ^ (a & c) ^ (b & c);
      const temp2 = (S0 + maj) >>> 0;
      h = g;
      g = f;
      f = e;
      e = (d + temp1) >>> 0;
      d = c;
      c = b;
      b = a;
      a = (temp1 + temp2) >>> 0;
    }
    h0 = (h0 + a) >>> 0;
    h1 = (h1 + b) >>> 0;
    h2 = (h2 + c) >>> 0;
    h3 = (h3 + d) >>> 0;
    h4 = (h4 + e) >>> 0;
    h5 = (h5 + f) >>> 0;
    h6 = (h6 + g) >>> 0;
    h7 = (h7 + h) >>> 0;
  }
  return [h0, h1, h2, h3, h4, h5, h6, h7].map((v) => v.toString(16).padStart(8, '0')).join('');
}
