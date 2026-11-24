// UI-M4 生命周期自检清单（Items 1–6）的 Node 单元测试。
// 覆盖：重试预算与退避、资源释放、原子持久化回滚、以及边界非法输入拒绝。

import assert from 'node:assert/strict';
import test from 'node:test';
import { existsSync, readFileSync } from 'node:fs';
import { registerHooks } from 'node:module';
import path from 'node:path';
import { pathToFileURL } from 'node:url';

registerHooks({
  resolve(specifier, context, nextResolve) {
    if (specifier.includes('?url')) {
      return { shortCircuit: true, url: `virtual:url:${encodeURIComponent(specifier)}` };
    }
    try {
      return nextResolve(specifier, context);
    } catch (err) {
      if ((specifier.startsWith('./') || specifier.startsWith('../')) && !/\.[a-z]+$/i.test(specifier)) {
        return nextResolve(`${specifier}.ts`, context);
      }
      throw err;
    }
  },
  load(url, context, nextLoad) {
    if (url.startsWith('virtual:url:')) {
      return {
        shortCircuit: true,
        format: 'module',
        source: `export default ${JSON.stringify(decodeURIComponent(url.slice('virtual:url:'.length)))};`,
      };
    }
    return nextLoad(url, context);
  },
});

// --- Minimal DOM/Canvas/IDB mocks installed BEFORE importing browser-targeting modules if needed ---
function makeFakeCanvas(width = 0, height = 0) {
  const ctx = {
    canvas: null,
    imageSmoothingQuality: 'high',
    globalCompositeOperation: 'source-over',
    globalAlpha: 1,
    filter: 'none',
    fillStyle: '#000',
    strokeStyle: '#000',
    lineWidth: 1,
    font: '',
    fontKerning: 'none',
    textBaseline: 'alphabetic',
    textAlign: 'left',
    clearRect() {},
    fillRect() {},
    drawImage(src) {
      if (src && typeof src === 'object' && 'closed' in src && src.closed) {
        throw new DOMException('Cannot draw closed ImageBitmap', 'InvalidStateError');
      }
    },
    putImageData() {},
    getImageData(_x, _y, w, h) {
      return { width: w, height: h, data: new Uint8ClampedArray(w * h * 4) };
    },
    createImageData(w, h) {
      return { width: w, height: h, data: new Uint8ClampedArray(w * h * 4) };
    },
    createRadialGradient() {
      return { addColorStop() {} };
    },
    save() {},
    restore() {},
    translate() {},
    scale() {},
    rotate() {},
    beginPath() {},
    moveTo() {},
    lineTo() {},
    closePath() {},
    fill() {},
    stroke() {},
    clip() {},
    ellipse() {},
    roundRect() {},
    fillText() {},
  };
  const canvas = {
    width,
    height,
    dataset: {},
    parentElement: null,
    attributes: {},
    setAttribute(k, v) {
      this.attributes[k] = v;
    },
    getContext() {
      return ctx;
    },
  };
  ctx.canvas = canvas;
  return canvas;
}

const createdCanvases = [];
if (typeof globalThis.document === 'undefined') {
  globalThis.document = {
    fonts: { add() {} },
    createElement(tag) {
      if (tag === 'canvas') {
        const cv = makeFakeCanvas(0, 0);
        createdCanvases.push(cv);
        return cv;
      }
      return { style: {}, dataset: {}, classList: { add() {}, remove() {}, toggle() {}, contains: () => false }, children: [] };
    },
  };
}
if (typeof globalThis.ImageData === 'undefined') {
  globalThis.ImageData = class ImageData {
    constructor(data, width, height) {
      this.data = data;
      this.width = width;
      this.height = height;
    }
  };
}
if (typeof globalThis.DOMException === 'undefined') {
  globalThis.DOMException = class DOMException extends Error {
    constructor(msg, name) {
      super(msg);
      this.name = name;
    }
  };
}

const fontLoader = await import('../../mobile/src/app/font_loader.ts');
const finishBg = await import('../../mobile/src/app/finish_bg.ts');
const finishTexture = await import('../../mobile/src/app/finish_texture.ts');
const finishDraw = await import('../../mobile/src/app/finish_draw.ts');
const logoKey = await import('../../mobile/src/lib/logo_key.ts');
const frameSpec = await import('../../mobile/src/lib/frame_spec.ts');

test('Checklist #4: font_loader enforces retry budget & exponential backoff and stops when !isAlive', async () => {
  assert.equal(typeof fontLoader.MAX_FONT_ATTEMPTS, 'number');
  assert.ok(fontLoader.MAX_FONT_ATTEMPTS >= 2 && fontLoader.MAX_FONT_ATTEMPTS <= 5);
  assert.ok(Array.isArray(fontLoader.FONT_RETRY_BACKOFF_MS) && fontLoader.FONT_RETRY_BACKOFF_MS.length >= 2);
  assert.equal(typeof fontLoader.resetFontLoaderForTest, 'function');
  assert.equal(typeof fontLoader.setFontLoaderHooksForTest, 'function');

  let now = 1000;
  let fetchCalls = 0;
  const origFetch = globalThis.fetch;
  globalThis.fetch = async () => {
    fetchCalls += 1;
    return { ok: false, status: 500, arrayBuffer: async () => new ArrayBuffer(0) };
  };

  try {
    fontLoader.resetFontLoaderForTest();
    fontLoader.setFontLoaderHooksForTest({ clock: () => now });

    // Attempt 1 fails -> backoff[0] applied
    let readyCalls = 0;
    let alive = true;
    const ret1 = fontLoader.ensureFontsLoaded(['atkinson'], () => {
      readyCalls += 1;
    }, () => alive);
    assert.equal(ret1, false);
    await new Promise((r) => setTimeout(r, 10));
    assert.equal(fetchCalls, 1);

    // Calling repeatedly inside backoff window must NOT spin fetches/microtasks
    for (let i = 0; i < 10; i++) {
      fontLoader.ensureFontsLoaded(['atkinson'], () => {
        readyCalls += 1;
      }, () => alive);
    }
    await new Promise((r) => setTimeout(r, 10));
    assert.equal(fetchCalls, 1, 'must respect backoff without spinning');

    // Advance past backoff 0 -> attempt 2
    now += fontLoader.FONT_RETRY_BACKOFF_MS[0] + 1;
    fontLoader.ensureFontsLoaded(['atkinson'], () => {
      readyCalls += 1;
    }, () => alive);
    await new Promise((r) => setTimeout(r, 10));
    assert.equal(fetchCalls, 2);

    // Advance past remaining attempts until budget exhausted
    for (let attempt = 2; attempt < fontLoader.MAX_FONT_ATTEMPTS; attempt++) {
      now += fontLoader.FONT_RETRY_BACKOFF_MS[Math.min(attempt - 1, fontLoader.FONT_RETRY_BACKOFF_MS.length - 1)] + 1;
      fontLoader.ensureFontsLoaded(['atkinson']);
      await new Promise((r) => setTimeout(r, 10));
    }
    assert.equal(fetchCalls, fontLoader.MAX_FONT_ATTEMPTS);

    // Once budget exhausted, further calls even far in the future never retry
    now += 1_000_000;
    for (let i = 0; i < 5; i++) {
      fontLoader.ensureFontsLoaded(['atkinson']);
      await fontLoader.loadFontFace('atkinson').catch(() => {});
    }
    assert.equal(fetchCalls, fontLoader.MAX_FONT_ATTEMPTS, 'must stop permanently after retry budget');
    assert.equal(readyCalls, 0);

    // Also test !isAlive suppresses late onReady callback on success
    fontLoader.resetFontLoaderForTest();
    const origFontFace = globalThis.FontFace;
    globalThis.FontFace = class {
      constructor() {}
      async load() {}
    };
    globalThis.fetch = async () => {
      fetchCalls += 1;
      return { ok: true, status: 200, arrayBuffer: async () => new ArrayBuffer(8) };
    };
    alive = false;
    fontLoader.ensureFontsLoaded(['inter'], () => {
      readyCalls += 1;
    }, () => alive);
    await new Promise((r) => setTimeout(r, 10));
    assert.equal(readyCalls, 0, 'late FontFace success must not invoke callback after switch/destroy');
    globalThis.FontFace = origFontFace;
  } finally {
    globalThis.fetch = origFetch;
    fontLoader.resetFontLoaderForTest();
  }
});

test('Checklist #3 & #4: finish_bg caps idle skips, returns cancel handle, and releases canvases', () => {
  assert.equal(typeof finishBg.MAX_BLUR_IDLE_SKIPS, 'number');
  assert.ok(finishBg.MAX_BLUR_IDLE_SKIPS <= 16);
  assert.equal(typeof finishBg.clearBlurCache, 'function');

  const scheduled = new Map();
  let nextIdleId = 1;
  const origReqIdle = globalThis.requestIdleCallback;
  const origCancelIdle = globalThis.cancelIdleCallback;

  globalThis.requestIdleCallback = (cb) => {
    const id = nextIdleId++;
    scheduled.set(id, cb);
    return id;
  };
  globalThis.cancelIdleCallback = (id) => {
    scheduled.delete(id);
  };

  try {
    const photoCv = makeFakeCanvas(200, 150);
    const blurFrame = {
      width: 300,
      height: 400,
      photo: [20, 20],
      bg: [0, 0, 0],
      items: [],
      bgfx: { kind: 'blur', sigma: 16, veil: 0.7, sat: 0.8 },
    };

    // Calling with low remaining time must stop after MAX_BLUR_IDLE_SKIPS
    const cancel1 = finishBg.primeBlurFrames(photoCv, [blurFrame], () => true);
    assert.equal(typeof cancel1, 'function');
    let runs = 0;
    while (scheduled.size > 0 && runs < 50) {
      runs += 1;
      const [[id, cb]] = scheduled.entries();
      scheduled.delete(id);
      cb({ didTimeout: false, timeRemaining: () => 0 });
    }
    assert.equal(scheduled.size, 0, 'idle skip loop must terminate cleanly');
    assert.ok(runs <= finishBg.MAX_BLUR_IDLE_SKIPS + 2, `excessive idle retries: ${runs}`);

    // Cancelling returned handle must remove scheduled idle callback
    const blurFrame2 = { ...blurFrame, width: 320 };
    const cancel2 = finishBg.primeBlurFrames(photoCv, [blurFrame2], () => true);
    assert.equal(scheduled.size, 1);
    cancel2();
    assert.equal(scheduled.size, 0, 'cancel handle must cancel pending requestIdleCallback');

    // blurBackdrop zeros temp small canvas & clearBlurCache zeros cached canvases
    const beforeLen = createdCanvases.length;
    const out = finishBg.blurBackdrop(photoCv, blurFrame);
    assert.ok(out !== null && out.width > 0 && out.height > 0);
    const createdDuring = createdCanvases.slice(beforeLen);
    assert.ok(createdDuring.length >= 2);
    // Temp small canvas (all created except cached `out`) must already have width===0, height===0
    for (const cv of createdDuring) {
      if (cv !== out) {
        assert.equal(cv.width, 0, 'temp blur canvas width must be zeroed');
        assert.equal(cv.height, 0, 'temp blur canvas height must be zeroed');
      }
    }
    finishBg.clearBlurCache(photoCv);
    assert.equal(out.width, 0, 'cached blur canvas width must be zeroed on clearBlurCache');
    assert.equal(out.height, 0, 'cached blur canvas height must be zeroed on clearBlurCache');
  } finally {
    globalThis.requestIdleCallback = origReqIdle;
    globalThis.cancelIdleCallback = origCancelIdle;
  }
});

test('Checklist #3: finish_draw and finish_texture release cached canvases and temp buffers cleanly', () => {
  assert.equal(typeof finishDraw.releaseFinishDrawResources, 'function');
  assert.equal(typeof finishTexture.clearTextureCaches, 'function');

  // Populate finish_texture grains & contours and finish_draw scratch/tint
  const sampleFrame = {
    width: 120,
    height: 160,
    photo: [10, 10],
    bg: [245, 245, 245],
    grain: 0.08,
    items: [
      {
        k: 'carrier',
        x: 10,
        y: 10,
        w: 100,
        h: 100,
        c: 2,
        jit: 1,
        seed: 42,
        fill: [20, 20, 20],
        paper: [245, 245, 245],
      },
    ],
  };
  const targetCv = makeFakeCanvas(120, 160);
  const startLen = createdCanvases.length;
  finishDraw.drawDecor(targetCv.getContext('2d'), {
    key: 'test',
    frame: sampleFrame,
    stamp: null,
    stampOrigin: null,
    ew: 100,
    eh: 100,
    width: 120,
    height: 160,
  }, 1);
  const grainCanvases = createdCanvases.slice(startLen);
  assert.ok(grainCanvases.some((c) => c.width > 0), 'expected grain canvas to be allocated');

  finishDraw.releaseFinishDrawResources();
  for (const c of grainCanvases) {
    assert.equal(c.width, 0, 'grain/scratch canvas width must be zeroed on releaseFinishDrawResources');
    assert.equal(c.height, 0, 'grain/scratch canvas height must be zeroed on releaseFinishDrawResources');
  }
});

test('Checklist #5: idb logo operations use atomic transactions and logoStore rollback preserves state on failure', async () => {
  let openedVersion = 0;
  const stores = new Set();
  const txLog = [];
  let failPut = false;
  let failRename = false;
  let failDelete = false;
  const dbRows = new Map();

  function makeReq(executor) {
    const req = { result: undefined, error: null, onsuccess: null, onerror: null };
    queueMicrotask(() => {
      try {
        req.result = executor();
        req.onsuccess?.();
      } catch (e) {
        req.error = e;
        req.onerror?.();
      }
    });
    return req;
  }

  globalThis.indexedDB = {
    open(_name, version) {
      openedVersion = version;
      const req = {
        result: {
          objectStoreNames: { contains: (s) => stores.has(s) },
          createObjectStore(s) {
            stores.add(s);
          },
          close() {},
          transaction(storeName, mode) {
            txLog.push({ storeName, mode });
            const tx = {
              error: null,
              oncomplete: null,
              onerror: null,
              onabort: null,
              objectStore() {
                return {
                  getAll: () => makeReq(() => [...dbRows.values()]),
                  get: (id) => makeReq(() => dbRows.get(id)),
                  put: (row) => {
                    if (failPut || (failRename && row.id === 'id1')) {
                      queueMicrotask(() => {
                        tx.error = new DOMException('aborted', 'AbortError');
                        tx.onabort?.();
                      });
                      return {};
                    }
                    dbRows.set(row.id, row);
                    queueMicrotask(() => tx.oncomplete?.());
                    return {};
                  },
                  delete: (id) => {
                    if (failDelete) {
                      queueMicrotask(() => {
                        tx.error = new DOMException('aborted', 'AbortError');
                        tx.onabort?.();
                      });
                      return {};
                    }
                    dbRows.delete(id);
                    queueMicrotask(() => tx.oncomplete?.());
                    return {};
                  },
                };
              },
            };
            if (mode === 'readonly') {
              queueMicrotask(() => queueMicrotask(() => tx.oncomplete?.()));
            }
            return tx;
          },
        },
        onupgradeneeded: null,
        onsuccess: null,
        onerror: null,
      };
      queueMicrotask(() => {
        req.onupgradeneeded?.();
        req.onsuccess?.();
      });
      return req;
    },
  };

  const idbMod = await import('../../mobile/src/lib/idb.ts');
  const existing = {
    id: 'id1',
    name: 'OrigMark',
    w: 64,
    h: 64,
    coverage: 0.5,
    opaque: false,
    lum: 100,
    rgba: new ArrayBuffer(64 * 64 * 4),
    createdAt: 100,
  };
  dbRows.set('id1', existing);

  const loaded = await idbMod.loadAllLogos();
  assert.equal(openedVersion, 4, 'IndexedDB version must be 4 after merging frame + LUT + camfiles');
  for (const name of ['calib', 'camfiles', 'logos', 'lut_records', 'lut_blobs', 'lut_meta']) {
    assert.ok(stores.has(name), `missing store ${name}`);
  }
  assert.equal(loaded.length, 1);

  // Calling upgrade check when stores already exist must not throw
  await idbMod.loadAllLogos();

  // Failed rename leaves DB intact
  failRename = true;
  await assert.rejects(() => idbMod.renameStoredLogo('id1', 'NewName'), /aborted/);
  assert.equal(dbRows.get('id1').name, 'OrigMark');
  failRename = false;

  // Failed delete leaves DB intact
  failDelete = true;
  await assert.rejects(() => idbMod.deleteStoredLogo('id1'), /aborted/);
  assert.equal(dbRows.has('id1'), true);
  failDelete = false;

  // Also check logoStore atomic error handling when IDB aborts
  const { logoStore } = await import('../../mobile/src/app/logo_store.ts');
  await logoStore.ensureLoaded();
  const revBefore = logoStore.revision();
  failRename = true;
  const renameRes = await logoStore.rename('id1', 'BadWrite');
  assert.equal(renameRes.ok, false);
  assert.equal(logoStore.info('id1')?.name, 'OrigMark', 'in-memory name must not mutate on failed IDB write');
  assert.equal(logoStore.revision(), revBefore, 'revision must not bump on failed rename');
  failRename = false;

  failDelete = true;
  const delRes = await logoStore.remove('id1');
  assert.equal(delRes.ok, false);
  assert.ok(logoStore.info('id1'), 'in-memory logo must remain on failed IDB delete');
  assert.equal(logoStore.revision(), revBefore, 'revision must not bump on failed delete');
  failDelete = false;
});

test('Checklist #6: hostile scalar/string inputs rejected without saturation', async () => {
  const src = { width: 4, height: 4, data: new Uint8ClampedArray(4 * 4 * 4).fill(255) };
  for (const badTol of [Number.NaN, Number.POSITIVE_INFINITY, Number.NEGATIVE_INFINITY, -1, 61, 1.5, 2 ** 32 + 1]) {
    assert.throws(
      () => logoKey.keyWhiteImageData(src, badTol),
      (err) => err instanceof Error && err.message.length > 0,
      `keyWhiteImageData must reject hostile tol=${badTol} instead of saturating`,
    );
  }
  assert.doesNotThrow(() => logoKey.keyWhiteImageData(src, 0));
  assert.doesNotThrow(() => logoKey.keyWhiteImageData(src, 60));

  // frame_spec.ts must reject over-long strings and invalid logo ids / versions without saturating
  const overLong = frameSpec.normalizeFrame({ ...frameSpec.DEFAULT_FRAME, style: 'leica', text: 'A'.repeat(25) });
  assert.equal(overLong.style, 'none', 'over-long text in normalizeFrame must reset to default, not truncate');
  const badLogo = frameSpec.normalizeFrame({ ...frameSpec.DEFAULT_FRAME, style: 'leica', logo: 'bad logo!' });
  assert.equal(badLogo.style, 'none');

  // Verify real WASM export_meta_photo rejects hostile tz_offset_min / mtime_ms AND worker.ts does not clamp them
  const wasmJs = path.resolve('mobile/src/wasm-pkg/gxr_wasm.js');
  const wasmBin = path.resolve('mobile/src/wasm-pkg/gxr_wasm_bg.wasm');
  assert.ok(existsSync(wasmJs) && existsSync(wasmBin), 'mobile wasm pkg required');
  const workerSource = readFileSync(path.resolve('mobile/src/core/worker.ts'), 'utf8');
  assert.ok(!workerSource.includes('Math.max(-1439, Math.min(1439'), 'worker.ts must not saturate tzOffsetMin');

  const mod = await import(pathToFileURL(wasmJs).href);
  await mod.default(readFileSync(wasmBin));

  for (const badTz of [Number.NaN, Number.POSITIVE_INFINITY, Number.NEGATIVE_INFINITY, 1.5, -1440, 1440, 2 ** 32 + 1]) {
    assert.throws(
      () => mod.export_meta_photo(undefined, undefined, 'A.JPG', 1700000000000, badTz),
      (err) => String(err).includes('bad_meta'),
      `WASM export_meta_photo must reject hostile tzOffsetMin=${badTz}`,
    );
  }
  for (const badMtime of [Number.NaN, Number.POSITIVE_INFINITY, Number.NEGATIVE_INFINITY, 1e20, -1e20]) {
    assert.throws(
      () => mod.export_meta_photo(undefined, undefined, 'A.JPG', badMtime, 0),
      (err) => String(err).includes('bad_meta'),
      `WASM export_meta_photo must reject hostile mtimeMs=${badMtime}`,
    );
  }
});

test('S1-3: normalizeFrame, normalizeStamp, and restore helpers refuse CJK without retaining it', async () => {
  const fontsMod = await import('../../mobile/src/lib/fonts.ts');

  const fCjk = frameSpec.normalizeFrame({ ...frameSpec.DEFAULT_FRAME, style: 'leica', text: '上海' });
  assert.equal(fCjk.style, 'none', 'normalizeFrame must reject CJK text');
  assert.equal(fCjk.text, '', 'normalizeFrame must not retain CJK');

  const sCjk = frameSpec.normalizeStamp({ enabled: true, content: 'text', text: '上海' });
  assert.equal(sCjk.enabled, false, 'normalizeStamp must reject CJK text');
  assert.equal(sCjk.text, '', 'normalizeStamp must not retain CJK');

  const sOver = frameSpec.normalizeStamp({ enabled: true, content: 'text', text: 'B'.repeat(25) });
  assert.equal(sOver.enabled, false, 'normalizeStamp must reject over-long text');
  assert.equal(sOver.text, '', 'normalizeStamp must not truncate over-long text');

  const rf = frameSpec.restoreFrameSpec({ ...frameSpec.DEFAULT_FRAME, style: 'leica' }, 'AB中CD');
  assert.equal(rf.ok, false);
  assert.equal(rf.error, fontsMod.CJK_REFUSE_MESSAGE);
  assert.equal(rf.spec.text, 'ABCD');

  const rs = frameSpec.restoreStampSpec({ ...frameSpec.DEFAULT_STAMP, enabled: true, content: 'text' }, '上海');
  assert.equal(rs.ok, false);
  assert.equal(rs.error, fontsMod.CJK_REFUSE_MESSAGE);
  assert.equal(rs.spec.text, '');
});

test('S1-6: keyWhiteImageData applies 3x3 ring soft-alpha on all 4 edges and 4 corners', () => {
  const w = 128;
  const h = 128;

  const paintRect = (x0, y0, x1, y1) => {
    const data = new Uint8ClampedArray(w * h * 4).fill(255);
    for (let y = y0; y <= y1; y++) {
      for (let x = x0; x <= x1; x++) {
        const o = (y * w + x) * 4;
        data[o] = 130;
        data[o + 1] = 130;
        data[o + 2] = 130;
        data[o + 3] = 255;
      }
    }
    return { width: w, height: h, data };
  };

  // Case 1 (exact Sol witness): left edge (0,28)..(99,99)
  const leftOut = logoKey.keyWhiteImageData(paintRect(0, 28, 99, 99), 30);
  const alphaAt = (res, x, y) => res.data[(y * w + x) * 4 + 3];
  assert.equal(alphaAt(leftOut, 0, 28), 199, 'left-edge top pixel (0,28) must be softened to 199');
  assert.equal(alphaAt(leftOut, 0, 99), 199, 'left-edge bottom pixel (0,99) must be softened to 199');
  assert.equal(alphaAt(leftOut, 0, 60), 255, 'interior left-edge pixel (0,60) must remain 255');

  // Other 3 edges: top, right, bottom
  const topOut = logoKey.keyWhiteImageData(paintRect(28, 0, 99, 99), 30);
  assert.equal(alphaAt(topOut, 28, 0), 199, 'top-edge corner (28,0) must be 199');
  assert.equal(alphaAt(topOut, 99, 0), 199, 'top-edge corner (99,0) must be 199');
  assert.equal(alphaAt(topOut, 60, 0), 255);

  const rightOut = logoKey.keyWhiteImageData(paintRect(28, 28, w - 1, 99), 30);
  assert.equal(alphaAt(rightOut, w - 1, 28), 199, 'right-edge top pixel must be 199');
  assert.equal(alphaAt(rightOut, w - 1, 99), 199, 'right-edge bottom pixel must be 199');
  assert.equal(alphaAt(rightOut, w - 1, 60), 255);

  const bottomOut = logoKey.keyWhiteImageData(paintRect(28, 28, 99, h - 1), 30);
  assert.equal(alphaAt(bottomOut, 28, h - 1), 199, 'bottom-edge left pixel must be 199');
  assert.equal(alphaAt(bottomOut, 99, h - 1), 199, 'bottom-edge right pixel must be 199');
  assert.equal(alphaAt(bottomOut, 60, h - 1), 255);

  // All 4 corners touching two image boundaries: corner pixel itself has only interior/edge neighbors inside logo,
  // while its two edge-termini have white neighbors and must soften to 199
  const corners = [
    { rect: [0, 0, 40, 40], soft: [[40, 0], [0, 40]], corner: [0, 0] },
    { rect: [w - 41, 0, w - 1, 40], soft: [[w - 41, 0], [w - 1, 40]], corner: [w - 1, 0] },
    { rect: [0, h - 41, 40, h - 1], soft: [[0, h - 41], [40, h - 1]], corner: [0, h - 1] },
    { rect: [w - 41, h - 41, w - 1, h - 1], soft: [[w - 41, h - 1], [w - 1, h - 41]], corner: [w - 1, h - 1] },
  ];
  for (const { rect, soft, corner } of corners) {
    const out = logoKey.keyWhiteImageData(paintRect(...rect), 30);
    for (const [sx, sy] of soft) {
      assert.equal(alphaAt(out, sx, sy), 199, `corner edge terminus (${sx},${sy}) must soften to 199`);
    }
    assert.equal(alphaAt(out, corner[0], corner[1]), 255, `extreme corner (${corner[0]},${corner[1]}) interior stays 255`);
  }
});

test('S3 Same-Family Audit: LogoStore.rename and remove await in-flight ensureLoaded without stale snapshot clobber', async () => {
  const rowsInDb = new Map([
    [
      'l_a',
      {
        id: 'l_a',
        name: 'InitialA',
        w: 16,
        h: 16,
        coverage: 0.4,
        opaque: false,
        lum: 120,
        rgba: new ArrayBuffer(16 * 16 * 4),
        createdAt: 10,
      },
    ],
    [
      'l_b',
      {
        id: 'l_b',
        name: 'InitialB',
        w: 16,
        h: 16,
        coverage: 0.4,
        opaque: false,
        lum: 120,
        rgba: new ArrayBuffer(16 * 16 * 4),
        createdAt: 20,
      },
    ],
  ]);

  let releaseGetAll = null;
  let holdNextGetAll = true;
  const origIdb = globalThis.indexedDB;
  globalThis.indexedDB = {
    open() {
      const req = {
        result: {
          objectStoreNames: { contains: () => true },
          close() {},
          transaction(_store, mode) {
            const tx = { error: null, oncomplete: null, onerror: null, onabort: null };
            return Object.assign(tx, {
              objectStore() {
                return {
                  getAll() {
                    const r = { result: undefined, onsuccess: null, onerror: null };
                    const snap = [...rowsInDb.values()].map((x) => ({ ...x }));
                    if (mode === 'readonly' && holdNextGetAll) {
                      holdNextGetAll = false;
                      releaseGetAll = () => {
                        r.result = snap;
                        r.onsuccess?.();
                        queueMicrotask(() => tx.oncomplete?.());
                      };
                    } else {
                      queueMicrotask(() => {
                        r.result = snap;
                        r.onsuccess?.();
                        queueMicrotask(() => tx.oncomplete?.());
                      });
                    }
                    return r;
                  },
                  get(id) {
                    const r = { result: undefined, onsuccess: null, onerror: null };
                    queueMicrotask(() => {
                      const cur = rowsInDb.get(id);
                      r.result = cur ? { ...cur } : undefined;
                      r.onsuccess?.();
                    });
                    return r;
                  },
                  put(row) {
                    rowsInDb.set(row.id, { ...row });
                    queueMicrotask(() => tx.oncomplete?.());
                    return {};
                  },
                  delete(id) {
                    rowsInDb.delete(id);
                    queueMicrotask(() => tx.oncomplete?.());
                    return {};
                  },
                };
              },
            });
          },
        },
        onupgradeneeded: null,
        onsuccess: null,
        onerror: null,
      };
      queueMicrotask(() => req.onsuccess?.());
      return req;
    },
  };

  try {
    const mod = await import('../../mobile/src/app/logo_store.ts');
    assert.equal(typeof mod.LogoStore, 'function', 'LogoStore class should be exported for isolated testing');
    const store = new mod.LogoStore();

    // Kick off ensureLoaded (held on readonly getAll with pre-mutation snapshot)
    const loadProm = store.ensureLoaded();
    await new Promise((r) => setTimeout(r, 5));
    assert.equal(typeof releaseGetAll, 'function');

    // Start rename('l_a', 'RenamedA') and remove('l_b') while ensureLoaded is still in flight
    const renProm = store.rename('l_a', 'RenamedA');
    const remProm = store.remove('l_b');
    releaseGetAll();

    const [, renRes, remRes] = await Promise.all([loadProm, renProm, remProm]);
    assert.equal(renRes.ok, true);
    assert.equal(remRes.ok, true);
    assert.equal(store.info('l_a')?.name, 'RenamedA', 'committed rename must not be overwritten by earlier ensureLoaded');
    assert.equal(store.info('l_b'), null, 'committed remove must not be resurrected by earlier ensureLoaded');
    assert.deepEqual(store.list().map((x) => x.id), ['l_a']);
  } finally {
    globalThis.indexedDB = origIdb;
  }
});

test('Merge: IndexedDB v2 -> v3 upgrade preserves LUT-only and logos-only v2 databases', async () => {
  const origIdb = globalThis.indexedDB;

  function makeV2Db(initialStores) {
    let currentVersion = 2;
    const storeMaps = new Map();
    for (const [name, keyPath] of initialStores) {
      storeMaps.set(name, { keyPath, rows: new Map() });
    }
    return {
      get version() {
        return currentVersion;
      },
      storeNames() {
        return [...storeMaps.keys()].sort();
      },
      seed(storeName, key, val) {
        storeMaps.get(storeName).rows.set(key, val);
      },
      idb: {
        open(_name, version = currentVersion) {
          if (version < currentVersion) {
            throw new Error(`VersionError: requested ${version} < existing ${currentVersion}`);
          }
          const needUpgrade = version > currentVersion;
          currentVersion = version;
          const db = {
            objectStoreNames: { contains: (s) => storeMaps.has(s) },
            createObjectStore(s, opts) {
              if (storeMaps.has(s)) throw new Error(`ConstraintError: store ${s} already exists`);
              storeMaps.set(s, { keyPath: opts?.keyPath ?? null, rows: new Map() });
            },
            close() {},
            transaction(names, _mode) {
              const list = Array.isArray(names) ? names : [names];
              for (const n of list) {
                if (!storeMaps.has(n)) throw new Error(`NotFoundError: missing store ${n}`);
              }
              const tx = {
                error: null,
                oncomplete: null,
                onerror: null,
                onabort: null,
                objectStore(n) {
                  const entry = storeMaps.get(n);
                  const runReq = (fn) => {
                    const r = { result: undefined, error: null, onsuccess: null, onerror: null };
                    queueMicrotask(() => {
                      r.result = fn();
                      r.onsuccess?.();
                    });
                    return r;
                  };
                  return {
                    getAll: () => runReq(() => [...entry.rows.values()]),
                    get: (k) => runReq(() => entry.rows.get(k)),
                    put: (val, k) => {
                      const key = entry.keyPath ? val[entry.keyPath] : k;
                      entry.rows.set(key, val);
                      return runReq(() => key);
                    },
                    delete: (k) => {
                      entry.rows.delete(k);
                      return runReq(() => undefined);
                    },
                  };
                },
              };
              queueMicrotask(() => queueMicrotask(() => tx.oncomplete?.()));
              return tx;
            },
          };
          const req = { result: db, onupgradeneeded: null, onsuccess: null, onerror: null };
          queueMicrotask(() => {
            if (needUpgrade) req.onupgradeneeded?.();
            req.onsuccess?.();
          });
          return req;
        },
      },
    };
  }

  try {
    const idbMod = await import('../../mobile/src/lib/idb.ts');
    const allSix = ['calib', 'camfiles', 'logos', 'lut_blobs', 'lut_meta', 'lut_records'];

    // Case A: v2 database holding only LUT stores (+ calib), opened at v4 -> gains logos + camfiles, LUT data unchanged
    const lutOnly = makeV2Db([
      ['calib', null],
      ['lut_records', 'cube_id'],
      ['lut_blobs', null],
      ['lut_meta', null],
    ]);
    const lutRec = {
      cube_id: 'cube_v2_1',
      label: 'Classic Film',
      filename: 'classic.cube',
      bytes: 16,
      n: 2,
      kind: '3d',
      domain_min: [0, 0, 0],
      domain_max: [1, 1, 1],
      input_space: 'srgb',
      output_space: 'srgb',
      input_gamut: null,
      output_gamut: null,
      input_range: 'full',
      output_range: 'full',
      space_source: 'default',
      version: 1,
      savedAt: 42,
    };
    const lutBytes = new Uint8Array([1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16]).buffer;
    lutOnly.seed('lut_records', 'cube_v2_1', lutRec);
    lutOnly.seed('lut_blobs', 'cube_v2_1', lutBytes);
    lutOnly.seed('lut_meta', 'order', ['cube_v2_1']);
    lutOnly.seed('lut_meta', 'watermark', 42);

    globalThis.indexedDB = lutOnly.idb;
    const logosFromLutV2 = await idbMod.loadAllLogos();
    assert.equal(lutOnly.version, 4);
    assert.deepEqual(lutOnly.storeNames(), allSix);
    assert.deepEqual(logosFromLutV2, []);
    const restoredLut = await idbMod.loadLutLibraryMeta();
    assert.equal(restoredLut.records.length, 1);
    assert.deepEqual(restoredLut.records[0], lutRec);
    assert.equal(restoredLut.watermark, 42);
    const restoredBlob = await idbMod.loadLutBlob('cube_v2_1');
    assert.deepEqual(new Uint8Array(restoredBlob), new Uint8Array(lutBytes));

    // Case B: v2 database holding only logos (+ calib), opened at v4 -> gains LUT + camfiles stores, logo data unchanged
    const logosOnly = makeV2Db([
      ['calib', null],
      ['logos', 'id'],
    ]);
    const logoRec = {
      id: 'logo_v2_1',
      name: 'V2Mark',
      w: 8,
      h: 8,
      coverage: 0.4,
      opaque: false,
      lum: 120,
      rgba: new Uint8Array(8 * 8 * 4).fill(200).buffer,
      createdAt: 77,
    };
    logosOnly.seed('logos', 'logo_v2_1', logoRec);

    globalThis.indexedDB = logosOnly.idb;
    const lutMetaFromLogoV2 = await idbMod.loadLutLibraryMeta();
    assert.equal(logosOnly.version, 4);
    assert.deepEqual(logosOnly.storeNames(), allSix);
    assert.deepEqual(lutMetaFromLogoV2, { records: [], order: [], totalBytes: 0, watermark: 0 });
    const restoredLogos = await idbMod.loadAllLogos();
    assert.equal(restoredLogos.length, 1);
    assert.equal(restoredLogos[0].id, 'logo_v2_1');
    assert.equal(restoredLogos[0].name, 'V2Mark');
    assert.equal(restoredLogos[0].rgba.byteLength, 8 * 8 * 4);
  } finally {
    globalThis.indexedDB = origIdb;
  }
});

test('Item 2: cropped photo gets exact geom_final_box frame layout size instead of uncropped frameMeta', async () => {
  const wasmJs = path.resolve('mobile/src/wasm-pkg/gxr_wasm.js');
  const wasmBin = path.resolve('mobile/src/wasm-pkg/gxr_wasm_bg.wasm');
  const wasm = await import(pathToFileURL(wasmJs).href);
  await wasm.default({ module_or_path: readFileSync(wasmBin) });

  const fontsMod = await import('../../mobile/src/lib/fonts.ts');
  const frameState = await import('../../mobile/src/app/frame_state.ts');

  const fontDir = path.resolve('src/gxr_studio/assets/fonts');
  const fontPairs = ['atkinson', 'inter', 'inter_sb'].map((k) => [
    k,
    new Uint8Array(readFileSync(path.join(fontDir, fontsMod.FONT_FILES[k]))),
  ]);
  const { indexJson, pack } = fontsMod.packFonts(fontPairs);

  // Also verify WASM gxr_mark matches GXR_MARK constant
  const wasmMark = JSON.parse(wasm.gxr_mark(indexJson, pack));
  assert.deepEqual(wasmMark, fontsMod.GXR_MARK);

  frameState.setFinalBoxResolverForTest(async (w, h, geometry, crop) =>
    JSON.parse(wasm.geom_final_box(w, h, JSON.stringify(geometry ?? {}), JSON.stringify(crop))),
  );

  try {
    const srcW = 4288;
    const srcH = 2848;
    const makeStubPhoto = (crop, straighten = 0) => ({
      id: 'p_crop',
      stem: 'R0001234',
      version: 1,
      dng: null,
      jpg: null,
      lastModified: 1700000000000,
      captureMs: null,
      status: 'ready',
      gone: false,
      cached: null,
      checked: 1,
      bitmap: null,
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
      jpgMeta: null,
      dngMeta: null,
      border: frameSpec.selectFrame(frameSpec.DEFAULT_FRAME, 'gallery'),
      stamp: { ...frameSpec.DEFAULT_STAMP },
      frameMeta: {
        frame: { lens: '', lens_short: '', aperture: '', shutter: '', iso: '', date: '', frame_no: '1234' },
        taken: '2026-11-16T12:00:00',
        file_time: null,
        width: srcW,
        height: srcH,
      },
      frame: { orientation: 1, width: srcW, height: srcH },
      geometry: { straighten },
      crop,
      rev: 1,
      choice: null,
      levels: new Map(),
      memory: new Map(),
      lut: null,
      looks: new Map(),
    });

    // 1. Uncropped photo uses full source dimensions [4288, 2848]
    const uncropped = makeStubPhoto(null, 0);
    assert.deepEqual(frameState.photoExportSize(uncropped, null), [srcW, srcH]);

    // 2. Square 1:1 crop via WASM geom_crop_write
    const sqWrite = JSON.parse(
      wasm.geom_crop_write(
        JSON.stringify({ frame: { orientation: 1, width: srcW, height: srcH }, geometry: null, crop: null }),
        JSON.stringify({ preset: '1:1' }),
      ),
    );
    const sqBox = JSON.parse(wasm.geom_final_box(srcW, srcH, '{}', JSON.stringify(sqWrite.crop))).box;
    assert.deepEqual([sqBox[2], sqBox[3]], [2848, 2848]);

    const sqPhoto = makeStubPhoto(sqWrite.crop, 0);
    // Half-res DNG engine (2144x1424 -> 1424x1424) must NOT leak half-res size or uncropped 4288x2848
    const halfResDngEngine = {
      layerSize: () => ({ w: 2144, h: 1424 }),
      finalSize: () => ({ w: 1424, h: 1424 }),
    };
    assert.deepEqual(frameState.photoExportSize(sqPhoto, halfResDngEngine), [2848, 2848]);
    // Uncropped Crop-tab engine (box: null -> finalSize == 4288x2848) must still return cropped size
    const cropTabEngine = {
      layerSize: () => ({ w: srcW, h: srcH }),
      finalSize: () => ({ w: srcW, h: srcH }),
    };
    assert.deepEqual(frameState.photoExportSize(sqPhoto, cropTabEngine), [2848, 2848]);

    const ensuredSq = await frameState.ensureCroppedPhotoSize(sqPhoto);
    assert.deepEqual(ensuredSq, [sqBox[2], sqBox[3]]);
    assert.deepEqual(frameState.photoExportSize(sqPhoto, null), [sqBox[2], sqBox[3]]);

    // 3. Straightened (3.5 deg) + portrait 4:5 crop: exact WASM geom_final_box matches export_output_sizes
    const geomSide = { straighten: 3.5, vertical: 0, horizontal: 0, distortion: 0, rotate90: 0, flip: false };
    const p45Write = JSON.parse(
      wasm.geom_crop_write(
        JSON.stringify({ frame: { orientation: 1, width: srcW, height: srcH }, geometry: geomSide, crop: null }),
        JSON.stringify({ preset: '4:5', orientation: 'portrait' }),
      ),
    );
    const p45Box = JSON.parse(
      wasm.geom_final_box(srcW, srcH, JSON.stringify(geomSide), JSON.stringify(p45Write.crop)),
    ).box;
    const p45Photo = makeStubPhoto(p45Write.crop, 3.5);
    p45Photo.border = frameSpec.selectFrame(frameSpec.DEFAULT_FRAME, 'leica');

    const ensuredP45 = await frameState.ensureCroppedPhotoSize(p45Photo);
    assert.deepEqual(ensuredP45, [p45Box[2], p45Box[3]]);
    const [ew, eh] = frameState.photoExportSize(p45Photo, halfResDngEngine);
    assert.equal(ew, p45Box[2]);
    assert.equal(eh, p45Box[3]);
    assert.ok(ew < eh, `portrait 4:5 crop must have ew < eh, got ${ew}x${eh}`);

    // Verify frame_layout at [ew, eh] matches export_output_sizes.sizes.original
    const reqJson = frameState.buildFrameRequestJson(ew, eh, p45Photo, p45Photo.border.logo);
    const layout = JSON.parse(wasm.frame_layout(reqJson, JSON.stringify(p45Photo.border), indexJson, pack));
    const outSizes = JSON.parse(
      wasm.export_output_sizes(
        srcW,
        srcH,
        JSON.stringify({
          frame: { orientation: 1, width: srcW, height: srcH },
          geometry: geomSide,
          crop: p45Write.crop,
          border: p45Photo.border,
        }),
        '{}',
        JSON.stringify({ meta: p45Photo.frameMeta.frame, logos: [], mark: fontsMod.GXR_MARK }),
        JSON.stringify(p45Write.crop.rect),
        p45Write.crop.preset,
        indexJson,
        pack,
      ),
    );
    assert.deepEqual(outSizes.photo, [ew, eh]);
    assert.deepEqual(outSizes.sizes.original, [layout.width, layout.height]);
  } finally {
    frameState.setFinalBoxResolverForTest(null);
  }
});
