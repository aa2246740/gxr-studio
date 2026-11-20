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

test('export_options: footnote matches manager-approved copy', async () => {
  const { readFileSync } = await import('node:fs');
  const src = readFileSync(new URL('../../mobile/src/app/export_options.ts', import.meta.url), 'utf8');
  const want = '「存到相册」每次都按这里的设置做。做好后点「存进相册」，在系统面板里点「存储图像」。';
  assert.ok(src.includes(want), `export_options.ts must contain exact approved footnote: ${want}`);
});

test('export_ready: failure bar summary shows queued count while photos are queued', async () => {
  const ready = await import('../../mobile/src/app/export_ready.ts');
  assert.equal(typeof ready.readyBarSummary, 'function', 'readyBarSummary exported');
  assert.equal(ready.readyBarSummary('ready', 0, 1, 0, 2), '1 张都没做成（另排 2 张）');
  assert.equal(ready.readyBarSummary('ready', 1, 1, 0, 1), '做好 1 张，1 张没做成（另排 1 张）');
  assert.equal(ready.readyBarSummary('ready', 0, 1, 1, 0), '1 张没做成');
  assert.equal(ready.readyBarSummary('stopped', 1, 0, 0, 0), '已停止。做好的 1 张还没存，点「存进相册」。');
});

test('export_queue: double tap deduplicates running, queued, and ready-unsaved photos', async () => {
  const { ExportBatch } = await import('../../mobile/src/app/export_batch.ts');
  const { ExportQueueState } = await import('../../mobile/src/app/export_queue.ts');
  const queue = new ExportQueueState();
  const opts = { size: 'original', quality: 'high', hdr: true };
  const p1 = { id: 'p1', stem: 'P1' };
  const p2 = { id: 'p2', stem: 'P2' };
  const p3 = { id: 'p3', stem: 'P3' };

  let resolveRun;
  const host = {
    run: () => new Promise((r) => { resolveRun = r; }),
    stop() {},
    idle() {},
    session: () => 1,
    photo: () => ({ version: 1, dng: null, jpg: null }),
    markGone() {},
    busy() {},
    changed() {},
  };
  const batch = new ExportBatch([{ id: 'p1', stem: 'P1', version: 1, dng: null, jpg: null, req: {} }], host);
  queue.beginBatch([p1], opts);
  batch.start();
  await Promise.resolve();
  assert.equal(batch.phase, 'running');

  // Second tap on running p1 is ignored
  assert.deepEqual(queue.filterQuickPhotos([p1], batch), []);

  // Queue p2 while p1 is running; tapping p2 again is also ignored
  const action = queue.enqueueOrPrepareStart([p2], opts, batch, () => []);
  assert.equal(action, 'queued');
  assert.deepEqual(queue.filterQuickPhotos([p2], batch), []);
  assert.equal(queue.queuedCount(), 1);

  // Finish p1 -> p1 is ready-and-unsaved; tapping p1 again is ignored
  resolveRun({ ok: true, file: new ArrayBuffer(16), result: { file_name: 'P1_gxr.jpg' }, shot: {}, ms: 1 });
  await batch.settled();
  assert.equal(batch.phase, 'ready');
  assert.deepEqual(queue.filterQuickPhotos([p1], batch), []);

  // Carryover p3 (ready-and-unsaved) is also blocked
  queue.carryover.push({ id: 'p3', stem: 'P3', file: new File([new Uint8Array(8)], 'P3_gxr.jpg'), position: 3, hdrRequested: true });
  assert.deepEqual(queue.filterQuickPhotos([p3], batch), []);

  // Once p1 is marked shared in a chunk, it is no longer blocked
  queue.markChunkShared(['p1']);
  assert.deepEqual(queue.filterQuickPhotos([p1], batch), [p1]);
});

test('export_queue: (a) fail mid-queue starts next queued photo and preserves failure for retry', async () => {
  const { ExportBatch } = await import('../../mobile/src/app/export_batch.ts');
  const { ExportQueueState } = await import('../../mobile/src/app/export_queue.ts');
  const queue = new ExportQueueState();
  const opts = { size: 'original', quality: 'high', hdr: true };
  const p3 = { id: 'p3', stem: 'P3' };
  const p4 = { id: 'p4', stem: 'P4' };

  const makeEntries = (b) => b.staged.map((s) => ({ id: s.id, stem: s.id.toUpperCase(), file: s.file, position: null, hdrRequested: true }));

  let resolveP3;
  const host1 = {
    run: () => new Promise((r) => { resolveP3 = r; }),
    stop() {},
    idle() {},
    session: () => 1,
    photo: () => ({ version: 1, dng: null, jpg: null }),
    markGone() {},
    busy() {},
    changed() {},
  };
  const batch1 = new ExportBatch([{ id: 'p3', stem: 'P3', version: 1, dng: null, jpg: null, req: {} }], host1);
  queue.beginBatch([p3], opts);
  batch1.start();
  await Promise.resolve();

  // Queue p4 while p3 is running
  assert.equal(queue.enqueueOrPrepareStart([p4], opts, batch1, makeEntries), 'queued');
  assert.equal(queue.queuedCount(), 1);

  // p3 fails mid-queue
  resolveP3({ ok: false, kind: 'trap', code: 'trap', message: '' });
  await batch1.settled();
  assert.equal(batch1.phase, 'ready');
  assert.equal(batch1.failures.size, 1);

  // Queue MUST advance even though batch1 ended with failures, and preserve p3's failure
  const next = queue.takeNextQueued(batch1, makeEntries);
  assert.ok(next, 'next queued job must start after a failed batch');
  assert.deepEqual(next.photos.map((p) => p.id), ['p4']);
  assert.equal(queue.queuedCount(), 0);
  assert.equal(queue.carriedFailures.size, 1);
  assert.ok(queue.carriedFailures.has('p3'));

  // Run p4 to completion
  const host2 = {
    run: async (item) => ({ ok: true, file: new ArrayBuffer(24), result: { file_name: `${item.stem}_gxr.jpg` }, shot: {}, ms: 1 }),
    stop() {},
    idle() {},
    session: () => 1,
    photo: () => ({ version: 1, dng: null, jpg: null }),
    markGone() {},
    busy() {},
    changed() {},
  };
  const batch2 = new ExportBatch([{ id: 'p4', stem: 'P4', version: 1, dng: null, jpg: null, req: {} }], host2);
  batch2.start();
  await batch2.settled();
  assert.equal(batch2.phase, 'ready');

  // Both p4's ready file AND p3's failure/retry survive
  const failures = queue.allFailures(batch2);
  assert.deepEqual(failures.map((f) => f.id), ['p3']);
  const retryJobs = queue.takeCarriedRetryJobs();
  assert.equal(retryJobs.length, 1);
  assert.deepEqual(retryJobs[0].photos.map((p) => p.id), ['p3']);
});

test('export_queue: (b) dismiss, stop, and clear empty pendingQueue with no stale ghost job', async () => {
  const { ExportBatch } = await import('../../mobile/src/app/export_batch.ts');
  const { ExportQueueState } = await import('../../mobile/src/app/export_queue.ts');
  const queue = new ExportQueueState();
  const opts = { size: 'original', quality: 'high', hdr: false };
  const p3 = { id: 'p3', stem: 'P3' };
  const p4 = { id: 'p4', stem: 'P4' };
  const p5 = { id: 'p5', stem: 'P5' };
  const makeEntries = (b) => b.staged.map((s) => ({ id: s.id, stem: s.id, file: s.file, position: null, hdrRequested: false }));

  // Case 1: dismiss clears pendingQueue so a later export does not run a ghost job
  let resolveRun;
  const host = {
    run: () => new Promise((r) => { resolveRun = r; }),
    stop() {},
    idle() {},
    session: () => 1,
    photo: () => ({ version: 1, dng: null, jpg: null }),
    markGone() {},
    busy() {},
    changed() {},
  };
  const batch = new ExportBatch([{ id: 'p3', stem: 'P3', version: 1, dng: null, jpg: null, req: {} }], host);
  queue.beginBatch([p3], opts);
  batch.start();
  await Promise.resolve();
  queue.enqueueOrPrepareStart([p4], opts, batch, makeEntries);
  assert.equal(queue.queuedCount(), 1);

  queue.dismiss();
  batch.cancel();
  assert.equal(queue.queuedCount(), 0);
  assert.equal(queue.carryover.length, 0);
  assert.equal(queue.carriedFailures.size, 0);

  // Subsequent export of p5 must not pick up stale p4
  const batch5 = new ExportBatch(
    [{ id: 'p5', stem: 'P5', version: 1, dng: null, jpg: null, req: {} }],
    { ...host, run: async () => ({ ok: true, file: new ArrayBuffer(12), result: { file_name: 'P5_gxr.jpg' }, shot: {}, ms: 1 }) },
  );
  queue.beginBatch([p5], opts);
  batch5.start();
  await batch5.settled();
  assert.equal(queue.takeNextQueued(batch5, makeEntries), null);

  // Case 2: stop empties pendingQueue while keeping carryover
  queue.carryover.push({ id: 'p5', stem: 'P5', file: new File([new Uint8Array(12)], 'P5_gxr.jpg'), position: 5, hdrRequested: false });
  queue.pendingQueue.push({ photos: [p4], options: opts });
  queue.stop();
  assert.equal(queue.queuedCount(), 0);
  assert.equal(queue.carryover.length, 1);

  // Case 3: clear empties everything
  queue.pendingQueue.push({ photos: [p4], options: opts });
  queue.clear();
  assert.equal(queue.queuedCount(), 0);
  assert.equal(queue.carryover.length, 0);
  assert.equal(queue.carriedFailures.size, 0);
});

test('export_queue: (c) partly failed batch followed by share, and new export between chunks never re-offers shared chunk', async () => {
  const { ExportBatch } = await import('../../mobile/src/app/export_batch.ts');
  const { ExportQueueState } = await import('../../mobile/src/app/export_queue.ts');
  const queue = new ExportQueueState();
  const opts = { size: 'original', quality: 'high', hdr: false };
  const pA = { id: 'a', stem: 'A' };
  const pB = { id: 'b', stem: 'B' };
  const pC = { id: 'c', stem: 'C' };
  const pD = { id: 'd', stem: 'D' };
  const makeEntries = (b) => b.staged.map((s) => ({ id: s.id, stem: s.id.toUpperCase(), file: s.file, position: null, hdrRequested: false }));

  const host = {
    run: async (item) =>
      item.id === 'a'
        ? { ok: false, kind: 'trap', code: 'trap', message: '' }
        : { ok: true, file: new ArrayBuffer(20), result: { file_name: `${item.stem}_gxr.jpg` }, shot: {}, ms: 1 },
    stop() {},
    idle() {},
    session: () => 1,
    photo: () => ({ version: 1, dng: null, jpg: null }),
    markGone() {},
    busy() {},
    changed() {},
  };
  const batch = new ExportBatch(
    [
      { id: 'a', stem: 'A', version: 1, dng: null, jpg: null, req: {} },
      { id: 'b', stem: 'B', version: 1, dng: null, jpg: null, req: {} },
      { id: 'c', stem: 'C', version: 1, dng: null, jpg: null, req: {} },
    ],
    host,
  );
  queue.beginBatch([pA, pB, pC], opts);
  batch.start();
  await batch.settled();
  assert.equal(batch.phase, 'ready');
  assert.equal(batch.failures.size, 1);
  assert.equal(batch.staged.length, 2);

  // User shares chunk 0 containing 'b', then starts a new export for 'd' BEFORE sharing chunk 1 ('c')
  queue.markChunkShared(['b']);
  const action = queue.enqueueOrPrepareStart([pD], opts, batch, makeEntries);
  assert.equal(action, 'launch');
  batch.cancel();

  // Only 'c' was carried over — already-shared 'b' is NEVER copied into carryover!
  assert.deepEqual(queue.carryover.map((e) => e.id), ['c']);
  // Failure 'a' is preserved and retryable!
  assert.deepEqual([...queue.carriedFailures.keys()], ['a']);
  assert.equal(queue.savedCount(null), 1);

  // Now share all ready files after 'd' finishes -> 'a' failure still survives for retry
  const batchD = new ExportBatch([{ id: 'd', stem: 'D', version: 1, dng: null, jpg: null, req: {} }], host);
  batchD.start();
  await batchD.settled();
  queue.finishShare(['c', 'd']);
  batchD.release();
  assert.equal(queue.carryover.length, 0);
  assert.equal(queue.allFailures(batchD).length, 1);
  assert.equal(queue.allFailures(batchD)[0].id, 'a');
  assert.ok(queue.savedCount(batchD) >= 3);
});

test('export_queue: saving carryover while another photo runs consumes only shared entries', async () => {
  const { ExportQueueState } = await import('../../mobile/src/app/export_queue.ts');
  const queue = new ExportQueueState();
  queue.carryover.push(
    { id: 'p3', stem: 'P3', file: new File([new Uint8Array(10)], 'P3_gxr.jpg'), position: 3, hdrRequested: true },
    { id: 'p4', stem: 'P4', file: new File([new Uint8Array(10)], 'P4_gxr.jpg'), position: 4, hdrRequested: true },
  );
  queue.consumeCarryover(['p3']);
  assert.deepEqual(queue.carryover.map((e) => e.id), ['p4']);
  assert.equal(queue.savedCount(null), 1);
});

test('export_queue: after 停止, 存到相册 on a photo that never ran starts it again (r2 P2)', async () => {
  const { ExportBatch } = await import('../../mobile/src/app/export_batch.ts');
  const { ExportQueueState } = await import('../../mobile/src/app/export_queue.ts');
  const queue = new ExportQueueState();
  const opts = { size: 'original', quality: 'high', hdr: true };
  const p1 = { id: 'p1', stem: 'P1' };
  const p2 = { id: 'p2', stem: 'P2' };
  const host = {
    run: () => new Promise(() => {}),
    stop() {},
    idle() {},
    session: () => 1,
    photo: () => ({ version: 1, dng: null, jpg: null }),
    markGone() {},
    busy() {},
    changed() {},
  };
  const items = [p1, p2].map((p) => ({ id: p.id, stem: p.stem, version: 1, dng: null, jpg: null, req: {} }));
  const batch = new ExportBatch(items, host);
  queue.beginBatch([p1, p2], opts);
  batch.start();
  await Promise.resolve();
  assert.equal(batch.phase, 'running');
  assert.deepEqual(queue.filterQuickPhotos([p1, p2], batch), [], 'while running both are blocked');
  batch.stop();
  assert.equal(batch.phase, 'stopped');
  assert.deepEqual(queue.filterQuickPhotos([p1, p2], batch), [p1, p2], 'after stop, waiting photos can be exported again');
});

test('export_queue: a re-exported photo is shared once, with the newer file (r2 P2)', async () => {
  const { ExportQueueState } = await import('../../mobile/src/app/export_queue.ts');
  const queue = new ExportQueueState();
  const oldFile = new File([new Uint8Array(8)], 'P1_gxr.jpg');
  const newFile = new File([new Uint8Array(9)], 'P1_gxr_hdr.jpg');
  const p3 = new File([new Uint8Array(4)], 'P3_gxr.jpg');
  const carried = [
    { id: 'p1', stem: 'P1', file: oldFile, position: 1, hdrRequested: false },
    { id: 'p3', stem: 'P3', file: p3, position: 3, hdrRequested: false },
  ];
  const current = [{ id: 'p1', stem: 'P1', file: newFile, position: 1, hdrRequested: true }];
  const merged = queue.readyEntries(carried, current);
  assert.deepEqual(merged.map((e) => e.id).sort(), ['p1', 'p3']);
  assert.equal(merged.find((e) => e.id === 'p1').file, newFile);
  // Until the new one is ready, the carried one is still offered
  assert.deepEqual(queue.readyEntries(carried, []).map((e) => e.id), ['p1', 'p3']);
});

test('export_ready: photo positions are listed in grid order (r2 P3)', async () => {
  const { readyPositionsSummary } = await import('../../mobile/src/app/export_ready.ts');
  assert.equal(readyPositionsSummary([1, 6, 5]), '第 1 张、第 5 张、第 6 张');
});

test('export_ready: phone-ready-slot labels and VoiceOver announcements', async () => {
  const ready = await import('../../mobile/src/app/export_ready.ts');
  assert.equal(ready.readyShareAriaLabel([3], 0, 1), '第 3 张做好了，存进相册，打开系统分享');
  assert.equal(ready.readyShareAriaLabel([3, 4], 1, 2), '2 张做好了，存进相册，打开系统分享，第 2 批，共 2 批');
  assert.equal(ready.runningBarLabel(1, 1, false, 0), '正在导出');
  assert.equal(ready.runningBarLabel(1, 1, true, 0), '正在导出 · HDR');
  assert.equal(ready.runningBarLabel(2, 5, true, 1), '正在导出 2 / 5 · HDR（另排 1 张）');
  assert.equal(ready.totalFailureLabel([{ id: 'p3', stem: 'P3', kind: 'trap', message: '' }], () => 3), '第 3 张没做成');
  assert.equal(ready.totalFailureLabel([{ id: 'p3', stem: 'P3', kind: 'trap', message: '' }], () => null), '1 张没做成');
  assert.equal(
    ready.totalFailureLabel(
      [
        { id: 'p1', stem: 'P1', kind: 'trap', message: '' },
        { id: 'p2', stem: 'P2', kind: 'read', message: '' },
      ],
      () => 1,
    ),
    '2 张没做成',
  );

  const file = new File([new Uint8Array(8)], 'P3_gxr.jpg');
  const e3 = { id: 'p3', stem: 'P3', file, position: 3, hdrRequested: false };
  const f2 = { id: 'p2', stem: 'P2', kind: 'trap', message: 'err' };
  // Running never announces per tick
  assert.equal(ready.stateAnnouncement('running', [e3], [], () => 3), null);
  assert.deepEqual(ready.stateAnnouncement('ready', [e3], [], () => 3), {
    key: 'ready:p3',
    text: '第 3 张做好了，可存进相册',
  });
  assert.deepEqual(ready.stateAnnouncement('ready', [e3], [f2], () => 2), {
    key: 'partial:1:p2',
    text: '做好 1 张，1 张没做成',
  });
  assert.deepEqual(ready.stateAnnouncement('ready', [], [f2], () => 2), {
    key: 'fail:p2',
    text: '第 2 张没做成',
  });
  assert.deepEqual(ready.stateAnnouncement('stopped', [e3], [], () => 3), {
    key: 'stopped:1',
    text: '导出已停止，做好的 1 张还没存',
  });
});

test('drag_guard: guards accidental taps for 250ms after control drag end', async () => {
  const { markControlDragEnd, isDragTapGuarded, resetDragGuard } = await import('../../mobile/src/app/drag_guard.ts');
  resetDragGuard();
  assert.equal(isDragTapGuarded(1000), false);
  markControlDragEnd(1000);
  assert.equal(isDragTapGuarded(1000), true);
  assert.equal(isDragTapGuarded(1249), true);
  assert.equal(isDragTapGuarded(1250), false);
  resetDragGuard();
  assert.equal(isDragTapGuarded(1050), false);
});
