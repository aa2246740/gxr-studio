// Sol r6 P1：显卡在忙时 histogram() 立刻给 null，编辑页的直方图重试不能接着在微任务里转，要让出事件循环。
// 在开发服务器的页面里 import 真的 EditorView，配一个假的忙引擎（Sol 的 hist-scheduler 探针同样的状态）。
// 1–3 用真的 rAF 和定时器（抓微任务自旋）；4 以后把 rAF 和定时器换成手里推进的队列，
// 300 次、250 ms 退避、空下来、换片、显卡丢了这些都按帧数和毫秒显式推进，不依赖页面跑多少 fps（Sol r7）。
// 不用照片，不用相机数据。

import { readFileSync } from 'node:fs';
import { pathToFileURL } from 'node:url';

const [runtime, url, manifestPath] = process.argv.slice(2);
const manifest = manifestPath ? JSON.parse(readFileSync(manifestPath, 'utf8')) : {};
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
    page.evaluate(async ({ repo }) => {
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
        v.histOpen = true;
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

      // 2. 一直忙：每帧最多问一次，期间页面定时器照常跑。
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

      // 4. 受控调度：rAF 和定时器进手里的队列，显式推进。
      const real = { raf: window.requestAnimationFrame, caf: window.cancelAnimationFrame, st: window.setTimeout, ct: window.clearTimeout };
      let next = 0;
      let now = 0;
      const frames = new Map();
      const timers = new Map();
      window.requestAnimationFrame = (fn) => {
        const id = ++next;
        frames.set(id, fn);
        return id;
      };
      window.cancelAnimationFrame = (id) => frames.delete(id);
      window.setTimeout = (fn, ms) => {
        const id = ++next;
        timers.set(id, { fn, at: now + (ms ?? 0) });
        return id;
      };
      window.clearTimeout = (id) => timers.delete(id);
      const flush = async () => {
        for (let i = 0; i < 12; i++) await Promise.resolve();
      };
      const step = async () => {
        const due = [...frames.values()];
        frames.clear();
        for (const fn of due) fn(now);
        await flush();
      };
      const advance = async (ms) => {
        now += ms;
        for (const [id, task] of [...timers]) {
          if (task.at <= now) {
            timers.delete(id);
            task.fn();
          }
        }
        await flush();
      };
      const queued = () => ({ frames: frames.size, timers: timers.size });
      const busyState = () => ({ calls: 0, busy: true, guardHit: false, frames: 0, callFrames: [] });
      try {
        // 4a. 300 次没拿到：前 300 次按帧，之后只剩一个 250 ms 定时器，不再排帧。
        const s1 = busyState();
        const e1 = fakeEngine(s1);
        const v1 = view(e1);
        v1.requestHist();
        await flush();
        for (let i = 0; i < 299; i++) await step();
        const head = [...timers.values()][0];
        result.threshold = { calls: s1.calls, ...queued(), delay: head ? head.at - now : null };
        await advance(249);
        result.before250 = s1.calls;
        await advance(1);
        result.at250 = s1.calls;
        for (let i = 0; i < 4; i++) await advance(250);
        result.fourSlowTicks = { calls: s1.calls, ...queued() };
        // 4b. 空下来：不调 requestHist，下一个慢周期自己画上，然后什么都不排。
        s1.busy = false;
        await advance(250);
        result.freeWithoutInput = { calls: s1.calls, histDrawn: v1.histDrawn, ...queued() };
        // 4c. 慢阶段里来一次新的调整：慢定时器取消，重新按帧试。
        s1.busy = true;
        e1.contentVersion = 2;
        v1.requestHist();
        await flush();
        for (let i = 0; i < 299; i++) await step();
        const staleSlow = [...timers.values()][0]?.fn;
        e1.contentVersion = 3;
        const beforeAdjust = s1.calls;
        v1.requestHist();
        await flush();
        result.newAdjustment = { calls: s1.calls - beforeAdjust, misses: v1.histMisses, ...queued() };
        // 4d. 换片：排着的帧取消；已经拿在手里的旧回调晚到也不再问。
        const staleFrame = [...frames.values()][0];
        v1.destroy();
        const stopped = s1.calls;
        staleFrame?.();
        staleSlow?.();
        await flush();
        result.destroyAndStale = { more: s1.calls - stopped, ...queued() };
        // 4e. 慢阶段里换片：定时器取消，再过 1 秒也不问。
        const s2 = busyState();
        const v2 = view(fakeEngine(s2));
        v2.requestHist();
        await flush();
        for (let i = 0; i < 299; i++) await step();
        const queuedSlow = [...timers.values()][0]?.fn;
        v2.destroy();
        queuedSlow?.();
        await flush();
        await advance(1000);
        result.destroySlow = { calls: s2.calls, ...queued() };
        // 4f. 读回 reject 也让出：每帧一次。
        const s3 = { ...busyState(), reject: true };
        const e3 = fakeEngine(s3);
        const realHist = e3.histogram;
        e3.histogram = async function () {
          await realHist.call(this);
          throw new Error('synthetic readback rejected');
        };
        const v3 = view(e3);
        v3.requestHist();
        await flush();
        for (let i = 0; i < 10; i++) await step();
        result.rejected = { calls: s3.calls, ...queued() };
        v3.destroy();
        // 4g. 读回还没回来就换片：回来以后不画、不再排。
        let release;
        const e4 = fakeEngine(busyState());
        e4.histogram = () =>
          new Promise((done) => {
            release = done;
          });
        const v4 = view(e4);
        v4.requestHist();
        await flush();
        v4.destroy();
        release({ counts: new Uint32Array(1026), samples: 1, version: 1 });
        await flush();
        result.lateReadback = { histDrawn: v4.histDrawn, ...queued() };

        // 5. Sol r7：显卡丢了、pending 一直是 true。用真的 Engine 原型的 pending getter 和 histogram()
        //    （丢了就给 null），不建显卡。pending 和忙一样占同一个预算：300 帧后每 250 ms 只看一次 pending，不排帧。
        const { Engine } = await import(`/@fs${repo}/frontend/src/engine.ts`);
        const lostEngine = Object.create(Engine.prototype);
        Object.assign(lostEngine, { lost: true, dirtyColor: true, dirtyGeo: false, raf: 0, version: 1, finalTex: {} });
        const pendingGet = Object.getOwnPropertyDescriptor(Engine.prototype, 'pending').get;
        const realHistogram = Engine.prototype.histogram;
        const lost = { calls: 0, pendingReads: 0 };
        Object.defineProperty(lostEngine, 'pending', {
          get() {
            lost.pendingReads += 1;
            return pendingGet.call(lostEngine);
          },
        });
        lostEngine.histogram = async () => {
          lost.calls += 1;
          return realHistogram.call(lostEngine);
        };
        lostEngine.detach = () => undefined;
        const v5 = view(lostEngine);
        v5.requestHist();
        await flush();
        for (let i = 0; i < 299; i++) await step();
        result.lostAt300 = { isLost: lostEngine.isLost, pendingReads: lost.pendingReads, histogramCalls: lost.calls, misses: v5.histMisses, ...queued() };
        const reads300 = lost.pendingReads;
        await advance(250);
        const afterTimer = { pendingReads: lost.pendingReads - reads300, ...queued() };
        for (let i = 0; i < 30; i++) await step();
        for (let i = 0; i < 3; i++) await advance(250);
        result.lostSlow = { afterTimer, pendingReads: lost.pendingReads - reads300, histogramCalls: lost.calls, ...queued() };
        v5.destroy();
        result.lostDestroyed = queued();

        // 6. pending 一直 true 超过 300 次、然后这一帧画完了：不用新的调整，下一个慢周期自己画上。
        const s6 = { ...busyState(), busy: false, pending: true };
        const e6 = fakeEngine(s6);
        Object.defineProperty(e6, 'pending', { get: () => s6.pending });
        const v6 = view(e6);
        v6.requestHist();
        await flush();
        for (let i = 0; i < 299; i++) await step();
        await advance(250);
        const whilePending = { calls: s6.calls, ...queued() };
        s6.pending = false;
        await advance(250);
        result.pendingThenReady = { whilePending, calls: s6.calls, histDrawn: v6.histDrawn, ...queued() };
        v6.destroy();
      } finally {
        window.requestAnimationFrame = real.raf;
        window.cancelAnimationFrame = real.caf;
        window.setTimeout = real.st;
        window.clearTimeout = real.ct;
      }
      return result;
    }, { repo: manifest.repo }),
    'histogram retry',
  );
} finally {
  await browser.close();
}
process.stdout.write(`${JSON.stringify(out)}\n`);
