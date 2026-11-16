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

const out = { pageErrors: [], race: null, failRelease: null, error: null };

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
          });
        }
        return super.postMessage(message, ...args);
      }
    };
  });

  await page.route('**/src/core/worker.ts*', async (route) => {
    const response = await route.fetch();
    const prefix = `self.addEventListener('message', (event) => {
      if (event.data && event.data.__reviewBusyMs) {
        event.stopImmediatePropagation();
        self.postMessage({ __reviewBusyStarted: true });
        const until = performance.now() + event.data.__reviewBusyMs;
        while (performance.now() < until) {}
        return;
      }
      if (event.data && event.data.__reviewFailNextSnapshot) {
        event.stopImmediatePropagation();
        self.__reviewFailNextSnapshot = true;
        self.postMessage({ __reviewFailArmed: true });
        return;
      }
      if (event.data && event.data.op === 'lut-snapshot' && self.__reviewFailNextSnapshot) {
        event.stopImmediatePropagation();
        self.__reviewFailNextSnapshot = false;
        self.postMessage({
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
    await appBook.restore();
    const cube = (name) =>
      `# Input: sRGB\nTITLE "${name}"\nLUT_3D_SIZE 2\n0 0 0\n1 0 0\n0 1 0\n1 1 0\n0 0 1\n1 0 1\n0 1 1\n1 1 1\n`;
    const texts = ['BarrierA', 'BarrierB'].map(cube);
    const imported = await appBook.importFiles(texts.map((text, i) => new File([text], `barrier_${i}.cube`)));
    if (imported.added.length !== 2) throw new Error('synthetic barrier LUT import failed');
    const [a, b] = imported.added;

    const workers = window.__reviewCoreWorkers;
    const postStart = window.__reviewCorePosts.length;
    const nextIdx = (workers.indexOf(window.__reviewLastCore) + 1) % workers.length;
    const busyWorker = workers[nextIdx];
    const busyStarted = new Promise((resolve) => {
      busyWorker.addEventListener('message', function onBusy(event) {
        if (event.data?.__reviewBusyStarted) {
          busyWorker.removeEventListener('message', onBusy);
          resolve();
        }
      });
    });
    busyWorker.postMessage({ __reviewBusyMs: 1500 });
    await busyStarted;

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

    const snapshot = [a, b].map((r, i) => ({
      id: r.cube_id,
      present: Boolean(snap[r.cube_id]),
      space: snap[r.cube_id] ? JSON.parse(snap[r.cube_id].space).input_space : null,
      bytesEqual: snap[r.cube_id] ? new TextDecoder().decode(snap[r.cube_id].cube) === texts[i] : false,
    }));

    const dispatches = window.__reviewCorePosts.slice(postStart);

    // 场景 2：快照读取失败时屏障必须释放，后续删除照常完成。
    const textC = cube('BarrierFailC');
    const impC = await appBook.importFiles([new File([textC], 'barrier_fail_c.cube')]);
    if (impC.added.length !== 1) throw new Error('synthetic fail-release LUT import failed');
    const [c] = impC.added;

    const failIdx = (workers.indexOf(window.__reviewLastCore) + 1) % workers.length;
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
      failRelease: {
        failOk: failRes.ok,
        failMessage: failRes.message,
        deleteOk: delAfterFail.ok,
        catalogueHasC: Boolean(appBook.get(c.cube_id)),
      },
    };
  });

  out.race = result.race;
  out.failRelease = result.failRelease;
  await page.close();
} catch (err) {
  out.error = err instanceof Error ? `${err.message}\n${err.stack ?? ''}` : String(err);
} finally {
  writeFileSync(manifest.out, JSON.stringify(out));
  await browser.close();
}

if (out.error) process.exit(1);
