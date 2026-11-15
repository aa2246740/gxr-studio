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
    60_000,
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
            state.callTimes?.push(performance.now());
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

      // 4. 经理 r6 裁定：忙超过 300 帧以后不停，放慢到每 250 ms 试一次；空下来后不用新的调整也能自己画上。
      //    慢下来以后照样让出事件循环（定时器照跑），也不是每帧都问。
      {
        const state = { calls: 0, busy: true, guardHit: false, frames: 0, callFrames: [], callTimes: [] };
        const v = view(fakeEngine(state));
        let ticks = 0;
        const interval = setInterval(() => {
          ticks += 1;
        }, 10);
        v.requestHist();
        // 按时间等（不按帧数），修坏了（比如到 300 就停）也是断言失败，不是整个驱动超时。
        const fastUntil = performance.now() + 12_000;
        while (state.calls <= 300 && performance.now() < fastUntil) await frame();
        const fastCalls = state.calls;
        // 慢阶段看 1.1 秒：每 250 ms 一次，大约 4 次。
        const slowFrom = state.calls;
        const slowStart = performance.now();
        await new Promise((done) => setTimeout(done, 1100));
        const slowCalls = state.calls - slowFrom;
        const gaps = state.callTimes.slice(Math.max(0, slowFrom - 1)).map((t, i, all) => (i ? t - all[i - 1] : null)).filter((g) => g !== null);
        const ticksSlow = ticks;
        // 空下来：不调 requestHist，等它自己画上。
        state.busy = false;
        const freedAt = performance.now();
        while (v.histDrawn !== 1 && performance.now() - freedAt < 2000) await frame();
        result.backoff = {
          fastCalls,
          slowCalls,
          slowMs: Math.round(performance.now() - slowStart),
          minGap: gaps.length ? Math.round(Math.min(...gaps)) : null,
          ticks: ticksSlow,
          histDrawn: v.histDrawn,
          drawnAfterMs: Math.round(performance.now() - freedAt),
          guardHit: state.guardHit,
        };
        // 画上以后不再问。
        const settled = state.calls;
        await new Promise((done) => setTimeout(done, 600));
        result.backoff.afterDrawn = state.calls - settled;
        // 慢阶段里关掉：排着的定时器取消，不再问。
        state.busy = true;
        state.calls = 0;
        state.callTimes = [];
        const w = view(fakeEngine(state));
        w.requestHist();
        const slowUntil = performance.now() + 12_000;
        while (state.calls <= 301 && performance.now() < slowUntil) await frame();
        w.destroy();
        const before = state.calls;
        await new Promise((done) => setTimeout(done, 600));
        result.backoff.afterDestroySlow = state.calls - before;
        clearInterval(interval);
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
