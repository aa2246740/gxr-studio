// 小工具：创建元素、短提示、弹窗。没有框架，界面全部是手写的 DOM。

export function h(tag, props = {}, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(props || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === 'class') el.className = v;
    else if (k === 'style') Object.assign(el.style, v);
    else if (k === 'dataset') Object.assign(el.dataset, v);
    else if (k.startsWith('on') && typeof v === 'function') el.addEventListener(k.slice(2).toLowerCase(), v);
    else if (typeof v === 'boolean') {
      if (k in el) el[k] = v;
      else if (v) el.setAttribute(k, '');
    } else if (k in el && k !== 'list' && k !== 'form') el[k] = v;
    else el.setAttribute(k, String(v));
  }
  for (const c of children.flat()) {
    if (c === null || c === undefined || c === false) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return el;
}

export function toast(message, kind = 'info') {
  const root = document.getElementById('toast-root');
  const el = h('div', { class: `toast ${kind}`, role: 'status' }, message);
  root.append(el);
  setTimeout(() => el.remove(), kind === 'error' ? 6000 : 3200);
}

const modalStack = [];

export function modalOpen() {
  return modalStack.length > 0;
}

// 弹窗：不挡住整个界面的内容以外的交互只在这里用；Esc 关闭最上面的一个。
export function openModal(title, body, { wide = false, onClose } = {}) {
  const close = () => {
    const i = modalStack.findIndex((m) => m.close === close);
    if (i >= 0) modalStack.splice(i, 1);
    overlay.remove();
    if (onClose) onClose();
  };
  const overlay = h(
    'div',
    { class: 'modal-overlay', onclick: (e) => e.target === overlay && close() },
    h(
      'div',
      { class: `modal${wide ? ' wide' : ''}`, role: 'dialog', 'aria-modal': 'true', 'aria-label': title },
      h('div', { class: 'modal-head' }, h('h2', {}, title), h('button', { class: 'btn ghost', type: 'button', onclick: close, title: '关闭（Esc）' }, '✕')),
      h('div', { class: 'modal-body' }, body),
    ),
  );
  document.getElementById('modal-root').append(overlay);
  modalStack.push({ close });
  return close;
}

export function closeTopModal() {
  const top = modalStack[modalStack.length - 1];
  if (top) top.close();
}

export function isTyping(target) {
  return !!target && (target.isContentEditable || ['INPUT', 'TEXTAREA', 'SELECT'].includes(target.tagName));
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

// "2026-10-30T09:13:00" -> "10-30 09:13"
export function fmtTime(iso) {
  if (!iso) return '未知时间';
  const m = /^\d{4}-(\d{2}-\d{2})T(\d{2}:\d{2})/.exec(iso);
  return m ? `${m[1]} ${m[2]}` : iso;
}

export function fmtDate(date) {
  return date === 'unknown' ? '未知日期' : date;
}
