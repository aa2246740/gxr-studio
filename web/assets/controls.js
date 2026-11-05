// 侧栏里的共用控件：滑块（双击重置、两端说明）、分段按钮、开关、提示卡、「还没准备好」。
// 滑块的 onBegin / onInput / onCommit：拖动开始、拖动中（只改画面）、松手（保存）。

import { h } from './dom.js';

// 轨道上的蓝色段：中间为 0 的滑块从 0 画到当前值，其它从默认值画到当前值。只改两个 CSS 变量，不重建节点。
export function fillRange(min, max, value, dflt = 0) {
  const span = max - min || 1;
  const pct = (x) => Math.max(0, Math.min(100, ((x - min) / span) * 100));
  const origin = min < 0 && max > 0 ? 0 : Math.max(min, Math.min(max, dflt));
  const a = pct(origin);
  const b = pct(value);
  return { lo: `${Math.min(a, b)}%`, hi: `${Math.max(a, b)}%`, at: b };
}

// onCancel：按下（或按键）又松开、值没变——不会有 change，也就没有 onCommit；这一步在这里结束（X01）
export function slider({ label, min, max, step, value, fmt = String, ends = null, dflt = 0, disabled = false, onBegin, onInput, onCommit, onCancel, onDblReset }) {
  const out = h('output', {}, fmt(value));
  const input = h('input', { type: 'range', min, max, step, value, 'aria-label': label, title: disabled ? '' : '双击重置' });
  const bubble = h('span', { class: 'bubble', 'aria-hidden': 'true' }, fmt(value));
  if (disabled) {
    input.disabled = true;
    input.setAttribute('aria-disabled', 'true');
  }
  let touched = false;
  let startValue = null;
  const begin = () => {
    if (!touched) startValue = input.value;
    touched = true;
    if (onBegin) onBegin();
  };
  input.addEventListener('pointerdown', () => {
    if (disabled) return;
    el.classList.add('dragging');
    begin();
  });
  // 值没变就结束这一步；变了的等 change / keyup 去保存
  const settle = () => {
    if (!touched || input.value !== startValue) return;
    touched = false;
    if (onCancel) onCancel();
  };
  const endDrag = () => {
    el.classList.remove('dragging');
    settle();
  };
  input.addEventListener('pointerup', endDrag);
  input.addEventListener('pointercancel', endDrag);
  input.addEventListener('blur', () => {
    endDrag();
    finish(); // 改了值、还没等到 keyup 就离开（例如按住方向键时点了别处）：照样保存
  });
  input.addEventListener('keydown', () => {
    if (disabled || touched) return;
    begin();
  });
  // 拖动反馈不等任何东西：数字、气泡、轨道同一帧更新（画面由 onInput 自己决定怎么跟）
  input.addEventListener('input', () => {
    const v = Number(input.value);
    paint(v);
    if (onInput) onInput(v);
  });
  const finish = () => {
    if (!touched) return;
    touched = false;
    if (onCommit) onCommit(Number(input.value));
  };
  input.addEventListener('change', finish);
  input.addEventListener('keyup', finish);
  input.addEventListener('dblclick', () => {
    if (disabled) return;
    input.value = dflt;
    paint(dflt);
    if (onBegin) onBegin();
    if (onCommit) onCommit(dflt);
    if (onDblReset) onDblReset(dflt);
  });
  const endRow = ends ? h('div', { class: 'ends' }, h('span', {}, ends[0] || ''), h('span', {}, ends[1] || '')) : null;
  const el = h('div', { class: `row${disabled ? ' dimmed' : ''}` }, h('div', { class: 'lab' }, h('span', {}, label), out), input, endRow, bubble);
  function paint(v) {
    const text = fmt(v);
    out.textContent = text;
    bubble.textContent = text;
    const f = fillRange(Number(min), Number(max), v, dflt);
    if (input.style.setProperty) {
      input.style.setProperty('--lo', f.lo);
      input.style.setProperty('--hi', f.hi);
    }
    // 气泡跟着滑块走：轨道从标签列之后开始（76px + 8px 间距），右边留数值列
    bubble.style.left = `calc(84px + (100% - 144px) * ${f.at / 100})`;
    el.classList.toggle('changed', Number(v) !== Number(dflt));
  }
  paint(Number(value));
  return {
    el,
    input,
    setValue(v) {
      input.value = v;
      paint(Number(v));
    },
  };
}

// 分段按钮：options = [[id, text], ...]
export function seg(options, value, onPick, { disabled = false, label = null } = {}) {
  const buttons = options.map(([id, text]) => {
    const b = h('button', { type: 'button', 'aria-pressed': String(value === id), 'aria-disabled': disabled ? 'true' : null }, text);
    b.addEventListener('click', () => {
      if (disabled) return;
      onPick(id);
    });
    return b;
  });
  const group = h('div', { class: 'seg', role: 'group', 'aria-label': label || null }, ...buttons);
  return label ? h('div', { class: 'seg-row' }, h('span', { class: 'seg-label' }, label), group) : group;
}

export function toggle(label, checked, onChange, { disabled = false } = {}) {
  const input = h('input', { type: 'checkbox', class: 'switch-input', role: 'switch', checked, 'aria-label': label });
  if (disabled) input.disabled = true;
  input.addEventListener('change', () => onChange(input.checked));
  return h('label', { class: 'switch-row' }, h('span', {}, label), input);
}

// 提示卡：kind 是 'amber'（需要知道，但不算失败）或 'grey'（用不了，说明原因）
export function notice(kind, ...children) {
  return h('div', { class: `notice ${kind === 'grey' ? 'grey' : 'amber'}` }, ...children);
}

// 新接口不在时：一行「暂不可用」。不写假选项，也不写说明句。
export function notReady(what) {
  return h('div', { class: 'notready' }, `${what}暂不可用`);
}

export function sectionTitle(text) {
  return h('div', { class: 'sec' }, text);
}

// 数字的显示：正数带 +
export const fmtSigned = (v) => (v > 0 ? `+${v}` : `${v}`);

export function swatch(hex, pressed, onClick, label) {
  return h('button', { type: 'button', class: 'sw', 'aria-pressed': String(pressed), 'aria-label': label, title: label, style: { background: hex }, onclick: onClick });
}

