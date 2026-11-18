// 一张照片交给 export_plan / export_prepare 的请求：从这次会话的编辑状态拼出来。纯函数，不碰 DOM。
// shot 照桌面图库的 shot 字典（宽高在 Worker 里从文件头补上）；side 照桌面 render/edits.write 写出的 sidecar；
// defaults 是没有默认设置文件的桌面（{}）；底图「自动」。

import { unitOfLens, type Unit } from './calib';
import { cloneCrop, geometrySidecar, type CropState, type GeometryState } from './crop';
import { CJK_REFUSE_MESSAGE, GXR_MARK } from './fonts';
import { LOGO_STYLES, type FrameSpec, type LogoInfo, type StampSpec } from './frame_spec';
import { MISSING_LOGO_LINE } from './logo_key';
import { isDisplaySpace } from './lut';
import type { Frame } from './photo_size';
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

/** 桌面 curves.js：四个通道，恒等的省略，全恒等是 null。 */
export const CURVE_CHANNELS = ['master', 'r', 'g', 'b'] as const;
export type CurveChannel = (typeof CURVE_CHANNELS)[number];
export type CurvePoint = [number, number];
export type CurveMap = Partial<Record<CurveChannel, CurvePoint[]>>;
export const CURVE_LABELS: ReadonlyArray<readonly [CurveChannel, string]> = [
  ['master', '主曲线'],
  ['r', '红'],
  ['g', '绿'],
  ['b', '蓝'],
];

/** 桌面 `normalizeCurves`：恒等通道（每个点 x = y）不写，一个通道都没有就是 null。 */
export function curveJson(curves: CurveMap | null | undefined): CurveMap | null {
  if (!curves) return null;
  const out: CurveMap = {};
  for (const key of CURVE_CHANNELS) {
    const points = curves[key];
    if (points && !points.every(([x, y]) => x === y)) out[key] = points.map((p) => [p[0], p[1]]);
  }
  return Object.keys(out).length ? out : null;
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
  /** 选中的 LUT（cube_id），没有是 null。 */
  lut?: string | null;
  /** 选中那个 LUT 的输入色彩空间（仅用于决定要不要解相机 JPG；真正交给核心的规格在 Worker 批次快照里）。 */
  lutInputSpace?: string | null;
  /** 选中那个配方的档位（只有用户动过的），键是卡的 id（`builtin:3`、`look:<id>`…）。 */
  levels: ReadonlyMap<string, Record<string, number>>;
  /** 桌面的 memory（`look_cards.js` recipeTarget：换配方时切走的那个），键是卡的 id；空的档位也是一项。 */
  memory: ReadonlyMap<string, Record<string, number>>;
  knobs: KnobValues;
  /** 桌面 sidecar 的 `curve`。null 是没调过（恒等）。 */
  curve?: CurveMap | null;
  frame?: Frame | null;
  geometry?: GeometryState;
  crop?: CropState | null;
  border?: FrameSpec | null;
  stamp?: StampSpec | null;
  /** 这张边框实际用到的自定义标志信息（`logoStore.info(border.logo)`），没有或内置/不显示时为 null。 */
  logoInfo?: LogoInfo | null;
  /** 源文件（有相机 JPG 用 JPG，否则 DNG，即桌面 serve._source_path）的 File.lastModified。 */
  sourceModified: number;
}

export interface ExportOptions {
  size: SizeTier;
  quality: QualityTier;
  /** 开了 HDR：每张都带上，做不做得成由核心定（只有 JPG 的是 `no_raw`，照常导出普通版本，和桌面一样）。 */
  hdr?: boolean;
}

export interface ExportRequest {
  /** 不含 width / height：Worker 读文件头后补上（桌面 photo_meta 的宽高，不用解码后的位图）。 */
  shot: { name: string; files: Record<string, { relpath: string }>; lens: string | null; iso: number | null };
  side: Record<string, unknown>;
  defaults: Record<string, never>;
  base: 'auto';
  layout: Record<string, unknown>;
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
  /** 这一张要不要解码相机 JPG（没选配方、又有相机 JPG，且不是 Log LUT）。 */
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

/** 这张边框实际会用到的自定义标志 id；无边框、不支持标志的样式、不显示或内置 GXR 标志时返回 null。 */
export function usedLogoId(border: FrameSpec | null | undefined): string | null {
  if (!border || border.style === 'none' || !LOGO_STYLES.includes(border.style)) return null;
  if (!border.show_logo || !border.logo || border.logo === 'ricoh' || border.logo === 'none') return null;
  return border.logo;
}

/** `FrameSpec.to_json`；`style === 'none'` 时不写进 sidecar。 */
export function borderJson(f: FrameSpec | null | undefined): Record<string, unknown> | null {
  if (!f || f.style === 'none') return null;
  return {
    version: 2,
    style: f.style,
    color: f.color,
    weight: f.weight,
    show_logo: f.show_logo,
    show_params: f.show_params,
    show_date: f.show_date,
    text: f.text,
    output_ratio: f.output_ratio,
    fmt: f.fmt,
    table: f.table,
    logo: f.logo,
    logo_mode: f.logo_mode,
    logo_size: f.logo_size,
    model: f.model,
  };
}

/** `StampSpec.to_json`；未开启时不写进 sidecar，落点为 `photo` 时照桌面省掉 `placement`。 */
export function stampJson(s: StampSpec | null | undefined): Record<string, unknown> | null {
  if (!s || !s.enabled) return null;
  const out: Record<string, unknown> = {
    enabled: true,
    content: s.content,
    date_format: s.date_format,
    show_time: s.show_time,
    date_override: s.date_override,
    text: s.text,
    font: s.font,
    color: s.color,
    size: s.size,
    corner: s.corner,
    glow: s.glow,
  };
  if (s.placement !== 'photo') out.placement = s.placement;
  return out;
}

export const INK_FALLBACK_MESSAGE = '导出失败。请重试。';

/** `export_ink` 的错误映射：陷阱走 M3 恢复，`missing_cjk` 走桌面中文提示，`unknown_logo` 走标志回退句，其余一律走桌面兜底。 */
export function mapInkError(err: unknown): { kind: 'trap' | 'core'; code: string; message: string } {
  if (err instanceof WebAssembly.RuntimeError || err instanceof RangeError) {
    return { kind: 'trap', code: 'trap', message: err.message };
  }
  const text = typeof err === 'string' ? err : err instanceof Error ? err.message : String(err);
  try {
    const parsed = JSON.parse(text) as { error?: { kind?: unknown } };
    const errorKind = parsed.error?.kind;
    if (errorKind === 'missing_cjk') {
      return { kind: 'core', code: 'missing_cjk', message: CJK_REFUSE_MESSAGE };
    }
    if (errorKind === 'unknown_logo') {
      return { kind: 'core', code: 'unknown_logo', message: MISSING_LOGO_LINE };
    }
  } catch {
    /* 非 JSON 错误归入兜底。 */
  }
  return { kind: 'core', code: 'ink_error', message: INK_FALLBACK_MESSAGE };
}

/**
 * 桌面 sidecar 里这张照片的编辑（`render/edits.write` 写出的那几块）：`recipe`（带 `memory`）、`lut`、`adjust`、`curve`、`geometry`、`crop`、`border`、`stamp`。
 * 没动过的照片是 `{}`。恒等曲线不写 `curve`。
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
  if (photo.lut) side.lut = photo.lut;
  const adjust = adjustJson(photo.knobs);
  if (adjust) side.adjust = adjust;
  const curve = curveJson(photo.curve);
  if (curve) side.curve = curve;
  const geom = photo.geometry ? geometrySidecar(photo.geometry.straighten) : null;
  if (geom) side.geometry = geom;
  if (photo.crop) side.crop = cloneCrop(photo.crop);
  const border = borderJson(photo.border);
  if (border) side.border = border;
  const stamp = stampJson(photo.stamp);
  if (stamp) side.stamp = stamp;
  if (photo.frame && Object.keys(side).length > 0) {
    side.frame = { orientation: photo.frame.orientation, width: photo.frame.width, height: photo.frame.height };
  }
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
  const isLogLut = Boolean(photo.lut && photo.lutInputSpace && !isDisplaySpace(photo.lutInputSpace));
  const mtimeMs = photo.sourceModified;
  const logoId = usedLogoId(photo.border);
  const usedLogos: LogoInfo[] = logoId && photo.logoInfo && photo.logoInfo.id === logoId ? [photo.logoInfo] : [];
  const layout: Record<string, unknown> = side.border || side.stamp ? { logos: usedLogos, mark: { ...GXR_MARK } } : {};
  return {
    shot: { name: photo.stem, files, lens: photo.lens, iso: photo.iso },
    side,
    defaults: {},
    base: 'auto',
    layout,
    binding: binding ?? `${BINDING_PREFIX}${build}`,
    quality: QUALITY_TIERS[options.quality],
    longEdge: SIZE_TIERS[options.size],
    hdr: options.hdr === true,
    mtimeMs,
    tzOffsetMin: -new Date(mtimeMs).getTimezoneOffset(),
    resolve,
    needsJpg: !isLogLut && photo.jpgName !== null && (!recipe || photo.dngName === null),
  };
}
