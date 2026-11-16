// P2-02 真实多 Worker 池竞态回归：
// 1. 一台 Worker 正在执行现有工作而阻塞时发起 exportSnapshot([A, B])，紧接 setSpace(B -> applelog) 与 remove(B)；
//    写屏障必须保证 lut-snapshot 先于 lut-patch/lut-delete 完成，快照保留点击时的 A/B 原字节与 sRGB，改动与删除随后照常生效。
// 2. 快照读取失败时写屏障必须释放（不卡死），后续删除照常完成。

import { pathToFileURL } from 'node:url';
import { readFileSync, writeFileSync } from 'node:fs';

const [runtime, url, manifestPath] = process.argv.slice(2);
const manifest = JSON.parse(readFileSync(manifestPath, 'utf8'));
const { launchPinnedChromium, verifyPinnedRuntime } = await import(pathToFileURL(runtime).href);
verifyPinnedRuntime();

const browser = await launchPinnedChromium({
  args: ['--enable-unsafe-webgpu', '--enable-unsafe-swiftshader', '--use-angle=swiftshader', '--use-webgpu-adapter=swiftshader'],
});

const out = { pageErrors: [], race: null, overlap: null, handoff: null, failRelease: null, error: null };

async function settle(page) {
  for (let attempt = 0; attempt < 5; attempt += 1) {
    await page.waitForSelector('.app[data-core="1"]', { timeout: 60_000 });
    const reloaded = await page.waitForEvent('framenavigated', { timeout: 4_000 }).then(() => true).catch(() => false);
    if (!reloaded) return;
  }
  throw new Error('page kept reloading');
}

try {
  const page = await browser.newPage({ viewport: { width: 390, height: 844 } });
  page.on('pageerror', (err) => out.pageErrors.push(err.message));

  await page.addInitScript(() => {
    const NativeWorker = window.Worker;
    window.__reviewCoreWorkers = [];
    window.__reviewCorePosts = [];
    window.__reviewLastCore = null;
    window.Worker = class extends NativeWorker {
      constructor(workerUrl, options) {
        super(workerUrl, options);
        if (String(workerUrl).includes('/src/core/worker.ts')) {
          window.__reviewCoreWorkers.push(this);
        }
      }
      postMessage(message, ...args) {
        if (window.__reviewCoreWorkers.includes(this) && typeof message?.op === 'string') {
          window.__reviewLastCore = this;
          window.__reviewCorePosts.push({
            worker: window.__reviewCoreWorkers.indexOf(this),
            op: message.op,
            detail:
              message.op === 'lut-patch'
                ? (message.lutPatch?.patch?.name ?? message.lutPatch?.patch?.input_space ?? null)
                : null,
          });
        }
        return super.postMessage(message, ...args);
      }
    };
  });

  await page.route('**/src/core/worker.ts*', async (route) => {
    const response = await route.fetch();
    const prefix = `const __reviewNativePost = self.postMessage.bind(self);
    const __reviewSnapshotIds = new Set();
    self.postMessage = function(message, ...args) {
      const holdMs =
        typeof message?.id === 'number' &&
        __reviewSnapshotIds.delete(message.id) &&
        self.__reviewHoldAfterReply > 0
          ? self.__reviewHoldAfterReply
          : 0;
      if (holdMs > 0) self.__reviewHoldAfterReply = 0;
      __reviewNativePost(message, ...args);
      if (holdMs > 0) {
        const until = performance.now() + holdMs;
        while (performance.now() < until) {}
      }
    };
    self.addEventListener('message', (event) => {
      if (event.data && event.data.__reviewHoldAfterReply) {
        event.stopImmediatePropagation();
        self.__reviewHoldAfterReply = event.data.__reviewHoldAfterReply;
        __reviewNativePost({ __reviewHoldArmed: true });
        return;
      }
      if (event.data && event.data.op === 'lut-snapshot') {
        __reviewSnapshotIds.add(event.data.id);
      }
      if (event.data && event.data.__reviewBusyMs) {
        event.stopImmediatePropagation();
        __reviewNativePost({ __reviewBusyStarted: true });
        const until = performance.now() + event.data.__reviewBusyMs;
        while (performance.now() < until) {}
        return;
      }
      if (event.data && event.data.__reviewFailNextSnapshot) {
        event.stopImmediatePropagation();
        self.__reviewFailNextSnapshot = true;
        __reviewNativePost({ __reviewFailArmed: true });
        return;
      }
      if (event.data && event.data.op === 'lut-snapshot' && self.__reviewFailNextSnapshot) {
        event.stopImmediatePropagation();
        __reviewSnapshotIds.delete(event.data.id);
        self.__reviewFailNextSnapshot = false;
        __reviewNativePost({
          id: event.data.id,
          op: 'lut-snapshot',
          ok: false,
          message: 'simulated snapshot failure',
          buffers: [],
        });
      }
    });\n`;
    await route.fulfill({ response, body: prefix + (await response.text()) });
  });

  await page.goto(url);
  await settle(page);
  await page.waitForFunction(
    () => Boolean(window.__gxrLutBook) && Array.isArray(window.__reviewCoreWorkers) && window.__reviewCoreWorkers.length >= 2,
    null,
    { timeout: 60_000 },
  );

  const result = await page.evaluate(async () => {
    const appBook = window.__gxrLutBook;
    const api = await import('/src/core/api.ts');
    await appBook.restore();
    const cube = (name) =>
      `# Input: sRGB\nTITLE "${name}"\nLUT_3D_SIZE 2\n0 0 0\n1 0 0\n0 1 0\n1 1 0\n0 0 1\n1 0 1\n0 1 1\n1 1 1\n`;
    const texts = ['BarrierA', 'BarrierB'].map(cube);
    const imported = await appBook.importFiles(texts.map((text, i) => new File([text], `barrier_${i}.cube`)));
    if (imported.added.length !== 2) throw new Error('synthetic barrier LUT import failed');
    const [a, b] = imported.added;

    const workers = window.__reviewCoreWorkers;
    const nextWorkerIndex = () => (workers.indexOf(window.__reviewLastCore) + 1) % workers.length;
    async function busyNextWorker(ms) {
      const idx = nextWorkerIndex();
      const w = workers[idx];
      const started = new Promise((resolve) => {
        w.addEventListener('message', function onBusy(event) {
          if (event.data?.__reviewBusyStarted) {
            w.removeEventListener('message', onBusy);
            resolve();
          }
        });
      });
      w.postMessage({ __reviewBusyMs: ms });
      await started;
      return idx;
    }
    async function rotatePoolTo(targetIdx) {
      let guard = 0;
      while (nextWorkerIndex() !== targetIdx && guard < workers.length * 2) {
        await api.warmCore();
        guard += 1;
      }
    }
    const snapView = (snap, rec, expectedText) => ({
      id: rec.cube_id,
      present: Boolean(snap[rec.cube_id]),
      space: snap[rec.cube_id] ? JSON.parse(snap[rec.cube_id].space).input_space : null,
      bytesEqual: snap[rec.cube_id] ? new TextDecoder().decode(snap[rec.cube_id].cube) === expectedText : false,
    });

    const postStart = window.__reviewCorePosts.length;
    const nextIdx = await busyNextWorker(1500);

    const events = [{ type: 'busy-started', worker: nextIdx }];
    const pending = appBook.exportSnapshot([a.cube_id, b.cube_id]);
    void pending.then(
      () => events.push({ type: 'snapshot-settled' }),
      () => events.push({ type: 'snapshot-rejected' }),
    );
    const patch = await appBook.setSpace(b.cube_id, { input_space: 'applelog' });
    events.push({ type: 'patch', ok: patch.ok, space: patch.ok ? patch.record.input_space : null });
    const removed = await appBook.remove(b.cube_id);
    events.push({ type: 'delete', ok: removed });
    const snap = await pending;

    const snapshot = [a, b].map((r, i) => snapView(snap, r, texts[i]));

    const dispatches = window.__reviewCorePosts.slice(postStart);

    // 场景 2（Opus r3 P2-01）：真实多 Worker 池上两次导出快照重叠（均排在繁忙 Worker 后依次完成），
    // 验证：(a) setSpace(applelog) -> S2 -> setSpace(srgb) 按调用顺序生效且最终为 srgb；
    //       (b) setSpace(applelog) -> S2 -> removeChecked 先改后删且两次写都成功，两个快照都保留 B 原字节与点击时空间。
    let overlapSpaceOrder;
    {
      const overlapTexts1 = ['OverlapSpaceA', 'OverlapSpaceB'].map(cube);
      const imp1 = await appBook.importFiles(overlapTexts1.map((t, i) => new File([t], `overlap_space_${i}.cube`)));
      if (imp1.added.length !== 2) throw new Error('synthetic overlap space LUT import failed');
      const [oa, ob] = imp1.added;

      const p0 = window.__reviewCorePosts.length;
      const busy1 = await busyNextWorker(2000);
      const s1 = appBook.exportSnapshot([oa.cube_id, ob.cube_id]);
      const w1 = appBook.setSpace(ob.cube_id, { input_space: 'applelog' });
      await rotatePoolTo(busy1);
      const s2 = appBook.exportSnapshot([oa.cube_id, ob.cube_id]);
      const w2 = appBook.setSpace(ob.cube_id, { input_space: 'srgb' });
      const [snap1, snap2, r1, r2] = await Promise.all([s1, s2, w1, w2]);
      const nonPing = window.__reviewCorePosts
        .slice(p0)
        .filter((d) => d.op !== 'ping')
        .map((d) => (d.detail ? `${d.op}:${d.detail}` : d.op));
      const persisted = await api.restoreLutLibrary();
      const persistedB = persisted.records.find((r) => r.cube_id === ob.cube_id) ?? null;

      overlapSpaceOrder = {
        nonPingDispatches: nonPing,
        snap1: [oa, ob].map((r, i) => snapView(snap1, r, overlapTexts1[i])),
        snap2: [oa, ob].map((r, i) => snapView(snap2, r, overlapTexts1[i])),
        w1Ok: r1.ok,
        w1Space: r1.ok ? r1.record.input_space : null,
        w2Ok: r2.ok,
        w2Space: r2.ok ? r2.record.input_space : null,
        memorySpace: appBook.get(ob.cube_id)?.input_space ?? null,
        persistedSpace: persistedB?.input_space ?? null,
      };
    }

    let overlapSpaceThenDelete;
    {
      const overlapTexts2 = ['OverlapDelA', 'OverlapDelB'].map(cube);
      const imp2 = await appBook.importFiles(overlapTexts2.map((t, i) => new File([t], `overlap_del_${i}.cube`)));
      if (imp2.added.length !== 2) throw new Error('synthetic overlap delete LUT import failed');
      const [da, db] = imp2.added;

      const p0 = window.__reviewCorePosts.length;
      const busy2 = await busyNextWorker(2000);
      const s1 = appBook.exportSnapshot([da.cube_id, db.cube_id]);
      const w1 = appBook.setSpace(db.cube_id, { input_space: 'applelog' });
      await rotatePoolTo(busy2);
      const s2 = appBook.exportSnapshot([da.cube_id, db.cube_id]);
      const w2 = appBook.removeChecked(db.cube_id);
      const [snap1, snap2, r1, r2] = await Promise.all([s1, s2, w1, w2]);
      const nonPing = window.__reviewCorePosts
        .slice(p0)
        .filter((d) => d.op !== 'ping')
        .map((d) => (d.detail ? `${d.op}:${d.detail}` : d.op));
      const persisted = await api.restoreLutLibrary();

      overlapSpaceThenDelete = {
        nonPingDispatches: nonPing,
        snap1: [da, db].map((r, i) => snapView(snap1, r, overlapTexts2[i])),
        snap2: [da, db].map((r, i) => snapView(snap2, r, overlapTexts2[i])),
        patchOk: r1.ok,
        patchMessage: r1.ok ? '' : r1.message,
        patchedSpace: r1.ok ? r1.record.input_space : null,
        deleteOk: r2.ok,
        catalogueHasA: Boolean(appBook.get(da.cube_id)),
        catalogueHasB: Boolean(appBook.get(db.cube_id)),
        persistedHasB: persisted.records.some((r) => r.cube_id === db.cube_id),
      };
    }

    // 场景 3（Sol r4 P2-01）：真实 4 Worker 池交接竞态（不替换 snapshotBatch 或 IndexedDB）：
    // S1 在繁忙 Worker 上执行（该 Worker 发回 S1 结果后继续受控忙碌），removeChecked(B) 排在屏障后；
    // 池轮转回该繁忙 Worker，在 S1 resolve 后经过 0/1/2 个微任务（含 Sol 指定的 2 微任务交接点）登记 S2，
    // 验证 S2 仍保留 B 的原始字节与 srgb/rec709 空间，且未发出的删除绝不绕过新登记的 S2。
    const cube709 = (name) =>
      `# Input: sRGB\n# Output: Rec.709\nTITLE "${name}"\nLUT_3D_SIZE 2\n0 0 0\n1 0 0\n0 1 0\n1 1 0\n0 0 1\n1 0 1\n0 1 1\n1 1 1\n`;
    const handoffView = (snap, rec, expectedText) => ({
      id: rec.cube_id,
      present: Boolean(snap[rec.cube_id]),
      space: snap[rec.cube_id] ? JSON.parse(snap[rec.cube_id].space).input_space : null,
      output: snap[rec.cube_id] ? JSON.parse(snap[rec.cube_id].space).output_space : null,
      bytesEqual: snap[rec.cube_id] ? new TextDecoder().decode(snap[rec.cube_id].cube) === expectedText : false,
    });
    const handoffCases = [];
    for (const gap of [0, 1, 2]) {
      const handoffTexts = [`HandoffA_${gap}`, `HandoffB_${gap}`].map(cube709);
      const impH = await appBook.importFiles(handoffTexts.map((t, i) => new File([t], `handoff_${gap}_${i}.cube`)));
      if (impH.added.length !== 2) throw new Error('synthetic handoff LUT import failed');
      const [ha, hb] = impH.added;

      const p0 = window.__reviewCorePosts.length;
      const targetWorker = workers[nextWorkerIndex()];
      const holdArmed = new Promise((resolve) => {
        targetWorker.addEventListener('message', function onHoldArm(event) {
          if (event.data?.__reviewHoldArmed) {
            targetWorker.removeEventListener('message', onHoldArm);
            resolve();
          }
        });
      });
      targetWorker.postMessage({ __reviewHoldAfterReply: 800 });
      await holdArmed;

      const busyIdx = await busyNextWorker(450);
      const s1 = appBook.exportSnapshot([ha.cube_id, hb.cube_id]);
      const deleting = appBook.removeChecked(hb.cube_id);
      await rotatePoolTo(busyIdx);
      const second = s1.then(async () => {
        for (let i = 0; i < gap; i += 1) await Promise.resolve();
        const writePostedAtSecondClick = window.__reviewCorePosts.slice(p0).some((d) => d.op === 'lut-delete');
        const memB = appBook.get(hb.cube_id);
        const memoryAtSecondClick = memB
          ? { present: true, space: memB.input_space, output: memB.output_space, version: memB.version }
          : { present: false, space: null, output: null, version: null };
        const snap2 = await appBook.exportSnapshot([ha.cube_id, hb.cube_id]);
        return { writePostedAtSecondClick, memoryAtSecondClick, snap2 };
      });

      const [snap1, secondRes, delRes] = await Promise.all([s1, second, deleting]);
      const nonPing = window.__reviewCorePosts
        .slice(p0)
        .filter((d) => d.op !== 'ping')
        .map((d) => (d.detail ? `${d.op}:${d.detail}` : d.op));
      const persisted = await api.restoreLutLibrary();

      handoffCases.push({
        gap,
        writePostedAtSecondClick: secondRes.writePostedAtSecondClick,
        memoryAtSecondClick: secondRes.memoryAtSecondClick,
        nonPingDispatches: nonPing,
        snap1: [ha, hb].map((r, i) => handoffView(snap1, r, handoffTexts[i])),
        snap2: [ha, hb].map((r, i) => handoffView(secondRes.snap2, r, handoffTexts[i])),
        deleteOk: delRes.ok,
        catalogueHasA: Boolean(appBook.get(ha.cube_id)),
        catalogueHasB: Boolean(appBook.get(hb.cube_id)),
        persistedHasB: persisted.records.some((r) => r.cube_id === hb.cube_id),
      });
    }

    // 场景 4：快照读取失败时屏障必须释放，后续删除照常完成。
    const textC = cube('BarrierFailC');
    const impC = await appBook.importFiles([new File([textC], 'barrier_fail_c.cube')]);
    if (impC.added.length !== 1) throw new Error('synthetic fail-release LUT import failed');
    const [c] = impC.added;

    const failIdx = nextWorkerIndex();
    const failWorker = workers[failIdx];
    const armed = new Promise((resolve) => {
      failWorker.addEventListener('message', function onArm(event) {
        if (event.data?.__reviewFailArmed) {
          failWorker.removeEventListener('message', onArm);
          resolve();
        }
      });
    });
    failWorker.postMessage({ __reviewFailNextSnapshot: true });
    await armed;

    const failSnap = appBook.exportSnapshot([c.cube_id]).then(
      () => ({ ok: true, message: '' }),
      (err) => ({ ok: false, message: err instanceof Error ? err.message : String(err) }),
    );
    const delAfterFail = await appBook.removeChecked(c.cube_id);
    const failRes = await failSnap;

    return {
      race: {
        workerCount: workers.length,
        snapshot,
        events,
        patchOk: patch.ok,
        patchedSpace: patch.ok ? patch.record.input_space : null,
        removed,
        catalogueHasA: Boolean(appBook.get(a.cube_id)),
        catalogueHasB: Boolean(appBook.get(b.cube_id)),
        dispatches,
      },
      overlap: {
        workerCount: workers.length,
        spaceOrder: overlapSpaceOrder,
        spaceThenDelete: overlapSpaceThenDelete,
      },
      handoff: {
        workerCount: workers.length,
        cases: handoffCases,
      },
      failRelease: {
        failOk: failRes.ok,
        failMessage: failRes.message,
        deleteOk: delAfterFail.ok,
        catalogueHasC: Boolean(appBook.get(c.cube_id)),
      },
    };
  });

  out.race = result.race;
  out.overlap = result.overlap;
  out.handoff = result.handoff;
  out.failRelease = result.failRelease;
  await page.close();
} catch (err) {
  out.error = err instanceof Error ? `${err.message}\n${err.stack ?? ''}` : String(err);
} finally {
  writeFileSync(manifest.out, JSON.stringify(out));
  await browser.close();
}

if (out.error) process.exit(1);
