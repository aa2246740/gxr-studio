// 配方目录、配方标签与舞台小标签的辅助函数（从 preview.js 拆出，保持模块体积紧凑）。

import { api } from './api.js';
import { state, on } from './state.js';
import { isPrediction, lookNoteText, originalNoteText } from './look_notes.js';
import { localRecipe } from './my_recipes.js';
import { isPairSecond, isSinglePairView } from './halfframe.js';

// 8 个底子的名字（与 UX.md 3.6 一致；28mm / MOUNT 的底子名字沿用同一套）
// 配方位置的默认名（/api/recipes 没给名字时用）。5–8 号在相机里是用户可以改的位置，不写别家的名字（U7）。
export const BASE_NAMES = ['GR 正片', 'GR 负片', 'GR 高对比黑白', '原厂标准', '空位 5', '空位 6', '空位 7', '空位 8'];

// 配方名：/api/recipes 给了名字就用它，没给按位置号取默认名
export function slotName(unitId, slot) {
  const unit = state.catalog && state.catalog.units ? state.catalog.units.find((u) => u.id === unitId) : null;
  const base = unit && unit.recipes ? unit.recipes.find((b) => b.slot === slot && (!b.kind || b.kind === 'builtin')) : null;
  const name = (base && base.name) || BASE_NAMES[slot - 1] || `空位 ${slot}`;
  return `${String(slot).padStart(2, '0')} ${name}`;
}

export function recipeLabel(unitId, recipe) {
  if (recipe.kind === 'local') return localRecipe(recipe.id)?.name || '本机完整配方';
  if (recipe.kind !== 'record') return slotName(unitId, recipe.slot);
  const recipes = state.catalog?.units?.find((u) => u.id === unitId)?.recipes || [];
  const index = recipes.findIndex((r) => r.kind === 'record' && r.id === recipe.id);
  const record = recipes[index];
  return record ? `${String(record.position || index + 1).padStart(2, '0')} ${record.name}` : '完整配方';
}

export const KNOB_NAMES = {
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

// 照片的镜头 → 单元（与后端 unit_of_lens 一致；认不出就返回 null，由调用方退回 50mm）
export function unitForPhoto(photo) {
  const text = `${photo.lens || ''} ${photo.unit || ''}`.toLowerCase();
  if (text.includes('mount')) return 'l08firm';
  if (text.includes('50mm')) return 'jlaunch';
  if (text.includes('28mm')) return 'qlaunch';
  return null;
}

let catalogPromise = null;
let catalogVersion = 0;
on('camdata-changed', () => {
  state.catalog = null;
  catalogPromise = null;
  catalogVersion++;
});
export function loadCatalog({ refresh = false } = {}) {
  if (refresh) {
    state.catalog = null;
    catalogPromise = null;
    catalogVersion++;
  }
  if (state.catalog) return Promise.resolve(state.catalog);
  if (!catalogPromise) {
    const version = catalogVersion;
    catalogPromise = api('/api/recipes').then(
      (r) => {
        if (version !== catalogVersion) return loadCatalog();
        state.catalog = r;
        return r;
      },
      (e) => {
        if (version !== catalogVersion) return loadCatalog();
        catalogPromise = null;
        throw e;
      },
    ).finally(() => { if (version === catalogVersion) catalogPromise = null; });
  }
  return catalogPromise;
}

export function selection(view) {
  const ed = view.ed;
  if (ed.recipe) return ed.recipe;
  return { unit: unitForPhoto(view.photo) || 'jlaunch', slot: 4, levels: {} };
}

export function recipeInfo(view, sel = selection(view)) {
  if (sel.kind === 'local') return localRecipe(sel.id);
  const unit = state.catalog?.units?.find((u) => u.id === sel.unit);
  if (sel.kind === 'look') return unit?.looks?.find((r) => r.id === sel.id) || null;
  if (sel.kind === 'record') return unit?.recipes?.find((r) => r.kind === 'record' && r.id === sel.id) || null;
  if (sel.kind === 'camera_user') return unit?.users?.find((r) => r.id === sel.id) || null;
  return unit?.recipes?.find((r) => (!r.kind || r.kind === 'builtin') && r.slot === sel.slot) || null;
}

export function recipeKey(sel) {
  return sel.kind && sel.kind !== 'builtin' ? `${sel.unit}/${sel.kind}/${sel.id}` : `${sel.unit}/${sel.slot}`;
}

// 旋钮的当前档位：没设过、或不在可选档位里，就取默认
export function effective(control, levels) {
  const v = levels?.[control.name];
  return control.levels.includes(v) ? v : control.default;
}

// 只留下这个底子真正有的旋钮，并且档位都是允许的
export function cleanLevels(info, levels) {
  const out = {};
  if (!info) return out;
  for (const c of info.controls) {
    const v = levels?.[c.name];
    if (v !== undefined && c.levels.includes(v)) out[c.name] = v;
  }
  return out;
}

export function ensureRecipe(view) {
  const ed = view.ed;
  if (!ed.recipe) {
    const sel = selection(view);
    ed.recipe = { ...sel, levels: {} };
  }
  return ed.recipe;
}

export function recipeBody(view, { memory = false } = {}) {
  const ed = view.ed;
  if (!ed.recipe) return null;
  const info = recipeInfo(view, ed.recipe);
  const body = {
    kind: ed.recipe.kind || 'builtin',
    ...(ed.recipe.id ? { id: ed.recipe.id } : {}),
    unit: ed.recipe.unit,
    slot: ed.recipe.slot,
    levels: ed.recipe.kind === 'local' && !info ? { ...(ed.recipe.levels || {}) } : cleanLevels(info, ed.recipe.levels),
  };
  if (memory && Object.keys(ed.memory).length) body.memory = ed.memory;
  return body;
}

// 后端的 X-GXR-Label 是机器值：prediction（电脑预测）、approx（近似）、original（原图，不加小标签）。
// 没有校准（X-GXR-Calibrated=0）的电脑预测在配方标签里说，不在画面上。界面上不出现英文机器值（R2-05）。
export const LABEL_WORDS = { prediction: '电脑预测', approx: '近似', 'camera-jpg': '相机 JPG', neutral: '中性转换', embedded: '内嵌预览' };

export function stageLabels(view) {
  const ed = view.ed;
  if (!ed || view.showOriginal) return [];
  const pairDiptych = isPairSecond(view.photo) && !isSinglePairView(view);
  if (ed.caps.recipe === 'approx' && ed.recipe) return [{ text: '近似', kind: 'warn' }];
  const label = ed.label;
  if (!label || label === 'original') return [];
  if (label === 'prediction' || label.includes('电脑预测')) {
    return [{ text: pairDiptych ? '电脑预测 · 两张 RAW 合成' : '电脑预测', kind: 'ok' }];
  }
  if (label === 'approx') return [{ text: '近似', kind: 'warn' }];
  if (pairDiptych && label === 'camera-jpg') return [{ text: '原图 · 相机双联', kind: 'grey' }];
  // 旧版后端直接给中文的时候照样显示
  if (/[^\x00-\x7f]/.test(label)) return [{ text: label.replace(/\s*未校准$/, ''), kind: 'grey' }];
  const word = LABEL_WORDS[label];
  return word ? [{ text: word, kind: 'grey' }] : [];
}

export function originalNote(photo) {
  return originalNoteText(photo && photo.preview_look);
}

export function defaultNote(photo) {
  const look = photo && photo.preview_look;
  return { text: lookNoteText(look), prediction: isPrediction(look) };
}
