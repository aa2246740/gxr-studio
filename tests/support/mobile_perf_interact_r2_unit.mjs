// perf-interact Round 2 单元测试（F1 并发按 Worker 数预留点击槽、F2 离屏空闲并发与输入回落、F3 按组高度估算、F4 去重 GPU 槽位上传与旋转保焦）。

import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { registerHooks } from 'node:module';
import test from 'node:test';
import { installDom, TestNode, tick } from './mini_dom.mjs';

registerHooks({
  resolve(spec, ctx, next) {
    if (/\.(css|ttf|woff2?|png|svg|txt)(\?.*)?$/.test(spec)) {
      return { url: 'data:text/javascript,export default "";', shortCircuit: true };
    }
    if (spec === './export_ui') {
      return { url: 'data:text/javascript,export function openExportOptions(){return false;}', shortCircuit: true };
    }
    try {
      return next(spec, ctx);
    } catch (err) {
      if (spec.startsWith('.') && !/\.[a-z]+$/.test(spec)) return next(`${spec}.ts`, ctx);
      throw err;
    }
  },
  load(url, ctx, next) {
    const res = next(url, ctx);
    if (url.endsWith('/paste_state.ts') && typeof res.source === 'string') {
      return { ...res, source: res.source.replaceAll('import.meta.env.DEV', 'false') };
    }
    if (url.endsWith('/paste_state.ts') && res.source instanceof Uint8Array) {
      return { ...res, source: Buffer.from(res.source).toString('utf8').replaceAll('import.meta.env.DEV', 'false') };
    }
    return res;
  },
});

function setupEnv() {
  installDom();
  globalThis.HTMLButtonElement = TestNode;
  globalThis.HTMLCanvasElement = TestNode;
  globalThis.HTMLDivElement = TestNode;
  TestNode.prototype.select = () => {};
  TestNode.prototype.scrollTop = 0;
  globalThis.ImageData = class FakeImageData {
    constructor(data, width, height) { this.data = data; this.width = width; this.height = height; }
  };
  globalThis.createImageBitmap = async (img) => ({
    width: img.width || 16, height: img.height || 12, _closed: false,
    close() { this._closed = true; this.width = 0; this.height = 0; },
  });
}

function makeDeferred() {
  let resolve;
  let reject;
  const promise = new Promise((res, rej) => { resolve = res; reject = rej; });
  return { promise, resolve, reject };
}

function makePhoto(id) {
  return {
    id, stem: id.toUpperCase(), version: 1,
    dng: { name: `${id}.DNG`, lastModified: 1_700_000_000_000 },
    jpg: { name: `${id}.JPG`, lastModified: 1_700_000_000_000 },
    lastModified: 1_700_000_000_000, captureMs: null, status: 'ready', gone: false, cached: null, checked: null,
    bitmap: null, neutral: null,
    knobs: { exposure_ev: 0, contrast: 0, highlight: 0, shadow: 0, black_point: 0, warm: 0, tint: 0, saturation: 0, vibrance: 0, vignette: 0 },
    curve: null, jpgMeta: { lens: 'GR LENS A12 50mm', iso: 200 }, dngMeta: { lens: 'GR LENS A12 50mm', iso: 200 },
    border: { version: 2, style: 'none', color: 'white', ratio: 'original', logo: 'ricoh', show_logo: true, logo_mode: 'auto', logo_size: 'medium', model: true, title: '', caption: '' },
    stamp: { enabled: false }, frameMeta: null, frame: { width: 4288, height: 2848, orientation: 1 },
    geometry: { straighten: 0 }, crop: null, rev: 0, choice: null, levels: new Map(), memory: new Map(),
    lut: null, looks: new Map(),
    cameraRecipe: { datKey: 'GXRCE.DAT:16:0', copyright: null, version: 1, result: { status: 'none' } },
  };
}

async function makeRecipeFixture(cores) {
  setupEnv();
  const origNav = Object.getOwnPropertyDescriptor(globalThis, 'navigator');
  Object.defineProperty(globalThis, 'navigator', { configurable: true, value: { hardwareConcurrency: cores } });
  const observed = [];
  let ioCallback = null;
  globalThis.IntersectionObserver = class FakeIO {
    constructor(cb) { ioCallback = cb; }
    observe(node) { observed.push(node); }
    unobserve() {}
    disconnect() {}
  };
  const idleQueue = [];
  globalThis.requestIdleCallback = (cb) => { idleQueue.push(cb); return idleQueue.length; };
  globalThis.cancelIdleCallback = () => {};

  const { RecipeView } = await import('../../mobile/src/app/recipe_view.ts');
  const { setExportPaused } = await import('../../mobile/src/app/export_pause.ts');
  setExportPaused(false);

  const workerCount = Math.min(4, Math.max(1, cores - 1));
  const controls = [{ name: 'amount', label: '强度', default: 5, min: 1, max: 9, levels: [1, 3, 5, 7, 9] }];
  const dat = {
    name: 'GXRCE.DAT', generation: 2,
    read: { bytes: new ArrayBuffer(16), entries: new Uint8Array(24 * 552), summary: '{}', listing: '{}', library: null },
    listing: {
      units: [{
        id: 'jlaunch',
        recipes: Array.from({ length: 8 }, (_, i) => ({ slot: i + 1, name: `配方${i + 1}`, controls })),
        looks: Array.from({ length: 12 }, (_, i) => ({ kind: 'look', id: `look_${i + 1}`, slot: 1, name: `Look ${i + 1}`, controls })),
        users: [],
      }],
    },
    library: null,
  };

  // N-worker 模拟池：每个 worker 一次只跑 1 个 bake，有空闲 worker 立即执行，否则排队等待
  const active = new Map();
  const waitQueue = [];
  const pendingByCard = new Map();
  const readyKeys = new Set();
  const startedImmediately = new Map();

  const pumpPool = () => {
    while (active.size < workerCount && waitQueue.length > 0) {
      const next = waitQueue.shift();
      active.set(next.cardId, next);
    }
  };
  const finishBake = (cardId) => {
    const job = active.get(cardId);
    assert.ok(job, `card ${cardId} is not actively running on a worker (active=${[...active.keys()].join(',')})`);
    active.delete(cardId);
    pendingByCard.delete(cardId);
    readyKeys.add(`k:${cardId}`);
    job.d.resolve(`k:${cardId}`);
    pumpPool();
  };

  const book = {
    dat, datError: null, datNote: null,
    pack: { kind: 'default', name: 'default', savedAt: 0, units: [{ unit: 'jlaunch' }], manifestSha256: 'x', files: new Map() },
    onChange: () => () => {},
    unitState: () => ({ state: 'ready' }),
    bakeKey: (card) => (readyKeys.has(`k:${card.id}`) ? `k:${card.id}` : null),
    ready: (key) => readyKeys.has(key),
    gpuKey: (key) => (readyKeys.has(key) ? `gpu:${key}` : null),
    pin: () => {},
    bake(card) {
      const existing = pendingByCard.get(card.id);
      if (existing) return existing.promise;
      const d = makeDeferred();
      pendingByCard.set(card.id, d);
      const job = { cardId: card.id, d };
      if (active.size < workerCount) {
        active.set(card.id, job);
        startedImmediately.set(card.id, true);
      } else {
        waitQueue.push(job);
        startedImmediately.set(card.id, false);
      }
      return d.promise;
    },
  };

  const photo = makePhoto(`p_${cores}`);
  const api = { book: () => book, photo: (id) => (id === photo.id ? photo : undefined), session: () => 1, pickDat: () => {}, pickCalib: () => {} };
  const engine = { thumb: async () => ({ w: 8, h: 6, data: new Uint8Array(8 * 6 * 4) }) };
  const appShell = document.createElement('div');
  appShell.className = 'app';
  document.body.append(appShell);
  const rv = new RecipeView(api, engine, photo, {
    mode: () => 'raw', look: () => {}, dragging: () => false, recipeTab: () => true,
    geometry: async () => ({ geometry: { straighten: 0, rotate90: 0, flip: false, vertical: 0, horizontal: 0, distortion: 0, box: null }, cropped: false }),
  });
  appShell.append(rv.panelEl());
  if (ioCallback && observed.length) {
    ioCallback(observed.map((target, idx) => ({ target, isIntersecting: idx < 4 })));
  }
  const restore = () => {
    rv.destroy();
    if (origNav) Object.defineProperty(globalThis, 'navigator', origNav);
    else delete globalThis.navigator;
  };
  return { rv, workerCount, active, waitQueue, startedImmediately, idleQueue, finishBake, restore };
}

test('R2-1 (F1): with N workers (2, 3, 4), a tap during background baking starts within 1 bake slot', async () => {
  for (const cores of [3, 4, 8]) {
    const fx = await makeRecipeFixture(cores);
    try {
      fx.rv.start(`raw:p_${cores}`);
      await tick(6);
      assert.ok(
        fx.active.size <= fx.workerCount - 1,
        `N=${fx.workerCount} workers: expected <= ${fx.workerCount - 1} active during initial bake so 1 worker stays free, got ${fx.active.size} (${[...fx.active.keys()].join(',')})`,
      );
      const tapNode = fx.rv.stripEl().querySelector('.rc[data-card="look:look_8"]');
      assert.ok(tapNode, 'tap target must exist');
      tapNode.click();
      await tick(3);
      assert.equal(
        fx.startedImmediately.get('look:look_8'),
        true,
        `N=${fx.workerCount} workers: tapped card queued behind background bake instead of starting within 1 bake slot`,
      );
      assert.ok(fx.active.has('look:look_8'), `N=${fx.workerCount} workers: tapped card must be running immediately on a worker`);
      fx.finishBake('look:look_8');
      await tick(4);
    } finally {
      fx.restore();
    }
  }
});

test('R2-2 (F2): off-screen idle bakes scale to F1 limit when quiet and drop back to 1 on user input', async () => {
  const fx = await makeRecipeFixture(8); // 4 workers -> F1 limit = 3 when stage is idle
  try {
    fx.rv.start('raw:p_8');
    await tick(6);
    for (let guard = 0; guard < 12; guard += 1) {
      const vis = [...fx.active.keys()].filter((id) => ['builtin:1', 'builtin:2', 'builtin:3', 'builtin:4'].includes(id));
      if (!vis.length) break;
      for (const id of vis) fx.finishBake(id);
      await tick(6);
    }
    assert.ok(fx.idleQueue.length > 0, 'off-screen cards must schedule idle work');
    fx.idleQueue.shift()();
    await tick(6);
    assert.equal(fx.active.size, 3, `quiet 4-worker off-screen idle concurrency must reach F1 limit 3, got ${fx.active.size}`);

    // 用户输入后立即回落至并发 1
    fx.rv.panelEl().dispatchEvent(new Event('pointerdown', { bubbles: true }));
    for (const id of [...fx.active.keys()]) fx.finishBake(id);
    await tick(6);
    if (fx.idleQueue.length > 0) {
      fx.idleQueue.shift()();
      await tick(6);
    }
    assert.ok(fx.active.size <= 1, `after user input off-screen idle concurrency must drop back to <= 1, got ${fx.active.size}`);
  } finally {
    fx.restore();
  }
});

test('R2-3 (F3+F4): dynamic --day-h CSS, single gpuKey upload in renderCardThumb, and rotation preserves focus', async () => {
  setupEnv();
  const appCss = readFileSync(new URL('../../web/assets/app.css', import.meta.url), 'utf8');
  assert.match(appCss, /contain-intrinsic-block-size:\s*auto\s+var\(--day-h/, 'app.css must use per-group var(--day-h) for contain-intrinsic-block-size');

  const { CardBakeController, renderCardThumb, lookCache } = await import('../../mobile/src/app/looks.ts');
  lookCache.clear();
  let gpuKeyCalls = 0;
  let readyCalls = 0;
  const photo = makePhoto('p_f4a');
  await renderCardThumb({
    engine: { thumb: async () => ({ w: 8, h: 6, data: new Uint8Array(8 * 6 * 4) }) },
    source: 'raw:p_f4a',
    photo,
    version: photo.version,
    cardId: 'builtin:2',
    geometry: async () => ({ straighten: 0, rotate90: 0, flip: false, vertical: 0, horizontal: 0, distortion: 0, box: null }),
    adjust: { exposure_ev: 0, wb: { warm: 0, tint: 0 }, contrast: 0, highlight: 0, shadow: 0, black_point: 0, saturation: 0, vibrance: 0, vignette: 0 },
    alive: () => true,
    isBusy: () => false,
    bakeCtrl: new CardBakeController(),
    bakeKey: () => 'k:cached',
    ready: () => { readyCalls += 1; return true; },
    gpuKey: () => { gpuKeyCalls += 1; return 'gpu:slot0'; },
    bake: async () => 'k:cached',
    onError: () => {},
    onPainted: () => {},
  });
  assert.equal(readyCalls, 0, `renderCardThumb must not call ready(), called ${readyCalls} times`);
  assert.equal(gpuKeyCalls, 1, `renderCardThumb must call gpuKey() once for a cached bake, called ${gpuKeyCalls} times`);

  const { syncEditorBarOrder } = await import('../../mobile/src/app/editor_hist.ts');
  const origRemove = TestNode.prototype.remove;
  TestNode.prototype.remove = function patchedRemove() {
    if (this.contains(document.activeElement)) document.activeElement = document.body;
    return origRemove.call(this);
  };
  try {
    const screen = document.createElement('div');
    const nav = document.createElement('div');
    const stage = document.createElement('div');
    const band = document.createElement('div');
    const bandBtn = document.createElement('button');
    band.append(bandBtn);
    const bar = document.createElement('div');
    const barBtn = document.createElement('button');
    bar.append(barBtn);
    screen.append(nav, stage, band, bar);
    document.body.append(screen);

    barBtn.focus();
    syncEditorBarOrder(screen, band, bar, true);
    assert.strictEqual(bar.nextElementSibling, band);
    assert.strictEqual(document.activeElement, barBtn, 'rotating to landscape must preserve focus inside toolbar');

    bandBtn.focus();
    syncEditorBarOrder(screen, band, bar, false);
    assert.strictEqual(band.nextElementSibling, bar);
    assert.strictEqual(document.activeElement, bandBtn, 'rotating to portrait must preserve focus inside band');

    bandBtn.focus();
    syncEditorBarOrder(screen, band, bar, true);
    assert.strictEqual(bar.nextElementSibling, band);
    assert.strictEqual(document.activeElement, bandBtn, 'rotating to landscape must preserve focus inside band');

    barBtn.focus();
    syncEditorBarOrder(screen, band, bar, false);
    assert.strictEqual(band.nextElementSibling, bar);
    assert.strictEqual(document.activeElement, barBtn, 'rotating to portrait must preserve focus inside toolbar');
  } finally {
    TestNode.prototype.remove = origRemove;
  }
});
