// perf-interact 单元测试（Node --experimental-strip-types + mini_dom）：
// 1) 配方卡可见优先、并发受限、用户点击抢占队列、离屏卡片空闲烘焙；
// 2) 网格单格增量更新（保持 DOM 节点身份）、拍摄时间到达时正确重排、绝不绘制已关闭的 ImageBitmap；
// 3) 滑块拖动期间零同步布局回流（fitGpuCanvas 复用缓存尺寸，拖动中不读 clientWidth/clientHeight）；
// 4) 桌面端 gallery.js 前 12-16 张缩略图 eager + high priority、桌面端与 iPad 胶片条 content-visibility: auto；
// 5) 中文输入法组合态按 Enter 不误提交（logo_sheet.ts、lut_sheet.ts、frame_logo_ui.js）；
// 6) 无 WebGPU 时调整滑块禁用样式与单行说明文案。

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
      const text = Buffer.from(res.source).toString('utf8').replaceAll('import.meta.env.DEV', 'false');
      return { ...res, source: text };
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
    constructor(data, width, height) {
      this.data = data;
      this.width = width;
      this.height = height;
    }
  };
  let drawCalls = 0;
  TestNode.prototype.getContext = function getContext(kind) {
    if (kind !== '2d') return null;
    if (!this._ctx2d) {
      this._ctx2d = {
        drawImage(bmp) {
          if (!bmp || bmp.width <= 0 || bmp.height <= 0 || bmp._closed) {
            throw new Error('InvalidStateError: ImageBitmap is closed');
          }
          drawCalls += 1;
        },
        clearRect() {},
        beginPath() {},
        moveTo() {},
        lineTo() {},
        closePath() {},
        fill() {},
        stroke() {},
      };
    }
    return this._ctx2d;
  };
  globalThis.createImageBitmap = async (img) => ({
    width: img.width || 16,
    height: img.height || 12,
    _closed: false,
    close() {
      this._closed = true;
      this.width = 0;
      this.height = 0;
    },
  });
  return {
    getDrawCalls: () => drawCalls,
    resetDrawCalls: () => {
      drawCalls = 0;
    },
  };
}

function makeDeferred() {
  let resolve;
  let reject;
  const promise = new Promise((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

function makePhoto(id, ms, opts = {}) {
  return {
    id,
    stem: id.toUpperCase(),
    version: 1,
    dng: opts.dng === null ? null : { name: `${id}.DNG`, lastModified: ms },
    jpg: opts.jpg === null ? null : { name: `${id}.JPG`, lastModified: ms },
    lastModified: ms,
    captureMs: opts.captureMs ?? null,
    status: opts.status ?? 'pending',
    gone: false,
    cached: null,
    checked: null,
    bitmap: opts.bitmap ?? null,
    neutral: null,
    knobs: {
      exposure_ev: 0,
      contrast: 0,
      highlight: 0,
      shadow: 0,
      black_point: 0,
      warm: 0,
      tint: 0,
      saturation: 0,
      vibrance: 0,
      vignette: 0,
    },
    curve: null,
    jpgMeta: { lens: 'GR LENS A12 50mm', iso: 200 },
    dngMeta: { lens: 'GR LENS A12 50mm', iso: 200 },
    border: { version: 2, style: 'none', color: 'white', ratio: 'original', logo: 'ricoh', show_logo: true, logo_mode: 'auto', logo_size: 'medium', model: true, title: '', caption: '' },
    stamp: { enabled: false },
    frameMeta: null,
    frame: { width: 4288, height: 2848, orientation: 1 },
    geometry: { straighten: 0 },
    crop: null,
    rev: 0,
    choice: null,
    levels: new Map(),
    memory: new Map(),
    lut: null,
    looks: new Map(),
    cameraRecipe: { datKey: 'test-dat', copyright: null, version: 1, result: { status: 'none' } },
  };
}

test('1. RecipeView: visible cards first, bounded concurrency, tap pre-empts queue, off-screen in idle', async () => {
  setupEnv();
  const observed = [];
  let ioCallback = null;
  globalThis.IntersectionObserver = class FakeIO {
    constructor(cb) {
      ioCallback = cb;
    }
    observe(node) {
      observed.push(node);
    }
    unobserve() {}
    disconnect() {}
  };
  const idleQueue = [];
  globalThis.requestIdleCallback = (cb) => {
    idleQueue.push(cb);
    return idleQueue.length;
  };
  globalThis.cancelIdleCallback = () => {};

  const { RecipeView } = await import('../../mobile/src/app/recipe_view.ts');
  const { setExportPaused } = await import('../../mobile/src/app/export_pause.ts');
  setExportPaused(false);

  const controls = [{ name: 'amount', label: '强度', default: 5, min: 1, max: 9, levels: [1, 3, 5, 7, 9] }];
  const recipes = Array.from({ length: 8 }, (_, i) => ({
    slot: i + 1,
    name: `配方${i + 1}`,
    controls,
  }));
  const looks = Array.from({ length: 12 }, (_, i) => ({
    kind: 'look',
    id: `look_${i + 1}`,
    slot: 1,
    name: `Look ${i + 1}`,
    controls,
  }));
  const dat = {
    name: 'GXRCE.DAT',
    generation: 2,
    read: { bytes: new ArrayBuffer(16), entries: new Uint8Array(24 * 552), summary: '{}', listing: '{}', library: null },
    listing: {
      units: [{ id: 'jlaunch', recipes, looks, users: [] }],
    },
    library: null,
  };

  const bakeLog = [];
  const pendingByCard = new Map();
  const readyKeys = new Set();
  let inFlight = 0;
  let peakBgInFlight = 0;

  const book = {
    dat,
    datError: null,
    datNote: null,
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
      bakeLog.push(card.id);
      inFlight += 1;
      const d = makeDeferred();
      pendingByCard.set(card.id, d);
      return d.promise;
    },
  };
  const finishBake = (cardId) => {
    const d = pendingByCard.get(cardId);
    assert.ok(d, `expected pending bake for ${cardId}`);
    pendingByCard.delete(cardId);
    inFlight -= 1;
    readyKeys.add(`k:${cardId}`);
    d.resolve(`k:${cardId}`);
  };

  const photo = makePhoto('p1', 1_700_000_000_000, { status: 'ready' });
  photo.cameraRecipe = { datKey: 'GXRCE.DAT:16:0', copyright: null, version: 1, result: { status: 'none' } };
  const api = {
    book: () => book,
    photo: (id) => (id === photo.id ? photo : undefined),
    session: () => 1,
    pickDat: () => {},
    pickCalib: () => {},
  };
  const engine = {
    thumb: async () => ({ w: 8, h: 6, data: new Uint8Array(8 * 6 * 4) }),
  };

  const appShell = document.createElement('div');
  appShell.className = 'app';
  document.body.append(appShell);

  const stageLooks = [];
  const rv = new RecipeView(api, engine, photo, {
    mode: () => 'raw',
    look: (key) => stageLooks.push(key),
    dragging: () => false,
    recipeTab: () => true,
    geometry: async () => ({
      geometry: { straighten: 0, rotate90: 0, flip: false, vertical: 0, horizontal: 0, distortion: 0, box: null },
      cropped: false,
    }),
  });
  appShell.append(rv.panelEl());

  // Report only first 4 cards (builtin:1..builtin:4) as visible via IntersectionObserver
  if (ioCallback && observed.length) {
    ioCallback(
      observed.map((target, idx) => ({ target, isIntersecting: idx < 4 })),
    );
  }

  rv.start('raw:p1');
  await tick(4);

  // While the active card's stage bake (DEFAULT_CARD = builtin:4) is still running, background cards must NOT flood book.bake
  assert.deepEqual(
    bakeLog,
    ['builtin:4'],
    `expected only active stage card builtin:4 while stage bake is pending, got ${bakeLog.length} queued bakes: ${bakeLog.slice(0, 6).join(',')}`,
  );

  // Finish active stage bake -> visible cards (builtin:1..3) start with bounded concurrency (<= 2)
  finishBake('builtin:4');
  await tick(6);
  peakBgInFlight = Math.max(peakBgInFlight, inFlight);
  assert.ok(inFlight >= 1 && inFlight <= 2, `visible concurrency should be 1..2, got ${inFlight}`);
  const dispatchedAfterStage = bakeLog.slice(1);
  assert.ok(
    dispatchedAfterStage.every((id) => ['builtin:1', 'builtin:2', 'builtin:3'].includes(id)),
    `only visible cards should dispatch before off-screen cards, got ${dispatchedAfterStage.join(',')}`,
  );

  // User taps off-screen card look:look_8 -> MUST jump the queue immediately and pause further background bakes
  const tapNode = rv.stripEl().querySelector('.rc[data-card="look:look_8"]');
  assert.ok(tapNode, 'tap target look:look_8 must exist');
  tapNode.click();
  await tick(3);
  assert.ok(bakeLog.includes('look:look_8'), 'user tap must dispatch look:look_8 immediately');

  // Finishing an already in-flight visible bake while user tap is pending must NOT launch another background bake yet
  const countBeforeResolve = bakeLog.length;
  const inflightVisible = dispatchedAfterStage[0];
  finishBake(inflightVisible);
  await tick(6);
  assert.equal(bakeLog.length, countBeforeResolve, 'background queue must stay paused while tapped stage bake is pending');

  // Resolve user tap -> stage updates immediately, then remaining visible cards resume
  finishBake('look:look_8');
  await tick(6);
  assert.ok(stageLooks.includes('gpu:k:look:look_8'), 'stage must apply tapped look immediately');

  // Finish remaining visible cards
  for (let guard = 0; guard < 10; guard += 1) {
    const visPending = [...pendingByCard.keys()].filter((k) => ['builtin:1', 'builtin:2', 'builtin:3'].includes(k));
    if (!visPending.length) break;
    for (const id of visPending) finishBake(id);
    await tick(6);
  }

  // Off-screen cards must wait for idle callback (not dispatch eagerly without idle)
  const offscreenStartedBeforeIdle = bakeLog.filter((id) => !['builtin:1', 'builtin:2', 'builtin:3', 'builtin:4', 'look:look_8'].includes(id));
  assert.equal(offscreenStartedBeforeIdle.length, 0, `off-screen cards should wait for idle, got ${offscreenStartedBeforeIdle.join(',')}`);
  assert.ok(idleQueue.length > 0, 'off-screen cards should schedule an idle callback');

  // Trigger idle callback -> at most 1 off-screen card dispatches at a time
  const nextIdle = idleQueue.shift();
  nextIdle();
  await tick(6);
  assert.ok(inFlight <= 1, `off-screen idle concurrency must be <= 1, got ${inFlight}`);
  assert.ok(peakBgInFlight <= 2, `peak background concurrency must be <= 2, got ${peakBgInFlight}`);

  rv.destroy();
});

test('2. GridView: update(id) updates single cell keeping DOM identity, re-sorts on captureMs, skips closed bitmap', async () => {
  const { getDrawCalls, resetDrawCalls } = setupEnv();
  const { GridView } = await import('../../mobile/src/app/grid.ts');

  const baseMs = Date.UTC(2026, 10, 20, 10, 0, 0);
  const photos = [
    makePhoto('p1', baseMs + 1000),
    makePhoto('p2', baseMs + 2000),
    makePhoto('p3', baseMs + 3000),
    makePhoto('p4', baseMs + 4000),
  ];
  const byId = new Map(photos.map((p) => [p.id, p]));
  const api = {
    session: () => 1,
    photos: () => photos,
    photo: (id) => byId.get(id),
    order: () => 'oldest',
    gap: () => 5,
    ignored: () => 0,
    hasDat: () => false,
    loading: () => ({ done: photos.filter((p) => p.status !== 'pending').length, total: photos.length }),
    cardOut: () => false,
    book: () => ({ datError: null, datNote: null, pack: null, loadingDefault: false }),
    watch: () => {},
    open: () => {},
    remove: () => {},
    clear: () => {},
    addPhotos: () => {},
    showHelp: () => {},
    showCalib: () => {},
    setOrder: () => {},
    setGap: () => {},
    exportPhotos: () => {},
  };

  const host = document.createElement('div');
  document.body.append(host);
  const grid = new GridView(host, api);
  grid.show();

  assert.equal(typeof grid.update, 'function', 'GridView must expose update(id) for single-cell updates');

  const beforeNodes = new Map(host.querySelectorAll('.cell').map((el) => [el.dataset.id, el]));
  assert.equal(beforeNodes.size, 4);
  resetDrawCalls();

  // Single cell thumbnail arrives without changing sort order -> DOM node identity kept for all cells
  photos[1].bitmap = { width: 120, height: 80, _closed: false, close() { this._closed = true; this.width = 0; this.height = 0; } };
  photos[1].status = 'ready';
  grid.update('p2');

  const afterNodes = new Map(host.querySelectorAll('.cell').map((el) => [el.dataset.id, el]));
  for (const id of ['p1', 'p2', 'p3', 'p4']) {
    assert.strictEqual(afterNodes.get(id), beforeNodes.get(id), `cell ${id} DOM node must be preserved on single-cell update`);
  }
  assert.equal(getDrawCalls(), 1, 'only the updated cell should drawImage');
  assert.ok(host.querySelector('.nav .t small').textContent.includes('1/4'));

  // Capture time arrives on p4 making it earliest -> re-sort moves p4 first while preserving existing drawn cell p2
  resetDrawCalls();
  photos[3].captureMs = baseMs - 60_000;
  photos[3].bitmap = { width: 120, height: 80, _closed: false, close() { this._closed = true; this.width = 0; this.height = 0; } };
  photos[3].status = 'ready';
  grid.update('p4');

  const sortedIds = host.querySelectorAll('.cell').map((el) => el.dataset.id);
  assert.deepEqual(sortedIds, ['p4', 'p1', 'p2', 'p3'], 'captureMs arrival must re-sort grid');
  assert.strictEqual(host.querySelector('.cell[data-id="p2"]'), beforeNodes.get('p2'), 're-sort should reuse already-mounted cell p2');
  assert.equal(getDrawCalls(), 1, 're-sort should only draw newly ready p4, not redraw p2');

  // Closed ImageBitmap must NEVER be passed to drawImage
  resetDrawCalls();
  const closedBmp = { width: 0, height: 0, _closed: true, close() {} };
  photos[2].bitmap = closedBmp;
  photos[2].status = 'ready';
  assert.doesNotThrow(() => grid.update('p3'));
  assert.equal(getDrawCalls(), 0, 'closed ImageBitmap must not be drawn');

  grid.destroy();
});

test('3. No forced layout reads when fitGpuCanvas uses cached ResizeObserver box', async () => {
  setupEnv();
  const { fitGpuCanvas } = await import('../../mobile/src/app/editor_hist.ts');

  let reads = 0;
  const stage = document.createElement('div');
  const pv = document.createElement('div');
  const gpu = document.createElement('canvas');
  for (const node of [stage, pv]) {
    for (const prop of ['clientWidth', 'clientHeight']) {
      Object.defineProperty(node, prop, {
        configurable: true,
        get() {
          reads += 1;
          return prop === 'clientWidth' ? 390 : 600;
        },
      });
    }
  }

  // Passing cached ResizeObserver box must perform ZERO synchronous layout reads
  const changed = fitGpuCanvas(stage, pv, gpu, { w: 4288, h: 2848 }, { w: 390, h: 600 });
  assert.equal(changed, true);
  assert.equal(reads, 0, `fitGpuCanvas with cached box read layout ${reads} times`);
  assert.equal(gpu.style.width, '390px');
  assert.equal(gpu.style.height, '259px');
});

test('4. Large-library: desktop gallery eager/high-priority first 16 thumbs + content-visibility CSS', async () => {
  setupEnv();
  const appCss = readFileSync(new URL('../../web/assets/app.css', import.meta.url), 'utf8');
  const ipadCss = readFileSync(new URL('../../mobile/src/app/ipad.css', import.meta.url), 'utf8');
  assert.match(appCss, /\.grid\s+\.day[\s\S]*?content-visibility:\s*auto/, 'web/assets/app.css must apply content-visibility: auto to off-screen .grid .day');
  assert.match(appCss, /\.grid\s+\.day[\s\S]*?contain-intrinsic-(?:size|block-size):/, 'web/assets/app.css must pair contain-intrinsic-size');
  assert.match(ipadCss, /\.ipad-filmstrip[\s\S]*?content-visibility:\s*auto/, 'ipad.css must apply content-visibility: auto to off-screen filmstrip cells');
  assert.match(ipadCss, /contain-intrinsic-size:\s*120px\s+80px/, 'ipad.css must set 120px 80px intrinsic size for 13" filmstrip');
  assert.match(ipadCss, /contain-intrinsic-size:\s*96px\s+64px/, 'ipad.css must set 96px 64px intrinsic size for 11" filmstrip');
  assert.match(ipadCss, /contain-intrinsic-size:\s*108px\s+72px/, 'ipad.css must set 108px 72px intrinsic size for portrait filmstrip');

  // Verify gallery.js sets loading="eager" + fetchpriority="high" for first 12-16 thumbs, lazy for later ones
  const { FilmstripView } = await import('../../mobile/src/app/filmstrip.ts');
  const baseMs = Date.UTC(2026, 10, 20, 10, 0, 0);
  const many = Array.from({ length: 30 }, (_, i) => makePhoto(`f${i}`, baseMs + i * 1000, { status: 'ready' }));
  const byId = new Map(many.map((p) => [p.id, p]));
  const fsView = new FilmstripView(
    {
      photos: () => many,
      photo: (id) => byId.get(id),
      gap: () => 5,
      order: () => 'oldest',
      open: () => {},
    },
    'f0',
  );
  fsView.setActive(true);
  const cells = fsView.el().querySelectorAll('.cell');
  assert.equal(cells.length, 30);
  assert.ok(!cells[0].classList.contains('cv-auto'), 'current visible filmstrip cell should stay eager');
  assert.ok(cells[29].classList.contains('cv-auto'), 'far off-screen filmstrip cell should get cv-auto class');
  fsView.destroy();
});

test('5. Chinese IME Enter while composing does not submit (logo_sheet, lut_sheet, frame_logo_ui)', async () => {
  setupEnv();

  // 5a. mobile/src/app/lut_sheet.ts
  const { openLutActionSheet } = await import('../../mobile/src/app/lut_sheet.ts');
  const lutHost = document.createElement('div');
  document.body.append(lutHost);
  let lutRenameCalls = 0;
  const fakeLutBook = {
    async rename() {
      lutRenameCalls += 1;
      return { ok: true, record: { cube_id: 'c1', name: '新名字', filename: 'a.cube' } };
    },
  };
  openLutActionSheet(lutHost, fakeLutBook, { cube_id: 'c1', name: '旧名字', filename: 'a.cube' }, { onChanged: () => {}, onDeleted: () => {} });
  lutHost.querySelector('[data-act="lut-rename"]').click();
  const lutInput = lutHost.querySelector('input.lut-name-input');
  assert.ok(lutInput, 'LUT rename input must open');

  const imeEvent1 = new Event('keydown', { cancelable: true });
  Object.assign(imeEvent1, { key: 'Enter', isComposing: true, keyCode: 13 });
  lutInput.dispatchEvent(imeEvent1);
  await tick(3);
  assert.equal(lutRenameCalls, 0, 'lut_sheet must not submit when e.isComposing is true');

  const imeEvent2 = new Event('keydown', { cancelable: true });
  Object.assign(imeEvent2, { key: 'Enter', isComposing: false, keyCode: 229 });
  lutInput.dispatchEvent(imeEvent2);
  await tick(3);
  assert.equal(lutRenameCalls, 0, 'lut_sheet must not submit when e.keyCode === 229');

  const realEnter = new Event('keydown', { cancelable: true });
  Object.assign(realEnter, { key: 'Enter', isComposing: false, keyCode: 13 });
  lutInput.dispatchEvent(realEnter);
  await tick(3);
  assert.equal(lutRenameCalls, 1, 'lut_sheet must submit on non-composing Enter');

  // 5b. mobile/src/app/logo_sheet.ts
  const { logoStore } = await import('../../mobile/src/app/logo_store.ts');
  const { openLogoSheet } = await import('../../mobile/src/app/logo_sheet.ts');
  let logoRenameCalls = 0;
  const origList = logoStore.list.bind(logoStore);
  const origLoaded = logoStore.loaded.bind(logoStore);
  const origEnsure = logoStore.ensureLoaded.bind(logoStore);
  const origCanvas = logoStore.canvas.bind(logoStore);
  const origRename = logoStore.rename.bind(logoStore);
  const origInfo = logoStore.info.bind(logoStore);
  const sampleLogo = { id: 'l1', name: '原标志', w: 64, h: 32, coverage: 0.5, opaque: false, lum: 0.2 };
  logoStore.list = () => [sampleLogo];
  logoStore.loaded = () => true;
  logoStore.ensureLoaded = async () => [sampleLogo];
  logoStore.canvas = () => null;
  logoStore.info = (id) => (id === 'l1' ? sampleLogo : null);
  logoStore.rename = async () => {
    logoRenameCalls += 1;
    return { ok: true, logo: sampleLogo };
  };
  try {
    const logoHost = document.createElement('div');
    document.body.append(logoHost);
    const p = makePhoto('plogo', 1_700_000_000_000);
    openLogoSheet(logoHost, p, () => {});
    const moreBtn = logoHost.querySelectorAll('button').find((b) => b.getAttribute('aria-label') === '原标志 更多操作');
    assert.ok(moreBtn, 'logo row more button must exist');
    moreBtn.click();
    const renameBtn = logoHost.querySelectorAll('.logo-row-acts button').find((b) => b.textContent === '重命名');
    assert.ok(renameBtn, 'logo rename button must exist');
    renameBtn.click();
    const logoInput = logoHost.querySelector('.logo-row-acts input.txtbox');
    assert.ok(logoInput, 'logo rename input must open');

    const ev1 = new Event('keydown', { cancelable: true });
    Object.assign(ev1, { key: 'Enter', isComposing: true, keyCode: 13 });
    logoInput.dispatchEvent(ev1);
    await tick(3);
    assert.equal(logoRenameCalls, 0, 'logo_sheet must not submit when e.isComposing is true');

    const ev2 = new Event('keydown', { cancelable: true });
    Object.assign(ev2, { key: 'Enter', isComposing: false, keyCode: 229 });
    logoInput.dispatchEvent(ev2);
    await tick(3);
    assert.equal(logoRenameCalls, 0, 'logo_sheet must not submit when e.keyCode === 229');

    const ev3 = new Event('keydown', { cancelable: true });
    Object.assign(ev3, { key: 'Enter', isComposing: false, keyCode: 13 });
    logoInput.dispatchEvent(ev3);
    await tick(3);
    assert.equal(logoRenameCalls, 1, 'logo_sheet must submit on non-composing Enter');
  } finally {
    logoStore.list = origList;
    logoStore.loaded = origLoaded;
    logoStore.ensureLoaded = origEnsure;
    logoStore.canvas = origCanvas;
    logoStore.rename = origRename;
    logoStore.info = origInfo;
  }

  // 5c. web/assets/frame_logo_ui.js (also confirm false positives untouched)
  const frameLogoSrc = readFileSync(new URL('../../web/assets/frame_logo_ui.js', import.meta.url), 'utf8');
  assert.match(
    frameLogoSrc,
    /if\s*\(\s*e\.key\s*===\s*'Enter'\s*\)[\s\S]{0,80}e\.isComposing\s*\|\|\s*e\.keyCode\s*===\s*229/,
    'frame_logo_ui.js must guard Enter with e.isComposing || e.keyCode === 229',
  );
  const devGlyphSrc = readFileSync(new URL('../../mobile/src/core/dev_glyph_masks.ts', import.meta.url), 'utf8');
  assert.ok(!devGlyphSrc.includes('toSorted'), 'dev_glyph_masks.ts false positive must stay untouched');
});

test('6. AdjustView: disabled state marks panel, styles disabled slider, and shows one-line reason', async () => {
  setupEnv();
  const { AdjustView } = await import('../../mobile/src/app/adjust_view.ts');
  const photo = makePhoto('p_adj', 1_700_000_000_000);
  const view = new AdjustView({
    photo: () => photo,
    push: () => {},
    enterDrag: () => {},
    leaveDrag: () => {},
    dragging: () => false,
  });
  const panel = view.panelEl();
  const input = panel.querySelector('input');
  const reason = panel.querySelector('[data-adjust-disabled-reason="1"]');
  assert.ok(reason, 'AdjustView must render a one-line disabled reason element');
  assert.equal(reason.hidden, true, 'disabled reason must be hidden when enabled');

  view.setDisabled(true);
  assert.equal(panel.dataset.disabled, '1');
  assert.equal(input.disabled, true);
  assert.equal(reason.hidden, false, 'disabled reason must be visible when disabled');
  assert.ok(reason.textContent.trim().length > 0, 'disabled reason text must be non-empty');
  assert.ok(input.getAttribute('aria-describedby'), 'disabled slider must reference reason via aria-describedby');

  view.setDisabled(false);
  assert.equal(panel.dataset.disabled, '0');
  assert.equal(input.disabled, false);
  assert.equal(reason.hidden, true, 'disabled reason must hide again when re-enabled');
  assert.equal(input.getAttribute('aria-describedby'), null);
  view.destroy();
});
