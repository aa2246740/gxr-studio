// U1 phone-fast-export 单元测试（Node --experimental-strip-types，不碰 DOM）：
// 1) prefs.ts：HDR 首次默认开、记住关；尺寸与品质默认 original/high 并持久化；
// 2) export_ready.ts：单张/多张就绪条文案、第 N 张列表、分批文案、折叠说明分组；
// 3) export_batch.ts：baseStagedBytes 纳入待存上限预算（队列保留旧成片时不超内存上限）。

import test from 'node:test';
import assert from 'node:assert/strict';
import { registerHooks } from 'node:module';

registerHooks({
  resolve(spec, ctx, next) {
    try {
      return next(spec, ctx);
    } catch (err) {
      if (spec.startsWith('.') && !/\.[a-z]+$/.test(spec)) return next(`${spec}.ts`, ctx);
      throw err;
    }
  },
});

function installMemoryStorage(seed = {}) {
  const map = new Map(Object.entries(seed));
  const storage = {
    getItem: (k) => (map.has(k) ? map.get(k) : null),
    setItem: (k, v) => map.set(k, String(v)),
    removeItem: (k) => map.delete(k),
    clear: () => map.clear(),
  };
  Object.defineProperty(globalThis, 'localStorage', { configurable: true, value: storage });
  return map;
}

test('prefs: HDR defaults ON on first run and preserves saved OFF', async () => {
  const map = installMemoryStorage();
  const prefs = await import('../../mobile/src/lib/prefs.ts');
  assert.equal(prefs.loadExportHdr(), true, 'first-run HDR default must be true');

  prefs.saveExportHdr(false);
  assert.equal(map.get('gxr.mobile.export_hdr'), '0');
  assert.equal(prefs.loadExportHdr(), false);

  prefs.saveExportHdr(true);
  assert.equal(map.get('gxr.mobile.export_hdr'), '1');
  assert.equal(prefs.loadExportHdr(), true);

  // Existing user with saved '0' stays false
  installMemoryStorage({ 'gxr.mobile.export_hdr': '0' });
  assert.equal(prefs.loadExportHdr(), false);
});

test('prefs: export size and quality default and persist across reloads', async () => {
  const map = installMemoryStorage();
  const prefs = await import('../../mobile/src/lib/prefs.ts');
  assert.equal(typeof prefs.loadExportSize, 'function', 'loadExportSize exported');
  assert.equal(typeof prefs.saveExportSize, 'function', 'saveExportSize exported');
  assert.equal(typeof prefs.loadExportQuality, 'function', 'loadExportQuality exported');
  assert.equal(typeof prefs.saveExportQuality, 'function', 'saveExportQuality exported');

  assert.equal(prefs.loadExportSize(), 'original');
  assert.equal(prefs.loadExportQuality(), 'high');

  prefs.saveExportSize('medium');
  prefs.saveExportQuality('low');
  assert.equal(prefs.loadExportSize(), 'medium');
  assert.equal(prefs.loadExportQuality(), 'low');

  // Corrupted storage falls back cleanly
  map.set('gxr.mobile.export_size', 'giant');
  map.set('gxr.mobile.export_quality', 'ultra');
  assert.equal(prefs.loadExportSize(), 'original');
  assert.equal(prefs.loadExportQuality(), 'high');
});

test('export_ready: single, multi, photo list, chunk label, and expanded notes', async () => {
  const ready = await import('../../mobile/src/app/export_ready.ts');
  assert.equal(ready.readyShareLabel([3], 0, 1), '第 3 张做好了 · 存进相册');
  assert.equal(ready.readyShareLabel([3, 4], 0, 1), '2 张做好了 · 存进相册');
  assert.equal(ready.readyShareLabel([3, 4], 0, 2), '2 张做好了 · 存进相册（第 1 批，共 2 批）');
  assert.equal(ready.readyShareLabel([3, 4], 1, 2), '2 张做好了 · 存进相册（第 2 批，共 2 批）');
  assert.equal(ready.readyPositionsSummary([3, 4]), '第 3 张、第 4 张');
  assert.equal(ready.readyPositionsSummary([1, 2, 3, 4, 5, 6]), '第 1、2、3、4 张等 6 张');
});

test('export_batch: baseStagedBytes counts carryover bytes toward stage cap', async () => {
  const { ExportBatch } = await import('../../mobile/src/app/export_batch.ts');
  let base = 60;
  const host = {
    run: async (item) => ({ ok: true, file: new ArrayBuffer(50), result: { file_name: `${item.stem}_gxr.jpg` }, shot: {}, ms: 1 }),
    stop() {},
    idle() {},
    session: () => 1,
    photo: () => ({ version: 1, dng: null, jpg: null }),
    markGone() {},
    busy() {},
    changed() {},
    baseStagedBytes: () => base,
  };
  const items = [{ id: 'a', stem: 'A', version: 1, dng: null, jpg: null, req: {} }];
  const batch = new ExportBatch(items, host, 100);
  batch.start();
  await batch.settled();
  // 60 (base) + 50 (new) > 100 cap => must hold until base is released!
  assert.equal(batch.phase, 'held');
  assert.equal(batch.staged.length, 0);
  base = 0;
  batch.release();
  await batch.settled();
  assert.equal(batch.phase, 'ready');
  assert.equal(batch.staged.length, 1);
});
