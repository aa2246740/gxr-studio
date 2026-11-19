// 配方列表和旋钮：数据来自核心的 `color_listing`（桌面 CameraData.listing 的移植），这里只排成卡片、
// 起名字、写说明，照桌面 `recipes.js`、`look_model.js`、`preview.js`。选中的配方用桌面 RecipeSel 的 JSON 交给
// `color_resolve`，旋钮在那一步套上。纯函数，不碰界面和 WASM。

import type { Unit } from './calib';

export const UNIT_ORDER: Unit[] = ['jlaunch', 'qlaunch', 'l08firm'];

/** `preview.js` 的 BASE_NAMES：八个位置的名字。 */
export const BASE_NAMES = ['GR 正片', 'GR 负片', 'GR 高对比黑白', '原厂标准', '空位 5', '空位 6', '空位 7', '空位 8'];

/** `preview.js` 的 KNOB_NAMES。 */
export const KNOB_NAMES: Record<string, string> = {
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

/** `look_model.js` 的说明表。 */
export const BUILTIN_TEXT: Record<number, string> = { 1: '饱和度高', 2: '饱和度略低', 3: '黑白，对比高', 4: '原厂标准色彩' };
export const LOOK_TEXT: Record<string, string> = {
  'M9 STD': '饱和度高',
  'HNCS HiCon': '饱和度高',
  'Tri-X 400': '饱和度低',
  Acros: '黑白',
  'Portra 400': '饱和度略低',
  'Velvia 50': '饱和度高',
  'Vision3 500T': '饱和度高，偏暗',
  'Eterna BB': '饱和度低，偏暗',
  'ARRI 709': '对比低，偏暗',
  'RED IPP2': '对比低，偏暗',
  'Agfa LeBox': '饱和度低，偏亮',
  Metropolis: '饱和度低，对比低',
  'Y2K Plady': '饱和度低，偏亮',
  'QianXi A': '偏暖，饱和度低',
  'QianXi B': '偏暖，饱和度低',
};
const CAMERA_USER_TEXT = '相机里的配方';

/** 第 135 代起旋钮是新版（±9）；更早的相机只认 ±5（`camdata.js` 的 NEW_KNOBS_FROM、`recipes.js` 的 levelsFor）。 */
const NEW_KNOBS_FROM = 135;
const NEW_TONE_KNOBS = ['contrast', 'highlight', 'shadow', 'saturation'];
const LEGACY_LIMIT = 5;

/** `color_listing` 里一个旋钮。 */
export interface Control {
  name: string;
  label: string;
  min: number;
  max: number;
  default: number;
  cap_lower: boolean;
  cap_upper: boolean;
  amount_step: number | null;
  levels: number[];
}

interface ListedRecipe {
  kind?: 'builtin' | 'record';
  id?: string;
  name?: string;
  slot: number;
  number?: string;
  controls: Control[];
}

interface ListedExtra {
  kind: 'look' | 'camera_user';
  id: string;
  name: string;
  slot: number;
  controls: Control[];
}

export interface ListingUnit {
  id: string;
  recipes: ListedRecipe[];
  looks?: ListedExtra[];
  users?: ListedExtra[];
}

export interface Listing {
  units: ListingUnit[];
}

export type CardKind = 'builtin' | 'record' | 'look' | 'camera_user';

export interface Card {
  /** `builtin:3`、`record:<id>`、`look:<id>`、`camera_user:<id>`：这一次会话里同一张卡的身份。 */
  id: string;
  kind: CardKind;
  name: string;
  desc: string | null;
  /** 底子的位置（1–8）。 */
  slot: number;
  /** 文件条目的身份（Looks、用户配方、r5 记录），内置配方没有。 */
  recipeId: string | null;
  /** 这个配方的旋钮（默认档位已经是相机里这个配方自己的）。 */
  controls: Control[];
  /** 编号（「相机里 · 07」）。 */
  number: string;
}

export interface Shelves {
  builtin: Card[];
  looks: Card[];
  users: Card[];
}

export function unitIndex(unit: Unit): number {
  return UNIT_ORDER.indexOf(unit);
}

/** 没有相机配方文件时只能列出八个位置的名字（桌面 Menu.listing 也不需要理光字节）。 */
export function namesOnly(): Card[] {
  return BASE_NAMES.map((name, i) => ({
    id: `builtin:${i + 1}`,
    kind: 'builtin' as const,
    name,
    desc: BUILTIN_TEXT[i + 1] ?? null,
    slot: i + 1,
    recipeId: null,
    controls: [],
    number: String(i + 1).padStart(2, '0'),
  }));
}

/** 这张照片所在单元的配方：内置（r5 时含文件记录，桌面顺序）、文件里的 Looks、相机里的用户配方。 */
export function shelves(listing: Listing, unit: Unit): Shelves {
  const row = listing.units.find((u) => u.id === unit) ?? listing.units[unitIndex(unit)];
  if (!row) return { builtin: [], looks: [], users: [] };
  const builtin: Card[] = row.recipes.map((r) => {
    const record = r.kind === 'record';
    return {
      id: record ? `record:${r.id ?? ''}` : `builtin:${r.slot}`,
      kind: record ? ('record' as const) : ('builtin' as const),
      name: record ? r.name ?? '' : BASE_NAMES[r.slot - 1] ?? `空位 ${r.slot}`,
      desc: record ? null : BUILTIN_TEXT[r.slot] ?? null,
      slot: r.slot,
      recipeId: record ? r.id ?? null : null,
      controls: r.controls,
      number: r.number ?? String(r.slot).padStart(2, '0'),
    };
  });
  const extra = (items: ListedExtra[] | undefined): Card[] =>
    (items ?? []).map((x) => ({
      id: `${x.kind}:${x.id}`,
      kind: x.kind,
      name: x.name,
      desc: x.kind === 'look' ? LOOK_TEXT[x.name] ?? null : CAMERA_USER_TEXT,
      slot: x.slot,
      recipeId: x.id,
      controls: x.controls,
      number: String(x.slot).padStart(2, '0'),
    }));
  return { builtin, looks: extra(row.looks), users: extra(row.users) };
}

/** 面板标题旁边的小标签：「相机里 · 07」。 */
export function cardTag(card: Card): string {
  if (card.kind === 'builtin' || card.kind === 'record') return `相机里 · ${card.number}`;
  if (card.kind === 'look') return 'Looks';
  return '相机里';
}

export type Shelf = 'all' | 'builtin' | 'looks' | 'mine';

export function shelf(list: Shelves, which: Shelf): Card[] {
  if (which === 'builtin') return list.builtin;
  if (which === 'looks') return list.looks;
  if (which === 'mine') return list.users;
  return [...list.builtin, ...list.looks, ...list.users];
}

/** 每个旋钮的默认档位。 */
export function defaults(card: Card): Record<string, number> {
  const out: Record<string, number> = {};
  for (const c of card.controls) out[c.name] = c.default;
  return out;
}

/** `preview.js` 的 effective：给的档位不在可选里就用默认。 */
export function effective(control: Control, levels: Record<string, number> | undefined): number {
  const v = levels?.[control.name];
  return v !== undefined && control.levels.includes(v) ? v : control.default;
}

/** `recipes.js` 的 levelsFor：旧版相机这四项只认 ±5。 */
export function choices(control: Control, generation: number | null): number[] {
  const levels = [...control.levels].sort((a, b) => a - b);
  const legacy = !(typeof generation === 'number' && generation >= NEW_KNOBS_FROM);
  if (legacy && NEW_TONE_KNOBS.includes(control.name)) return levels.filter((v) => Math.abs(v) <= LEGACY_LIMIT);
  return levels;
}

/** `recipes.js` 的 nearestIndex。 */
export function nearestIndex(levels: number[], v: number): number {
  let best = 0;
  levels.forEach((x, i) => {
    if (Math.abs(x - v) < Math.abs(levels[best] - v)) best = i;
  });
  return best;
}

/** 滑块右边的数字：照设计稿（强度 100、对比度 +1、褪色 0）。 */
export function levelText(control: Control, v: number): string {
  if (control.name === 'amount') return String(v);
  return v > 0 ? `+${v}` : String(v);
}

/** 交给 `color_resolve` 的选择：桌面 RecipeSel 的 JSON。档位只带这个配方真有的旋钮，值都在可选里。 */
export function selection(card: Card, unit: Unit, levels: Record<string, number> | undefined): Record<string, unknown> {
  const clean: Record<string, number> = {};
  for (const c of card.controls) clean[c.name] = effective(c, levels);
  const out: Record<string, unknown> = { kind: card.kind, unit, slot: card.slot, levels: clean };
  if (card.recipeId) out.id = card.recipeId;
  return out;
}

/** 选择的身份（缓存键）。键的顺序固定，同一个选择总是同一个字符串。 */
export function selectionKey(sel: Record<string, unknown>): string {
  const levels = sel.levels as Record<string, number>;
  const sorted = Object.keys(levels)
    .sort()
    .map((k) => [k, levels[k]]);
  return JSON.stringify([sel.kind, sel.unit, sel.slot, sel.id ?? null, sorted]);
}

/** 这次选中的是哪张卡，以及它的档位。存在照片的编辑状态里，跟调整放在一起。 */
export interface Choice {
  id: string;
  unit: Unit;
}

/** 没选配方、也没有拍摄标签时桌面的默认选择：原厂标准（slot 4）。 */
export const DEFAULT_CARD = 'builtin:4';
/** `resolveRecipe` 走 `camdata_camera_recipe` 时的前缀（不改 `core/api.ts`）。 */
export const CAMERA_RECIPE_PREFIX = '@camera-recipe:';
/** 桌面 `recipes.js:168`：拍摄标签未记录微调时的说明。 */
export const CAMERA_RECIPE_LEVELS_UNKNOWN = '微调未知，电脑预测使用配方默认值';

export interface CameraRecipeSelection {
  kind: CardKind;
  unit: Unit;
  slot: number;
  id?: string;
  levels: Record<string, number>;
}

export interface CameraRecipeError {
  code: string;
  message: string;
}

export type CameraRecipeResult =
  | { status: 'none' }
  | { status: 'ok'; recipe: CameraRecipeSelection; levelsUnknown: boolean }
  | { status: 'error'; error: CameraRecipeError };

export interface CameraRecipeCache {
  datKey: string;
  copyright: string | null;
  version: number;
  result: CameraRecipeResult;
}

export interface AdoptedSelection {
  choice: Choice;
  levels: Record<string, number>;
  levelsUnknown: boolean;
}

interface AdoptablePhoto {
  dng: File | null;
  diptych?: boolean;
  choice: Choice | null;
  levels: Map<string, Record<string, number>>;
  cameraRecipe?: CameraRecipeCache | null;
}

const datKeys = new WeakMap<object, string>();
let nextDatKey = 1;

/** 给当前加载的 `Book.dat` 实例一个稳定的内存缓存键；未加载时返回 null。 */
export function datKeyOf(dat: object | null | undefined): string | null {
  if (!dat) return null;
  let key = datKeys.get(dat);
  if (!key) {
    key = `dat:${nextDatKey++}`;
    datKeys.set(dat, key);
  }
  return key;
}

export function cameraRecipeCardId(recipe: { kind: string; slot: number; id?: string }): string {
  return recipe.kind === 'builtin' ? `builtin:${recipe.slot}` : `${recipe.kind}:${recipe.id ?? ''}`;
}

export function findAdoptedCard(listing: Listing, recipe: { kind: string; unit: Unit; slot: number; id?: string }): Card | null {
  const id = cameraRecipeCardId(recipe);
  return shelf(shelves(listing, recipe.unit), 'all').find((card) => card.id === id) ?? null;
}

export function encodeCameraRecipePayload(json: string): string {
  const bytes = new TextEncoder().encode(json);
  let hex = '';
  for (let i = 0; i < bytes.length; i += 1) hex += bytes[i].toString(16).padStart(2, '0');
  return hex;
}

export function decodeCameraRecipePayload(raw: Uint8Array | string): CameraRecipeResult {
  const bytes =
    typeof raw === 'string'
      ? (() => {
          const out = new Uint8Array(raw.length >> 1);
          for (let i = 0; i < out.length; i += 1) out[i] = Number.parseInt(raw.slice(i * 2, i * 2 + 2), 16);
          return out;
        })()
      : raw;
  const parsed = JSON.parse(new TextDecoder().decode(bytes)) as {
    recipe?: CameraRecipeSelection | null;
    levels_unknown?: boolean;
    error?: CameraRecipeError;
  };
  if (parsed.error && typeof parsed.error.message === 'string') {
    return { status: 'error', error: { code: parsed.error.code || 'camera_recipe_error', message: parsed.error.message } };
  }
  if (!parsed.recipe) return { status: 'none' };
  return { status: 'ok', recipe: parsed.recipe, levelsUnknown: Boolean(parsed.levels_unknown) };
}

/** 未显式选配方的 RAW 照片若匹配当前相机配方文件，返回采用的拍摄配方；显式选择、无 DNG、半格双联均返回 null。 */
export function adoptedSelectionOf(photo: Pick<AdoptablePhoto, 'dng' | 'diptych' | 'choice' | 'cameraRecipe'>, dat: object | null | undefined): AdoptedSelection | null {
  if (!photo.dng || photo.diptych || photo.choice !== null) return null;
  const key = datKeyOf(dat);
  if (!key) return null;
  const cache = photo.cameraRecipe;
  if (!cache || cache.datKey !== key || cache.result.status !== 'ok') return null;
  const r = cache.result.recipe;
  return {
    choice: { id: cameraRecipeCardId(r), unit: r.unit },
    levels: { ...r.levels },
    levelsUnknown: cache.result.levelsUnknown,
  };
}

/** 未显式选配方的 RAW 照片在当前相机配方文件下解析拍摄标签出错时，返回桌面同款错误码与文案。 */
export function cameraRecipeErrorOf(photo: Pick<AdoptablePhoto, 'dng' | 'diptych' | 'choice' | 'cameraRecipe'>, dat: object | null | undefined): CameraRecipeError | null {
  if (!photo.dng || photo.diptych || photo.choice !== null) return null;
  const key = datKeyOf(dat);
  if (!key) return null;
  const cache = photo.cameraRecipe;
  if (!cache || cache.datKey !== key || cache.result.status !== 'error') return null;
  return cache.result.error;
}

/**
 * 给导出与复制调整的只读视图：没有显式 `choice` 时把已采用的拍摄配方叠上去，不改原照片对象。
 * 原照片的 `choice` 仍保持 null，网格的铅笔角标因此不会亮起。
 */
export function photoWithAdopted<T extends AdoptablePhoto>(photo: T, dat: object | null | undefined): T {
  const adopted = adoptedSelectionOf(photo, dat);
  if (!adopted) return photo;
  return {
    ...photo,
    choice: { ...adopted.choice },
    levels: new Map([[adopted.choice.id, { ...adopted.levels }]]),
  };
}
