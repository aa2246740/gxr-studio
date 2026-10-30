// 侧栏里的共用控件：滑块（双击重置、两端说明）、分段按钮、开关、提示卡、「还没准备好」。
// 滑块的 onBegin / onInput / onCommit：拖动开始、拖动中（只改画面）、松手（保存）。

import { h } from './dom.js';

export function slider({ label, min, max, step, value, fmt = String, ends = null, dflt = 0, disabled = false, onBegin, onInput, onCommit, onDblReset }) {
  const out = h('output', {}, fmt(value));
  const input = h('input', { type: 'range', min, max, step, value, 'aria-label': label, title: disabled ? '' : '双击重置' });
  if (disabled) {
    input.disabled = true;
    input.setAttribute('aria-disabled', 'true');
  }
  let touched = false;
  input.addEventListener('pointerdown', () => {
    if (disabled) return;
    touched = true;
    if (onBegin) onBegin();
  });
  input.addEventListener('keydown', () => {
    if (disabled || touched) return;
    if (onBegin) onBegin();
    touched = true;
  });
  input.addEventListener('input', () => {
    const v = Number(input.value);
    out.textContent = fmt(v);
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
    out.textContent = fmt(dflt);
    if (onBegin) onBegin();
    if (onCommit) onCommit(dflt);
    if (onDblReset) onDblReset(dflt);
  });
  const endRow = ends ? h('div', { class: 'ends' }, h('span', {}, ends[0] || ''), h('span', {}, ends[1] || '')) : null;
  const el = h('div', { class: `row${disabled ? ' dimmed' : ''}` }, h('div', { class: 'lab' }, h('span', {}, label), out), input, endRow);
  return {
    el,
    setValue(v) {
      input.value = v;
      out.textContent = fmt(v);
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
  return h('div', { class: 'seg' }, label ? h('span', { class: 'small dim seg-label' }, label) : null, ...buttons);
}

export function toggle(label, checked, onChange, { disabled = false } = {}) {
  const input = h('input', { type: 'checkbox', checked, 'aria-label': label });
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

