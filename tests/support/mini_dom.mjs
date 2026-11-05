// 给前端模块测试用的最小 DOM：只有 Node 自带的能力（EventTarget），不依赖浏览器或第三方包。
// 够 dom.js 的 h()、弹窗、提示条和各页面拼 DOM 用；不做排版，尺寸一律给固定值。
// 用法：import './mini_dom.mjs'（装到 globalThis），再 import 被测模块。

class ClassList {
  constructor(el) { this.el = el; }
  _get() { return new Set(String(this.el.className || '').split(/\s+/).filter(Boolean)); }
  _set(s) { this.el.className = [...s].join(' '); }
  add(...n) { const s = this._get(); n.forEach((x) => s.add(x)); this._set(s); }
  remove(...n) { const s = this._get(); n.forEach((x) => s.delete(x)); this._set(s); }
  contains(n) { return this._get().has(n); }
  toggle(n, force) {
    const s = this._get();
    const on = force === undefined ? !s.has(n) : !!force;
    if (on) s.add(n); else s.delete(n);
    this._set(s);
    return on;
  }
}

export class TestNode extends EventTarget {
  constructor(tag = '', text = '') {
    super();
    this.tagName = String(tag).toUpperCase();
    this.nodeType = tag ? 1 : 3;
    this.text = text;
    this.children = [];
    this.parentNode = null;
    this.style = { setProperty(k, v) { this[k] = v; }, removeProperty(k) { delete this[k]; } };
    this.dataset = {};
    this.attrs = {};
    this.className = '';
    this.disabled = false;
    this.hidden = false;
    this.checked = false;
    this.value = '';
    this.type = '';
    this.id = '';
    this.classList = new ClassList(this);
  }
  get textContent() { return this.text + this.children.map((c) => c.textContent).join(''); }
  set textContent(v) { this.text = String(v); this.children = []; }
  get firstChild() { return this.children[0] || null; }
  get lastChild() { return this.children[this.children.length - 1] || null; }
  get firstElementChild() { return this.children.find((c) => c.nodeType === 1) || null; }
  get childElementCount() { return this.children.filter((c) => c.nodeType === 1).length; }
  get parentElement() { return this.parentNode; }
  get isConnected() { return this === document.body || this === document.documentElement || !!(this.parentNode && this.parentNode.isConnected); }
  get offsetWidth() { return 800; }
  get offsetHeight() { return 600; }
  get clientWidth() { return 800; }
  get clientHeight() { return 600; }
  _adopt(child) {
    if (child == null || child === false) return null;
    const node = typeof child === 'string' ? document.createTextNode(child) : child;
    if (node.parentNode) node.remove();
    node.parentNode = this;
    return node;
  }
  append(...children) { for (const c of children) { const n = this._adopt(c); if (n) this.children.push(n); } }
  appendChild(c) { this.append(c); return c; }
  prepend(...children) { const ns = children.map((c) => this._adopt(c)).filter(Boolean); this.children.unshift(...ns); }
  insertBefore(c, ref) {
    const n = this._adopt(c);
    const i = ref ? this.children.indexOf(ref) : -1;
    if (i < 0) this.children.push(n); else this.children.splice(i, 0, n);
    return c;
  }
  get nextElementSibling() {
    const p = this.parentNode;
    if (!p) return null;
    const sib = p.children.slice(p.children.indexOf(this) + 1);
    return sib.find((n) => n.nodeType === 1) || null;
  }
  before(...ns) { if (!this.parentNode) return; for (const n of ns) this.parentNode.insertBefore(n, this); }
  after(...ns) {
    const p = this.parentNode;
    if (!p) return;
    const i = p.children.indexOf(this);
    const ref = p.children[i + 1] || null;
    for (const n of ns) p.insertBefore(n, ref);
  }
  replaceChildren(...nodes) {
    for (const c of this.children) c.parentNode = null;
    this.children = [];
    this.append(...nodes);
  }
  replaceWith(n) { const p = this.parentNode; if (!p) return; p.insertBefore(n, this); this.remove(); }
  remove() {
    if (!this.parentNode) return;
    this.parentNode.children = this.parentNode.children.filter((c) => c !== this);
    this.parentNode = null;
  }
  removeChild(c) { c.remove(); return c; }
  contains(n) { for (let x = n; x; x = x.parentNode) if (x === this) return true; return false; }
  setAttribute(k, v) { this.attrs[k] = String(v); if (k === 'id') this.id = String(v); if (k === 'class') this.className = String(v); }
  getAttribute(k) {
    if (k === 'id') return this.id || null;
    if (k === 'class') return this.className || null;
    return Object.prototype.hasOwnProperty.call(this.attrs, k) ? this.attrs[k] : null;
  }
  hasAttribute(k) { return this.getAttribute(k) !== null; }
  removeAttribute(k) { delete this.attrs[k]; }
  toggleAttribute(k, force) { const on = force === undefined ? !this.hasAttribute(k) : force; if (on) this.setAttribute(k, ''); else this.removeAttribute(k); return on; }
  focus() { document.activeElement = this; this.dispatchEvent(new Event('focus')); }
  blur() { if (document.activeElement === this) document.activeElement = document.body; }
  click() { if (!this.disabled) this.dispatchEvent(new Event('click', { bubbles: true, cancelable: true })); }
  scrollIntoView() {}
  scrollTo() {}
  setPointerCapture() {}
  releasePointerCapture() {}
  animate() { return { finished: Promise.resolve(), cancel() {}, onfinish: null }; }
  getBoundingClientRect() { return { x: 0, y: 0, left: 0, top: 0, width: 800, height: 600, right: 800, bottom: 600 }; }
  getContext() { return null; }
  decode() { return Promise.resolve(); }
  dispatchEvent(event) {
    const r = super.dispatchEvent(event);
    if (event.bubbles && !event.cancelBubble && this.parentNode) this.parentNode.dispatchEvent(event);
    return r;
  }
  // 选择器只认：tag、#id、.class、[attr]、[attr="v"]，以及它们的组合；逗号分组；不认后代组合
  matches(sel) { return sel.split(',').some((s) => matchOne(this, s.trim())); }
  closest(sel) { for (let x = this; x && x.nodeType === 1; x = x.parentNode) if (x.matches(sel)) return x; return null; }
  querySelectorAll(sel) {
    const out = [];
    const walk = (n) => { for (const c of n.children) { if (c.nodeType === 1 && c.matches(sel)) out.push(c); walk(c); } };
    walk(this);
    return out;
  }
  querySelector(sel) { return this.querySelectorAll(sel)[0] || null; }
}

function matchOne(el, sel) {
  const last = sel.split(/\s+/).pop();
  const re = /([#.]?)([\w-]+)|\[([\w-]+)(?:="?([^"\]]*)"?)?\]/g;
  let m;
  let any = false;
  while ((m = re.exec(last))) {
    any = true;
    if (m[3]) {
      const v = el.getAttribute(m[3]) ?? (m[3].startsWith('data-') ? el.dataset[m[3].slice(5).replace(/-(\w)/g, (_, c) => c.toUpperCase())] ?? null : null);
      if (v === null) return false;
      if (m[4] !== undefined && String(v) !== m[4]) return false;
    } else if (m[1] === '#') { if (el.id !== m[2]) return false; }
    else if (m[1] === '.') { if (!el.classList.contains(m[2])) return false; }
    else if (el.tagName !== m[2].toUpperCase()) return false;
  }
  return any;
}

export function installDom() {
  globalThis.Node = globalThis.HTMLElement = globalThis.Element = TestNode;
  globalThis.HTMLInputElement = TestNode;
  const body = new TestNode('body');
  const html = new TestNode('html');
  html.append(body);
  globalThis.document = {
    body,
    documentElement: html,
    activeElement: body,
    hidden: false,
    visibilityState: 'visible',
    createElement: (tag) => new TestNode(tag),
    createElementNS: (_ns, tag) => new TestNode(tag),
    createTextNode: (t) => new TestNode('', t),
    createDocumentFragment: () => new TestNode('#fragment'),
    getElementById: (id) => body.querySelector(`#${id}`),
    querySelector: (s) => body.querySelector(s),
    querySelectorAll: (s) => body.querySelectorAll(s),
    _l: new EventTarget(),
    addEventListener(...a) { this._l.addEventListener(...a); },
    removeEventListener(...a) { this._l.removeEventListener(...a); },
    dispatchEvent(e) { return this._l.dispatchEvent(e); },
  };
  const win = new EventTarget();
  globalThis.window = globalThis;
  globalThis.addEventListener = win.addEventListener.bind(win);
  globalThis.removeEventListener = win.removeEventListener.bind(win);
  globalThis.location = { hash: '#/photos', pathname: '/' };
  globalThis.matchMedia = () => ({ matches: false, addEventListener() {}, removeEventListener() {} });
  globalThis.requestAnimationFrame = (fn) => setTimeout(() => fn(Date.now()), 0);
  globalThis.cancelAnimationFrame = (t) => clearTimeout(t);
  globalThis.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} };
  globalThis.IntersectionObserver = class { constructor(cb) { this.cb = cb; } observe() {} unobserve() {} disconnect() {} };
  globalThis.getComputedStyle = () => ({ getPropertyValue: () => '' });
  globalThis.localStorage = (() => { const m = new Map(); return { getItem: (k) => (m.has(k) ? m.get(k) : null), setItem: (k, v) => m.set(k, String(v)), removeItem: (k) => m.delete(k) }; })();
  if (!globalThis.navigator) Object.defineProperty(globalThis, 'navigator', { value: { platform: 'MacIntel', userAgent: 'node' }, configurable: true, writable: true });
  URL.createObjectURL = () => `blob:${Math.random().toString(36).slice(2)}`;
  URL.revokeObjectURL = () => {};
  for (const id of ['view', 'modal-root', 'toast-root']) {
    const n = new TestNode('div');
    n.id = id;
    body.append(n);
  }
  return document;
}

export const textOf = (n) => (n ? n.textContent : '');

// 测试里控制请求什么时候返回：hold(key) 返回一个挂起的 Promise，release(key, value) / fail(key, err) 放行
export function gate() {
  const waiting = new Map();
  return {
    hold(key) {
      return new Promise((resolve, reject) => {
        if (!waiting.has(key)) waiting.set(key, []);
        waiting.get(key).push({ resolve, reject });
      });
    },
    has(key) { return (waiting.get(key) || []).length > 0; },
    release(key, value) { const q = waiting.get(key) || []; const w = q.shift(); if (!w) throw new Error(`没有挂起的请求：${key}`); w.resolve(value); },
    fail(key, err) { const q = waiting.get(key) || []; const w = q.shift(); if (!w) throw new Error(`没有挂起的请求：${key}`); w.reject(err); },
  };
}

export const tick = async (n = 5) => { for (let i = 0; i < n; i++) await new Promise((r) => setTimeout(r, 0)); };
