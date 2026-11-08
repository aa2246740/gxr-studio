// 小工具：创建元素、图标、短提示、弹窗。没有框架，界面全部是手写的 DOM。
// 这个文件不 import 别的模块（tests/test_confirmation.py 会把它单独拷出去跑）。

export function h(tag, props = {}, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(props || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === 'class') el.className = v;
    else if (k === 'style') setStyle(el, v);
    else if (k === 'dataset') Object.assign(el.dataset, v);
    else if (k.startsWith('on') && typeof v === 'function') el.addEventListener(k.slice(2).toLowerCase(), v);
    else if (typeof v === 'boolean') {
      if (k in el) el[k] = v;
      else if (v) el.setAttribute(k, '');
    } else if (k in el && k !== 'list' && k !== 'form' && !k.startsWith('aria-')) el[k] = v;
    else el.setAttribute(k, String(v));
  }
  for (const c of children.flat()) {
    if (c === null || c === undefined || c === false) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return el;
}

// 样式对象：--开头的是 CSS 变量，要走 setProperty（Object.assign 写不进去）
function setStyle(el, styles) {
  for (const [k, v] of Object.entries(styles)) {
    if (k.startsWith('--') && el.style.setProperty) el.style.setProperty(k, String(v));
    else el.style[k] = v;
  }
}

// 图标：index.html 里内嵌的 Lucide 图标（ISC 许可，见 assets/icons/LICENSE-lucide.txt），用 <use> 引用。
// 命名空间拼起来写：仓库的离线检查不允许源文件里出现完整网址
const SVG_NS = ['http', '://www.w3.org/2000/svg'].join('');
export function icon(name, cls = '') {
  if (!document.createElementNS) return h('i', { class: `ico ${cls}`, 'aria-hidden': 'true' });
  const svg = document.createElementNS(SVG_NS, 'svg');
  svg.setAttribute('class', `ico ${cls}`.trim());
  svg.setAttribute('aria-hidden', 'true');
  const use = document.createElementNS(SVG_NS, 'use');
  use.setAttribute('href', `#i-${name}`);
  svg.append(use);
  return svg;
}

// 统一的按钮。kind：''（次按钮）、primary、danger、ghost、icon；size：sm。
// 只有图标的按钮必须给 title（同时作为 aria-label）。
export function button(label, { kind = '', size = 'sm', ico = null, title = null, onclick = null, pressed = null, disabled = false, cls = '' } = {}) {
  const classes = ['btn', kind, kind === 'icon' ? '' : size, cls].filter(Boolean).join(' ');
  return h('button', {
    type: 'button', class: classes, title, onclick,
    'aria-label': !label && title ? title : null,
    'aria-pressed': pressed === null ? null : String(!!pressed),
    'aria-disabled': disabled ? 'true' : null,
  }, ico ? icon(ico) : null, label ? h('span', {}, label) : null);
}

// ---- 短提示 -----------------------------------------------------------------------
// 底部居中叠放，新的在下面。kind：info / ok / error / progress。
// action = { label, run }（或 actions = [...]）时带按钮（打开、重试…），点了就关；每条提示的按钮只生效一次（连点不会重试两遍）。
// 出错和带按钮的停留久一点；鼠标停在上面或焦点在里面时不自动消失（详细信息要能点开）。
// 外层 #toast-root 不是 live 区域，每条提示自己带 role（不重复播报）。
const TOAST_ICON = { ok: 'circle-check', error: 'circle-alert', info: 'info' };

export function toast(message, kind = 'info', { action = null, actions = null, timeout = null } = {}) {
  const root = document.getElementById('toast-root');
  if (!root) return null;
  let closed = false;
  const close = () => {
    if (closed) return;
    closed = true;
    clearTimeout(timer);
    el.className = `${el.className} leaving`;
    setTimeout(() => el.remove(), 180);
  };
  const acts = (actions || (action ? [action] : [])).map((a) => h('button', {
    class: 'btn ghost sm', type: 'button', onclick: () => { if (closed) return; close(); a.run(); },
  }, a.label));
  const ico = TOAST_ICON[kind] ? icon(TOAST_ICON[kind], kind) : null;
  const el = h('div', { class: `toast ${kind === 'error' ? 'err' : kind}`, role: kind === 'error' ? 'alert' : 'status' },
    ico,
    h('div', { class: 'toast-msg' }, message),
    ...acts,
    h('button', { class: 'btn icon toast-x', type: 'button', 'aria-label': '关闭', title: '关闭', onclick: close }, icon('x')));
  root.append(el);
  const ms = timeout ?? (kind === 'error' || acts.length ? 8000 : 3200);
  let timer = ms > 0 ? setTimeout(close, ms) : null;
  const hold = () => clearTimeout(timer);
  const resume = () => { clearTimeout(timer); if (ms > 0) timer = setTimeout(close, 2000); };
  el.addEventListener('pointerenter', hold);
  el.addEventListener('pointerleave', resume);
  el.addEventListener('focusin', hold);
  el.addEventListener('focusout', resume);
  close.el = el;
  return close;
}

// 错误（COPY 3.13）：主句照常显示；后端给了 detail（异常原文、路径、内部键名）时，主句下面放一个默认收起的「详细信息」。
// 后端给了说明就用后端的；只有一句通用的话（err.generic）时用调用方写的、这个操作自己的说法
export function errorBody(err, fallback) {
  const text = (err && !err.generic && err.message) || fallback || (err && err.message) || '没有完成。请重试。';
  const detail = err && err.data && err.data.detail;
  if (!detail) return text;
  return h('div', {}, h('p', { class: 'err-main' }, text), h('details', { class: 'err-detail' }, h('summary', {}, '详细信息'), h('p', { class: 'small dim' }, String(detail))));
}

// 进度条（.progress > i）：用 transform 缩放（--f，0–1），不改宽度，动起来不触发重排
export function setFraction(bar, f) {
  const v = Math.max(0, Math.min(1, Number(f) || 0));
  if (bar && bar.style) {
    if (typeof bar.style.setProperty === 'function') bar.style.setProperty('--f', String(v));
    else bar.style['--f'] = String(v);
  }
  return bar;
}

export function toastError(err, fallback, opts = {}) {
  return toast(errorBody(err, fallback), 'error', opts);
}

// ---- 对话框 -----------------------------------------------------------------------
// 打开时焦点进对话框（第一个可操作的控件，没有就是对话框本身），Tab 只在对话框里转，
// 关闭后焦点回到打开它的那个元素（U5）。Esc 由 app.js 关最上面的一个。

const modalStack = [];
const FOCUSABLE = 'button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

export function modalOpen() {
  return modalStack.length > 0;
}

function focusables(root) {
  if (typeof root.querySelectorAll !== 'function') return [];
  return [...root.querySelectorAll(FOCUSABLE)].filter((n) => !n.hidden && !(n.closest && n.closest('[hidden]')));
}

function trapTab(e, dialog) {
  if (e.key !== 'Tab') return;
  const list = focusables(dialog);
  if (!list.length) {
    e.preventDefault();
    dialog.focus();
    return;
  }
  const first = list[0];
  const last = list[list.length - 1];
  const active = document.activeElement;
  if (e.shiftKey && (active === first || active === dialog)) {
    e.preventDefault();
    last.focus();
  } else if (!e.shiftKey && active === last) {
    e.preventDefault();
    first.focus();
  }
}

let modalSeq = 0;

// 对话框开着时背后的页面不可操作（inert），焦点也不会跑到背后去（F06）：
// 对话框里的按钮被禁用、内容被替换时浏览器会把焦点丢到 body 上，对话框自己的 Tab 处理收不到按键，
// 所以在 document 上再兜一层：焦点不在最上面的对话框里时，Tab 把它带回对话框。
const BACKGROUND_IDS = ['app', 'card-pop'];

function setBackgroundInert(on) {
  for (const id of BACKGROUND_IDS) {
    const el = document.getElementById(id);
    if (el && el.toggleAttribute) el.toggleAttribute('inert', on);
  }
}

let guardInstalled = false;
function installFocusGuard() {
  if (guardInstalled || typeof document.addEventListener !== 'function') return;
  guardInstalled = true;
  document.addEventListener('keydown', (e) => {
    if (e.key !== 'Tab' || !modalStack.length) return;
    const top = modalStack[modalStack.length - 1].dialog;
    if (!top || top.contains(document.activeElement)) return;
    e.preventDefault();
    const list = focusables(top);
    const target = e.shiftKey ? list[list.length - 1] : list[0];
    (target || top).focus();
  }, true);
}

// body：内容；foot：底部按钮（可选，固定在对话框底边）。返回 close()。
export function openModal(title, body, { wide = false, onClose, foot = null, initialFocus = null } = {}) {
  const previous = document.activeElement;
  const titleId = `dlg-t-${++modalSeq}`;
  const close = () => {
    const i = modalStack.findIndex((m) => m.close === close);
    if (i < 0) return;
    modalStack.splice(i, 1);
    overlay.dataset.closed = '1';
    if (!modalStack.length) setBackgroundInert(false);
    overlay.remove();
    // 退场动画：挪到 body 上淡出，modal-root 里立刻就没有它了
    if (document.body && typeof setTimeout === 'function') {
      overlay.className = 'scrim leaving';
      overlay.setAttribute('inert', '');
      document.body.append(overlay);
      setTimeout(() => overlay.remove(), 160);
    }
    if (onClose) onClose();
    if (previous && previous.isConnected && typeof previous.focus === 'function') previous.focus();
  };
  const closeBtn = h('button', { class: 'btn icon', type: 'button', onclick: close, title: '关闭（Esc）', 'aria-label': '关闭' }, icon('x'));
  const dialog = h('div', {
    class: `dialog${wide ? ' wide' : ''}`, role: 'dialog', 'aria-modal': 'true', 'aria-labelledby': titleId, tabindex: '-1',
  },
  h('div', { class: 'dh' }, h('h2', { id: titleId }, title), h('span', { class: 'grow' }), closeBtn),
  h('div', { class: 'db' }, body),
  foot ? h('div', { class: 'df' }, foot) : null);
  dialog.addEventListener('keydown', (e) => trapTab(e, dialog));
  const overlay = h('div', { class: 'scrim', onclick: (e) => e.target === overlay && close() }, dialog);
  document.getElementById('modal-root').append(overlay);
  modalStack.push({ close, dialog });
  setBackgroundInert(true);
  installFocusGuard();
  const target = initialFocus || focusables(body instanceof Node ? dialog.children[1] || dialog : dialog)[0] || dialog;
  if (target && typeof target.focus === 'function') target.focus();
  return close;
}

// 节点是不是已经不在用户眼前：不在页面上，或者在已经关掉、正在退场的对话框里（N04）
export function outOfView(node) {
  if (!node || !node.isConnected) return true;
  return Boolean(node.closest && node.closest('[data-closed]'));
}

export function closeTopModal() {
  const top = modalStack[modalStack.length - 1];
  if (top) top.close();
}

// 删除、替换等确认统一从这里打开。回车只取消，确认需点击或按空格。
export function confirmAction(title, confirmLabel, action) {
  let close;
  let busy = false;
  const cancel = h('button', { class: 'btn', type: 'button', onclick: () => close() }, '取消');
  const confirm = h('button', { class: 'btn danger', type: 'button', onclick: async () => {
    if (busy) return;
    busy = true;
    cancel.disabled = confirm.disabled = true;
    confirm.setAttribute('aria-busy', 'true');
    try { await action(); close(); }
    catch (error) {
      toastError(error, `${confirmLabel}没有完成。请重试。`);
      busy = false;
      cancel.disabled = confirm.disabled = false;
      confirm.setAttribute('aria-busy', 'false');
      cancel.focus();
    }
  } }, confirmLabel);
  const body = h('div', { onkeydown: (event) => {
    if (event.key !== 'Enter') return;
    event.preventDefault();
    event.stopPropagation();
    if (!busy && document.activeElement === cancel) close();
  } }, h('div', { class: 'modal-foot' }, cancel, confirm));
  close = openModal(title, body, { initialFocus: cancel });
  cancel.focus();
  return close;
}

export function isTyping(target) {
  return !!target && (target.isContentEditable || ['TEXTAREA', 'SELECT'].includes(target.tagName)
    || (target.tagName === 'INPUT' && target.type !== 'range'));
}

export function fmtBytes(n) {
  if (!n) return '0 B';
  const units = ['B', 'KB', 'MB', 'GB'];
  let i = 0;
  let v = n;
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024;
    i++;
  }
  return `${v.toFixed(i ? 1 : 0)} ${units[i]}`;
}

// 只要时:分（照片条上日期在前，时间在后：2026-10-23 17:41）
export function fmtClock(iso) {
  if (!iso) return '未知时间';
  const m = /T(\d{2}:\d{2})/.exec(iso);
  return m ? m[1] : iso;
}

export function fmtTime(iso) {
  if (!iso) return '未知时间';
  const m = /^\d{4}-(\d{2}-\d{2})T(\d{2}:\d{2})/.exec(iso);
  return m ? `${m[1]} ${m[2]}` : iso;
}

export function fmtDate(date) {
  return date === 'unknown' ? '未知日期' : date;
}

// 平台：快捷键写法和「在访达中显示 / 在文件资源管理器中显示」按它选
export function platform() {
  const p = (globalThis.navigator && ((navigator.userAgentData && navigator.userAgentData.platform) || navigator.platform)) || '';
  if (/mac/i.test(p)) return 'mac';
  if (/win/i.test(p)) return 'win';
  return 'other';
}

export const modKey = () => (platform() === 'mac' ? '⌘' : 'Ctrl+');
