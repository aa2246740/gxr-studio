// 大图「配方」标签：现行相机顺序的完整配方、历史文件条目和用户配方，档位来自 /api/recipes。
// 三种状态（来自 /capabilities，旧版后端由 has_raw 推断）：
//   full：有 RAW，全部可用。
//   approx：只有 JPG，内置配方用近似（不带旋钮，旋钮置灰），画面标「近似」。
//   none：数据尚不可用，或照片没有 RAW，按实际原因显示提示，配方和旋钮置灰。

import { h } from './dom.js';
import { state } from './state.js';
import { camdataGuide, camdataReady, isLegacyCamera } from './camdata.js';
import {
  KNOB_NAMES,
  effective,
  ensureRecipe,
  persist,
  recipeBody,
  recipeInfo,
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
import { uncalibratedReason } from './calibration_note.js';
import { CAMERA_SOURCE, cachedMyRecipes, loadMyRecipes, sourceText } from './my_recipes.js';
import { computerTarget, newDeck, recipeTarget, renderRecipe } from './look_cards.js';
import { describeLook, groupComputer } from './look_model.js';

const NEW_TONE_KNOBS = ['contrast', 'highlight', 'shadow', 'saturation'];
const COMPUTER_ONLY = ['fade', 'warmth', 'tint', 'split', 'filter'];
const LEGACY_LIMIT = 5;
const APPROX_NOTE = '只有 JPG：配方按近似效果显示，旋钮不能用';

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
  const photoUnit = unitForPhoto(view.photo);

  const nodes = [h('div', { class: 'pane-head' }, h('h2', {}, '配方'))];
  nodes.push(photoLutField(view, { beginChange, persist, endChange, schedulePreview }));
  if (hasRaw && !camdataReady()) nodes.push(camdataGuide({ onDone: () => reloadEdit(view) }));

  // COPY 3.3：近似只给一个小标签，后面一行说清楚为什么。只有 JPG 时面板里写一行「配方需要 RAW 文件」，下面一行小字，按钮置灰；点了只闪一下这一行
  let noticeEl = null;
  if (state3 === 'approx') {
    nodes.push(h('div', { class: 'row-tags' }, h('span', { class: 'chip warn' }, '近似'), h('span', { class: 'small dim' }, APPROX_NOTE)));
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

  if (!unit) {
    nodes.push(h('p', { class: 'small' }, '无法读取配方。请重试。'));
    return h('div', { class: 'edit-panel' }, ...nodes);
  }
  if (!info) nodes.push(h('p', { class: 'small' }, '找不到这个配方。请重新选择。'));

  // 每个配方一张卡片：当前这张照片套上它的样子（look_cards.js），名字和一行说明
  const deck = newDeck(view);
  const approx = state3 === 'approx';
  const off = (on) => (on ? {} : { disabled: true });
  const builtinCard = (target, name, extra = {}) => {
    const r = recipeInfo(view, { unit: sel.unit, ...target });
    const t = recipeTarget(view, target);
    return {
      id: `${target.kind}:${target.id || target.slot}`,
      name,
      desc: target.kind === 'record' ? null : describeLook({ kind: target.kind, slot: target.slot, name }),
      pressed: target.kind === 'builtin' ? (!sel.kind || sel.kind === 'builtin') && target.slot === sel.slot && !!ed.recipe : sel.kind === target.kind && sel.id === target.id,
      render: usable && r ? renderRecipe(view, t.same ? sel : t.next, approx) : null,
      onPick: () => (usable ? switchRecipe(view, target) : flash()),
      ...off(usable),
      ...extra,
    };
  };
  const chosen = [];
  const section = (title, items, source = null) => {
    chosen.push(...items);
    if (title) nodes.push(h('div', { class: 'sec' }, title));
    if (source) nodes.push(h('div', { class: 'small dim sec-source' }, source));
    nodes.push(h('div', { class: usable ? 'look-group' : 'look-group dimmed-block' }, deck.grid(items)));
  };

  section('内置配方', unit.recipes.map((r) => builtinCard({ kind: r.kind || 'builtin', slot: r.slot, ...(r.id ? { id: r.id } : {}) }, r.kind === 'record' ? r.name : slotName(sel.unit, r.slot))));
  if ((unit.looks || []).length) {
    section('历史文件条目', unit.looks.map((r) => builtinCard({ kind: 'look', slot: r.slot, id: r.id }, r.name)));
  }

  // 「我的配方」有两个来源，分开写清楚：相机里（相机配方文件里的自定义配方）、电脑上（用 LUT 生成的，存在这台电脑里）
  const { usable: mine, other } = groupComputer(cachedMyRecipes(), sel.unit, photoUnit);
  if (!cachedMyRecipes()) loadMyRecipes().then((list) => list.length && view.redrawSide?.());
  if ((unit.users || []).length || mine.length) {
    nodes.push(h('div', { class: 'sec' }, '我的配方'));
    if ((unit.users || []).length) {
      section(null, unit.users.map((r) => builtinCard({ kind: r.kind, slot: r.slot, id: r.id }, r.name, { title: `${CAMERA_SOURCE}的配方` })), CAMERA_SOURCE);
    }
    if (mine.length) {
      // 电脑上的配方 = 底子加档位。只有 JPG（近似）时不带旋钮：预览会丢掉档位、导出会拒绝，所以只在完整配方时能点
      const mineUsable = usable && state3 === 'full';
      const note = approx ? '近似效果只支持默认参数，电脑上的配方需要 RAW 文件' : null;
      const items = mine.map((r) => computerCard(view, r, mineUsable, flash, note));
      chosen.push(...items);
      nodes.push(h('div', { class: 'small dim sec-source' }, '电脑上 · 用 LUT 生成'),
        h('div', { class: mineUsable ? 'look-group' : 'look-group dimmed-block' }, deck.grid(items)));
    }
  }
  // 为别的镜头做的配方：不藏起来，灰着列出，写清原因，给一条路（去配方页为这张照片的镜头做一个）
  if (other.length) nodes.push(otherUnitGroup(other, photoUnit));

  // 当前配方的名字，名字下面一行安静的「未校准」（原因在提示气泡里）。只在选了配方、又没套 LUT 时才有意义：
  // 配方的颜色是电脑预测，没有校准的照片上预测可能有偏差；LUT 的颜色不经过相机的校准，不用提醒
  if (ed.recipe) {
    const now = chosen.find((c) => c.pressed);
    const name = now ? now.name : info && (info.name || slotName(sel.unit, sel.slot));
    const calibratedIsos = unit.calibrated_isos || (sel.unit === 'jlaunch' ? [200, 1600] : [200]);
    const warn = !ed.lut && (ed.calibrated === false || !photoUnit || sel.unit !== photoUnit || !calibratedIsos.includes(Number(view.photo.iso)));
    nodes.splice(1, 0, h('div', { class: 'cur-look' },
      h('div', { class: 'small' }, h('span', { class: 'dim' }, '当前：'), h('span', { class: 'strong' }, name || '')),
      warn ? h('div', { class: 'small dim calib-note', title: uncalibratedReason(view, photoUnit) }, '未校准：颜色可能有些偏差') : null,
      ed.recipeFromCamera && view.photo.recipe_tag?.levels === null ? h('div', { class: 'small dim' }, '微调未知，电脑预测使用配方默认值') : null,
      unit.exposure_note ? h('div', { class: 'small dim' }, unit.exposure_note) : null));
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
  const panel = h('div', { class: 'edit-panel' }, ...nodes);
  deck.start(panel);
  return panel;
}

// 电脑上的配方：套用 = 换成它的底子和档位。选中的判断看底子和每个旋钮的有效值是否一致
function sameAsComputer(view, r) {
  const sel = selection(view);
  if (!view.ed.recipe || (sel.kind && sel.kind !== 'builtin') || sel.unit !== r.recipe.unit || sel.slot !== r.recipe.slot) return false;
  const info = recipeInfo(view, { unit: r.recipe.unit, kind: 'builtin', slot: r.recipe.slot });
  return !!info && info.controls.every((c) => effective(c, sel.levels) === effective(c, r.recipe.levels));
}

function computerCard(view, r, usable, flash, note = null) {
  return {
    id: `computer:${r.id}`,
    name: r.name,
    desc: describeLook({ kind: 'computer', source: r.source }),
    title: note ? `${sourceText(r)}（${note}）` : sourceText(r),
    pressed: sameAsComputer(view, r),
    render: usable ? renderRecipe(view, computerTarget(view, r).next, false) : null,
    onPick: () => (usable ? applyComputer(view, r) : flash()),
    disabled: !usable,
  };
}

// 为别的镜头单元做的配方：灰着列出，每个写清原因；下面一条路——去配方页，选好这张照片的镜头单元，用 LUT 再做一个
function otherUnitGroup(other, photoUnit) {
  const rows = other.map((o) => h('li', { class: 'other-recipe', 'aria-disabled': 'true' },
    h('span', { class: 'nm' }, o.recipe.name), h('span', { class: 'small dim' }, o.reason)));
  const way = photoUnit
    ? h('a', { class: 'small', href: `#/recipes?unit=${encodeURIComponent(photoUnit)}` }, '去配方页，为这张照片的镜头做一个配方')
    : null;
  return h('div', { class: 'other-units' }, h('div', { class: 'sec' }, '这张照片用不了的配方'), h('ul', { class: 'other-list' }, ...rows), way);
}

function applyComputer(view, r) {
  const ed = view.ed;
  if ((ed.caps || {}).recipe !== 'full') return;
  if (sameAsComputer(view, r)) return;
  const { next, memory } = computerTarget(view, r);
  beginChange(view);
  ed.memory = memory;
  ed.recipe = next;
  persist(view, { recipe: recipeBody(view, { memory: true }) }).then(() => endChange(view));
  schedulePreview(view, { full: true });
  view.redrawSide?.();
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
    onCancel: () => endChange(view),
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

// 内置配方：换配方时记住原来的旋钮，取回这个配方上次的旋钮（每个配方各自记住）；卡片画的也是 recipeTarget 给的这一个
function switchRecipe(view, target) {
  const ed = view.ed;
  const t = recipeTarget(view, target);
  if (t.same) return;
  beginChange(view);
  ed.memory = t.memory;
  ed.recipe = t.next;
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
