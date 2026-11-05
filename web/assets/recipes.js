// 大图「配方」标签：8 个内置配方、文件内 Looks 和用户配方，档位来自 /api/recipes。
// 三种状态（来自 /capabilities，旧版后端由 has_raw 推断）：
//   full：有 RAW，全部可用。
//   approx：只有 JPG，内置配方用近似（不带旋钮，旋钮置灰），画面标「近似」。
//   none：数据尚不可用，或照片没有 RAW，按实际原因显示提示，配方和旋钮置灰。

import { h } from './dom.js';
import { state } from './state.js';
import { camdataGuide, camdataReady, isLegacyCamera } from './camdata.js';
import {
  KNOB_NAMES,
  cleanLevels,
  effective,
  ensureRecipe,
  persist,
  recipeBody,
  recipeInfo,
  recipeKey,
  reloadEdit,
  slotName,
  schedulePreview,
  selection,
  unitForPhoto,
  beginChange,
  endChange,
} from './preview.js';
import { slider } from './controls.js';
import { photoLutField } from './lut_library.js';
import { UNCALIBRATED_TITLE } from './preview.js';

const NEW_TONE_KNOBS = ['contrast', 'highlight', 'shadow', 'saturation'];
const COMPUTER_ONLY = ['fade', 'warmth', 'tint', 'split', 'filter'];
const LEGACY_LIMIT = 5;

export function recipePanel(view) {
  const ed = view.ed;
  const caps = ed.caps || { recipe: 'none' };
  const state3 = caps.recipe;
  const sel = selection(view);
  const unit = state.catalog?.units.find((u) => u.id === sel.unit);
  const info = recipeInfo(view, sel);
  const hasRaw = caps.raw ?? view.photo.has_raw;
  const usable = camdataReady() && (state3 === 'full' || state3 === 'approx');
  const knobsOn = usable && state3 === 'full';

  const nodes = [h('div', { class: 'pane-head' }, h('h2', {}, '配方'))];
  nodes.push(photoLutField(view, { beginChange, persist, endChange, schedulePreview }));
  nodes.push(h('div', { class: 'sec' }, '内置配方'));
  if (hasRaw && !camdataReady()) nodes.push(camdataGuide({ onDone: () => reloadEdit(view) }));

  // COPY 3.3：近似只给一个小标签。只有 JPG 时面板里写一行「配方需要 RAW 文件」，下面一行小字，按钮置灰；点了只闪一下这一行
  let noticeEl = null;
  if (state3 === 'approx') {
    nodes.push(h('div', { class: 'row-tags' }, h('span', { class: 'chip warn' }, '近似')));
  } else if (!hasRaw) {
    noticeEl = h('div', { class: 'notice grey' },
      h('div', { class: 'strong' }, '配方需要 RAW 文件'),
      h('div', { class: 'small dim' }, '拍摄时将画质设为 RAW+JPEG'),
    );
    nodes.push(noticeEl);
  }

  const flash = () => {
    if (!noticeEl) return;
    noticeEl.classList.remove('flash');
    void noticeEl.offsetWidth;
    noticeEl.classList.add('flash');
  };

  // 跨单元或跨 ISO（或认不出的单元）：只显示「未校准」小标签，提示气泡写「使用 A12 50mm 的颜色模型」（COPY 3.9）
  if (ed.calibrated === false || unitForPhoto(view.photo) !== 'jlaunch') {
    nodes.push(h('div', { class: 'row-tags' }, h('span', { class: 'chip warn', title: UNCALIBRATED_TITLE }, '未校准')));
  }

  if (!unit) {
    nodes.push(h('p', { class: 'small' }, '无法读取配方。请重试。'));
    return h('div', { class: 'edit-panel' }, ...nodes);
  }
  if (!info) nodes.push(h('p', { class: 'small' }, '找不到这个配方。请重新选择。'));


  const baseButtons = unit.recipes.map((r) =>
    h('button', {
      class: 'btn base',
      type: 'button',
      'aria-pressed': String((!sel.kind || sel.kind === 'builtin') && r.slot === sel.slot && !!ed.recipe),
      'aria-disabled': usable ? null : 'true',
      onclick: () => {
        if (!usable) return flash();
        switchRecipe(view, { kind: 'builtin', slot: r.slot });
      },
    }, slotName(sel.unit, r.slot)),
  );
  nodes.push(h('div', { class: usable ? 'base-grid' : 'base-grid dimmed-block' }, baseButtons));

  for (const [key, label] of [['looks', 'Looks'], ['users', '我的配方']]) {
    const entries = unit[key] || [];
    if (!entries.length) continue;
    nodes.push(h('div', { class: 'sec' }, label));
    nodes.push(h('div', { class: usable ? 'base-grid' : 'base-grid dimmed-block' }, entries.map((r) =>
      h('button', {
        class: 'btn base', type: 'button',
        'aria-pressed': String(sel.kind === r.kind && sel.id === r.id),
        'aria-disabled': usable ? null : 'true',
        onclick: () => usable ? switchRecipe(view, r) : flash(),
      }, r.name),
    )));
  }

  // 强度和旋钮：配方不能用时整块变淡；点了（捕获阶段拦住）闪一下
  const rest = h('div', { class: knobsOn ? null : 'dimmed-block', title: state3 === 'approx' ? '近似效果只支持默认参数' : null });
  if (!knobsOn) {
    rest.addEventListener('pointerdown', (e) => {
      e.stopPropagation();
      e.preventDefault();
      flash();
    }, true);
  }
  const controls = info?.controls || [];
  const amount = controls.find((c) => c.name === 'amount');
  if (amount) rest.append(levelRow(view, amount, KNOB_NAMES.amount, knobsOn, { title: state3 === 'approx' ? '近似效果只支持默认参数' : null }));
  for (const c of controls) {
    if (c.name === 'amount') continue;
    rest.append(levelRow(view, c, KNOB_NAMES[c.name] || c.label, knobsOn, { tag: tagFor(c), title: state3 === 'approx' ? '近似效果只支持默认参数' : null }));
  }
  nodes.push(rest);
  // 底部只留 [重置] 靠右，固定在面板底边。原来的「存储为我的配方」「写入存储卡」点了没有反应，删掉（U8）；
  // 我的配方在「配方」页从 LUT 生成，存储卡不写。
  const reset = resetButton(view, usable && !!info);
  reset.classList.add('end');
  nodes.push(h('div', { class: 'sticky-foot' }, reset));

  view.flashRecipe = flash;
  return h('div', { class: 'edit-panel' }, ...nodes);
}

// 写不进旧版相机配方文件的旋钮打「仅电脑」（新版相机上不打标签，COPY 3.10）
function tagFor(control) {
  return isLegacyCamera() && COMPUTER_ONLY.includes(control.name) ? '仅电脑' : null;
}

// 旧版相机配方文件只认 ±5：这四项的滑块范围直接限制在 ±5，不打标签（COPY 3.10）
function levelsFor(control) {
  const levels = [...control.levels].sort((a, b) => a - b);
  if (isLegacyCamera() && NEW_TONE_KNOBS.includes(control.name)) return levels.filter((v) => Math.abs(v) <= LEGACY_LIMIT);
  return levels;
}

// 当前值在档位里的序号；值不在可选范围里（比如旧版范围外的值）就取最近的一档
function nearestIndex(levels, v) {
  let best = 0;
  levels.forEach((x, i) => {
    if (Math.abs(x - v) < Math.abs(levels[best] - v)) best = i;
  });
  return best;
}

function fmtValue(control, v) {
  const text = control.name === 'amount' ? `${v}%` : v > 0 ? `+${v}` : `${v}`;
  const levels = control.levels;
  const atEnd = (v === levels[levels.length - 1] && control.cap_upper) || (v === levels[0] && control.cap_lower);
  return atEnd ? `${text}!` : text;
}

// 一根档位滑块：滑块的位置是档位的序号
function levelRow(view, control, label, enabled, { tag = null, title = null } = {}) {
  const levels = levelsFor(control);
  const cur = effective(control, selection(view).levels);
  const tagEl = tag ? h('span', { class: 'tag soft' }, tag) : null;
  return h(
    'div',
    { class: 'knob', title },
    tagEl ? h('div', { class: 'knob-tag-line' }, tagEl) : null,
    sliderEl(view, { label, levels, control, index: nearestIndex(levels, cur), enabled }),
  );
}

function sliderEl(view, { label, levels, control, index, enabled }) {
  const setLevelAt = (i) => {
    ensureRecipe(view).levels[control.name] = levels[i];
  };
  const defaultIndex = nearestIndex(levels, control.default);
  const s = slider({
    label,
    min: 0,
    max: levels.length - 1,
    step: 1,
    value: index,
    fmt: (i) => fmtValue(control, levels[i]),
    dflt: defaultIndex,
    disabled: !enabled,
    onBegin: () => beginChange(view),
    onInput: (i) => {
      setLevelAt(i);
      schedulePreview(view);
    },
    onCommit: (i) => {
      setLevelAt(i);
      persist(view, { recipe: recipeBody(view, { memory: true }) }).then(() => endChange(view));
      schedulePreview(view, { full: true });
    },
  });
  return s.el;
}

// 内置配方：换配方时记住原来的旋钮，取回这个配方上次的旋钮（每个配方各自记住）
function switchRecipe(view, target) {
  const ed = view.ed;
  const sel = selection(view);
  const next = { unit: sel.unit, kind: target.kind, slot: target.slot, ...(target.id ? { id: target.id } : {}), levels: {} };
  if (ed.recipe && recipeKey(sel) === recipeKey(next)) return;
  beginChange(view);
  const memory = { ...ed.memory, [recipeKey(sel)]: cleanLevels(recipeInfo(view, sel), sel.levels) };
  next.levels = cleanLevels(recipeInfo(view, next), memory[recipeKey(next)] || {});
  ed.memory = memory;
  ed.recipe = next;
  persist(view, { recipe: recipeBody(view, { memory: true }) }).then(() => endChange(view));
  schedulePreview(view, { full: true });
  view.redrawSide?.();
}

// 重置：只清旋钮，强度保留
function resetButton(view, usable) {
  const btn = h('button', { class: 'btn', type: 'button', 'aria-disabled': usable ? null : 'true' }, '重置');
  btn.addEventListener('click', () => {
    if (!usable) {
      view.flashRecipe?.();
      return;
    }
    const r = view.ed.recipe;
    if (!r) return;
    beginChange(view);
    const kept = {};
    if (r.levels.amount !== undefined) kept.amount = r.levels.amount;
    r.levels = kept;
    persist(view, { recipe: recipeBody(view, { memory: true }) }).then(() => endChange(view));
    schedulePreview(view, { full: true });
    view.redrawSide?.();
  });
  return btn;
}
