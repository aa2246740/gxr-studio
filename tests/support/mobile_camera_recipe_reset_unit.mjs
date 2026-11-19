// 回归测试（验收 #3）：「全部重置」同时清掉调整滑块和曲线，rev 只加 1，网格铅笔徽章清掉；全空再点不动。
// Node 直接跑 TypeScript。

import assert from 'node:assert/strict';
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
    min: '',
    max: '',
    step: '',
    value: '',
    disabled: false,
    title: '',
    parentElement: null,
    style: {
      setProperty() {},
    },
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
      for (const kid of kids) {
        if (kid && typeof kid === 'object') kid.parentElement = this;
        this.children.push(kid);
      }
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
    click() {
      for (const fn of [...(this.listeners.click ?? [])]) fn({ target: this });
    },
    closest(sel) {
      let cur = this;
      while (cur) {
        if (matches(cur, sel)) return cur;
        cur = cur.parentElement;
      }
      return null;
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
    add(name) {
      const parts = new Set(node.className.split(/\s+/).filter(Boolean));
      parts.add(name);
      node.className = [...parts].join(' ');
    },
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
  const parts = sel.trim().split(/\s+/);
  const last = parts[parts.length - 1];
  const segments = last.split('.');
  const tag = segments[0];
  const classes = segments.slice(1);
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

globalThis.document = {
  createElement: (tag) => makeNode(tag),
  createElementNS: (_ns, tag) => makeNode(tag),
  createTextNode: (text) => ({ tag: '#text', textContent: String(text) }),
};

const { AdjustView } = await import('../../mobile/src/app/adjust_view.ts');
const { zeroKnobs } = await import('../../mobile/src/app/model.ts');
const { groupsPresent } = await import('../../mobile/src/lib/paste.ts');
const { curveJson } = await import('../../mobile/src/lib/export_request.ts');
const { zeroGeometry } = await import('../../mobile/src/lib/crop.ts');
const { DEFAULT_FRAME, DEFAULT_STAMP } = await import('../../mobile/src/lib/frame_spec.ts');

function makePhoto() {
  return {
    id: 'p1',
    stem: 'R0000001',
    version: 1,
    dng: {},
    jpg: {},
    diptych: false,
    lastModified: 0,
    captureMs: null,
    status: 'ready',
    gone: false,
    cached: null,
    checked: null,
    bitmap: null,
    neutral: null,
    knobs: zeroKnobs(),
    curve: null,
    jpgMeta: null,
    dngMeta: null,
    border: { ...DEFAULT_FRAME },
    stamp: { ...DEFAULT_STAMP },
    frameMeta: null,
    frame: null,
    geometry: zeroGeometry(),
    crop: null,
    rev: 0,
    choice: null,
    levels: new Map(),
    memory: new Map(),
    lut: null,
    looks: new Map(),
  };
}

function findResetButton(panel) {
  return panel.querySelectorAll('button').find((b) => b.textContent === '全部重置') ?? null;
}

test('全部重置 clears both exposure and curve with one rev bump, clears badge, and is no-op when already clean', () => {
  const photo = makePhoto();
  photo.knobs.exposure_ev = 1.2;
  photo.curve = {
    master: [
      [0, 0],
      [128, 172],
      [255, 255],
    ],
  };
  photo.rev = 2;

  let pushes = 0;
  const view = new AdjustView(
    {
      photo: () => photo,
      push: () => {
        pushes += 1;
      },
      enterDrag: () => undefined,
      leaveDrag: () => undefined,
      dragging: () => false,
    },
    makeNode('div'),
  );
  const reset = findResetButton(view.panelEl());
  assert.ok(reset, '全部重置 button exists');
  assert.deepEqual(groupsPresent(photo), ['adjust']);

  reset.click();

  assert.equal(photo.knobs.exposure_ev, 0, 'exposure cleared');
  assert.equal(curveJson(photo.curve), null, 'curve cleared');
  assert.equal(photo.curve, null, 'photo.curve set to null');
  assert.equal(photo.rev, 3, 'rev bumped exactly once');
  assert.equal(pushes, 1, 'push called once');
  assert.deepEqual(groupsPresent(photo), [], 'grid pencil badge cleared');

  // Pressing again when nothing is set must be a complete no-op.
  reset.click();
  assert.equal(photo.rev, 3, 'second reset does not bump rev');
  assert.equal(pushes, 1, 'second reset does not push');
});

test('全部重置 clears curve even when knobs are already zero, without touching recipe choice', () => {
  const photo = makePhoto();
  photo.choice = { id: 'builtin:1', unit: 'jlaunch' };
  photo.curve = {
    r: [
      [0, 0],
      [128, 160],
      [255, 255],
    ],
  };
  photo.rev = 4;

  let pushes = 0;
  const view = new AdjustView(
    {
      photo: () => photo,
      push: () => {
        pushes += 1;
      },
      enterDrag: () => undefined,
      leaveDrag: () => undefined,
      dragging: () => false,
    },
    makeNode('div'),
  );
  const reset = findResetButton(view.panelEl());
  assert.ok(reset);

  reset.click();

  assert.equal(photo.curve, null, 'curve cleared when knobs were already 0');
  assert.equal(photo.rev, 5, 'rev bumped once');
  assert.equal(pushes, 1, 'push called once');
  assert.deepEqual(photo.choice, { id: 'builtin:1', unit: 'jlaunch' }, 'recipe choice preserved');
  assert.deepEqual(groupsPresent(photo), ['recipe'], 'only recipe remains in groupsPresent');
});
