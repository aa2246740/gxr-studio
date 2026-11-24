// perf-interact Round 2/3 单元测试（F1 并发按 Worker 数预留点击槽、N1 严格轮询池与静默上限 2、N2 跨 cancel 真实运行计数、F2/N3 舞台/工具栏输入回落、F3/N4 按组高度估算与去重写入、F4 去重 GPU 槽位与旋转保焦）。

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

/** 严格轮询 N-worker 模拟池（与 main 分支 core/pool.ts:59 一致），支持 resolve + bake 两步或单步派发。 */
async function makeRecipeFixture(cores, { twoStep = false } = {}) {
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
  const { shelves: shelvesOf } = await import('../../mobile/src/lib/recipes.ts');
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

  // 严格轮询：每个 Worker 维护各自 FIFO 队列，next 严格按 (next++ % workerCount) 轮转
  const workerQueues = Array.from({ length: workerCount }, () => []);
  let nextSlot = 0;
  const pendingByCard = new Map();
  const readyKeys = new Set();
  const queueDepthOnDispatch = new Map();
  const activeCards = () => workerQueues.filter((q) => q.length > 0).map((q) => q[0].cardId);
  const totalInPool = () => workerQueues.reduce((sum, q) => sum + q.length, 0);
  let peakBgCount = 0;
  let stageCard = 'builtin:4';

  const recordPeakBg = () => {
    let bg = 0;
    for (const q of workerQueues) {
      for (const job of q) {
        if (job.cardId !== stageCard) bg += 1;
      }
    }
    peakBgCount = Math.max(peakBgCount, bg);
  };

  const enqueueOnPool = (cardId, op) => {
    const slot = nextSlot % workerCount;
    nextSlot += 1;
    const q = workerQueues[slot];
    const ahead = q.length;
    const d = makeDeferred();
    const job = { cardId, op, slot, d };
    q.push(job);
    if (op === 'bake') queueDepthOnDispatch.set(cardId, ahead);
    recordPeakBg();
    return { job, ahead };
  };

  const finishOp = (cardId, op = 'bake') => {
    for (const q of workerQueues) {
      if (q.length > 0 && q[0].cardId === cardId && q[0].op === op) {
        const done = q.shift();
        if (op === 'bake') {
          pendingByCard.delete(cardId);
          readyKeys.add(`k:${cardId}`);
        }
        done.d.resolve(`k:${cardId}`);
        return true;
      }
    }
    return false;
  };

  const finishBake = (cardId) => {
    const ok = finishOp(cardId, 'bake');
    assert.ok(ok, `card ${cardId} is not at head of any round-robin worker (heads=${activeCards().join(',')})`);
  };

  const book = {
    dat, datError: null, datNote: null,
    pack: { kind: 'default', name: 'default', savedAt: 0, units: [{ unit: 'jlaunch' }], manifestSha256: 'x', files: new Map() },
    onChange: () => () => {},
    unitState: () => ({ state: 'ready' }),
    usableDatFor() { return this.dat; },
    unitStateFor(_p, unit) { return this.unitState(unit, this.dat); },
    shelvesFor(_p, unit) { return this.dat ? shelvesOf(this.dat.listing, unit) : null; },
    bakeKey: (card) => (readyKeys.has(`k:${card.id}`) ? `k:${card.id}` : null),
    ready: (key) => readyKeys.has(key),
    gpuKey: (key) => (readyKeys.has(key) ? `gpu:${key}` : null),
    pin: () => {},
    bake(card) {
      const existing = pendingByCard.get(card.id);
      if (existing) return existing;
      if (twoStep) {
        const resSlot = nextSlot % workerCount;
        nextSlot += 1;
        const resolveBehind = workerQueues[resSlot].length;
        const p = Promise.resolve().then(() => {
          const { job, ahead } = enqueueOnPool(card.id, 'bake');
          queueDepthOnDispatch.set(card.id, resolveBehind + ahead);
          return job.d.promise;
        });
        pendingByCard.set(card.id, p);
        return p;
      }
      const { job } = enqueueOnPool(card.id, 'bake');
      pendingByCard.set(card.id, job.d.promise);
      return job.d.promise;
    },
  };

  const photo = makePhoto(`p_${cores}`);
  const api = { book: () => book, photo: (id) => (id === photo.id ? photo : undefined), session: () => 1, pickDat: () => {}, pickCalib: () => {} };
  const engine = { thumb: async () => ({ w: 8, h: 6, data: new Uint8Array(8 * 6 * 4) }) };

  // 按真实编辑器结构挂载：.app > .screen.editor > (.stage + .band + .toolbar)
  const appShell = document.createElement('div');
  appShell.className = 'app';
  const screen = document.createElement('div');
  screen.className = 'screen editor';
  const stage = document.createElement('div');
  stage.className = 'stage';
  const band = document.createElement('div');
  band.className = 'band';
  const toolbar = document.createElement('div');
  toolbar.className = 'toolbar';
  screen.append(stage, band, toolbar);
  appShell.append(screen);
  document.body.append(appShell);

  const rv = new RecipeView(api, engine, photo, {
    mode: () => 'raw', look: () => {}, dragging: () => false, recipeTab: () => true,
    geometry: async () => ({ geometry: { straighten: 0, rotate90: 0, flip: false, vertical: 0, horizontal: 0, distortion: 0, box: null }, cropped: false }),
  });
  band.append(rv.panelEl());
  const setVisibleIndices = (indices) => {
    const set = new Set(indices);
    if (ioCallback && observed.length) {
      ioCallback(observed.map((target, idx) => ({ target, isIntersecting: set.has(idx) })));
    }
  };
  setVisibleIndices([0, 1, 2, 3]);

  const restore = () => {
    rv.destroy();
    if (origNav) Object.defineProperty(globalThis, 'navigator', origNav);
    else delete globalThis.navigator;
  };
  return {
    rv, screen, stage, toolbar, workerCount, workerQueues,
    activeCards, totalInPool, queueDepthOnDispatch, idleQueue,
    finishBake, setVisibleIndices,
    setStageCard: (id) => { stageCard = id; },
    getPeakBgCount: () => peakBgCount,
    resetPeakBgCount: () => { peakBgCount = 0; },
    restore,
  };
}

test('R3-1 (F1+N1): strict round-robin pool keeps free worker on N=2..4 and caps quiet idle at 2 without tap regression', async () => {
  const { QUIET_IDLE_BAKE_CAP } = await import('../../mobile/src/app/looks.ts');
  assert.equal(QUIET_IDLE_BAKE_CAP, 2, 'QUIET_IDLE_BAKE_CAP must be named constant 2 while pool.ts is round-robin');

  // 1) N=2, 3, 4 严格轮询池：初始打开期间至少留 1 个空闲 Worker，点选离屏卡在 1 个 bake 槽内直接启动（ahead === 0）
  for (const cores of [3, 4, 8]) {
    const fx = await makeRecipeFixture(cores);
    try {
      fx.rv.start(`raw:p_${cores}`);
      await tick(6);
      assert.ok(
        fx.totalInPool() <= fx.workerCount - 1,
        `N=${fx.workerCount} workers: expected <= ${fx.workerCount - 1} in pool during initial bake, got ${fx.totalInPool()}`,
      );
      const tapNode = fx.rv.stripEl().querySelector('.rc[data-card="look:look_8"]');
      assert.ok(tapNode, 'tap target must exist');
      fx.setStageCard('look:look_8');
      tapNode.click();
      await tick(3);
      assert.equal(
        fx.queueDepthOnDispatch.get('look:look_8'),
        0,
        `N=${fx.workerCount} workers: tap queued behind background on strict round-robin pool (ahead=${fx.queueDepthOnDispatch.get('look:look_8')})`,
      );
      fx.finishBake('look:look_8');
      await tick(4);
    } finally {
      fx.restore();
    }
  }

  // 2) N=4 严格轮询池（含 resolve + bake 两步）：静默离屏并发上限为 2（留 2 个空闲 Worker），
  //    点选未烘卡时 resolve 与 bake 均落到空闲 Worker（ahead === 0，对比 7507c7f 无回归；6fa193e 并发 3 时必撞车）
  const fx4 = await makeRecipeFixture(8, { twoStep: true });
  try {
    fx4.rv.start('raw:p_8');
    await tick(6);
    for (let guard = 0; guard < 12; guard += 1) {
      const vis = fx4.activeCards().filter((id) => ['builtin:1', 'builtin:2', 'builtin:3', 'builtin:4'].includes(id));
      if (!vis.length) break;
      for (const id of vis) fx4.finishBake(id);
      await tick(6);
    }
    assert.ok(fx4.idleQueue.length > 0, 'off-screen cards must schedule idle work');
    fx4.idleQueue.shift()();
    await tick(6);
    assert.equal(fx4.totalInPool(), 2, `quiet 4-worker idle concurrency must be capped at 2, got ${fx4.totalInPool()}`);

    const coldTap = fx4.rv.stripEl().querySelector('.rc[data-card="look:look_12"]');
    assert.ok(coldTap, 'cold off-screen tap target must exist');
    fx4.setStageCard('look:look_12');
    coldTap.click();
    await tick(4);
    assert.equal(
      fx4.queueDepthOnDispatch.get('look:look_12'),
      0,
      `quiet idle tap regressed on 4-worker strict round-robin pool (ahead=${fx4.queueDepthOnDispatch.get('look:look_12')})`,
    );
    fx4.finishBake('look:look_12');
  } finally {
    fx4.restore();
  }
});

test('R3-2 (F2+N3): stage pinch/pan and toolbar taps trigger idle concurrency drop-back to 1', async () => {
  for (const [label, targetKey, evType] of [
    ['stage pinch/pan (pointerdown)', 'stage', 'pointerdown'],
    ['stage wheel zoom', 'stage', 'wheel'],
    ['toolbar tap', 'toolbar', 'pointerdown'],
  ]) {
    const fx = await makeRecipeFixture(8);
    try {
      fx.rv.start('raw:p_8');
      await tick(6);
      for (let guard = 0; guard < 12; guard += 1) {
        const vis = fx.activeCards().filter((id) => ['builtin:1', 'builtin:2', 'builtin:3', 'builtin:4'].includes(id));
        if (!vis.length) break;
        for (const id of vis) fx.finishBake(id);
        await tick(6);
      }
      assert.ok(fx.idleQueue.length > 0);
      fx.idleQueue.shift()();
      await tick(6);
      assert.equal(fx.totalInPool(), 2, `${label}: quiet idle should start at cap 2`);

      fx[targetKey].dispatchEvent(new Event(evType, { bubbles: true }));
      for (const id of fx.activeCards()) fx.finishBake(id);
      await tick(6);
      if (fx.idleQueue.length > 0) {
        fx.idleQueue.shift()();
        await tick(6);
      }
      assert.ok(fx.totalInPool() <= 1, `${label}: idle concurrency must drop back to <= 1, got ${fx.totalInPool()}`);
    } finally {
      fx.restore();
    }
  }
});

test('R3-3 (N2): scroll, tap, and quick second tap never exceed background concurrency cap across cancel()', async () => {
  const fx = await makeRecipeFixture(8); // 4 workers -> max background cap = 3 (stage idle) / 2 (stage baking)
  try {
    fx.rv.start('raw:p_8');
    await tick(6);
    fx.finishBake('builtin:4');
    await tick(6);
    fx.resetPeakBgCount();

    // 滚动到新可见区间，同时触发同 token 的 refresh()（内部 cancel + start）模拟旧代仍在 Worker 运行
    fx.setVisibleIndices([4, 5, 6, 7]);
    await tick(4);
    fx.rv.refresh();
    await tick(4);

    // 第一次点击
    const tap1 = fx.rv.stripEl().querySelector('.rc[data-card="look:look_7"]');
    fx.setStageCard('look:look_7');
    tap1.click();
    await tick(4);
    fx.rv.refresh();
    await tick(4);
    assert.ok(fx.getPeakBgCount() <= 3, `after scroll + tap1 + refresh, peakBg=${fx.getPeakBgCount()} exceeded cap 3`);
    fx.finishBake('look:look_7');
    await tick(4);

    // 紧接着第二次快速点击 + 再滚一页 + refresh
    fx.setVisibleIndices([8, 9, 10, 11]);
    const tap2 = fx.rv.stripEl().querySelector('.rc[data-card="look:look_11"]');
    fx.setStageCard('look:look_11');
    tap2.click();
    await tick(4);
    fx.rv.refresh();
    await tick(4);
    assert.ok(fx.getPeakBgCount() <= 3, `after quick second tap + refresh, peakBg=${fx.getPeakBgCount()} exceeded cap 3`);

    // 排空旧代与两次点击后，剩余卡片必须继续推进且在途后台数始终不超过上限 3
    let finishedLook11 = false;
    for (let guard = 0; guard < 30; guard += 1) {
      const heads = fx.activeCards();
      if (!heads.length) break;
      for (const id of heads) {
        if (id === 'look:look_11') finishedLook11 = true;
        fx.finishBake(id);
      }
      await tick(6);
    }
    assert.equal(finishedLook11, true, 'quick second tap look:look_11 must complete');
    assert.ok(fx.getPeakBgCount() <= 3, `peak background bakes after drain exceeded cap 3: ${fx.getPeakBgCount()}`);
  } finally {
    fx.restore();
  }
});

test('R3-4 (F3+F4+N4): dynamic --day-h skips unchanged writes, no dead style[--cell], single gpuKey, focus preserved', async () => {
  setupEnv();
  const appCss = readFileSync(new URL('../../web/assets/app.css', import.meta.url), 'utf8');
  assert.match(appCss, /contain-intrinsic-block-size:\s*auto\s+var\(--day-h/, 'app.css must use per-group var(--day-h)');
  const galleryJs = readFileSync(new URL('../../web/assets/gallery.js', import.meta.url), 'utf8');
  assert.ok(!galleryJs.includes("style['--cell']"), 'gallery.js must not keep dead wrap.style["--cell"] fallback');
  assert.match(galleryJs, /getPropertyValue\?\.\('--day-h'\)\s*!==/, 'syncDaySizes must skip unchanged --day-h writes');

  const { CardBakeController, renderCardThumb, lookCache } = await import('../../mobile/src/app/looks.ts');
  lookCache.clear();
  let gpuKeyCalls = 0;
  let readyCalls = 0;
  const photo = makePhoto('p_f4a');
  await renderCardThumb({
    engine: { thumb: async () => ({ w: 8, h: 6, data: new Uint8Array(8 * 6 * 4) }) },
    source: 'raw:p_f4a', photo, version: photo.version, cardId: 'builtin:2',
    geometry: async () => ({ straighten: 0, rotate90: 0, flip: false, vertical: 0, horizontal: 0, distortion: 0, box: null }),
    adjust: { exposure_ev: 0, wb: { warm: 0, tint: 0 }, contrast: 0, highlight: 0, shadow: 0, black_point: 0, saturation: 0, vibrance: 0, vignette: 0 },
    alive: () => true, isBusy: () => false,
    bakeCtrl: new CardBakeController(),
    bakeKey: () => 'k:cached',
    ready: () => { readyCalls += 1; return true; },
    gpuKey: () => { gpuKeyCalls += 1; return 'gpu:slot0'; },
    bake: async () => 'k:cached',
    onError: () => {}, onPainted: () => {},
  });
  assert.equal(readyCalls, 0);
  assert.equal(gpuKeyCalls, 1);

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
    assert.strictEqual(document.activeElement, barBtn);

    bandBtn.focus();
    syncEditorBarOrder(screen, band, bar, false);
    assert.strictEqual(band.nextElementSibling, bar);
    assert.strictEqual(document.activeElement, bandBtn);

    bandBtn.focus();
    syncEditorBarOrder(screen, band, bar, true);
    assert.strictEqual(bar.nextElementSibling, band);
    assert.strictEqual(document.activeElement, bandBtn);

    barBtn.focus();
    syncEditorBarOrder(screen, band, bar, false);
    assert.strictEqual(band.nextElementSibling, bar);
    assert.strictEqual(document.activeElement, barBtn);
  } finally {
    TestNode.prototype.remove = origRemove;
  }
});
