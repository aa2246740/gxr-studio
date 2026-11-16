// 一张照片交给 export_plan / export_prepare 的请求：从这次会话的编辑状态拼出来。纯函数，不碰 DOM。
// shot 照桌面图库的 shot 字典（宽高在 Worker 里从文件头补上）；side 照桌面 render/edits.write 写出的 sidecar；
// defaults 是没有默认设置文件的桌面（{}）；底图「自动」。M3 不能设边框、日期戳、裁剪、LUT、曲线（M4 才有）。

import { unitOfLens, type Unit } from './calib';
import type { Card } from './recipes';

/** 桌面 export.SIZE_TIERS：长边像素，原始尺寸是 undefined。 */
export const SIZE_TIERS = { original: undefined, large: 3000, medium: 2048, small: 1600 } as const;
/** 桌面 export.QUALITY_TIERS。 */
export const QUALITY_TIERS = { high: 95, medium: 85, low: 70 } as const;
export type SizeTier = keyof typeof SIZE_TIERS;
export type QualityTier = keyof typeof QUALITY_TIERS;

/** 和 app/model.ts 的 Knobs 同一个形状（这里不 import app 层）。 */
export interface KnobValues {
  exposure_ev: number;
  contrast: number;
  highlight: number;
  shadow: number;
  black_point: number;
  warm: number;
  tint: number;
  saturation: number;
  vibrance: number;
  vignette: number;
}

/** 拼请求要的那部分照片状态。 */
export interface RequestPhoto {
  stem: string;
  dngName: string | null;
  jpgName: string | null;
  /** 镜头名和 ISO：先用相机 JPG 的，没有再用 DNG 的（M2 的 mergeMeta）。 */
  lens: string | null;
  iso: number | null;
  choice: { id: string; unit: Unit } | null;
  /** 选中那个配方的档位（只有用户动过的），键是卡的 id（`builtin:3`、`look:<id>`…）。 */
  levels: ReadonlyMap<string, Record<string, number>>;
  /** 桌面的 memory（`look_cards.js` recipeTarget：换配方时切走的那个），键是卡的 id；空的档位也是一项。 */
  memory: ReadonlyMap<string, Record<string, number>>;
  knobs: KnobValues;
  /** 源文件（有相机 JPG 用 JPG，否则 DNG，即桌面 serve._source_path）的 File.lastModified。 */
  sourceModified: number;
}

export interface ExportOptions {
  size: SizeTier;
  quality: QualityTier;
}

export interface ExportRequest {
  /** 不含 width / height：Worker 读文件头后补上（桌面 photo_meta 的宽高，不用解码后的位图）。 */
  shot: { name: string; files: Record<string, { relpath: string }>; lens: string | null; iso: number | null };
  side: Record<string, unknown>;
  defaults: Record<string, never>;
  base: 'auto';
  layout: Record<string, never>;
  binding: string;
  quality: number;
  longEdge: number | undefined;
  hdr: boolean;
  mtimeMs: number;
  tzOffsetMin: number;
  /**
   * 要交给 color_resolve 的配方选择（RecipeSel 的 JSON），没有就是 null。export-source 的「自动」：选了配方用它；
   * 没选、又有相机 JPG 时走相机 JPG，不要条目；只有 RAW 时用这个单元的原厂 04（source/plan.rs 的 selection）。
   */
  resolve: string | null;
  /** 这一张要不要解码相机 JPG（没选配方、又有相机 JPG：原样导出或编辑过的相机 JPG）。 */
  needsJpg: boolean;
}

/** 桌面 preview.js 的 recipeKey：内置是 `<单元>/<位置>`，其余 `<单元>/<种类>/<id>`。 */
export function recipeKey(unit: Unit, card: Pick<Card, 'kind' | 'slot' | 'recipeId'>): string {
  return card.kind !== 'builtin' ? `${unit}/${card.kind}/${card.recipeId ?? ''}` : `${unit}/${card.slot}`;
}

/** 桌面 preview.js 的 cleanLevels：只留这个配方真有的旋钮，值要在可选档位里。 */
export function cleanLevels(card: Pick<Card, 'controls'>, levels: Record<string, number> | undefined): Record<string, number> {
  const out: Record<string, number> = {};
  for (const c of card.controls) {
    const v = levels?.[c.name];
    if (v !== undefined && c.levels.includes(v)) out[c.name] = v;
  }
  return out;
}

/** 键按字母排（Python parse_recipe 按名字排序后写回）。 */
function sorted(levels: Record<string, number>): Record<string, number> {
  const out: Record<string, number> = {};
  for (const key of Object.keys(levels).sort()) out[key] = levels[key];
  return out;
}

/** `RecipeSel.to_json`：kind、unit、slot、levels，有 id 时带 id。 */
function recipeJson(unit: Unit, card: Card, levels: Record<string, number>): Record<string, unknown> {
  return { kind: card.kind, unit, slot: card.slot, levels: sorted(levels), ...(card.recipeId ? { id: card.recipeId } : {}) };
}

/** `Adjust.to_json`；全是 0 时返回 null（render/edits.write 不写恒等的调整）。 */
export function adjustJson(k: KnobValues): Record<string, unknown> | null {
  const keys: Array<keyof KnobValues> = ['exposure_ev', 'warm', 'tint', 'contrast', 'highlight', 'shadow', 'black_point', 'saturation', 'vibrance', 'vignette'];
  if (keys.every((key) => k[key] === 0)) return null;
  const out: Record<string, unknown> = {
    exposure_ev: k.exposure_ev,
    wb: { warm: k.warm, tint: k.tint },
    contrast: k.contrast,
    highlight: k.highlight,
    shadow: k.shadow,
  };
  for (const key of ['black_point', 'saturation', 'vibrance', 'vignette'] as const) if (k[key]) out[key] = k[key];
  return out;
}

/**
 * 桌面 sidecar 里这张照片的编辑（`render/edits.write` 写出的那几块）：`recipe`（带 `memory`）和 `adjust`。
 * `cards` 是这个单元的全部配方卡（`shelf(list, 'all')`），按 id 找卡的位置、条目和旋钮。
 * memory 就是照片的 `memory`（桌面 look_cards.js recipeTarget 记下的那份），键照桌面 recipeKey 改写；空的档位
 * （比如第一次选配方时记下的原厂 04 `{}`）照样是一项，和 render/edits.write 存的一样；整份是空的才不写。
 * 没动过的照片（没选配方、调整全是 0）是 `{}`。
 */
export function buildSide(photo: RequestPhoto, cards: readonly Card[]): Record<string, unknown> {
  const side: Record<string, unknown> = {};
  const unit: Unit = photo.choice?.unit ?? unitOfLens(photo.lens) ?? 'jlaunch';
  const byId = new Map(cards.map((card) => [card.id, card]));
  const chosen = photo.choice ? byId.get(photo.choice.id) ?? null : null;
  if (chosen && photo.choice) {
    const recipe = recipeJson(photo.choice.unit, chosen, cleanLevels(chosen, photo.levels.get(chosen.id)));
    const memory: Record<string, Record<string, number>> = {};
    for (const [id, levels] of photo.memory) {
      const card = byId.get(id);
      if (!card) continue;
      memory[recipeKey(unit, card)] = sorted(cleanLevels(card, levels));
    }
    side.recipe = Object.keys(memory).length ? { ...recipe, memory } : recipe;
  }
  const adjust = adjustJson(photo.knobs);
  if (adjust) side.adjust = adjust;
  return side;
}

export const BINDING_PREFIX = 'gxr-core ';

/** 一张照片的整份请求。`build` 是构建号（vite 的 define），`binding` 给了就用它（只有开发服务器的测试会给）。 */
export function buildRequest(photo: RequestPhoto, cards: readonly Card[], options: ExportOptions, build: string, binding?: string): ExportRequest {
  const side = buildSide(photo, cards);
  const files: Record<string, { relpath: string }> = {};
  if (photo.dngName) files.dng = { relpath: photo.dngName };
  if (photo.jpgName) files.jpg = { relpath: photo.jpgName };
  const recipe = side.recipe as Record<string, unknown> | undefined;
  let resolve: string | null = null;
  if (recipe) {
    resolve = JSON.stringify(Object.fromEntries(Object.entries(recipe).filter(([key]) => key !== 'memory')));
  } else if (!photo.jpgName && photo.dngName) {
    const unit = unitOfLens(photo.lens) ?? 'jlaunch';
    resolve = JSON.stringify({ kind: 'builtin', unit, slot: 4, levels: {} });
  }
  const mtimeMs = photo.sourceModified;
  return {
    shot: { name: photo.stem, files, lens: photo.lens, iso: photo.iso },
    side,
    defaults: {},
    base: 'auto',
    layout: {},
    binding: binding ?? `${BINDING_PREFIX}${build}`,
    quality: QUALITY_TIERS[options.quality],
    longEdge: SIZE_TIERS[options.size],
    hdr: false,
    mtimeMs,
    tzOffsetMin: -new Date(mtimeMs).getTimezoneOffset(),
    resolve,
    needsJpg: photo.jpgName !== null && (!recipe || photo.dngName === null),
  };
}
