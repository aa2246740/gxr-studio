// Sol r1 的五个 P2，在浏览器里用真的 ExportFlow / ExportBatch / ExportCore（开发服务器给的模块）重放，输入照 Sol 的探针：
// 合成的小文件，不读照片。P2-01 先存成功的、点「好」，失败的还能重试；P2-02 停在上限时重试，不丢队列；P2-03 下载兜底的
// 对象 URL 在重画、交出、清空、新的一批时都撤销；P2-04 建 Worker 时 RangeError，这张按陷阱失败、接着做下一张；
// P2-05 留着的待交文件任何时候都不超过上限。

const SETUP = async () => {
  const { ExportFlow } = await import('/src/app/export_ui.ts');
  const { ExportBatch, TEXT } = await import('/src/app/export_batch.ts');
  const { ExportCore } = await import('/src/core/export_api.ts');
  const { exportPaused } = await import('/src/app/export_pause.ts');
  const t = { ExportFlow, ExportBatch, ExportCore, exportPaused, TEXT, live: new Set(), created: 0, revoked: 0 };
  const create = URL.createObjectURL.bind(URL);
  const revoke = URL.revokeObjectURL.bind(URL);
  URL.createObjectURL = (blob) => {
    const url = create(blob);
    t.live.add(url);
    t.created += 1;
    return url;
  };
  URL.revokeObjectURL = (url) => {
    t.live.delete(url);
    t.revoked += 1;
    revoke(url);
  };
  const tick = () => new Promise((done) => setTimeout(done, 0));
  /** 一组可控的回复：`plan[id]` 是一串结果，每次 run 取下一个。 */
  t.host = (plan, onChange = () => undefined) => {
    const calls = [];
    const seen = { maxStaged: 0, stops: 0 };
    const host = {
      calls,
      seen,
      session: () => 1,
      photo: () => ({ version: 1, dng: null, jpg: null }),
      run: async (item) => {
        calls.push(item.id);
        await tick();
        const next = plan[item.id].length > 1 ? plan[item.id].shift() : plan[item.id][0];
        if (typeof next === 'number') return { ok: true, file: new ArrayBuffer(next), result: { file_name: `S${item.id}_gxr.jpg` }, shot: {}, ms: 1 };
        return { ok: false, kind: next, code: next, message: '' };
      },
      stop() {
        seen.stops += 1;
      },
      idle() {},
      markGone() {},
      busy() {},
      changed() {
        onChange();
      },
    };
    return host;
  };
  t.items = (n) => Array.from({ length: n }, (_, i) => ({ id: String(i), stem: `S${i}`, version: 1, dng: null, jpg: null, req: {} }));
  /** 真的 ExportFlow 画在一个单独的壳里，批次用上面的假回复。 */
  t.flow = async (plan, n) => {
    const shell = document.createElement('div');
    shell.className = 'app';
    shell.id = 'state-shell';
    document.querySelector('#state-shell')?.remove();
    document.body.append(shell);
    const app = { photos: () => [], photo: () => undefined, session: () => 1, book: () => ({ dat: null }), markGone() {} };
    const flow = new ExportFlow(shell, app, null);
    const host = t.host(plan, () => flow.paint());
    const batch = new ExportBatch(t.items(n), host);
    flow.batch = batch;
    batch.start();
    await batch.settled();
    t.current = { flow, batch, host, shell };
    return t.current;
  };
  t.tick = tick;
  window.__state = t;
};

const dockOf = (page) =>
  page.evaluate(() => {
    const { batch, shell, flow } = window.__state.current;
    return {
      phase: batch.phase,
      failures: batch.failures.size,
      staged: batch.staged.length,
      retry: shell.querySelectorAll('[data-act="retry"]').length,
      share: shell.querySelectorAll('[data-act="share"]').length,
      dock: shell.querySelector('.pdock')?.innerText ?? null,
      liveUrls: window.__state.live.size,
      flowUrls: flow.liveUrls?.() ?? null,
    };
  });

export async function state({ newPage, out }) {
  const page = await newPage('record');
  await page.evaluate(SETUP);
  const steps = out.steps;
  try {
    // P2-01：两张，第一张读不了。先存成功的那张、在 05e 点「好」：06f 还在，能重试；重试成功后才结束。
    await page.evaluate(() => window.__state.flow({ 0: ['read', 10], 1: [10] }, 2));
    await page.locator('#state-shell [data-act="share"]').click();
    await page.locator('#state-shell [data-act="done"]').click();
    const afterDone = await dockOf(page);
    await page.locator('#state-shell [data-act="retry"]').click();
    await page.waitForFunction(() => window.__state.current.batch.phase === 'ready');
    const afterRetry = await dockOf(page);
    await page.locator('#state-shell [data-act="share"]').click();
    const savedDock = await page.locator('#state-shell .pdock').innerText();
    await page.locator('#state-shell [data-act="done"]').click();
    steps.retryKept = { afterDone, afterRetry, savedDock, end: await dockOf(page) };
  } catch (err) {
    steps.retryKept = { error: String(err?.message ?? err).split('\n')[0].slice(0, 200) };
    await page.evaluate(() => window.__state.current?.flow?.clear?.()).catch(() => undefined);
  }
  try {
    // P2-02 和 P2-05：上限 100，p0 读不了，p1、p2 各 60，p3、p4 各 10。p2 会超过上限：放掉、排回去；停着时点重试。
    steps.held = await page.evaluate(async () => {
      const t = window.__state;
      const plan = { 0: ['read', 1], 1: [60], 2: [60], 3: [10], 4: [10] };
      let batch = null;
      let maxRetained = 0;
      const host = t.host(plan, () => {
        if (batch) maxRetained = Math.max(maxRetained, batch.stagedBytes);
      });
      batch = new t.ExportBatch(t.items(5), host, 100);
      batch.start();
      await batch.settled();
      const heldAt = { phase: batch.phase, staged: batch.staged.map((s) => s.id), pending: batch.pending?.() ?? null, stagedBytes: batch.stagedBytes };
      batch.retry();
      const afterRetry = { phase: batch.phase, pending: batch.pending?.() ?? null, calls: host.calls.length };
      batch.release();
      await batch.settled();
      return {
        heldAt,
        afterRetry,
        end: { phase: batch.phase, staged: batch.staged.map((s) => s.id), failures: batch.failures.size, waits: [...batch.states.values()].filter((s) => s === 'wait').length },
        calls: host.calls,
        maxRetained,
        stagedHigh: batch.stagedHigh,
      };
    });
  } catch (err) {
    steps.held = { error: String(err?.message ?? err).split('\n')[0].slice(0, 200) };
    await page.evaluate(() => window.__state.current?.flow?.clear?.()).catch(() => undefined);
  }
  try {
    // P2-05 照 Sol 的原样：40、50、30（再 25），上限 100。
    steps.cap = await page.evaluate(async () => {
      const t = window.__state;
      let batch = null;
      let maxRetained = 0;
      const host = t.host({ 0: [40], 1: [50], 2: [30], 3: [25] }, () => {
        if (batch) maxRetained = Math.max(maxRetained, batch.stagedBytes);
      });
      batch = new t.ExportBatch(t.items(4), host, 100);
      batch.start();
      await batch.settled();
      const held = { phase: batch.phase, stagedBytes: batch.stagedBytes, stagedHigh: batch.stagedHigh };
      batch.release();
      await batch.settled();
      return { held, end: { phase: batch.phase, sizes: batch.staged.map((s) => s.file.size) }, calls: host.calls, maxRetained, stagedHigh: batch.stagedHigh };
    });
  } catch (err) {
    steps.cap = { error: String(err?.message ?? err).split('\n')[0].slice(0, 200) };
    await page.evaluate(() => window.__state.current?.flow?.clear?.()).catch(() => undefined);
  }
  try {
    // P2-03：没有共享面板。连点两次「存到相册」（重画），点「好」再清空；再来一批，显示链接后直接清空；再一批后开始新的一批。
    await page.evaluate(() => {
      Object.defineProperty(navigator, 'share', { configurable: true, value: undefined });
      Object.defineProperty(navigator, 'canShare', { configurable: true, value: undefined });
    });
    await page.evaluate(() => window.__state.flow({ 0: [1024] }, 1));
    await page.locator('#state-shell [data-act="share"]').click();
    const firstPaint = await dockOf(page);
    await page.locator('#state-shell [data-act="share"]').click();
    const secondPaint = await dockOf(page);
    await page.locator('#state-shell .links button').click();
    await page.evaluate(() => window.__state.current.flow.clear());
    const afterDoneClear = await dockOf(page);
    await page.evaluate(() => window.__state.flow({ 0: [1024] }, 1));
    await page.locator('#state-shell [data-act="share"]').click();
    await page.evaluate(() => window.__state.current.flow.clear());
    const afterClear = await dockOf(page);
    await page.evaluate(() => window.__state.flow({ 0: [1024] }, 1));
    await page.locator('#state-shell [data-act="share"]').click();
    // 这一批结束（取消后重画）也撤销。
    await page.evaluate(() => {
      const { flow } = window.__state.current;
      flow.batch.cancel();
      flow.paint();
    });
    const afterEnd = await dockOf(page);
    // 显示链接时开始新的一批（flow 的 start 路径，合成的空照片，没有文件）：上一批的链接先撤销。
    await page.evaluate(() => window.__state.flow({ 0: [1024] }, 1));
    await page.locator('#state-shell [data-act="share"]').click();
    const beforeStart = await dockOf(page);
    await page.evaluate(() => {
      const { flow } = window.__state.current;
      const knobs = { exposure_ev: 0, contrast: 0, highlight: 0, shadow: 0, black_point: 0, warm: 0, tint: 0, saturation: 0, vibrance: 0, vignette: 0 };
      const photo = { id: 'n', stem: 'N', version: 1, dng: null, jpg: null, lastModified: 0, captureMs: 0, jpgMeta: null, dngMeta: null, choice: null, levels: new Map(), memory: new Map(), knobs };
      flow.start([photo], { size: 'original', quality: 'high' });
    });
    const afterStart = await page.evaluate(() => ({ liveUrls: window.__state.live.size, flowUrls: window.__state.current.flow.liveUrls?.() ?? null }));
    await page.evaluate(() => window.__state.current.flow.clear());
    steps.urls = {
      firstPaint,
      secondPaint,
      afterDoneClear,
      afterClear,
      afterEnd,
      beforeStart,
      afterStart,
      created: await page.evaluate(() => window.__state.created),
      revoked: await page.evaluate(() => window.__state.revoked),
    };
  } catch (err) {
    steps.urls = { error: String(err?.message ?? err).split('\n')[0].slice(0, 200) };
    await page.evaluate(() => window.__state.current?.flow?.clear?.()).catch(() => undefined);
  }
  try {
    // P2-04：建 Worker 时 RangeError（工厂每次都抛）。两张都按陷阱失败，各试一次建 Worker，批次停在 ready，暂停解除。
    steps.workerOom = await page.evaluate(async () => {
      const t = window.__state;
      let attempts = 0;
      const core = new t.ExportCore(() => {
        attempts += 1;
        throw new RangeError('synthetic Worker allocation failure');
      });
      core.begin({ dat: null, calibIndex: '', calibBlob: null });
      const flow = { busy: false };
      const host = {
        run: (item) => core.run({ dng: null, jpg: null, req: item.req }),
        session: () => 1,
        photo: () => ({ version: 1, dng: null, jpg: null }),
        stop: () => core.stop(),
        idle: () => core.release(),
        markGone() {},
        busy(on) {
          flow.busy = on;
        },
        changed() {},
      };
      const batch = new t.ExportBatch(t.items(2), host);
      batch.start();
      await batch.settled();
      return {
        phase: batch.phase,
        states: Object.fromEntries(batch.states),
        messages: [...batch.failures.values()].map((f) => f.message === t.TEXT.memory),
        kinds: [...batch.failures.values()].map((f) => f.kind),
        attempts,
        busy: flow.busy,
        alive: core.alive(),
      };
    });
  } catch (err) {
    steps.workerOom = { error: String(err?.message ?? err).split('\n')[0].slice(0, 200) };
    await page.evaluate(() => window.__state.current?.flow?.clear?.()).catch(() => undefined);
  }
  try {
    // 只有第一次建 Worker 抛：第二张用真的导出 Worker 做一张合成的空请求（它会按核心的拒绝失败，但 Worker 建得起来、批次走完）。
    steps.workerOomOnce = await page.evaluate(async () => {
      const t = window.__state;
      let attempts = 0;
      const core = new t.ExportCore(() => {
        attempts += 1;
        if (attempts === 1) throw new RangeError('synthetic Worker allocation failure');
        return new Worker(new URL('/src/core/export_worker.ts', location.href), { type: 'module' });
      });
      core.begin({ dat: null, calibIndex: '', calibBlob: null });
      const req = { shot: { name: 'S', files: {} }, side: {}, defaults: {}, base: 'auto', layout: {}, binding: 'x', quality: 95, longEdge: undefined, hdr: false, mtimeMs: 0, tzOffsetMin: 0, resolve: null, needsJpg: false };
      const host = {
        run: (item) => core.run({ dng: null, jpg: null, req }),
        session: () => 1,
        photo: () => ({ version: 1, dng: null, jpg: null }),
        stop: () => core.stop(),
        idle: () => core.stop(),
        markGone() {},
        busy() {},
        changed() {},
      };
      const batch = new t.ExportBatch(t.items(2), host);
      batch.start();
      await batch.settled();
      return { phase: batch.phase, kinds: [...batch.failures.values()].map((f) => f.kind), attempts, calls: batch.items.map((it) => batch.states.get(it.id)) };
    });
  } catch (err) {
    steps.workerOomOnce = { error: String(err?.message ?? err).split('\n')[0].slice(0, 200) };
    await page.evaluate(() => window.__state.current?.flow?.clear?.()).catch(() => undefined);
  }
  // Sol r2 P2-04（照它的复现）：一张的导出面板先查成品尺寸，这时已经建好 Worker；点「存到相册」后给这个 Worker 发设置时
  // RangeError（第 2 次发设置时注入，真的导出 Worker 和 WASM，浏览器里现画的 2×2 JPG）。这张按陷阱失败、写内存那句、
  // 有重试；Worker 关掉；重试用新的 Worker 做好。
  try {
    await page.evaluate(async () => {
      const t = window.__state;
      const shell = document.createElement('div');
      shell.className = 'app';
      shell.id = 'state-shell';
      document.querySelector('#state-shell')?.remove();
      document.body.append(shell);
      const canvas = document.createElement('canvas');
      canvas.width = 2;
      canvas.height = 2;
      canvas.getContext('2d').fillRect(0, 0, 2, 2);
      const jpg = new File([await new Promise((done) => canvas.toBlob(done, 'image/jpeg'))], 'S0.JPG');
      const knobs = { exposure_ev: 0, contrast: 0, highlight: 0, shadow: 0, black_point: 0, warm: 0, tint: 0, saturation: 0, vibrance: 0, vignette: 0 };
      const photo = { id: 'p0', stem: 'S0', version: 1, dng: null, jpg, lastModified: 0, captureMs: 0, jpgMeta: null, dngMeta: null, choice: null, levels: new Map(), memory: new Map(), knobs };
      const app = { photos: () => [photo], photo: (id) => (id === photo.id ? photo : undefined), session: () => 1, book: () => ({ dat: null, pack: null }), markGone() {} };
      const flow = new t.ExportFlow(shell, app, null);
      const calls = { setup: 0, sizes: 0, run: 0, terminate: 0 };
      flow.core.factory = () => {
        const worker = new Worker(new URL('/src/core/export_worker.ts', location.href), { type: 'module' });
        const post = worker.postMessage.bind(worker);
        const terminate = worker.terminate.bind(worker);
        worker.postMessage = (msg, ...args) => {
          if (msg.op in calls) calls[msg.op] += 1;
          if (msg.op === 'setup' && calls.setup === 2) throw new RangeError('synthetic warm setup allocation');
          return post(msg, ...args);
        };
        worker.terminate = () => {
          calls.terminate += 1;
          return terminate();
        };
        return worker;
      };
      t.warm = { flow, shell, calls };
      flow.open(['p0']);
    });
    await page.waitForFunction(() => document.querySelector('#state-shell .size')?.textContent.includes('2 × 2'), null, { timeout: 60_000 });
    const before = await page.evaluate(() => ({ alive: window.__state.warm.flow.core.alive(), calls: { ...window.__state.warm.calls } }));
    await page.locator('#state-shell .ms.export .go').click();
    await page.waitForFunction(() => window.__state.warm.flow.current()?.phase === 'ready', null, { timeout: 60_000 });
    const warmState = () =>
      page.evaluate(() => {
        const { flow, shell, calls } = window.__state.warm;
        const b = flow.current();
        return {
          phase: b?.phase ?? null,
          kinds: b ? [...b.failures.values()].map((f) => f.kind) : [],
          staged: b?.staged.length ?? 0,
          alive: flow.core.alive(),
          calls: { ...calls },
          retry: shell.querySelectorAll('[data-act="retry"]').length,
          memoryLine: (shell.querySelector('.pdock')?.textContent ?? '').includes(window.__state.TEXT.memory),
          sheet: shell.querySelector('.ms.export') !== null,
        };
      });
    const after = await warmState();
    await page.locator('#state-shell [data-act="retry"]').click();
    await page.waitForFunction(() => window.__state.warm.flow.current()?.phase === 'ready', null, { timeout: 60_000 });
    const retried = await warmState();
    await page.evaluate(() => window.__state.warm.flow.clear());
    steps.warmSetup = { before, after, retried };
  } catch (err) {
    steps.warmSetup = { error: String(err?.message ?? err).split('\n')[0].slice(0, 200) };
    await page.evaluate(() => window.__state.warm?.flow?.clear?.()).catch(() => undefined);
  }

  // 几张一批、Worker 已经建好（先查过尺寸）时发设置抛错：这张按陷阱失败，下一张用新的 Worker 做好。
  try {
    steps.warmBatch = await page.evaluate(async () => {
      const t = window.__state;
      let attempts = 0;
      let setups = 0;
      let terminations = 0;
      const core = new t.ExportCore(() => {
        attempts += 1;
        return {
          postMessage(msg) {
            if (msg.op === 'setup') {
              setups += 1;
              if (setups === 2) throw new RangeError('synthetic warm setup allocation');
            }
            if (msg.op === 'run') {
              queueMicrotask(() => this.onmessage({ data: { op: 'done', id: msg.job.id, ok: true, file: new ArrayBuffer(20), result: { file_name: 'S_gxr.jpg' }, shot: {}, ms: 1 } }));
            }
            if (msg.op === 'sizes') queueMicrotask(() => this.onmessage({ data: { op: 'sizes', id: msg.job.id, ok: true, json: '{}' } }));
          },
          terminate() {
            terminations += 1;
          },
        };
      });
      const probe = await core.sizes({ dng: null, jpg: null, side: {}, useJpg: true });
      core.begin({ dat: new ArrayBuffer(4), calibIndex: '', calibBlob: new ArrayBuffer(4) });
      let busy = true;
      const batch = new t.ExportBatch(t.items(2), {
        run: (item) => core.run({ dng: null, jpg: null, req: item.req }),
        session: () => 1,
        photo: () => ({ version: 1, dng: null, jpg: null }),
        stop: () => core.stop(),
        idle() {},
        markGone() {},
        busy(on) {
          busy = on;
        },
        changed() {},
      });
      batch.start();
      await batch.settled();
      const result = {
        probe: probe.ok,
        phase: batch.phase,
        states: Object.fromEntries(batch.states),
        kinds: [...batch.failures.values()].map((f) => f.kind),
        memoryLine: [...batch.failures.values()].every((f) => f.message === t.TEXT.memory),
        attempts,
        setups,
        terminations,
        busy,
      };
      core.stop();
      return result;
    });
  } catch (err) {
    steps.warmBatch = { error: String(err?.message ?? err).split('\n')[0].slice(0, 200) };
  }
  steps.perfEmpty = await page.evaluate(() => {
    const t = window.__state;
    const shell = document.createElement('div');
    shell.className = 'app';
    const perf = document.createElement('p');
    perf.hidden = false;
    const app = { photos: () => [], photo: () => undefined, session: () => 1, book: () => ({ dat: null }), markGone() {} };
    const flow = new t.ExportFlow(shell, app, perf);
    const batch = new t.ExportBatch(t.items(1), t.host({ 0: [10] }));
    flow.batch = batch;
    flow.paint();
    const text = perf.textContent ?? '';
    flow.clear();
    return text;
  });
  steps.paused = await page.evaluate(() => window.__state.exportPaused());
  await page.close();
}
