// 修图的编辑状态与预览：每张照片一份（view.ed），配方、调整、曲线的读写；
// 拖动时防抖地请求小图，松手后请求清晰版并保存。所有预览都是"电脑预测"：标签来自接口的响应头和 render 信息。

import { api, apiBlob } from './api.js';
import { state } from './state.js';
import { h, toast } from './dom.js';
import { applyCamdataStatus } from './camdata.js';
import { CAMERA_JPG_NOTE, isPrediction, lookNoteText, originalNoteText } from './look_notes.js';

export const EDIT_TABS = ['recipes', 'adjust'];
export const DEFAULT_NOTE = CAMERA_JPG_NOTE;

// 没有编辑时大图画的是什么，如实写出来（只有 RAW 的照片不是"照片本身"）；文字见 look_notes.js
export function defaultNote(photo) {
  const look = photo && photo.preview_look;
  const text = lookNoteText(look);
  return isPrediction(look) ? [h('span', { class: 'pred-chip' }, '电脑预测'), ` ${text}`] : text;
}

export function originalNote(photo) {
  return originalNoteText(photo && photo.preview_look);
}

const PREVIEW_EDGE = 800; // 拖动时的小图长边；松手后换成清晰版（不限长边）
const DEBOUNCE_MS = 120;
export const CURVE_MAX = 16;

// 8 个底子的名字（与 UX.md 3.6 一致；28mm / MOUNT 的底子名字沿用同一套）
export const BASE_NAMES = ['GR 正片', 'GR 负片', '高反差黑白', '标准', '富士 CC', '富士 CN', '徕卡 Chrome', '徕卡 Vivid'];
export const KNOB_NAMES = {
  amount: '整体强度',
  contrast: '对比',
  highlight: '高光',
  shadow: '阴影',
  fade: '褪色',
  saturation: '饱和度',
  warmth: '冷暖',
  tint: '品绿',
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
export function loadCatalog() {
  if (state.catalog) return Promise.resolve(state.catalog);
  if (!catalogPromise) {
    catalogPromise = api('/api/recipes').then(
      (r) => {
        state.catalog = r;
        return r;
      },
      (e) => {
        catalogPromise = null;
        throw e;
      },
    );
  }
  return catalogPromise;
}

// ---- 读写 -------------------------------------------------------------------

function applyEdit(ed, edit) {
  ed.rev = edit.rev;
  ed.memory = edit.memory || {};
  ed.recipe = edit.recipe ? { unit: edit.recipe.unit, slot: edit.recipe.slot, levels: { ...(edit.recipe.levels || {}) } } : null;
  ed.adj = {
    exposure_ev: edit.adjust?.exposure_ev ?? 0,
    warm: edit.adjust?.wb?.warm ?? 0,
    tint: edit.adjust?.wb?.tint ?? 0,
    contrast: edit.adjust?.contrast ?? 0,
    highlight: edit.adjust?.highlight ?? 0,
    shadow: edit.adjust?.shadow ?? 0,
  };
  ed.curve = edit.curve?.master ? edit.curve.master.map((p) => [...p]) : null;
  ed.render = edit.render || null;
}

// 裁切和配方/调整写的是同一份 sidecar，共用一个版本号（rev）：任何一边写成功或重新读取之后，
// 两边手里的版本号都要换成最新的，否则下一次保存会被后端以 409 rev_conflict 拒绝。
export function syncRev(view, rev) {
  if (view.ed) view.ed.rev = rev;
  if (view.crop) view.crop.rev = rev;
}

async function fetchEdit(view) {
  return api(`/api/photos/${view.photo.id}/edit`);
}

export async function ensureEdit(view) {
  if (!view.edPromise) {
    view.edPromise = (async () => {
      const [edit] = await Promise.all([fetchEdit(view), loadCatalog()]);
      const ed = {
        seq: 0,
        timer: null,
        ctl: null,
        url: null,
        label: null,
        error: null,
        busy: false,
        save: Promise.resolve(),
        redraw: null,
      };
      applyEdit(ed, edit);
      view.ed = ed;
      syncRev(view, ed.rev);
      return ed;
    })().catch((e) => {
      view.edPromise = null;
      throw e;
    });
  }
  return view.edPromise;
}

// 从接口重新读一次（导入相机数据后、保存冲突后用）
export async function reloadEdit(view) {
  const ed = view.ed;
  if (!ed) return;
  try {
    applyEdit(ed, await fetchEdit(view));
    syncRev(view, ed.rev);
  } catch (e) {
    toast(e.message, 'error');
    return;
  }
  ed.redraw?.();
  schedulePreview(view, { final: true });
  renderNote(view);
}

// ---- 当前底子与参数 ----------------------------------------------------------

// 当前底子：没选过配方时就是该单元的原厂 04（电脑模拟）
export function selection(view) {
  const ed = view.ed;
  if (ed.recipe) return ed.recipe;
  return { unit: unitForPhoto(view.photo) || 'jlaunch', slot: 4, levels: {} };
}

export function recipeInfo(view, sel = selection(view)) {
  const unit = state.catalog?.units.find((u) => u.id === sel.unit);
  return unit?.recipes.find((r) => r.slot === sel.slot) || null;
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
    ed.recipe = { unit: sel.unit, slot: sel.slot, levels: {} };
  }
  return ed.recipe;
}

export function recipeBody(view, { memory = false } = {}) {
  const ed = view.ed;
  if (!ed.recipe) return null;
  const body = {
    kind: 'builtin',
    unit: ed.recipe.unit,
    slot: ed.recipe.slot,
    levels: cleanLevels(recipeInfo(view, ed.recipe), ed.recipe.levels),
  };
  if (memory && Object.keys(ed.memory).length) body.memory = ed.memory;
  return body;
}

export function adjustBody(ed) {
  const a = ed.adj;
  return { exposure_ev: a.exposure_ev, wb: { warm: a.warm, tint: a.tint }, contrast: a.contrast, highlight: a.highlight, shadow: a.shadow };
}

export function curveBody(ed) {
  return ed.curve ? { master: ed.curve } : null;
}

// ---- 保存（一次一个，按顺序；带 rev 做乐观并发） ----------------------------

export function persist(view, changes) {
  const ed = view.ed;
  ed.save = ed.save.then(async () => {
    try {
      const r = await api(`/api/photos/${view.photo.id}/edit`, { method: 'PUT', body: { ...changes, expected_rev: ed.rev } });
      syncRev(view, r.rev);
      ed.render = r.render || ed.render;
      if (r.warning) toast(r.warning, 'info');
    } catch (e) {
      toast(e.message, 'error');
      await reloadEdit(view);
    }
    renderNote(view);
  });
  return ed.save;
}

// ---- 预览 -------------------------------------------------------------------

export function schedulePreview(view, { final = false } = {}) {
  const ed = view.ed;
  if (!ed) return;
  clearTimeout(ed.timer);
  ed.timer = setTimeout(() => runPreview(view, final), final ? 0 : DEBOUNCE_MS);
}

function setUrl(ed, url) {
  if (ed.url) URL.revokeObjectURL(ed.url);
  ed.url = url;
}

async function runPreview(view, final) {
  const ed = view.ed;
  if (!ed || !EDIT_TABS.includes(view.tab)) return;
  if (ed.render && ed.render.reason === 'no_raw') return; // 没有 RAW：界面已经说明原因，不再请求
  ed.seq += 1;
  const seq = ed.seq;
  ed.ctl?.abort();
  const ctl = new AbortController();
  ed.ctl = ctl;
  const q = final ? '' : `?max_edge=${PREVIEW_EDGE}`;
  ed.busy = true;
  renderNote(view);
  try {
    const { blob, headers } = await apiBlob(`/api/photos/${view.photo.id}/render${q}`, {
      body: { recipe: recipeBody(view), adjust: adjustBody(ed), curve: curveBody(ed) },
      signal: ctl.signal,
    });
    if (seq !== ed.seq) return;
    if (view.ed !== ed) return;
    ed.label = {
      text: decodeURIComponent(headers.get('X-GXR-Label') || '电脑预测'),
      calibrated: headers.get('X-GXR-Calibrated') === '1',
    };
    ed.error = null;
    if (ed.render) ed.render = { ...ed.render, available: true, message: null, reason: null };
    setUrl(ed, URL.createObjectURL(blob));
    syncImage(view);
  } catch (e) {
    if (e.name === 'AbortError' || seq !== ed.seq) return;
    ed.error = e;
    if (e.data?.status) applyCamdataStatus(e.data.status);
  } finally {
    if (seq === ed.seq) ed.busy = false;
    if (view.ed === ed) renderNote(view);
  }
}

// 图片显示什么：编辑标签且没有按住 Space 时显示预览，否则显示照片本身
export function syncImage(view) {
  const img = view.img;
  if (!img) return;
  const ed = view.ed;
  const editing = EDIT_TABS.includes(view.tab) && !view.showOriginal;
  const want = editing && ed?.url ? ed.url : view.photo.preview_url;
  if (img.dataset.src !== want) {
    img.dataset.src = want;
    img.src = want;
  }
}

// 预览下面的说明：电脑预测标签、未标定的原因、为什么没有图
export function renderNote(view) {
  const note = view.noteEl;
  if (!note) return;
  if (!EDIT_TABS.includes(view.tab)) {
    note.replaceChildren(...[defaultNote(view.photo)].flat());
    return;
  }
  if (view.showOriginal) {
    note.textContent = originalNote(view.photo);
    return;
  }
  const ed = view.ed;
  if (!ed) {
    note.textContent = '正在读取…';
    return;
  }
  const parts = [];
  const render = ed.render || {};
  if (render.available === false && render.reason === 'no_raw') {
    parts.push(h('span', { class: 'small' }, '这张照片没有 RAW，只有相机 JPG，不能套配方。'));
  } else {
    const text = ed.label?.text || render.label || '电脑预测';
    parts.push(h('span', { class: 'pred-chip' }, '电脑预测'));
    if (text !== '电脑预测') parts.push(h('span', { class: 'small warn-text' }, ` ${text}`));
    for (const reason of render.reasons || []) parts.push(h('div', { class: 'small dim' }, `· ${reason}`));
    if (ed.error) parts.push(h('div', { class: 'small warn-text' }, ed.error.message));
    else if (render.available === false && render.message) parts.push(h('div', { class: 'small warn-text' }, render.message));
    if (ed.busy) parts.push(h('div', { class: 'small dim' }, '正在预览…'));
  }
  parts.push(h('div', { class: 'small dim' }, '预览是整张画面（不带裁切），裁切在导出时套用。按住 Space 看照片本身。'));
  note.replaceChildren(...parts);
}

export function disposeEdit(view) {
  const ed = view.ed;
  if (!ed) return;
  clearTimeout(ed.timer);
  ed.ctl?.abort();
  if (ed.url) URL.revokeObjectURL(ed.url);
  ed.url = null;
  view.ed = null;
  view.edPromise = null;
}
