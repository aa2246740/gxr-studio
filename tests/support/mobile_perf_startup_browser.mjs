// 启动性能浏览器回归驱动（perf-startup，不含已拆至 studio/offline-sw 的 SW 用例）。
// 模式：wasm-once / start-before-editor / worker-wasm-retry / pool-slot-recovery / pool-idle-cold-slot / import-before-prewarm。
// 懒加载分包失败与竞态的用例在 mobile_perf_startup_lazy_browser.mjs。
// 所有服务只绑 127.0.0.1 临时端口（避开禁用端口），结束后确保关闭浏览器和 HTTP 服务。

import fs from 'node:fs';
import http from 'node:http';
import { stripTypeScriptTypes } from 'node:module';
import path from 'node:path';
import { pathToFileURL } from 'node:url';

const [runtimePath, mode, manifestPath] = process.argv.slice(2);
const manifest = JSON.parse(fs.readFileSync(manifestPath, 'utf8'));

const FORBIDDEN_PORTS = new Set([60887, 18772, 18801, 18802, 18803, 5290]);
const CHROMIUM_ARGS = [
  '--enable-unsafe-webgpu',
  '--enable-unsafe-swiftshader',
  '--use-angle=swiftshader',
  '--use-webgpu-adapter=swiftshader',
];
const MIME = {
  '.html': 'text/html; charset=utf-8',
  '.js': 'text/javascript; charset=utf-8',
  '.mjs': 'text/javascript; charset=utf-8',
  '.css': 'text/css; charset=utf-8',
  '.wasm': 'application/wasm',
  '.zip': 'application/zip',
  '.ttf': 'font/ttf',
  '.woff2': 'font/woff2',
  '.txt': 'text/plain; charset=utf-8',
  '.json': 'application/json; charset=utf-8',
  '.svg': 'image/svg+xml',
  '.png': 'image/png',
  '.jpg': 'image/jpeg',
};

async function loadRuntime() {
  const mod = await import(pathToFileURL(runtimePath).href);
  mod.verifyPinnedRuntime();
  return mod;
}

async function loadWebKit(pinnedRuntime) {
  const { webkit } = await import(pathToFileURL(pinnedRuntime.modulePath).href);
  const direct = webkit.executablePath();
  if (direct && fs.existsSync(direct)) return { webkit, executablePath: direct };
  const baseName = direct ? path.basename(direct) : 'pw_run.sh';
  if (fs.existsSync(pinnedRuntime.browsersPath)) {
    for (const entry of fs.readdirSync(pinnedRuntime.browsersPath)) {
      if (!entry.startsWith('webkit-')) continue;
      const candidate = path.join(pinnedRuntime.browsersPath, entry, baseName);
      if (fs.existsSync(candidate)) return { webkit, executablePath: candidate };
    }
  }
  return null;
}

function startStaticServer(initialRoot) {
  const rootDir = path.resolve(initialRoot);
  let blockCalib = false;
  let reqLog = [];
  const server = http.createServer((req, res) => {
    const rawUrl = req.url || '/';
    const cleanPath = decodeURIComponent(rawUrl.split('?')[0]);
    if (blockCalib && cleanPath === '/calib/gxr-default-calib.zip') {
      res.statusCode = 404;
      res.end('default calib blocked');
      reqLog.push({ path: cleanPath, status: 404, bytes: 0, ts: performance.now() });
      return;
    }
    const rel = cleanPath === '/' ? 'index.html' : cleanPath.replace(/^\/+/, '');
    const full = path.resolve(rootDir, rel);
    if (!full.startsWith(rootDir + path.sep) && full !== rootDir) {
      res.statusCode = 403;
      res.end('forbidden');
      return;
    }
    if (!fs.existsSync(full) || !fs.statSync(full).isFile()) {
      res.statusCode = 404;
      res.end('not found');
      reqLog.push({ path: cleanPath, status: 404, bytes: 0, ts: performance.now() });
      return;
    }
    const buf = fs.readFileSync(full);
    const ext = path.extname(full).toLowerCase();
    res.setHeader('Content-Type', MIME[ext] || 'application/octet-stream');
    if (cleanPath.startsWith('/assets/')) {
      res.setHeader('Cache-Control', 'public, max-age=31536000, immutable');
    } else if (cleanPath.startsWith('/calib/')) {
      res.setHeader('Cache-Control', 'public, max-age=3600');
    } else {
      res.setHeader('Cache-Control', 'no-cache');
    }
    res.setHeader('Content-Length', String(buf.byteLength));
    res.end(buf);
    reqLog.push({ path: cleanPath, status: 200, bytes: buf.byteLength, ts: performance.now() });
  });

  return new Promise((resolve, reject) => {
    const tryListen = () => {
      server.listen(0, '127.0.0.1', () => {
        const addr = server.address();
        if (!addr || typeof addr === 'string') return reject(new Error('bad address'));
        if (FORBIDDEN_PORTS.has(addr.port)) {
          server.close(() => tryListen());
          return;
        }
        resolve({
          url: `http://127.0.0.1:${addr.port}`,
          setBlockCalib(on) { blockCalib = Boolean(on); },
          resetLog() { reqLog = []; },
          snapshotLog() { return reqLog.slice(); },
          close() { return new Promise((r) => server.close(r)); },
        });
      });
    };
    server.on('error', reject);
    tryListen();
  });
}

async function runWasmOnce() {
  const { pinnedRuntime, launchPinnedChromium } = await loadRuntime();
  const server = await startStaticServer(manifest.distDir);
  const results = {};
  try {
    const engines = [
      { name: 'chromium', launch: () => launchPinnedChromium({ args: CHROMIUM_ARGS }) },
    ];
    const wk = await loadWebKit(pinnedRuntime);
    if (wk) {
      engines.push({
        name: 'webkit',
        launch: () => wk.webkit.launch({ headless: true, executablePath: wk.executablePath }),
      });
    } else {
      results.webkitSkipped = true;
    }

    for (const eng of engines) {
      const browser = await eng.launch();
      try {
        // Case A: normal cold open with default calibration available
        server.setBlockCalib(false);
        server.resetLog();
        const ctxA = await browser.newContext({ viewport: { width: 390, height: 844 }, hasTouch: true, isMobile: true, deviceScaleFactor: 2 });
        const pageA = await ctxA.newPage();
        const errorsA = [];
        pageA.on('pageerror', (e) => errorsA.push(e.message));
        await pageA.goto(`${server.url}/`, { waitUntil: 'domcontentloaded' });
        await pageA.waitForSelector('.app[data-screen="start"] .start button.btn.primary', { state: 'visible', timeout: 30000 });
        await pageA.waitForTimeout(1800);
        const logA = server.snapshotLog();
        await ctxA.close();

        // Case B: cold open with default calibration unavailable (404) and empty IDB
        server.setBlockCalib(true);
        server.resetLog();
        const ctxB = await browser.newContext({ viewport: { width: 390, height: 844 }, hasTouch: true, isMobile: true, deviceScaleFactor: 2 });
        const pageB = await ctxB.newPage();
        const errorsB = [];
        pageB.on('pageerror', (e) => errorsB.push(e.message));
        await pageB.goto(`${server.url}/`, { waitUntil: 'domcontentloaded' });
        await pageB.waitForSelector('.app[data-screen="start"] .start button.btn.primary', { state: 'visible', timeout: 30000 });
        await pageB.waitForTimeout(1200);
        const logB = server.snapshotLog();
        await ctxB.close();

        results[eng.name] = {
          normal: {
            wasmRequests: logA.filter((r) => r.path.endsWith('.wasm') && r.status === 200).length,
            totalRequests: logA.length,
            totalBytes: logA.reduce((s, r) => s + r.bytes, 0),
            paths: logA.map((r) => r.path),
            pageErrors: errorsA,
          },
          noCalib: {
            wasmRequests: logB.filter((r) => r.path.endsWith('.wasm') && r.status === 200).length,
            totalRequests: logB.length,
            paths: logB.map((r) => r.path),
            pageErrors: errorsB,
          },
        };
      } finally {
        await browser.close();
      }
    }
  } finally {
    await server.close();
  }
  return results;
}

async function runStartBeforeEditor() {
  const { launchPinnedChromium } = await loadRuntime();
  const server = await startStaticServer(manifest.distDir);
  const assetsDir = path.join(manifest.distDir, 'assets');
  const distAssets = fs.readdirSync(assetsDir);
  const editorChunkFiles = distAssets.filter((f) => /^editor_chunk-.*\.js$/.test(f));
  const gridChunkFiles = distAssets.filter((f) => /^grid-.*\.js$/.test(f));

  const browser = await launchPinnedChromium({ args: CHROMIUM_ARGS });
  try {
    const ctx = await browser.newContext({ viewport: { width: 390, height: 844 }, hasTouch: true, isMobile: true, deviceScaleFactor: 2 });
    const page = await ctx.newPage();
    const pageErrors = [];
    page.on('pageerror', (e) => pageErrors.push(e.message));

    await page.addInitScript(() => {
      window.__BOOT_TRACE__ = {
        gpuRequestedAt: null,
        interactiveAt: null,
        gpuBeforeInteractive: null,
        resourcesAtInteractive: null,
      };
      if (navigator.gpu && typeof navigator.gpu.requestAdapter === 'function') {
        const orig = navigator.gpu.requestAdapter.bind(navigator.gpu);
        navigator.gpu.requestAdapter = function patched(...args) {
          if (window.__BOOT_TRACE__.gpuRequestedAt === null) {
            window.__BOOT_TRACE__.gpuRequestedAt = performance.now();
          }
          return orig(...args);
        };
      }
      const checkStart = () => {
        if (window.__BOOT_TRACE__.interactiveAt !== null) return;
        const btn = document.querySelector('.app[data-screen="start"] .start button.btn.primary');
        if (btn) {
          const rect = btn.getBoundingClientRect();
          if (rect.width > 0 && rect.height > 0) {
            window.__BOOT_TRACE__.interactiveAt = performance.now();
            window.__BOOT_TRACE__.gpuBeforeInteractive = window.__BOOT_TRACE__.gpuRequestedAt !== null;
            window.__BOOT_TRACE__.resourcesAtInteractive = performance
              .getEntriesByType('resource')
              .map((entry) => new URL(entry.name, location.href).pathname);
          }
        }
      };
      const mo = new MutationObserver(checkStart);
      mo.observe(document, { childList: true, subtree: true, attributes: true });
      document.addEventListener('DOMContentLoaded', checkStart);
      requestAnimationFrame(function poll() {
        checkStart();
        if (window.__BOOT_TRACE__.interactiveAt === null) requestAnimationFrame(poll);
      });
    });

    await page.goto(`${server.url}/`, { waitUntil: 'domcontentloaded' });
    await page.waitForSelector('.app[data-screen="start"] .start button.btn.primary', { state: 'visible', timeout: 30000 });
    await page.waitForFunction(() => window.__BOOT_TRACE__?.interactiveAt !== null, null, { timeout: 10000 });
    const trace = await page.evaluate(() => window.__BOOT_TRACE__);
    const swRegistrations = await page.evaluate(async () =>
      'serviceWorker' in navigator ? (await navigator.serviceWorker.getRegistrations()).length : 0,
    );

    await page.locator('input.filepick:not(.calibpick)').first().setInputFiles(manifest.photoFiles);
    await page.waitForSelector('.cell canvas[data-drawn]', { timeout: 30000 });
    await page.locator('.cell').first().click();
    await page.waitForSelector('.editor .stage', { state: 'visible', timeout: 30000 });
    await page.waitForFunction(
      () =>
        document.querySelector('.app')?.dataset.frame === '1' ||
        (document.querySelector('.app')?.dataset.settled && document.querySelector('.app').dataset.settled !== '0'),
      null,
      { timeout: 60000 },
    );

    const afterOpen = await page.evaluate(() => ({
      screen: document.querySelector('.app')?.dataset.screen ?? null,
      gpu: document.querySelector('.app')?.dataset.gpu ?? null,
      gpuRequestedAt: window.__BOOT_TRACE__.gpuRequestedAt,
      resourcesAfterOpen: performance.getEntriesByType('resource').map((entry) => new URL(entry.name, location.href).pathname),
    }));
    const reqsAfterOpen = server.snapshotLog().map((r) => r.path);

    await ctx.close();
    return {
      editorChunkFiles,
      gridChunkFiles,
      swRegistrations,
      trace,
      afterOpen,
      reqsAfterOpen,
      pageErrors,
    };
  } finally {
    await browser.close();
    await server.close();
  }
}

async function runWorkerWasmRetry() {
  const { launchPinnedChromium } = await loadRuntime();
  const assets = fs.readdirSync(path.join(manifest.distDir, 'assets'));
  const workerPath = '/assets/' + assets.find((f) => /^worker-.*\.js$/.test(f));
  const wasmPath = '/assets/' + assets.find((f) => /\.wasm$/.test(f));
  const server = await startStaticServer(manifest.distDir);
  const browser = await launchPinnedChromium({ args: CHROMIUM_ARGS });
  try {
    const ctx = await browser.newContext();
    const page = await ctx.newPage();
    await page.goto(`${server.url}/`, { waitUntil: 'domcontentloaded', timeout: 20000 });
    await page.locator('.app[data-screen="start"] .start button.btn.primary').waitFor({ state: 'visible', timeout: 20000 });
    const result = await page.evaluate(
      async ({ workerPath: wp, wasmPath: mp }) => {
        const wasm = await WebAssembly.compileStreaming(fetch(mp));
        const worker = new Worker(wp, { type: 'module' });
        let needCount = 0;
        const replies = new Map();
        worker.onmessage = ({ data }) => {
          if (data?.needWasm) {
            needCount += 1;
            worker.postMessage(needCount === 1 ? { wasmError: 'temporary fetch failure' } : { wasmModule: wasm });
          } else if (typeof data?.id === 'number') {
            replies.get(data.id)?.(data);
          }
        };
        const send = (id, extra = {}) =>
          new Promise((resolve) => {
            replies.set(id, resolve);
            worker.postMessage({
              id,
              op: 'export-output-sizes',
              wasmManaged: true,
              buffer: null,
              args: null,
              sizes: { w: 96, h: 64, side: '{}', rect: '' },
              ...extra,
            });
          });
        try {
          const first = await send(1);
          const second = await send(2, { wasmModule: wasm });
          return {
            needCount,
            firstOk: Boolean(first.ok),
            firstError: first.message ?? null,
            secondOk: Boolean(second.ok),
            secondError: second.message ?? null,
          };
        } finally {
          worker.terminate();
        }
      },
      { workerPath, wasmPath },
    );
    await ctx.close();
    return result;
  } finally {
    await browser.close();
    await server.close();
  }
}

async function runPoolSlotRecovery() {
  const corePath = path.resolve(manifest.repoDir, 'mobile', 'src', 'core');
  const source = fs
    .readFileSync(path.join(corePath, 'pool.ts'), 'utf8')
    .replace("'./protocol'", JSON.stringify(pathToFileURL(path.join(corePath, 'protocol.ts')).href))
    .replaceAll('import.meta.url', JSON.stringify(pathToFileURL(path.join(corePath, 'pool.ts')).href));
  const { Pool } = await import(`data:text/javascript;base64,${Buffer.from(stripTypeScriptTypes(source)).toString('base64')}`);

  const instances = [];
  let holdWorkers = new Set();
  const heldReplies = [];
  globalThis.Worker = class FakeWorker {
    dead = false;
    sent = [];
    constructor() {
      instances.push(this);
    }
    postMessage(message) {
      this.sent.push(message);
      const reply = () => {
        if (!this.dead) this.onmessage?.({ data: { id: message.id, ok: true, json: '{}', buffers: [] } });
      };
      if (holdWorkers.has(this)) heldReplies.push(reply);
      else queueMicrotask(reply);
    }
    terminate() {
      this.dead = true;
    }
  };

  Object.defineProperty(navigator, 'hardwareConcurrency', { value: 2, configurable: true });
  const single = new Pool();
  const p1 = single.request('ping', null, []);
  instances[0].dead = true;
  instances[0].onerror?.({ message: 'simulated module load failure' });
  const firstResult = await p1.then(() => 'resolved', () => 'rejected');
  const p2 = single.request('ping', null, []);
  const retryResult = await Promise.race([
    p2.then(() => 'resolved', () => 'rejected'),
    new Promise((resolve) => setTimeout(() => resolve('pending'), 300)),
  ]);
  const singleSummary = {
    firstResult,
    retryResult,
    workersCreated: instances.length,
    messagesToFailedWorker: instances[0].sent.length,
    failedWorkerTerminated: instances[0].dead,
  };
  single.stop();
  await p2.catch(() => {});

  instances.length = 0;
  heldReplies.length = 0;
  holdWorkers = new Set();
  Object.defineProperty(navigator, 'hardwareConcurrency', { value: 5, configurable: true });
  const origWorker = globalThis.Worker;
  globalThis.Worker = class HeldFirstTwo extends origWorker {
    constructor() {
      super();
      if (instances.length <= 2) holdWorkers.add(this);
    }
  };
  const iso = new Pool();
  const r0 = iso.request('ping', null, []);
  const r1 = iso.request('ping', null, []);
  instances[0].onerror?.({ message: 'slot 0 crashed' });
  const slot0Result = await r0.then(() => 'resolved', () => 'rejected');
  for (const fn of heldReplies.splice(0)) fn();
  const slot1Result = await Promise.race([
    r1.then(() => 'resolved', () => 'rejected'),
    new Promise((resolve) => setTimeout(() => resolve('pending'), 300)),
  ]);
  iso.stop();
  await r1.catch(() => {});

  return { singleSummary, isolationSummary: { slot0Result, slot1Result } };
}

async function runPoolIdleColdSlot() {
  const corePath = path.resolve(manifest.repoDir, 'mobile', 'src', 'core');
  const source = fs
    .readFileSync(path.join(corePath, 'pool.ts'), 'utf8')
    .replace("'./protocol'", JSON.stringify(pathToFileURL(path.join(corePath, 'protocol.ts')).href))
    .replaceAll('import.meta.url', JSON.stringify(pathToFileURL(path.join(corePath, 'pool.ts')).href));
  const { Pool } = await import(`data:text/javascript;base64,${Buffer.from(stripTypeScriptTypes(source)).toString('base64')}`);

  Object.defineProperty(navigator, 'hardwareConcurrency', { value: 8, configurable: true });
  const log = [];
  const workers = [];
  globalThis.Worker = class ControlledWorker {
    constructor() {
      this.index = workers.length;
      this.held = [];
      workers.push(this);
    }
    postMessage(message) {
      if (!message.op) return;
      log.push({ slot: this.index, op: message.op, module: Boolean(message.wasmModule) });
      if (message.op === 'ping') queueMicrotask(() => this.reply(message));
      else this.held.push(message);
    }
    reply(message) {
      this.onmessage?.({ data: { id: message.id, ok: true, json: '{}', buffers: [] } });
    }
    release() {
      for (const message of this.held.splice(0)) this.reply(message);
    }
    terminate() {}
  };
  let wasmFetches = 0;
  globalThis.fetch = async () => {
    wasmFetches += 1;
    return new Response(Uint8Array.from([0, 97, 115, 109, 1, 0, 0, 0]), {
      headers: { 'content-type': 'application/wasm' },
    });
  };

  const pool = new Pool();
  const req = (op) => pool.request(op, null, []);
  const warm = [req('bake'), req('bake'), req('bake')];
  while (log.length < 3) await new Promise((r) => setTimeout(r, 5));
  for (const w of workers) w.release();
  await Promise.all(warm);
  await req('ping');
  const pingSlot = log.at(-1).slot;

  const background = [req('bake'), req('bake'), req('bake')];
  const resolved = req('resolve');
  while (log.length < 8) await new Promise((r) => setTimeout(r, 5));
  const dispatch = log.slice(-4);
  const slot3Held = workers[3]?.held.length ?? 0;
  for (const w of workers) w.release();
  await Promise.all([...background, resolved]);
  pool.stop();

  return { wasmFetches, pingSlot, dispatch, slot3Held };
}

async function runImportBeforePrewarm() {
  const { launchPinnedChromium } = await loadRuntime();
  const assets = fs.readdirSync(path.join(manifest.distDir, 'assets'));
  const editorChunk = assets.find((f) => /^editor_chunk-.*\.js$/.test(f)) || null;
  const server = await startStaticServer(manifest.distDir);
  const browser = await launchPinnedChromium({ args: CHROMIUM_ARGS });
  try {
    const ctx = await browser.newContext({ viewport: { width: 390, height: 844 }, hasTouch: true, isMobile: true });
    const pageErrors = [];
    const page = await ctx.newPage();
    page.on('pageerror', (e) => pageErrors.push(e.message));
    await page.addInitScript(() => {
      window.__FIRST_THUMB_TRACE__ = { drawnAt: null };
      const check = () => {
        if (window.__FIRST_THUMB_TRACE__.drawnAt !== null) return;
        const c = document.querySelector('.cell canvas[data-drawn]');
        if (c) window.__FIRST_THUMB_TRACE__.drawnAt = performance.now();
      };
      new MutationObserver(check).observe(document, { childList: true, subtree: true, attributes: true });
    });
    await page.goto(`${server.url}/`, { waitUntil: 'domcontentloaded', timeout: 20000 });
    await page.locator('.app[data-screen="start"] .start button.btn.primary').waitFor({ state: 'visible', timeout: 20000 });
    await page.waitForTimeout(300);
    await page.locator('input.filepick:not(.calibpick)').first().setInputFiles(manifest.photoFiles);
    await page.waitForFunction(() => window.__FIRST_THUMB_TRACE__?.drawnAt !== null, null, { timeout: 20000 });
    const drawnAt = await page.evaluate(() => window.__FIRST_THUMB_TRACE__.drawnAt);

    // 等首图绘制后的后台预热把编辑器分包拉完，再用 startTime 对照 drawnAt，不漏掉已发起未结束的请求。
    await page.waitForFunction(
      (editor) => performance.getEntriesByType('resource').some((e) => new URL(e.name).pathname.endsWith('/' + editor)),
      editorChunk,
      { timeout: 20000 },
    );

    const order = await page.evaluate(
      ([editor, tDraw]) => {
        const entries = performance.getEntriesByType('resource');
        const first = entries.find((e) => new URL(e.name).pathname.endsWith('/' + editor));
        return {
          drawnAt: tDraw,
          editor: first ? editor : null,
          editorBegunBeforeDraw: Boolean(first && first.startTime < tDraw),
          editorCompletedBeforeDraw: Boolean(first && first.responseEnd < tDraw),
          editorRequests: entries.filter((e) => new URL(e.name).pathname.endsWith('/' + editor)).length,
        };
      },
      [editorChunk, drawnAt],
    );

    // 进入编辑器并从顶栏「…」打开「导出选项」，验证动态导入链路正常。
    await page.locator('.cell').first().click();
    await page.locator('.editor .stage').waitFor({ state: 'visible', timeout: 20000 });
    await page.locator('[data-act="editor-more"]').click();
    await page.locator('.ms[data-state="editor-more"] [data-act="more-export-options"]').click();
    const exportOptionsOpened = await page
      .locator('.ms.export[data-state="export-sheet"]')
      .first()
      .waitFor({ state: 'visible', timeout: 10000 })
      .then(() => true, () => false);
    // 打开编辑器和导出选项都不再请求任何分包（导出选项和编辑器在同一个分包里）。
    const lazyRequests = await page.evaluate(() =>
      performance
        .getEntriesByType('resource')
        .map((e) => new URL(e.name).pathname)
        .filter((p) => /\/assets\/[^/]+\.js$/.test(p) && !/\/assets\/(main|worker|thumb|export_worker|off_image_worker|lut_table)-/.test(p)),
    );

    await ctx.close();
    return { order, exportOptionsOpened, lazyRequests, pageErrors };
  } finally {
    await browser.close();
    await server.close();
  }
}

let output;
if (mode === 'wasm-once') output = await runWasmOnce();
else if (mode === 'start-before-editor') output = await runStartBeforeEditor();
else if (mode === 'worker-wasm-retry') output = await runWorkerWasmRetry();
else if (mode === 'pool-slot-recovery') output = await runPoolSlotRecovery();
else if (mode === 'pool-idle-cold-slot') output = await runPoolIdleColdSlot();
else if (mode === 'import-before-prewarm') output = await runImportBeforePrewarm();
else throw new Error(`unknown mode: ${mode}`);

fs.writeFileSync(manifest.out, JSON.stringify(output, null, 2));
process.stdout.write('done\n');
