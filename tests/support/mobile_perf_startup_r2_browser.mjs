// Round 2 回归驱动：覆盖 Sol r1 的 5 项 P1 与 1 项 P2（pool 槽位恢复）。
// 模式：offline-full-flow / open-old-tab / calib-update / index-redirect / worker-wasm-retry / pool-slot-recovery。

import crypto from 'node:crypto';
import fs from 'node:fs';
import http from 'node:http';
import { stripTypeScriptTypes } from 'node:module';
import path from 'node:path';
import { pathToFileURL } from 'node:url';

const [runtimePath, mode, manifestPath] = process.argv.slice(2);
const manifest = JSON.parse(fs.readFileSync(manifestPath, 'utf8'));
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
const CHROMIUM_ARGS = [
  '--enable-unsafe-webgpu',
  '--enable-unsafe-swiftshader',
  '--use-angle=swiftshader',
  '--use-webgpu-adapter=swiftshader',
];

function startServer(initialRoot) {
  let rootDir = path.resolve(initialRoot);
  let disconnected = false;
  let delaySw = false;
  let redirectIndex = false;
  let failPathSuffix = '';
  let calibCacheHeader = 'public, max-age=3600';
  let heldSw = [];
  let reqLog = [];

  const server = http.createServer(async (req, res) => {
    const reqUrl = new URL(req.url || '/', 'http://127.0.0.1');
    const cleanPath = decodeURIComponent(reqUrl.pathname);
    if (disconnected) {
      reqLog.push({ path: cleanPath, query: reqUrl.search, status: 'offline' });
      res.destroy();
      return;
    }
    if (delaySw && cleanPath === '/sw.js') {
      await new Promise((resolve) => heldSw.push(resolve));
      if (res.destroyed) return;
    }
    if (failPathSuffix && cleanPath.endsWith(failPathSuffix)) {
      res.writeHead(500, { 'Cache-Control': 'no-store' });
      res.end('injected precache failure');
      reqLog.push({ path: cleanPath, query: reqUrl.search, status: 500 });
      return;
    }
    if (redirectIndex && cleanPath === '/index.html') {
      res.writeHead(302, { Location: '/', 'Cache-Control': 'no-store' });
      res.end();
      reqLog.push({ path: cleanPath, query: reqUrl.search, status: 302 });
      return;
    }
    const rel = cleanPath === '/' ? 'index.html' : cleanPath.replace(/^\/+/, '');
    const full = path.resolve(rootDir, rel);
    if (!full.startsWith(rootDir + path.sep) && full !== rootDir) {
      res.writeHead(403, { 'Cache-Control': 'no-store' });
      res.end('forbidden');
      return;
    }
    if (!fs.existsSync(full) || !fs.statSync(full).isFile()) {
      res.writeHead(404, { 'Cache-Control': 'no-store' });
      res.end('not found');
      reqLog.push({ path: cleanPath, query: reqUrl.search, status: 404 });
      return;
    }
    const buf = fs.readFileSync(full);
    const ext = path.extname(full).toLowerCase();
    const cc = cleanPath.startsWith('/assets/')
      ? 'public, max-age=31536000, immutable'
      : cleanPath.startsWith('/calib/')
        ? calibCacheHeader
        : 'no-cache';
    res.writeHead(200, {
      'Content-Type': MIME[ext] || 'application/octet-stream',
      'Content-Length': String(buf.byteLength),
      'Cache-Control': cc,
    });
    res.end(buf);
    reqLog.push({ path: cleanPath, query: reqUrl.search, status: 200 });
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
          setRoot(next) { rootDir = path.resolve(next); },
          setDisconnected(on) { disconnected = Boolean(on); },
          setDelaySw(on) {
            delaySw = Boolean(on);
            if (!delaySw) for (const fn of heldSw.splice(0)) fn();
          },
          setRedirectIndex(on) { redirectIndex = Boolean(on); },
          setFailPathSuffix(suffix) { failPathSuffix = suffix; },
          resetLog() { reqLog = []; },
          snapshotLog() { return reqLog.slice(); },
          close() {
            delaySw = false;
            for (const fn of heldSw.splice(0)) fn();
            return new Promise((r) => server.close(r));
          },
        });
      });
    };
    server.on('error', reject);
    tryListen();
  });
}

function readCacheName(distDir) {
  const sw = fs.readFileSync(path.join(distDir, 'sw.js'), 'utf8');
  const m = sw.match(/const CACHE_NAME = '([^']+)'/);
  if (!m) throw new Error(`missing CACHE_NAME in ${distDir}/sw.js`);
  return m[1];
}

function sha256File(filePath) {
  return crypto.createHash('sha256').update(fs.readFileSync(filePath)).digest('hex');
}

async function waitControlled(page, expectedCache) {
  await page.waitForFunction(
    async (cacheName) => {
      if (!('serviceWorker' in navigator) || !navigator.serviceWorker.controller) return false;
      const names = await caches.keys();
      return names.includes(cacheName);
    },
    expectedCache,
    { timeout: 15000 },
  );
}

async function openStart(ctx, url, query = '?sw=1') {
  const page = await ctx.newPage();
  await page.goto(`${url}/${query}`, { waitUntil: 'domcontentloaded', timeout: 20000 });
  await page.locator('.app[data-screen="start"] .start button.btn.primary').waitFor({ state: 'visible', timeout: 20000 });
  return page;
}

async function fetchShaFromPage(page, resource) {
  return page.evaluate(async (resUrl) => {
    const r = await fetch(resUrl);
    if (!r.ok) return `HTTP_${r.status}`;
    const digest = await crypto.subtle.digest('SHA-256', await r.arrayBuffer());
    return Array.from(new Uint8Array(digest), (b) => b.toString(16).padStart(2, '0')).join('');
  }, resource);
}

async function launchBrowser() {
  const { verifyPinnedRuntime, launchPinnedChromium } = await import(pathToFileURL(runtimePath).href);
  verifyPinnedRuntime();
  return launchPinnedChromium({ args: CHROMIUM_ARGS });
}

async function runOfflineFullFlow() {
  const cacheName = readCacheName(manifest.distDir);
  const assets = fs.readdirSync(path.join(manifest.distDir, 'assets'));
  const gridFile = assets.find((f) => /^grid-.*\.js$/.test(f));
  const server = await startServer(manifest.distDir);
  const browser = await launchBrowser();
  try {
    // Part A: broken precache asset must abort install and NOT activate SW
    server.setFailPathSuffix(gridFile || 'never-match');
    const ctxFail = await browser.newContext({ viewport: { width: 390, height: 844 }, hasTouch: true, isMobile: true });
    const pFail = await openStart(ctxFail, server.url, '?sw=1');
    await pFail.waitForTimeout(1500);
    const failedInstallState = await pFail.evaluate(async () => {
      const regs = await navigator.serviceWorker.getRegistrations();
      const names = await caches.keys();
      return {
        hasActive: regs.some((r) => Boolean(r.active)),
        controlled: Boolean(navigator.serviceWorker.controller),
        cacheNames: names,
      };
    });
    await ctxFail.close();
    server.setFailPathSuffix('');

    // Part B: visit start screen once online -> disconnect -> full offline import -> edit -> export
    const ctx = await browser.newContext({ viewport: { width: 390, height: 844 }, hasTouch: true, isMobile: true });
    const pageErrors = [];
    const pOnline = await openStart(ctx, server.url, '?sw=1');
    pOnline.on('pageerror', (e) => pageErrors.push(`online:${e.message}`));
    await waitControlled(pOnline, cacheName);
    const precachedEntries = await pOnline.evaluate(async (name) => {
      const c = await caches.open(name);
      return (await c.keys()).map((r) => new URL(r.url).pathname + new URL(r.url).search).sort();
    }, cacheName);
    await pOnline.close();

    server.setDisconnected(true);
    server.resetLog();
    const pOff = await openStart(ctx, server.url, '?sw=1');
    pOff.on('pageerror', (e) => pageErrors.push(`offline:${e.message}`));

    await pOff.locator('input.filepick:not(.calibpick)').first().setInputFiles(manifest.photoFiles);
    const thumbDrawn = await pOff
      .locator('.cell canvas[data-drawn]')
      .first()
      .waitFor({ timeout: 12000 })
      .then(() => true, () => false);

    let stageReady = false;
    let exportReady = false;
    if (thumbDrawn) {
      await pOff.locator('.cell').first().click();
      await pOff.locator('.editor .stage').waitFor({ state: 'visible', timeout: 15000 });
      stageReady = await pOff
        .waitForFunction(
          () => {
            const app = document.querySelector('.app');
            return app?.dataset.frame === '1' || (app?.dataset.settled && app.dataset.settled !== '0');
          },
          null,
          { timeout: 30000 },
        )
        .then(() => true, () => false);
      if (stageReady) {
        await pOff.locator('.toolbar [data-act="export"]').click();
        exportReady = await pOff
          .locator('.pdock[data-visual-state="ready"]')
          .waitFor({ timeout: 60000 })
          .then(() => true, () => false);
      }
    }
    const offlineNetAttempts = server.snapshotLog();
    await ctx.close();
    return {
      failedInstallState,
      precachedEntries,
      thumbDrawn,
      stageReady,
      exportReady,
      offlineNetAttempts,
      pageErrors,
    };
  } finally {
    await browser.close();
    await server.close();
  }
}

async function runOpenOldTab() {
  const cacheA = readCacheName(manifest.distV1);
  const cacheB = readCacheName(manifest.distV2);
  const gridA = fs.readdirSync(path.join(manifest.distV1, 'assets')).find((f) => /^grid-.*\.js$/.test(f));
  const gridB = fs.readdirSync(path.join(manifest.distV2, 'assets')).find((f) => /^grid-.*\.js$/.test(f));
  const server = await startServer(manifest.distV1);
  const browser = await launchBrowser();
  try {
    const ctx = await browser.newContext({ viewport: { width: 390, height: 844 }, hasTouch: true, isMobile: true });
    const pageErrors = [];
    const oldPage = await openStart(ctx, server.url, '?sw=1');
    oldPage.on('pageerror', (e) => pageErrors.push(`old:${e.message}`));
    await waitControlled(oldPage, cacheA);

    // Deploy V2 (server now ONLY has V2 chunks; gridA returns 404 on server)
    server.setRoot(manifest.distV2);
    const newPage = await openStart(ctx, server.url, '?sw=1');
    newPage.on('pageerror', (e) => pageErrors.push(`new:${e.message}`));
    const newDocBuild = await newPage.locator('html').getAttribute('data-sw-test-build');
    await newPage.evaluate(async () => {
      const reg = await navigator.serviceWorker.getRegistration();
      if (reg) await reg.update().catch(() => undefined);
    });
    await waitControlled(newPage, cacheB);
    const cachesWhileOldOpen = await newPage.evaluate(async () => (await caches.keys()).sort());

    // Keep oldPage open and import photos in oldPage (still V1 HTML referencing gridA)
    server.resetLog();
    await oldPage.locator('input.filepick:not(.calibpick)').first().setInputFiles(manifest.photoFiles);
    const oldTabThumb = await oldPage
      .locator('.cell canvas[data-drawn]')
      .first()
      .waitFor({ timeout: 12000 })
      .then(() => true, () => false);
    let oldTabEditor = false;
    if (oldTabThumb) {
      await oldPage.locator('.cell').first().click();
      oldTabEditor = await oldPage
        .locator('.editor .stage')
        .waitFor({ state: 'visible', timeout: 15000 })
        .then(() => true, () => false);
    }
    const oldDocBuild = await oldPage.locator('html').getAttribute('data-sw-test-build');
    const oldGrid404 = server.snapshotLog().some((r) => r.path.endsWith(`/${gridA}`) && r.status === 404);

    // Close V1 tab, reload V2 tab -> V1 cache should be pruned once no V1 client remains
    await oldPage.close();
    await newPage.reload({ waitUntil: 'domcontentloaded' });
    await newPage.locator('.app[data-screen="start"] .start button.btn.primary').waitFor({ state: 'visible', timeout: 15000 });
    await newPage
      .waitForFunction(
        async (oldName) => !(await caches.keys()).includes(oldName),
        cacheA,
        { timeout: 10000 },
      )
      .catch(() => undefined);
    const cachesAfterOldClosed = await newPage.evaluate(async () => (await caches.keys()).sort());

    await ctx.close();
    return {
      cacheA,
      cacheB,
      gridA,
      gridB,
      newDocBuild,
      oldDocBuild,
      cachesWhileOldOpen,
      oldTabThumb,
      oldTabEditor,
      oldGrid404,
      cachesAfterOldClosed,
      pageErrors,
    };
  } finally {
    await browser.close();
    await server.close();
  }
}

async function runCalibUpdate() {
  const cacheA = readCacheName(manifest.distV1);
  const cacheB = readCacheName(manifest.distV2);
  const hashA = sha256File(path.join(manifest.distV1, 'calib', 'gxr-default-calib.zip'));
  const hashB = sha256File(path.join(manifest.distV2, 'calib', 'gxr-default-calib.zip'));
  const calibA = `/calib/gxr-default-calib.zip?v=${hashA.slice(0, 12)}`;
  const calibB = `/calib/gxr-default-calib.zip?v=${hashB.slice(0, 12)}`;

  const server = await startServer(manifest.distV1);
  const browser = await launchBrowser();
  try {
    const ctx = await browser.newContext({ viewport: { width: 390, height: 844 }, hasTouch: true, isMobile: true });
    const pageErrors = [];
    const p1 = await openStart(ctx, server.url, '?sw=1');
    p1.on('pageerror', (e) => pageErrors.push(`p1:${e.message}`));
    await waitControlled(p1, cacheA);
    const v1Sha = await fetchShaFromPage(p1, calibA);

    // Deploy V2 with /sw.js delayed so p2 loads BEFORE SW switches to V2
    server.setDelaySw(true);
    server.setRoot(manifest.distV2);
    const p2 = await openStart(ctx, server.url, '?sw=1');
    p2.on('pageerror', (e) => pageErrors.push(`p2:${e.message}`));
    const shaBeforeSwSwitch = await fetchShaFromPage(p2, calibB);

    // Trigger real app UI calib check in p2 before SW switches
    await p2.locator('input.filepick:not(.calibpick)').first().setInputFiles(manifest.photoFiles);
    await p2.locator('.cell canvas[data-drawn]').first().waitFor({ timeout: 15000 });
    await p2.getByRole('button', { name: '更多', exact: true }).click({ timeout: 8000 });
    await p2.locator('.ms .lrow', { hasText: '校准数据' }).click({ timeout: 8000 });
    await p2.waitForTimeout(1500);
    const calibFailedBefore = await p2.locator('.calib-sheet [data-state="default-failed"]').count();

    // Release /sw.js, wait for V2 SW active, verify V2 tab STILL receives V2 ZIP
    server.setDelaySw(false);
    await p2.evaluate(async () => {
      const reg = await navigator.serviceWorker.getRegistration();
      if (reg) await reg.update().catch(() => undefined);
    });
    await waitControlled(p2, cacheB);
    const shaAfterSwSwitch = await fetchShaFromPage(p2, calibB);

    await p1.close();
    await p2.close();
    await ctx.close();
    return {
      hashA,
      hashB,
      v1Sha,
      shaBeforeSwSwitch,
      shaAfterSwSwitch,
      calibFailedBefore,
      pageErrors,
    };
  } finally {
    await browser.close();
    await server.close();
  }
}

async function runIndexRedirect() {
  const cacheA = readCacheName(manifest.distDir);
  const server = await startServer(manifest.distDir);
  server.setRedirectIndex(true);
  const browser = await launchBrowser();
  try {
    const ctx = await browser.newContext({ viewport: { width: 390, height: 844 }, hasTouch: true, isMobile: true });
    const pageErrors = [];
    const p1 = await openStart(ctx, server.url, '?sw=1');
    p1.on('pageerror', (e) => pageErrors.push(`p1:${e.message}`));
    await waitControlled(p1, cacheA);

    const cacheInspect = await p1.evaluate(async (name) => {
      const c = await caches.open(name);
      const rootRes = await c.match(`${location.origin}/`);
      const idxRes = await c.match(`${location.origin}/index.html`);
      return {
        rootCached: Boolean(rootRes),
        rootRedirected: rootRes ? rootRes.redirected : null,
        indexCachedRedirected: idxRes ? idxRes.redirected : null,
      };
    }, cacheA);

    // Online navigation to /index.html -> should follow 302 -> / cleanly
    const pOnlineIdx = await ctx.newPage();
    pOnlineIdx.on('pageerror', (e) => pageErrors.push(`onlineIdx:${e.message}`));
    const onlineIndexResult = await pOnlineIdx
      .goto(`${server.url}/index.html`, { waitUntil: 'domcontentloaded', timeout: 15000 })
      .then(async () => {
        await pOnlineIdx.locator('.app[data-screen="start"] .start button.btn.primary').waitFor({ state: 'visible', timeout: 10000 });
        return { ok: true, pathname: new URL(pOnlineIdx.url()).pathname };
      }, (err) => ({ ok: false, error: err.message.split('\n')[0] }));
    await pOnlineIdx.close();

    // Offline navigation to /?sw=1 and /index.html -> both must render start screen
    server.setDisconnected(true);
    const pOffRoot = await ctx.newPage();
    const offlineRootResult = await pOffRoot
      .goto(`${server.url}/?sw=1`, { waitUntil: 'domcontentloaded', timeout: 15000 })
      .then(async () => {
        await pOffRoot.locator('.app[data-screen="start"] .start button.btn.primary').waitFor({ state: 'visible', timeout: 10000 });
        return { ok: true };
      }, (err) => ({ ok: false, error: err.message.split('\n')[0] }));
    await pOffRoot.close();

    const pOffIdx = await ctx.newPage();
    const offlineIndexResult = await pOffIdx
      .goto(`${server.url}/index.html`, { waitUntil: 'domcontentloaded', timeout: 15000 })
      .then(async () => {
        await pOffIdx.locator('.app[data-screen="start"] .start button.btn.primary').waitFor({ state: 'visible', timeout: 10000 });
        return { ok: true };
      }, (err) => ({ ok: false, error: err.message.split('\n')[0] }));
    await pOffIdx.close();

    await ctx.close();
    return { cacheInspect, onlineIndexResult, offlineRootResult, offlineIndexResult, pageErrors };
  } finally {
    await browser.close();
    await server.close();
  }
}

async function runWorkerWasmRetry() {
  const assets = fs.readdirSync(path.join(manifest.distDir, 'assets'));
  const workerPath = '/assets/' + assets.find((f) => /^worker-.*\.js$/.test(f));
  const wasmPath = '/assets/' + assets.find((f) => /\.wasm$/.test(f));
  const server = await startServer(manifest.distDir);
  const browser = await launchBrowser();
  try {
    const ctx = await browser.newContext();
    const page = await openStart(ctx, server.url, '?sw=0');
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

  // Case 1: single-slot pool permanent error -> slot dropped, next request spawns fresh worker and resolves
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

  // Case 2: multi-slot isolation -> failing slot 0 rejects only slot 0, leaving slot 1 intact
  instances.length = 0;
  heldReplies.length = 0;
  Object.defineProperty(navigator, 'hardwareConcurrency', { value: 5, configurable: true });
  const multi = new Pool();
  const m0 = multi.request('ping', null, []);
  const m1 = multi.request('ping', null, []);
  // Hold slot 1 before slot 0 fails by creating with hold enabled on fresh pool
  await Promise.all([m0, m1]);
  holdWorkers = new Set([instances[0], instances[1]]);
  const slow0 = multi.request('ping', null, []);
  const slow1 = multi.request('ping', null, []);
  // Rotate cursor back to 0..1: wait, slots used were 2 and 3. Use fresh multi pool with held FakeWorker.
  multi.stop();
  await Promise.allSettled([slow0, slow1]);

  instances.length = 0;
  heldReplies.length = 0;
  holdWorkers = new Set();
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

async function runImportBeforePrewarm() {
  const assets = fs.readdirSync(path.join(manifest.distDir, 'assets'));
  const editorChunk = assets.find((f) => /^editor-.*\.js$/.test(f));
  const exportChunk = assets.find((f) => /^export_ui-.*\.js$/.test(f));
  const server = await startServer(manifest.distDir);
  const browser = await launchBrowser();
  try {
    const ctx = await browser.newContext({ viewport: { width: 390, height: 844 }, hasTouch: true, isMobile: true });
    const pageErrors = [];
    const page = await ctx.newPage();
    page.on('pageerror', (e) => pageErrors.push(e.message));
    await page.addInitScript(() => {
      window.__FIRST_THUMB_TRACE__ = { drawnAt: null, resourcesAtFirstThumb: null };
      const check = () => {
        if (window.__FIRST_THUMB_TRACE__.drawnAt !== null) return;
        const c = document.querySelector('.cell canvas[data-drawn]');
        if (c) {
          window.__FIRST_THUMB_TRACE__.drawnAt = performance.now();
          window.__FIRST_THUMB_TRACE__.resourcesAtFirstThumb = performance
            .getEntriesByType('resource')
            .map((e) => new URL(e.name, location.href).pathname);
        }
      };
      new MutationObserver(check).observe(document, { childList: true, subtree: true, attributes: true });
    });
    await page.goto(`${server.url}/?sw=0`, { waitUntil: 'domcontentloaded', timeout: 20000 });
    await page.locator('.app[data-screen="start"] .start button.btn.primary').waitFor({ state: 'visible', timeout: 20000 });
    await page.waitForTimeout(300);
    server.resetLog();
    await page.locator('input.filepick:not(.calibpick)').first().setInputFiles(manifest.photoFiles);
    await page.waitForFunction(() => window.__FIRST_THUMB_TRACE__?.drawnAt !== null, null, { timeout: 20000 });
    const trace = await page.evaluate(() => window.__FIRST_THUMB_TRACE__);
    // 打开编辑器验证按需/延后预热正常工作
    await page.locator('.cell').first().click();
    await page.locator('.editor .stage').waitFor({ state: 'visible', timeout: 20000 });
    const resourcesAfterOpen = await page.evaluate(() =>
      performance.getEntriesByType('resource').map((e) => new URL(e.name, location.href).pathname),
    );
    await ctx.close();
    return {
      editorChunk,
      exportChunk,
      trace,
      resourcesAfterOpen,
      pageErrors,
    };
  } finally {
    await browser.close();
    await server.close();
  }
}

let output;
if (mode === 'offline-full-flow') output = await runOfflineFullFlow();
else if (mode === 'open-old-tab') output = await runOpenOldTab();
else if (mode === 'calib-update') output = await runCalibUpdate();
else if (mode === 'index-redirect') output = await runIndexRedirect();
else if (mode === 'worker-wasm-retry') output = await runWorkerWasmRetry();
else if (mode === 'pool-slot-recovery') output = await runPoolSlotRecovery();
else if (mode === 'import-before-prewarm') output = await runImportBeforePrewarm();
else throw new Error(`unknown mode: ${mode}`);

fs.writeFileSync(manifest.out, JSON.stringify(output, null, 2));
process.stdout.write('done\n');
