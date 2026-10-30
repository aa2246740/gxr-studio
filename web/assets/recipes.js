// 大图「配方」标签：按照片所属单元列 8 个底子；整体强度；每个旋钮一根滑块（档位来自 /api/recipes）；
// 全部归零（两步确认）；"存为我的配方"占位（M3）。

import { h, toast } from './dom.js';
import { state } from './state.js';
import { camdataGuide, camdataReady, isLegacyCamera } from './camdata.js';
import {
  BASE_NAMES,
  KNOB_NAMES,
  cleanLevels,
  effective,
  ensureRecipe,
  persist,
  recipeBody,
  recipeInfo,
  reloadEdit,
  schedulePreview,
  selection,
  unitForPhoto,
} from './preview.js';

const NEW_TONE_KNOBS = ['contrast', 'highlight', 'shadow', 'saturation'];
const COMPUTER_ONLY_IF_LEGACY = ['fade', 'warmth', 'tint', 'split', 'filter'];

export function recipePanel(view) {
  const ed = view.ed;
  const sel = selection(view);
  const unit = state.catalog?.units.find((u) => u.id === sel.unit);
  const info = recipeInfo(view, sel);
  const nodes = [
    h('h2', {}, '配方'),
    h('p', { class: 'dim small' }, '底子是相机里的风格，你在它上面微调。预览一律标「电脑预测」。'),
  ];
  if (!camdataReady()) nodes.push(camdataGuide({ onDone: () => reloadEdit(view) }));

  const render = ed.render || {};
  if (render.available === false && render.reason === 'no_raw') {
    nodes.push(h('div', { class: 'result bad small' }, '这张照片没有 RAW，只有相机 JPG，不能套配方。'));
    return h('div', { class: 'edit-panel' }, ...nodes);
  }
  if (render.available === false && render.message && !String(render.reason || '').startsWith('camera_data')) {
    nodes.push(h('div', { class: 'result bad small' }, render.message));
  }

  if (!unit || !info) {
    nodes.push(h('p', { class: 'small' }, '还没读到配方列表，请稍后再试。'));
    return h('div', { class: 'edit-panel' }, ...nodes);
  }

  const lensNote = unitForPhoto(view.photo) ? `这张照片是 ${unit.label} 拍的，用它的底子。` : '认不出这张照片的镜头，先按 50mm 的底子列出。';
  nodes.push(h('p', { class: 'small dim' }, lensNote));
  if (unit.calibration_note) nodes.push(h('p', { class: 'small warn-text' }, unit.calibration_note));

  nodes.push(h('div', { class: 'field' }, h('span', { class: 'small dim' }, '8 个底子'),
    h('div', { class: 'base-grid' }, unit.recipes.map((r) => baseButton(view, r, sel.slot)))));

  const amount = info.controls.find((c) => c.name === 'amount');
  if (amount) nodes.push(amountRow(view, amount));

  nodes.push(h('div', { class: 'field' }, h('span', { class: 'small dim' }, '微调（每项 −9 到 +9，0 是原样）'),
    info.controls.filter((c) => c.name !== 'amount').map((c) => knobRow(view, c))));

  if (isLegacyCamera()) {
    nodes.push(h('p', { class: 'small dim' }, '相机的这个版本只认 ±5：对比、高光、阴影、饱和度。超出的部分写回相机前会提示你。'));
    nodes.push(h('p', { class: 'small dim' }, '褪色、冷暖、品绿、分离色调、黑白滤镜：相机的这个版本里没有，只在电脑上生效。'));
  } else {
    nodes.push(h('p', { class: 'small dim' }, '这台相机的配方版本较新，各项都能写入。'));
  }

  nodes.push(h('div', { class: 'modal-foot', style: { justifyContent: 'flex-start' } },
    resetButton(view),
    h('button', { class: 'btn', type: 'button', disabled: true, title: '从这里直接存还没做好。想把调色文件做成配方，请去上面的「配方」页。' }, '存为我的配方（即将推出）'),
  ));
  return h('div', { class: 'edit-panel' }, ...nodes);
}

function baseButton(view, r, activeSlot) {
  return h(
    'button',
    {
      class: 'btn base',
      type: 'button',
      'aria-pressed': String(r.slot === activeSlot),
      title: r.is_stock ? '原厂 04：没选配方时用它' : '',
      onclick: () => switchBase(view, r.slot),
    },
    `${r.number} ${BASE_NAMES[r.slot - 1]}`,
    r.is_stock ? h('span', { class: 'dim small' }, ' 原厂') : null,
  );
}

// 换底子：记住原来的微调，取回这个底子上次的微调（每个底子各自记住）
function switchBase(view, slot) {
  const ed = view.ed;
  const sel = selection(view);
  if (ed.recipe && sel.slot === slot) return;
  const memory = { ...ed.memory, [`${sel.unit}/${sel.slot}`]: cleanLevels(recipeInfo(view, sel), sel.levels) };
  const next = { unit: sel.unit, slot, levels: {} };
  next.levels = cleanLevels(recipeInfo(view, next), memory[`${sel.unit}/${slot}`] || {});
  ed.memory = memory;
  ed.recipe = next;
  persist(view, { recipe: recipeBody(view, { memory: true }) });
  schedulePreview(view, { final: true });
  ed.redraw?.();
}

function setLevel(view, control, value) {
  const r = ensureRecipe(view);
  r.levels[control.name] = value;
}

function fmtValue(control, v) {
  const levels = control.levels;
  const atEnd = (v === levels[levels.length - 1] && control.cap_upper) || (v === levels[0] && control.cap_lower);
  const text = control.name === 'amount' ? `${v}%` : v > 0 ? `+${v}` : `${v}`;
  return atEnd ? `${text} !` : text;
}

function tagFor(control) {
  if (control.name === 'amount') return null;
  const legacy = isLegacyCamera();
  if (NEW_TONE_KNOBS.includes(control.name)) return legacy ? '相机 ±5' : '相机';
  if (COMPUTER_ONLY_IF_LEGACY.includes(control.name)) return legacy ? '仅电脑' : '相机';
  return null;
}

function sliderRow(view, control, { label, sub }) {
  const levels = [...control.levels].sort((a, b) => a - b);
  const value = effective(control, selection(view).levels);
  const out = h('output', {}, fmtValue(control, value));
  const commit = () => {
    persist(view, { recipe: recipeBody(view, { memory: true }) });
    schedulePreview(view, { final: true });
  };
  const input = h('input', {
    type: 'range',
    min: 0,
    max: levels.length - 1,
    step: 1,
    value: levels.indexOf(value),
    'aria-label': KNOB_NAMES[control.name],
    title: '双击归零',
    oninput: (e) => {
      const v = levels[Number(e.target.value)];
      setLevel(view, control, v);
      out.textContent = fmtValue(control, v);
      schedulePreview(view);
    },
    onchange: (e) => {
      setLevel(view, control, levels[Number(e.target.value)]);
      commit();
    },
    ondblclick: () => {
      const d = control.default;
      setLevel(view, control, d);
      input.value = levels.indexOf(d);
      out.textContent = fmtValue(control, d);
      commit();
    },
  });
  const tag = tagFor(control);
  return h(
    'div',
    { class: 'knob-row' },
    h('div', { class: 'knob-head' },
      h('span', {}, label),
      tag ? h('span', { class: `knob-tag${tag === '仅电脑' || tag.startsWith('相机 ±5') ? ' soft' : ''}` }, tag) : null,
    ),
    h('div', { class: 'field-row' }, input, out),
    sub ? h('div', { class: 'small dim' }, sub) : null,
  );
}

function knobRow(view, control) {
  const name = KNOB_NAMES[control.name] || control.label;
  return sliderRow(view, control, { label: `${name}（${control.label}）` });
}

function amountRow(view, control) {
  const step = control.amount_step ? `每格 ${control.amount_step}%` : '';
  return h('div', { class: 'field' },
    sliderRow(view, control, {
      label: '整体强度',
      sub: `100% = 配方原样；0% = 原厂 04 标准。${step}`,
    }),
  );
}

// 全部归零：只清微调（不动整体强度）；第一次点变成"再点一次"，第二次才生效
function resetButton(view) {
  let armed = false;
  let timer = null;
  const btn = h('button', { class: 'btn', type: 'button', title: '清掉这个底子上的微调，整体强度保留' }, '全部归零');
  btn.addEventListener('click', () => {
    if (!armed) {
      armed = true;
      btn.textContent = '再点一次：全部归零';
      btn.classList.add('danger');
      clearTimeout(timer);
      timer = setTimeout(() => {
        armed = false;
        btn.textContent = '全部归零';
        btn.classList.remove('danger');
      }, 4000);
      return;
    }
    clearTimeout(timer);
    const r = view.ed.recipe;
    if (!r) {
      toast('这个底子还没有微调', 'info');
      return;
    }
    const kept = {};
    if (r.levels.amount !== undefined) kept.amount = r.levels.amount;
    r.levels = kept;
    persist(view, { recipe: recipeBody(view, { memory: true }) });
    schedulePreview(view, { final: true });
    view.ed.redraw?.();
  });
  return btn;
}
