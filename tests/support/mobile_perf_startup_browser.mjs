// 启动性能与 Service Worker 浏览器回归驱动（perf-startup）。
// 模式：wasm-once / start-before-editor / service-worker。
// 所有服务只绑 127.0.0.1 临时端口（避开禁用端口），结束后确保关闭浏览器和 HTTP 服务。

import fs from 'node:fs';
import http from 'node:http';
import path from 'node:path';
import { pathToFileURL } from 'node:url';

const [runtimePath, mode, manifestPath] = process.argv.slice(2);
const manifest = JSON.parse(fs.readFileSync(manifestPath, 'utf8'));
const { pinnedRuntime, verifyPinnedRuntime, launchPinnedChromium } = await import(pathToFileURL(runtimePath).href);
verifyPinnedRuntime();

const FORBIDDEN_PORTS = new Set([60887, 18772, 18801, 18802, 18803, 5290]);
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

async function loadWebKit() {
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
  let rootDir = path.resolve(initialRoot);
  let blockCalib = false;
  let reqLog = [];
  const server = http.createServer((req, res) => {
    const rawUrl = req.url || '/';
    const cleanPath = decodeURIComponent(rawUrl.split('?')[0]);
    if (cleanPath === '/other/user-photo.jpg' || cleanPath === '/other/sample.dng') {
      const body = Buffer.from(cleanPath.endsWith('.jpg') ? '\xff\xd8\xff\xd9' : 'II*\0', 'latin1');
      res.setHeader('Content-Type', cleanPath.endsWith('.jpg') ? 'image/jpeg' : 'application/octet-stream');
      res.setHeader('Cache-Control', 'no-store');
      res.setHeader('Content-Length', String(body.byteLength));
      res.end(body);
      reqLog.push({ path: cleanPath, status: 200, bytes: body.byteLength, ts: performance.now() });
      return;
    }
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
          setRoot(nextRoot) { rootDir = path.resolve(nextRoot); },
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
  const server = await startStaticServer(manifest.distDir);
  const results = {};
  try {
    const engines = [
      {
        name: 'chromium',
        launch: () =>
          launchPinnedChromium({
            args: ['--enable-unsafe-webgpu', '--enable-unsafe-swiftshader', '--use-angle=swiftshader', '--use-webgpu-adapter=swiftshader'],
          }),
      },
    ];
    const wk = await loadWebKit();
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
  const server = await startStaticServer(manifest.distDir);
  const assetsDir = path.join(manifest.distDir, 'assets');
  const distAssets = fs.readdirSync(assetsDir);
  const editorChunkFiles = distAssets.filter((f) => /^editor[-.].*\.js$/.test(f));
  const bootChunkFiles = distAssets.filter((f) => /^boot[-.].*\.js$/.test(f));
  const bootBytes = bootChunkFiles.reduce((sum, f) => sum + fs.statSync(path.join(assetsDir, f)).size, 0);

  const browser = await launchPinnedChromium({
    args: ['--enable-unsafe-webgpu', '--enable-unsafe-swiftshader', '--use-angle=swiftshader', '--use-webgpu-adapter=swiftshader'],
  });
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
    const reqsAtInteractive = server.snapshotLog().map((r) => r.path);

    // Now pick a synthetic photo and open it in the editor to verify on-demand loading works.
    await page.locator('input.filepick:not(.calibpick)').first().setInputFiles(manifest.photoFiles);
    await page.waitForSelector('.cell canvas[data-drawn]', { timeout: 30000 });
    await page.locator('.cell').first().click();
    await page.waitForSelector('.editor .stage', { state: 'visible', timeout: 30000 });
    await page.waitForFunction(() => document.querySelector('.app')?.dataset.frame === '1' || (document.querySelector('.app')?.dataset.settled && document.querySelector('.app').dataset.settled !== '0'), null, { timeout: 60000 });

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
      bootChunkFiles,
      bootBytes,
      trace,
      reqsAtInteractive,
      afterOpen,
      reqsAfterOpen,
      pageErrors,
    };
  } finally {
    await browser.close();
    await server.close();
  }
}

async function runServiceWorker() {
  const server = await startStaticServer(manifest.distV1);
  const browser = await launchPinnedChromium({
    args: ['--enable-unsafe-webgpu', '--enable-unsafe-swiftshader', '--use-angle=swiftshader', '--use-webgpu-adapter=swiftshader'],
  });
  try {
    // 1. Disabled by default in automated tests (no ?sw=1)
    const ctxDefault = await browser.newContext({ viewport: { width: 390, height: 844 }, hasTouch: true, isMobile: true, deviceScaleFactor: 2 });
    const pageDefault = await ctxDefault.newPage();
    await pageDefault.goto(`${server.url}/`, { waitUntil: 'domcontentloaded' });
    await pageDefault.waitForSelector('.app[data-screen="start"] .start button.btn.primary', { state: 'visible', timeout: 30000 });
    await pageDefault.waitForTimeout(800);
    const defaultRegs = await pageDefault.evaluate(async () => (await navigator.serviceWorker.getRegistrations()).length);
    await ctxDefault.close();

    // 2. Enabled explicitly via ?sw=1
    const ctx = await browser.newContext({ viewport: { width: 390, height: 844 }, hasTouch: true, isMobile: true, deviceScaleFactor: 2 });
    const pageErrors = [];
    const dialogs = [];

    const p1 = await ctx.newPage();
    p1.on('pageerror', (e) => pageErrors.push(`p1:${e.message}`));
    p1.on('dialog', async (d) => { dialogs.push(d.message()); await d.dismiss(); });
    server.resetLog();
    await p1.goto(`${server.url}/?sw=1`, { waitUntil: 'domcontentloaded' });
    await p1.waitForSelector('.app[data-screen="start"] .start button.btn.primary', { state: 'visible', timeout: 30000 });
    const swRegistered = await p1.evaluate(async () => {
      if (!('serviceWorker' in navigator)) return false;
      const deadline = Date.now() + 10000;
      while (Date.now() < deadline) {
        const regs = await navigator.serviceWorker.getRegistrations();
        if (regs.length > 0 && regs[0].active) break;
        await new Promise((r) => setTimeout(r, 100));
      }
      const regs = await navigator.serviceWorker.getRegistrations();
      if (!regs.length) return false;
      if (!navigator.serviceWorker.controller) {
        await new Promise((resolve) => {
          const t = setTimeout(resolve, 4000);
          navigator.serviceWorker.addEventListener('controllerchange', () => { clearTimeout(t); resolve(); }, { once: true });
        });
      }
      return Boolean(regs[0].active);
    });
    await p1.waitForTimeout(1500);
    const load1Paths = server.snapshotLog().map((r) => r.path);
    await p1.close();

    // Second load: assets must come from SW cache
    server.resetLog();
    const p2 = await ctx.newPage();
    p2.on('pageerror', (e) => pageErrors.push(`p2:${e.message}`));
    p2.on('dialog', async (d) => { dialogs.push(d.message()); await d.dismiss(); });
    await p2.goto(`${server.url}/?sw=1`, { waitUntil: 'domcontentloaded' });
    await p2.waitForSelector('.app[data-screen="start"] .start button.btn.primary', { state: 'visible', timeout: 30000 });
    await p2.waitForTimeout(1500);
    const load2Log = server.snapshotLog();
    const load2Controlled = await p2.evaluate(() => Boolean(navigator.serviceWorker.controller));

    // User files + blob/data + non-allowlisted same-origin fetches must NEVER be cached
    await p2.locator('input.filepick:not(.calibpick)').first().setInputFiles(manifest.userFiles);
    await p2.waitForSelector('.cell canvas[data-drawn]', { timeout: 30000 });
    await p2.locator('input.lutpick').first().setInputFiles([manifest.lutFile]);
    await p2.waitForTimeout(500);
    await p2.evaluate(async () => {
      const blobUrl = URL.createObjectURL(new Blob(['user-photo-bytes'], { type: 'application/octet-stream' }));
      try {
        await fetch(blobUrl);
      } finally {
        URL.revokeObjectURL(blobUrl);
      }
      await fetch('data:text/plain;base64,dXNlcg==');
      await fetch('/other/user-photo.jpg');
      await fetch('/other/sample.dng');
    });
    const cacheAuditV1 = await p2.evaluate(async () => {
      const names = await caches.keys();
      const entries = {};
      for (const name of names) {
        const cache = await caches.open(name);
        const reqs = await cache.keys();
        entries[name] = reqs.map((r) => r.url).sort();
      }
      return { names: names.sort(), entries };
    });
    await p2.close();

    // Offline load: cached index.html + assets still open start screen
    await ctx.setOffline(true);
    const pOff = await ctx.newPage();
    pOff.on('pageerror', (e) => pageErrors.push(`pOff:${e.message}`));
    let offlineStartVisible = false;
    try {
      await pOff.goto(`${server.url}/?sw=1`, { waitUntil: 'domcontentloaded', timeout: 15000 });
      await pOff.waitForSelector('.app[data-screen="start"] .start button.btn.primary', { state: 'visible', timeout: 15000 });
      offlineStartVisible = true;
    } catch {
      offlineStartVisible = false;
    }
    await pOff.close();
    await ctx.setOffline(false);

    // New build version (V2): next load uses V2 code with no prompt, and V2 SW replaces V1 cache
    server.setRoot(manifest.distV2);
    server.resetLog();
    const p3 = await ctx.newPage();
    p3.on('pageerror', (e) => pageErrors.push(`p3:${e.message}`));
    p3.on('dialog', async (d) => { dialogs.push(d.message()); await d.dismiss(); });
    await p3.goto(`${server.url}/?sw=1`, { waitUntil: 'domcontentloaded' });
    await p3.waitForSelector('.app[data-screen="start"] .start button.btn.primary', { state: 'visible', timeout: 30000 });
    const v2Marker = await p3.evaluate(() => document.documentElement.dataset.swTestBuild || null);
    await p3.evaluate(async (oldNames) => {
      const reg = await navigator.serviceWorker.getRegistration();
      if (reg) await reg.update().catch(() => undefined);
      const deadline = Date.now() + 15000;
      while (Date.now() < deadline) {
        const now = await caches.keys();
        if (now.length > 0 && oldNames.every((n) => !now.includes(n))) break;
        await new Promise((r) => setTimeout(r, 150));
      }
    }, cacheAuditV1.names);
    const cacheAuditV2 = await p3.evaluate(async () => {
      const names = await caches.keys();
      const entries = {};
      for (const name of names) {
        const cache = await caches.open(name);
        const reqs = await cache.keys();
        entries[name] = reqs.map((r) => r.url).sort();
      }
      return { names: names.sort(), entries };
    });
    await p3.close();
    await ctx.close();

    return {
      defaultRegs,
      swRegistered,
      load1Paths,
      load2Controlled,
      load2Paths: load2Log.map((r) => r.path),
      offlineStartVisible,
      cacheAuditV1,
      v2Marker,
      cacheAuditV2,
      dialogs,
      pageErrors,
    };
  } finally {
    await browser.close();
    await server.close();
  }
}

let output;
if (mode === 'wasm-once') output = await runWasmOnce();
else if (mode === 'start-before-editor') output = await runStartBeforeEditor();
else if (mode === 'service-worker') output = await runServiceWorker();
else throw new Error(`unknown mode: ${mode}`);

fs.writeFileSync(manifest.out, JSON.stringify(output, null, 2));
process.stdout.write('done\n');
