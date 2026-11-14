// 配方列表：照桌面 `render/recipes.py`（Menu / CameraData.listing、request_for）和 `recipes.js` 的顺序与名字。
// 纯函数，不碰界面和 WASM。输入是 `camdata_parse` 的摘要和 `camdata_menu` 的菜单（都已 JSON.parse）。

import type { Unit } from './calib';

export const UNIT_ORDER: Unit[] = ['jlaunch', 'qlaunch', 'l08firm'];

/** `preview.js` 的 BASE_NAMES：八个位置的名字。 */
export const BASE_NAMES = ['GR 正片', 'GR 负片', 'GR 高对比黑白', '原厂标准', '空位 5', '空位 6', '空位 7', '空位 8'];

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
/** 不在菜单里的旋钮，只有取这个「中性值」才算没动（recipes.py 的 NEUTRAL）。 */
const NEUTRAL: Record<string, number> = { amount: 100 };
const USER_KNOBS = ['contrast', 'highlight', 'shadow', 'saturation'];

export interface MenuItem {
  control: number;
  min_level: number;
  max_level: number;
  default_level: number;
  points: Array<{ level: number }>;
}

export interface Menu {
  controls: Array<{ name: string; label: string }>;
  recipes: Array<{ unit: number; slot: number; first_item: number; item_count: number }>;
  items: MenuItem[];
}

export interface DatSummary {
  generation: number;
  units: Array<{ unit: string; unit_index: number; entries: Array<{ slot: number; sha256: string }> }>;
  looks: Array<{ name: string; base: number; unit_mask: number; sha256: string }>;
  recipes: Array<{ name: string; base: number; unit_mask: number; sha256: string }>;
  users: Array<{ name: string; base: number; levels: number[] }>;
}

export type CardKind = 'builtin' | 'look' | 'camera_user';

export interface Card {
  /** `builtin:3`、`look:0`、`camera_user:1`：这一次会话里同一张卡的身份。 */
  id: string;
  kind: CardKind;
  name: string;
  desc: string | null;
  /** 底子的位置（1–8）。旋钮菜单按它取。 */
  slot: number;
  /** 底子条目的 sha256（桌面 entry_sha256），烘焙表按它和模型缓存。 */
  entrySha256: string;
  /** 菜单里每一项的档位（没给的取默认）。null：这张卡在这个单元上用不了（error 写着原因）。 */
  levels: number[] | null;
  error: string | null;
  /** 文件里的条目字节手机上拿不到（Looks、r5 记录），所以还画不了。 */
  pending: boolean;
}

export interface Listing {
  builtin: Card[];
  looks: Card[];
  users: Card[];
}

export function unitIndex(unit: Unit): number {
  return UNIT_ORDER.indexOf(unit);
}

function items(menu: Menu, unit: number, slot: number): MenuItem[] | null {
  const recipe = menu.recipes.find((r) => r.unit === unit && r.slot === slot);
  if (!recipe) return null;
  return menu.items.slice(recipe.first_item, recipe.first_item + recipe.item_count);
}

/** 菜单旋钮名（按菜单顺序）。 */
export function controlNames(menu: Menu, unit: number, slot: number): string[] {
  return (items(menu, unit, slot) ?? []).map((item) => menu.controls[item.control]?.name ?? '');
}

/**
 * 照 `Menu.request_for`：给的档位 → 菜单顺序的档位列表。菜单里没有的旋钮只能取中性值，否则用不了。
 * 档位是否在点表里由核心检查（knobs.apply 前的 menu.request）。
 */
export function levelsFor(menu: Menu, unit: number, slot: number, given: Record<string, number>): { levels: number[] } | { error: string } {
  const list = items(menu, unit, slot);
  if (!list) return { error: '找不到这个配方。请重新选择。' };
  const names = list.map((item) => menu.controls[item.control]?.name ?? '');
  for (const [name, value] of Object.entries(given)) {
    if (!names.includes(name) && value !== (NEUTRAL[name] ?? 0)) return { error: '无法保存配方。有一个旋钮无法使用。' };
  }
  return { levels: list.map((item, i) => (names[i] in given ? given[names[i]] : item.default_level)) };
}

function entrySha(dat: DatSummary, unit: number, slot: number): string {
  return dat.units[unit]?.entries.find((e) => e.slot === slot)?.sha256 ?? '';
}

/** 没有相机配方文件时只能列出八个位置的名字（桌面 Menu.listing 也不需要理光字节）。 */
export function namesOnly(): Card[] {
  return BASE_NAMES.map((name, i) => ({
    id: `builtin:${i + 1}`,
    kind: 'builtin' as const,
    name,
    desc: BUILTIN_TEXT[i + 1] ?? null,
    slot: i + 1,
    entrySha256: '',
    levels: null,
    error: null,
    pending: false,
  }));
}

/** 这张照片所在单元的配方：内置 8 个（桌面顺序）、文件里的 Looks、相机里的用户配方。 */
export function listing(dat: DatSummary, menu: Menu, unit: Unit): Listing {
  const u = unitIndex(unit);
  const builtin: Card[] = [];
  for (let slot = 1; slot <= 8; slot++) {
    const made = levelsFor(menu, u, slot, {});
    builtin.push({
      id: `builtin:${slot}`,
      kind: 'builtin',
      name: BASE_NAMES[slot - 1],
      desc: BUILTIN_TEXT[slot] ?? null,
      slot,
      entrySha256: entrySha(dat, u, slot),
      levels: 'levels' in made ? made.levels : null,
      error: 'error' in made ? made.error : null,
      pending: false,
    });
  }
  const looks: Card[] = [];
  dat.looks.forEach((look, index) => {
    if (((look.unit_mask >> u) & 1) === 0) return;
    looks.push({
      id: `look:${index}`,
      kind: 'look',
      name: look.name,
      desc: LOOK_TEXT[look.name] ?? null,
      slot: look.base,
      entrySha256: look.sha256,
      levels: null,
      error: null,
      pending: true,
    });
  });
  const users: Card[] = dat.users.map((user, index) => {
    const given: Record<string, number> = {};
    USER_KNOBS.forEach((name, i) => {
      if (i < user.levels.length) given[name] = user.levels[i];
    });
    const made = levelsFor(menu, u, user.base, given);
    return {
      id: `camera_user:${index}`,
      kind: 'camera_user' as const,
      name: user.name,
      desc: CAMERA_USER_TEXT,
      slot: user.base,
      entrySha256: entrySha(dat, u, user.base),
      levels: 'levels' in made ? made.levels : null,
      error: 'error' in made ? made.error : null,
      pending: false,
    };
  });
  return { builtin, looks, users };
}

/** 面板标题旁边的小标签：「相机里 · 07」。 */
export function cardTag(card: Card): string {
  if (card.kind === 'builtin') return `相机里 · ${String(card.slot).padStart(2, '0')}`;
  if (card.kind === 'look') return 'Looks';
  return '相机里';
}

export type Shelf = 'all' | 'builtin' | 'looks' | 'mine';

export function shelf(list: Listing, which: Shelf): Card[] {
  if (which === 'builtin') return list.builtin;
  if (which === 'looks') return list.looks;
  if (which === 'mine') return list.users;
  return [...list.builtin, ...list.looks, ...list.users];
}

/** 一张卡的烘焙参数（color_bake 的 params_json）：每次都从底子重算旋钮，和桌面 CameraData.resolve 一样。 */
export function bakeParams(card: Card, unit: Unit): string | null {
  if (!card.levels) return null;
  return JSON.stringify({ step: 0.375, knobs: { levels: card.levels, unit: unitIndex(unit), slot: card.slot } });
}

/** 这次选中的是哪张卡。存在照片的编辑状态里，跟调整放在一起。 */
export interface Choice {
  id: string;
  unit: Unit;
}
