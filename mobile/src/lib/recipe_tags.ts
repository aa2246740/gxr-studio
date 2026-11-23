// EXIF Copyright GXR1 拍摄配方标签解析、封存目录定位与展示格式化。
// 规则对齐桌面 `src/gxr_studio/camdata/recipe_tags.py`、`catalog_names.py`、`datcodec.py` 与 `web/assets/info_panel.js`。

const GXR1_TAG_RE = /^GXR1 ([0-9a-fA-F]{10}) (28|50|MT) (0[1-9]|[1-9][0-9])(?: ([0-9A-I-]{10}))?$/;
const MAX_TAG_BYTES = 47;
const COPYRIGHT_FIELD_CHARS = 69;
const TAG_ALPHABET = '0123456789ABCDEFGHI';
const TAG_KNOB_ORDER = ['amount', 'contrast', 'highlight', 'shadow', 'fade', 'saturation', 'warmth', 'tint', 'split', 'filter'] as const;
const TAG_KNOB_LABELS: Record<string, string> = {
  amount: '强度',
  contrast: '对比度',
  highlight: '高光',
  shadow: '阴影',
  fade: '褪色',
  saturation: '饱和度',
  warmth: '色温',
  tint: '色调',
  split: '分离色调',
  filter: '黑白滤镜',
};
const TAG_UNIT_LABELS: Record<string, string> = { '28': '28mm', '50': '50mm', MT: 'MOUNT' };
const TAG_UNIT_BITS: Record<string, number> = { '50': 1, '28': 2, MT: 4 };
const TAG_BASE_NAMES = ['GRPos', 'GRNeg', 'HC BW', 'Std', 'K64', 'P160', 'K25', 'Gold'];

/** 封存目录 ID → 库代数，与桌面 `src/gxr_studio/camdata/catalog_names.py:CATALOG_GENERATIONS` 保持一致。 */
export const TAG_CATALOG_GENERATIONS: Readonly<Record<string, number>> = {
  d25d2f0a68: 176,
  '6a3a5a3c02': 178,
  '6adf847602': 178,
  '0ccc86c19b': 178,
};

/** DAT 代数 → 库代数，与桌面 `src/gxr_studio/camdata/datcodec.py:PROFILES[gen].library_generation` 保持一致。 */
export const DAT_LIBRARY_GENERATIONS: Readonly<Record<number, number | null>> = {
  127: null, 129: null, 131: null, 135: null, 137: null, 138: null, 150: null,
  152: null, 153: null, 154: null, 155: null, 156: null, 158: null,
  176: 176,
  178: 178, 181: 178, 182: 178, 185: 178,
  250: 178, 253: 178, 256: 178, 257: 178, 258: 178, 259: 178, 260: 178, 262: 178, 266: 178,
};

const G176_DESCRIPTORS: ReadonlyArray<readonly [string, number]> = [
  ['Acros', 1], ['Acros', 2], ['Acros', 4], ['ARRI 709', 7], ['M9 STD', 1], ['M9 STD', 2], ['M9 STD', 4],
  ['instax mini', 7], ['HNCS HiCon', 1], ['HNCS HiCon', 2], ['HNCS HiCon', 4], ['HNCS LowSat', 3], ['HNCS LowSat', 4],
  ['Tri-X 400', 7], ['Ektachrm 100', 1], ['Ektachrm 100', 2], ['Ektachrm 100', 4], ['Provia 100F', 3], ['Provia 100F', 4],
  ['Portra 800', 3], ['Portra 800', 4], ['Vision3 500T', 1], ['Vision3 500T', 4], ['F-CC', 5], ['F-CC', 2],
  ['F-CN', 3], ['F-CN', 4], ['Leica F1', 7], ['Teal Orange', 7],
];
const G178_RECORDS = [
  'F-CC', 'F-CN', 'Provia100F', 'M9 STD', 'L-Classic', 'L-Contemp', 'Leica F1', 'K64', 'K25',
  'Portra160', 'Gold200', 'Ektachrm100', 'HNCS HiCon', 'HNCS LowSat', 'Teal Orange',
  'Y2K Plady', 'Agfa LeBox', 'Acros', 'Tri-X 400',
];
const KUBIK_RECORDS = ['K-0815', 'K-PACIFIC', 'K-KINO'];
const G178_DESCRIPTORS: ReadonlyArray<readonly [string, number]> = G178_RECORDS.flatMap((name) => [
  [name, 1] as const,
  [name, 2] as const,
  [name, 4] as const,
]);
const KUBIK_DESCRIPTORS: ReadonlyArray<readonly [string, number]> = [
  ...G178_DESCRIPTORS,
  ...KUBIK_RECORDS.flatMap((name) => [
    [name, 1] as const,
    [name, 2] as const,
    [name, 4] as const,
  ]),
];

/** 封存目录描述符表，与桌面 `src/gxr_studio/camdata/catalog_names.py:CATALOG_DESCRIPTORS` 保持一致。 */
export const CATALOG_DESCRIPTORS: Readonly<Record<string, ReadonlyArray<readonly [string, number]>>> = {
  d25d2f0a68: G176_DESCRIPTORS,
  '6a3a5a3c02': G178_DESCRIPTORS,
  '6adf847602': G178_DESCRIPTORS,
  '0ccc86c19b': KUBIK_DESCRIPTORS,
};

export interface CatalogTarget {
  kind: 'builtin' | 'record';
  slot: number | null;
  effect: number | null;
  name: string;
}

/** 照桌面 `catalog_names.py:resolve_descriptors`：`pos = effect ? effect + 4 : slot`。 */
export function resolveDescriptors(
  descriptors: ReadonlyArray<readonly [string, number]>,
  unit: string,
  position: number,
): CatalogTarget | null {
  const unitBit = TAG_UNIT_BITS[unit];
  if (!unitBit || position < 1) return null;
  if (position <= 4) return { kind: 'builtin', slot: position, effect: null, name: TAG_BASE_NAMES[position - 1] };
  const effect = position - 4;
  if (effect >= 1 && effect <= descriptors.length) {
    const [name, mask] = descriptors[effect - 1];
    if (mask & unitBit) return { kind: 'record', slot: null, effect, name };
  }
  if (position <= 8) return { kind: 'builtin', slot: position, effect: null, name: TAG_BASE_NAMES[position - 1] };
  return null;
}

/** 照桌面 `catalog_names.py:resolve_catalog_position`，按封存目录、单元与位置码解析目标类型与名称。 */
export function resolveCatalogPosition(identity: string, unit: string, position: number): CatalogTarget | null {
  const descriptors = CATALOG_DESCRIPTORS[identity];
  if (!descriptors) return null;
  return resolveDescriptors(descriptors, unit, position);
}

export interface ParsedRecipeTag {
  catalogIdentity: string;
  unit: string;
  unitLabel: string;
  position: number;
  name: string | null;
  knobCode: string | null;
  levels: Record<string, number | null> | null;
}

/** 解析 EXIF Copyright 的 GXR1 标签（不依赖 DAT）。 */
export function parseRecipeTag(value: string | null | undefined): ParsedRecipeTag | null {
  if (typeof value !== 'string' || value.length > COPYRIGHT_FIELD_CHARS + 1) return null;
  for (let i = 0; i < value.length; i += 1) {
    if (value.charCodeAt(i) > 127) return null;
  }
  let text = value.endsWith('\0') ? value.slice(0, -1) : value;
  if (text.length === COPYRIGHT_FIELD_CHARS) text = text.replace(/ +$/, '');
  if (text.length >= MAX_TAG_BYTES) return null;
  const m = GXR1_TAG_RE.exec(text);
  if (!m) return null;
  const catalogIdentity = m[1].toLowerCase();
  const unit = m[2];
  const position = Number.parseInt(m[3], 10);
  const knobCode = m[4] ?? null;
  let levels: Record<string, number | null> | null = null;
  if (knobCode !== null) {
    levels = {};
    for (let i = 0; i < TAG_KNOB_ORDER.length; i += 1) {
      const name = TAG_KNOB_ORDER[i];
      const ch = knobCode[i];
      const idx = ch === '-' ? -1 : TAG_ALPHABET.indexOf(ch);
      levels[name] = idx < 0 || name === 'amount' ? null : idx - 9;
    }
  }
  return {
    catalogIdentity,
    unit,
    unitLabel: TAG_UNIT_LABELS[unit] ?? unit,
    position,
    name: resolveCatalogPosition(catalogIdentity, unit, position)?.name ?? null,
    knobCode,
    levels,
  };
}

/** 照桌面 `web/assets/info_panel.js` 的 shootingRecipeRows 格式化单格拍摄配方说明。 */
export function formatShootingRecipeTag(copyright: string | null | undefined, amountLevels?: readonly number[] | null): string {
  const tag = parseRecipeTag(copyright);
  if (!tag) return '未标记';
  const pos = String(tag.position).padStart(2, '0');
  const base = tag.name ? `${tag.name}（${tag.unitLabel} · ${pos}）` : `GXR1 ${tag.catalogIdentity} ${tag.unit} ${pos}`;
  if (tag.levels === null) return `${base} · 微调未知`;
  const code = tag.knobCode?.[0] ?? '-';
  const idx = code === '-' ? -1 : TAG_ALPHABET.indexOf(code);
  const hits = idx >= 0 && amountLevels ? amountLevels.filter((v) => Math.floor(v / 10) === idx) : [];
  const amt = hits.length === 1 ? hits[0] : null;
  const changes: string[] = amt !== null && amt !== 100 ? [`${TAG_KNOB_LABELS.amount} ${amt}%`] : [];
  for (const key of TAG_KNOB_ORDER) {
    const val = tag.levels[key];
    if (val !== null && val !== undefined && val !== 0) changes.push(`${TAG_KNOB_LABELS[key] ?? key} ${val > 0 ? `+${val}` : String(val)}`);
  }
  if (code !== '-' && amt === null) changes.push('强度未知');
  return changes.length ? `${base} · ${changes.join(' · ')}` : base;
}
