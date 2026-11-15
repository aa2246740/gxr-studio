// Sol r6 P1：显卡在忙时 histogram() 立刻给 null，编辑页的直方图重试不能接着在微任务里转，要让出事件循环。
// 在开发服务器的页面里 import 真的 EditorView，配一个假的忙引擎（Sol 的 hist-scheduler 探针同样的状态）。
// 不用照片，不用相机数据。

import { pathToFileURL } from 'node:url';

const [runtime, url] = process.argv.slice(2);
const { launchPinnedChromium, verifyPinnedRuntime } = await import(pathToFileURL(runtime).href);
verifyPinnedRuntime();
const browser = await launchPinnedChromium({ args: [] });
const out = { pageErrors: [] };

// 页面卡死时 evaluate 不会回来：Node 这边自己设期限，卡住就当失败报出来。
function within(ms, promise, what) {
  let timer;
  const late = new Promise((_, fail) => {
    timer = setTimeout(() => fail(new Error(`${what}：${ms} ms 内页面无回应（直方图重试没有让出事件循环？）`)), ms);
  });
  return Promise.race([promise, late]).finally(() => clearTimeout(timer));
}

try {
  const page = await browser.newPage();
  page.on('pageerror', (error) => out.pageErrors.push(error.message));
  await page.goto(url);
  for (let attempt = 0; attempt < 5; attempt += 1) {
    await page.waitForSelector('.app[data-core="1"]', { timeout: 60_000 });
    const reloaded = await page.waitForEvent('framenavigated', { timeout: 4_000 }).then(() => true).catch(() => false);
    if (!reloaded) break;
  }
  out.cases = await within(
    30_000,
    page.evaluate(async () => {
      const { EditorView } = await import('/src/app/editor.ts');
      const frame = () => new Promise((done) => requestAnimationFrame(done));
      // 生产没有上限；这里在 5000 次截住，修坏了也只是报失败，不把页面挂死。
      const GUARD = 5000;
      function fakeEngine(state) {
        return {
          pending: false,
          contentVersion: 1,
          detach() {},
          async histogram() {
            state.calls += 1;
            state.callFrames.push(state.frames);
            if (state.busy && state.calls < GUARD) return null;
            if (state.busy) state.guardHit = true;
            return { counts: new Uint32Array(1026), samples: 1, version: this.contentVersion };
          },
        };
      }
      function view(engine) {
        const v = new EditorView(document.createElement('div'), document.createElement('div'), {}, engine);
        v.mode = 'raw';
        return v;
      }
      function counter(state) {
        let on = true;
        const tick = () => {
          if (!on) return;
          state.frames += 1;
          requestAnimationFrame(tick);
        };
        requestAnimationFrame(tick);
        return () => {
          on = false;
        };
      }
      const result = {};

      // 1. Sol 的状态：RAW、pending=false、内容版本 ≠ 已画的版本，引擎正忙（上一张的读回）。
      //    事先排好的 0 ms 定时器必须能跑（它把忙状态解掉），几帧内画上。
      {
        const state = { calls: 0, busy: true, guardHit: false, frames: 0, callFrames: [] };
        const stop = counter(state);
        const v = view(fakeEngine(state));
        let timerAtCalls = null;
        setTimeout(() => {
          timerAtCalls = state.calls;
          state.busy = false;
        }, 0);
        const t0 = performance.now();
        v.requestHist();
        let drawnAtFrame = null;
        for (let i = 0; i < 60; i++) {
          await frame();
          if (v.histDrawn === 1) {
            drawnAtFrame = state.frames;
            break;
          }
        }
        stop();
        result.busyThenFree = {
          calls: state.calls,
          guardHit: state.guardHit,
          timerAtCalls,
          histDrawn: v.histDrawn,
          drawnAtFrame,
          ms: Math.round(performance.now() - t0),
        };
        v.destroy();
      }

      // 2. 一直忙：每帧最多问一次，期间页面定时器照常跑；连着拿不到到上限就停。
      {
        const state = { calls: 0, busy: true, guardHit: false, frames: 0, callFrames: [] };
        const stop = counter(state);
        const v = view(fakeEngine(state));
        let ticks = 0;
        const interval = setInterval(() => {
          ticks += 1;
        }, 10);
        v.requestHist();
        for (let i = 0; i < 30; i++) await frame();
        const perFrame = {};
        for (const f of state.callFrames) perFrame[f] = (perFrame[f] ?? 0) + 1;
        result.alwaysBusy = {
          calls: state.calls,
          frames: state.frames,
          maxPerFrame: Math.max(0, ...Object.values(perFrame)),
          ticks,
          guardHit: state.guardHit,
        };
        // 3. 换片（destroy）以后排在下一帧的那次取消，不再去问。
        v.destroy();
        const before = state.calls;
        for (let i = 0; i < 5; i++) await frame();
        clearInterval(interval);
        stop();
        result.afterDestroy = { more: state.calls - before };
      }

      // 4. 连着拿不到到上限后停下，下一次调整（不是重试）重新开始。
      {
        const state = { calls: 0, busy: true, guardHit: false, frames: 0, callFrames: [] };
        const v = view(fakeEngine(state));
        v.requestHist();
        let last = -1;
        for (let i = 0; i < 2000 && last !== state.calls; i++) {
          last = state.calls;
          for (let k = 0; k < 3; k++) await frame();
        }
        const stoppedAt = state.calls;
        state.busy = false;
        v.requestHist();
        for (let i = 0; i < 10 && v.histDrawn !== 1; i++) await frame();
        result.capped = { stoppedAt, histDrawn: v.histDrawn, guardHit: state.guardHit };
        v.destroy();
      }
      return result;
    }),
    'histogram retry',
  );
} finally {
  await browser.close();
}
process.stdout.write(`${JSON.stringify(out)}\n`);
