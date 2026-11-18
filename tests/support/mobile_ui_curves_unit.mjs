// 曲线：点的规则、预览 JSON、调整组的复制粘贴和撤销、拖动中断掉。Node 直接跑 TypeScript。

import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import { registerHooks } from 'node:module';

registerHooks({
  resolve(spec, ctx, next) {
    if (spec.endsWith('.css')) return { shortCircuit: true, url: 'data:text/javascript,export default ""' };
    try {
      return next(spec, ctx);
    } catch (err) {
      if (spec.startsWith('.') && !/\.[a-z]+$/.test(spec)) return next(`${spec}.ts`, ctx);
      throw err;
    }
  },
});

function makeNode(tag) {
  const node = {
    tag,
    className: '',
    textContent: '',
    type: '',
    disabled: false,
    title: '',
    style: {},
    dataset: {},
    attrs: {},
    children: [],
    listeners: {},
    setAttribute(k, v) {
      this.attrs[k] = String(v);
      if (k === 'class') this.className = String(v);
    },
    getAttribute(k) {
      return Object.prototype.hasOwnProperty.call(this.attrs, k) ? this.attrs[k] : null;
    },
    append(...kids) {
      for (const kid of kids) this.children.push(kid);
    },
    replaceChildren(...kids) {
      this.children = [];
      if (kids.length) this.append(...kids);
    },
    addEventListener(type, fn) {
      (this.listeners[type] ??= []).push(fn);
    },
    removeEventListener(type, fn) {
      this.listeners[type] = (this.listeners[type] ?? []).filter((item) => item !== fn);
    },
    dispatch(type, ev) {
      for (const fn of [...(this.listeners[type] ?? [])]) fn(ev);
    },
    setPointerCapture() {},
    getBoundingClientRect() {
      return { left: 0, top: 0, width: 255, height: 255 };
    },
    querySelector(sel) {
      const found = [];
      walk(this, sel, found, true);
      return found[0] ?? null;
    },
    querySelectorAll(sel) {
      const found = [];
      walk(this, sel, found, false);
      return found;
    },
  };
  node.classList = {
    toggle(name, on) {
      const parts = new Set(node.className.split(/\s+/).filter(Boolean));
      if (on) parts.add(name);
      else parts.delete(name);
      node.className = [...parts].join(' ');
      return on;
    },
    contains(name) {
      return node.className.split(/\s+/).includes(name);
    },
  };
  return node;
}

function matches(node, sel) {
  const attr = sel.match(/^\[([\w-]+)="([^"]+)"\]$/);
  if (attr) return node.getAttribute(attr[1]) === attr[2];
  const parts = sel.split('.');
  const tag = parts[0];
  const classes = parts.slice(1);
  if (tag && node.tag !== tag) return false;
  return classes.every((name) => node.className.split(/\s+/).includes(name));
}

function walk(node, sel, found, stop) {
  for (const kid of node.children ?? []) {
    if (matches(kid, sel)) {
      found.push(kid);
      if (stop) return;
    }
    walk(kid, sel, found, stop);
    if (stop && found.length) return;
  }
}

const pending = new Map();
let nextFrame = 1;
globalThis.requestAnimationFrame = (fn) => {
  const id = nextFrame;
  nextFrame += 1;
  pending.set(id, fn);
  return id;
};
globalThis.cancelAnimationFrame = (id) => {
  pending.delete(id);
};
function flush() {
  const jobs = [...pending.values()];
  pending.clear();
  for (const fn of jobs) fn();
}

globalThis.document = {
  createElement: (tag) => makeNode(tag),
  createElementNS: (_ns, tag) => makeNode(tag),
};

const { buildSide, curveJson } = await import('../../mobile/src/lib/export_request.ts');
const {
  CURVE_MAX,
  CurvesView,
  channelPoints,
  clampMoved,
  previewCurve,
  tryInsert,
  touchHit,
} = await import('../../mobile/src/app/curves_view.ts');
const { PasteUndo, adjustIsSet, copyFrom, curveSummary, pasteOne, undoBatch } = await import('../../mobile/src/lib/paste.ts');

function lines(rel) {
  const text = readFileSync(new URL(rel, import.meta.url), 'utf8');
  return text.split('\n').length - (text.endsWith('\n') ? 1 : 0);
}

const ZERO = {
  exposure_ev: 0, contrast: 0, highlight: 0, shadow: 0, black_point: 0, warm: 0, tint: 0, saturation: 0, vibrance: 0, vignette: 0,
};

function basePhoto(over = {}) {
  return { knobs: { ...ZERO }, levels: new Map(), memory: new Map(), choice: null, lens: null, ...over };
}

function knobs(over = {}) {
  return { ...ZERO, ...over };
}

function editable(id, over = {}) {
  return { id, rev: 0, choice: null, levels: new Map(), memory: new Map(), knobs: knobs(), ...over };
}

test('curveJson matches the desktop: identity channels drop out, all identity is null', () => {
  const s = [[0, 0], [64, 32], [192, 220], [255, 255]];
  assert.deepEqual(curveJson({ master: s, r: [[0, 0], [128, 128], [255, 255]], g: null }), { master: s.map((p) => [...p]) });
  assert.equal(curveJson({ master: [[0, 0], [255, 255]] }), null);
  assert.equal(curveJson(null), null);
  const side = buildSide(basePhoto({ curve: { master: s } }), []);
  assert.deepEqual(side.curve, { master: s });
  assert.equal('adjust' in side, false);
  const identity = buildSide(basePhoto({ curve: { master: [[0, 0], [10, 10], [255, 255]] } }), []);
  const none = buildSide(basePhoto(), []);
  assert.deepEqual(identity, none);
  assert.equal('curve' in identity, false);
});

test('previewCurve is what setLook receives, and holding drops it', () => {
  const curve = { r: [[0, 0], [128, 40], [255, 255]] };
  assert.deepEqual(previewCurve(curve, false), curve);
  assert.equal(previewCurve(curve, true), null);
  assert.equal(previewCurve({ master: [[0, 0], [255, 255]] }, false), null);
});

test('point rules: clamp, duplicate x, and the 16 point cap', () => {
  const pts = [[0, 0], [100, 80], [255, 255]];
  assert.deepEqual(clampMoved(pts, 0, 40, 10), [0, 10]);
  assert.deepEqual(clampMoved(pts, 2, 10, 300), [255, 255]);
  assert.deepEqual(clampMoved(pts, 1, 0, -4), [1, 0]);
  assert.deepEqual(clampMoved(pts, 1, 254, 20), [254, 20]);
  const packed = channelPoints(null, 'master');
  while (packed.length < CURVE_MAX) {
    const x = (packed.length - 1) * 16;
    assert.equal(typeof tryInsert(packed, [x, x]), 'number');
  }
  assert.equal(packed.length, CURVE_MAX);
  assert.equal(tryInsert(packed, [8, 8]), null);
  assert.equal(tryInsert([[0, 0], [255, 255]], [0, 20]), null);
  assert.equal(touchHit(0), 14);
  assert.ok(touchHit(200) > 14);
});

function harness(curve = null) {
  const photo = { id: 'p', rev: 0, curve };
  let current = photo;
  let dragging = false;
  const pushes = [];
  const host = {
    photo: () => current,
    push() {
      pushes.push(structuredClone(current.curve));
    },
    enterDrag() {
      dragging = true;
    },
    leaveDrag() {
      dragging = false;
    },
    dragging: () => dragging,
  };
  const view = new CurvesView(host);
  const svg = view.panelEl().querySelector('svg.curve');
  const at = (x, y) => ({ clientX: x, clientY: 255 - y, button: 0, pointerId: 1, preventDefault() {} });
  return {
    photo,
    pushes,
    view,
    svg,
    get dragging() {
      return dragging;
    },
    setCurrent(next) {
      current = next;
    },
    down(x, y) {
      svg.dispatch('pointerdown', at(x, y));
    },
    move(x, y) {
      svg.dispatch('pointermove', at(x, y));
    },
    up() {
      svg.dispatch('pointerup', at(0, 0));
    },
    cancel() {
      svg.dispatch('pointercancel', at(0, 0));
    },
    double(x, y) {
      svg.dispatch('dblclick', at(x, y));
    },
  };
}

test('gestures send the curve JSON, and rev waits for pointerup', () => {
  const h = harness();
  h.down(64, 32);
  h.move(64, 40);
  assert.equal(h.photo.rev, 0);
  assert.equal(h.pushes.length, 0);
  assert.equal(pending.size, 1);
  flush();
  assert.deepEqual(h.pushes.at(-1), { master: [[0, 0], [64, 40], [255, 255]] });
  assert.equal(h.photo.rev, 0);
  h.up();
  assert.equal(h.photo.rev, 1);
  assert.equal(h.dragging, false);
  assert.deepEqual(h.photo.curve, { master: [[0, 0], [64, 40], [255, 255]] });

  h.view.panelEl().querySelector('[data-channel="r"]').dispatch('click', {});
  h.down(128, 40);
  h.up();
  flush();
  assert.equal(h.photo.rev, 2);
  assert.deepEqual(h.photo.curve.r, [[0, 0], [128, 40], [255, 255]]);
  assert.deepEqual(h.photo.curve.master, [[0, 0], [64, 40], [255, 255]]);

  h.view.panelEl().querySelector('[data-channel="master"]').dispatch('click', {});
  h.down(255, 255);
  h.move(10, 0);
  flush();
  h.up();
  assert.deepEqual(h.photo.curve.master[2], [255, 0]);
  assert.equal(h.photo.curve.master[2][0], 255);

  const before = h.photo.rev;
  h.down(64, 0);
  h.up();
  assert.equal(h.photo.rev, before);
  assert.equal(h.photo.curve.master.filter(([x]) => x === 64).length, 1);
});

test('pointercancel and destroy restore the committed curve and leave no frame', () => {
  const h = harness({ master: [[0, 0], [80, 40], [255, 255]] });
  h.down(80, 40);
  h.move(80, 10);
  assert.equal(h.photo.curve.master[1][1], 10);
  assert.equal(pending.size, 1);
  h.cancel();
  assert.deepEqual(h.photo.curve, { master: [[0, 0], [80, 40], [255, 255]] });
  assert.equal(h.photo.rev, 0);
  assert.equal(pending.size, 0);
  assert.equal(h.dragging, false);
  assert.deepEqual(h.pushes.at(-1), { master: [[0, 0], [80, 40], [255, 255]] });

  h.down(80, 40);
  h.move(80, 20);
  const other = { id: 'q', rev: 7, curve: { b: [[0, 0], [10, 20], [255, 255]] } };
  h.setCurrent(other);
  h.view.destroy();
  assert.deepEqual(h.photo.curve, { master: [[0, 0], [80, 40], [255, 255]] });
  assert.equal(h.photo.rev, 0);
  assert.deepEqual(other.curve, { b: [[0, 0], [10, 20], [255, 255]] });
  assert.equal(pending.size, 0);
  flush();
  assert.equal(other.rev, 7);
});

test('double-click deletes an interior point, reset clears a channel, reset-all is identity', () => {
  const h = harness();
  h.down(64, 32);
  h.up();
  h.down(192, 220);
  h.up();
  assert.equal(h.photo.curve.master.length, 4);
  h.double(64, 32);
  assert.deepEqual(h.photo.curve, { master: [[0, 0], [192, 220], [255, 255]] });
  h.double(0, 0);
  assert.equal(h.photo.curve.master[0][0], 0);
  const rev = h.photo.rev;
  h.view.panelEl().querySelectorAll('button').find((btn) => btn.textContent === '重置此通道').dispatch('click', {});
  assert.equal(h.photo.curve, null);
  assert.equal(h.photo.rev, rev + 1);
  h.down(40, 10);
  h.up();
  h.view.panelEl().querySelector('[data-channel="g"]').dispatch('click', {});
  h.down(90, 200);
  h.up();
  h.view.panelEl().querySelectorAll('button').find((btn) => btn.textContent === '重置全部曲线').dispatch('click', {});
  assert.equal(h.photo.curve, null);
  h.view.panelEl().querySelectorAll('button').find((btn) => btn.textContent === '重置全部曲线').dispatch('click', {});
  assert.equal(h.photo.rev, rev + 4);
});

test('copying and pasting 调整 carries the curve and undo puts it back', () => {
  const master = [[0, 0], [64, 32], [255, 255]];
  const source = editable('s', { curve: { master: master.map((p) => [...p]), r: [[0, 0], [20, 20], [255, 255]] }, knobs: knobs({ exposure_ev: 0.5 }) });
  assert.equal(adjustIsSet(source), true);
  assert.equal(adjustIsSet(editable('z', { curve: { g: [[0, 0], [8, 8], [255, 255]] } })), false);
  const only = editable('c', { knobs: knobs(), curve: { b: [[0, 0], [90, 140], [255, 255]] } });
  assert.equal(adjustIsSet(only), true);
  const { clip, copied } = copyFrom(source, 'SRC', ['adjust']);
  assert.deepEqual(copied, ['adjust']);
  source.curve.master[1][1] = 1;
  assert.deepEqual(clip.curve, { master });
  assert.equal(curveSummary(clip.curve), '主曲线');
  assert.equal(curveSummary({ master, r: [[0, 0], [1, 2], [255, 255]], g: [[0, 1], [255, 255]], b: [[0, 0], [4, 5], [255, 255]] }), '主曲线、红曲线、绿曲线、蓝曲线');

  const target = editable('t', { knobs: knobs({ contrast: 3 }), curve: { g: [[0, 0], [30, 10], [255, 255]] } });
  const pasted = pasteOne({ photo: target, raw: true, unit: 'jlaunch' }, clip, ['adjust'], { listed: () => true, recipeWithoutRaw: false });
  assert.equal(pasted.outcome.status, 'applied');
  assert.deepEqual(target.curve, { master });
  assert.equal(target.knobs.exposure_ev, 0.5);
  assert.equal(target.knobs.contrast, 0);
  assert.equal(target.rev, 1);
  const undo = new PasteUndo();
  undo.add('b', target.id, pasted.outcome.rev, pasted.before);
  assert.deepEqual(undoBatch(undo.take('b'), () => target).counts, { restored: 1, changed: 0, error: 0 });
  assert.deepEqual(target.curve, { g: [[0, 0], [30, 10], [255, 255]] });
  assert.equal(target.knobs.contrast, 3);
  assert.equal(target.rev, 2);

  const clear = copyFrom(editable('n', { knobs: knobs({ warm: 4 }), curve: null }), 'N', ['adjust']).clip;
  const kept = editable('k', { curve: { master: [[0, 0], [12, 40], [255, 255]] } });
  pasteOne({ photo: kept, raw: true, unit: null }, clear, ['adjust'], { listed: () => true, recipeWithoutRaw: false });
  assert.equal(kept.curve, null);
  assert.equal(kept.knobs.warm, 4);

  const legacy = { groups: ['adjust'], from: { id: 's', stem: 'S' }, recipe: null, lut: null, knobs: knobs({ tint: 2 }), geometry: null, crop: null };
  const old = editable('o', { curve: { r: [[0, 0], [50, 80], [255, 255]] } });
  pasteOne({ photo: old, raw: true, unit: null }, legacy, ['adjust'], { listed: () => true, recipeWithoutRaw: false });
  assert.deepEqual(old.curve, { r: [[0, 0], [50, 80], [255, 255]] });
});

test('two-pointer drag ignores other pointers', () => {
  const h = harness({ master: [[0, 0], [128, 120], [255, 255]] });
  const ev = (x, y, pointerId) => ({ clientX: x, clientY: 255 - y, button: 0, pointerId, preventDefault() {} });
  h.svg.dispatch('pointerdown', ev(128, 120, 1));
  assert.equal(h.dragging, true);
  h.svg.dispatch('pointermove', ev(128, 220, 2));
  assert.deepEqual(h.photo.curve.master[1], [128, 120]);
  h.svg.dispatch('pointerup', ev(128, 220, 2));
  assert.equal(h.dragging, true);
  assert.equal(h.photo.rev, 0);
  h.svg.dispatch('pointercancel', ev(128, 220, 2));
  assert.equal(h.dragging, true);
  assert.equal(h.photo.rev, 0);
  h.svg.dispatch('pointermove', ev(128, 160, 1));
  h.svg.dispatch('pointerup', ev(128, 160, 1));
  assert.equal(h.dragging, false);
  assert.equal(h.photo.rev, 1);
  assert.deepEqual(h.photo.curve.master[1], [128, 160]);
});

test('grab offset keeps a touched point from jumping on initial move or 1 px drag', () => {
  const h = harness({ master: [[0, 0], [128, 120], [255, 255]] });
  h.svg.getBoundingClientRect = () => ({ left: 0, top: 0, width: 203, height: 203 });
  const client = (x, y) => ({ clientX: (x / 255) * 203, clientY: ((255 - y) / 255) * 203, button: 0, pointerId: 1, preventDefault() {} });
  const down = client(150, 132);
  h.svg.dispatch('pointerdown', down);
  assert.equal(h.photo.curve.master.length, 3);
  h.svg.dispatch('pointermove', { ...down, clientX: down.clientX + 0.3 });
  assert.deepEqual(h.photo.curve.master[1], [128, 120]);
  h.svg.dispatch('pointermove', { ...down, clientX: down.clientX + 1 });
  assert.deepEqual(h.photo.curve.master[1], [129, 120]);
  h.svg.dispatch('pointerup', { ...down, clientX: down.clientX + 1 });
  assert.equal(h.photo.rev, 1);
});

test('curve count moves into .col with nowrap and unused later_panels is removed', () => {
  const h = harness();
  const col = h.view.panelEl().querySelector('.col');
  assert.ok(col?.querySelector('.curve-count'), 'curve-count is inside .col');
  const css = readFileSync(new URL('../../mobile/src/app/shell.css', import.meta.url), 'utf8');
  assert.match(css, /\.curve-count\s*\{[^}]*white-space:\s*nowrap/);
  assert.equal(css.includes('.later-panel'), false);
  assert.throws(() => readFileSync(new URL('../../mobile/src/app/later_panels.ts', import.meta.url), 'utf8'));
});

test('editor.ts and boot.ts stay within 800 lines', () => {
  assert.ok(lines('../../mobile/src/app/editor.ts') <= 800);
  assert.ok(lines('../../mobile/src/app/boot.ts') <= 800);
});
