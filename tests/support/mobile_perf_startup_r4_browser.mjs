// Round 4 / Round 6 回归驱动：覆盖懒加载竞态、失败重试、模块/ExportFlow 单例复用与调度回归。
// 模式：menu-race-and-fail / raw-only-prewarm / pool-idle-cold-slot / grid-fail-recovery / export-retry-and-reuse / reopen-single-instance。

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

function startStaticServer(initialRoot) {
  const rootDir = path.resolve(initialRoot);
  let reqLog = [];
  const server = http.createServer((req, res) => {
    const rawUrl = req.url || '/';
    const cleanPath = decodeURIComponent(rawUrl.split('?')[0]);
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
      reqLog.push({ path: cleanPath, rawUrl, status: 404 });
      return;
    }
    const buf = fs.readFileSync(full);
    const ext = path.extname(full).toLowerCase();
    res.setHeader('Content-Type', MIME[ext] || 'application/octet-stream');
    res.setHeader('Cache-Control', cleanPath.startsWith('/assets/') ? 'public, max-age=31536000, immutable' : 'no-cache');
    res.setHeader('Content-Length', String(buf.byteLength));
    res.end(buf);
    reqLog.push({ path: cleanPath, rawUrl, status: 200 });
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

async function openEditorOnPhoto(page, url, photoFile) {
  await page.goto(`${url}/`, { waitUntil: 'domcontentloaded', timeout: 20000 });
  await page.locator('.app[data-screen="start"][data-core="1"] .start button.btn.primary').waitFor({ state: 'visible', timeout: 20000 });
  await page.locator('input.filepick:not(.calibpick)').first().setInputFiles([photoFile]);
  await page.locator('.cell:has(canvas[data-drawn])').first().click({ timeout: 20000 });
  await page.locator('.editor .stage').waitFor({ state: 'visible', timeout: 20000 });
}

async function runMenuRaceAndFail() {
  const { launchPinnedChromium } = await loadRuntime();
  const server = await startStaticServer(manifest.distDir);
  const browser = await launchPinnedChromium({ args: CHROMIUM_ARGS });
  try {
    // Part A: pause off_image_sheet chunk -> navigate back to grid -> release -> sheet MUST NOT pop up
    const ctxRace = await browser.newContext({ viewport: { width: 390, height: 844 }, hasTouch: true, isMobile: true });
    const pageRace = await ctxRace.newPage();
    const raceErrors = [];
    pageRace.on('pageerror', (e) => raceErrors.push(e.message));
    let releaseOff;
    let startedOff;
    const offStarted = new Promise((r) => { startedOff = r; });
    const offRelease = new Promise((r) => { releaseOff = r; });
    await pageRace.route(/\/assets\/off_image_sheet-.*\.js(?:\?|$)/, async (route) => {
      startedOff();
      await offRelease;
      await route.continue();
    });
    await openEditorOnPhoto(pageRace, server.url, manifest.jpgFile);
    await pageRace.locator('[data-act="editor-more"]').click();
    await pageRace.locator('.ms[data-state="editor-more"] [data-act="more-off-image"]').click();
    await offStarted;
    const moreVisibleWhileLoading = await pageRace.locator('.ms[data-state="editor-more"]').isVisible();
    // Close menu if still open, then navigate back to grid
    const doneBtn = pageRace.locator('.ms[data-state="editor-more"] .hd .nb');
    if (await doneBtn.isVisible().catch(() => false)) {
      await doneBtn.click();
    }
    await pageRace.locator('button.nav-back').click();
    await pageRace.locator('.app[data-screen="grid"] .cell').first().waitFor({ state: 'visible', timeout: 15000 });
    releaseOff();
    await pageRace.waitForTimeout(500);
    const raceResult = {
      moreVisibleWhileLoading,
      screenAfterRelease: await pageRace.locator('.app').getAttribute('data-screen'),
      offImagePoppedOnGrid: await pageRace.locator('[data-state="off-image"]').isVisible(),
      pageErrors: raceErrors,
    };
    await ctxRace.close();

    // Part B: abort off_image_sheet -> visible alert + retry -> unblock -> click retry -> sheet opens
    const ctxFail = await browser.newContext({ viewport: { width: 390, height: 844 }, hasTouch: true, isMobile: true });
    const pageFail = await ctxFail.newPage();
    const failErrors = [];
    pageFail.on('pageerror', (e) => failErrors.push(e.message));
    let blockOff = true;
    let offAttempts = 0;
    await pageFail.route(/\/assets\/off_image_sheet-.*\.js(?:\?|$)/, (route) => {
      offAttempts += 1;
      return blockOff ? route.abort() : route.continue();
    });
    await openEditorOnPhoto(pageFail, server.url, manifest.jpgFile);
    await pageFail.locator('[data-act="editor-more"]').click();
    await pageFail.locator('.ms[data-state="editor-more"] [data-act="more-off-image"]').click();
    await pageFail.waitForTimeout(500);
    const moreVisibleOnFail = await pageFail.locator('.ms[data-state="editor-more"]').isVisible();
    const alertVisibleOnFail = await pageFail.locator('.ms[data-state="editor-more"] [role="alert"]').isVisible();
    const retryBtn = pageFail.locator('.ms[data-state="editor-more"] [data-act="more-load-retry"]');
    const retryVisibleOnFail = await retryBtn.isVisible();
    const reloadVisibleOnFail = await pageFail.locator('.ms[data-state="editor-more"] [data-act="more-load-reload"]').isVisible();

    let recoveredAfterRetry = false;
    if (retryVisibleOnFail) {
      blockOff = false;
      await retryBtn.click();
      recoveredAfterRetry = await pageFail
        .locator('[data-state="off-image"]')
        .waitFor({ state: 'visible', timeout: 15000 })
        .then(() => true, () => false);
    }
    const failResult = {
      offAttempts,
      moreVisibleOnFail,
      alertVisibleOnFail,
      retryVisibleOnFail,
      reloadVisibleOnFail,
      recoveredAfterRetry,
      pageErrors: failErrors,
    };
    await ctxFail.close();

    return { raceResult, failResult };
  } finally {
    await browser.close();
    await server.close();
  }
}

async function runRawOnlyPrewarm() {
  const { launchPinnedChromium } = await loadRuntime();
  const server = await startStaticServer(manifest.distDir);
  const browser = await launchPinnedChromium({ args: CHROMIUM_ARGS });
  try {
    // Part A: import valid preview-less DNG -> wait 700ms -> editor/export_ui MUST NOT prewarm yet;
    // then wait for settled idle timeout -> background prewarm runs cleanly.
    const ctx = await browser.newContext({ viewport: { width: 390, height: 844 }, hasTouch: true, isMobile: true });
    const page = await ctx.newPage();
    const pageErrors = [];
    page.on('pageerror', (e) => pageErrors.push(e.message));
    await page.goto(`${server.url}/`, { waitUntil: 'domcontentloaded', timeout: 20000 });
    await page.locator('.app[data-screen="start"][data-core="1"] .start button.btn.primary').waitFor({ state: 'visible', timeout: 20000 });
    await page.locator('input.filepick:not(.calibpick)').first().setInputFiles([manifest.rawOnlyDng]);
    await page.getByText('只有 RAW', { exact: true }).waitFor({ state: 'visible', timeout: 20000 });
    await page.waitForTimeout(700);
    const early = await page.evaluate(() => ({
      rawOnly: document.querySelectorAll('.cell .raw').length,
      drawn: document.querySelectorAll('.cell canvas[data-drawn]').length,
      chunks: performance
        .getEntriesByType('resource')
        .map((e) => new URL(e.name, location.href).pathname)
        .filter((p) => /\/assets\/(editor|export_ui)-.*\.js$/.test(p)),
    }));
    // After settled idle timeout (~1500ms + idle), background prewarm should eventually run
    const settledPrewarmed = await page
      .waitForFunction(
        () =>
          ['editor', 'export_ui'].every((name) =>
            performance.getEntriesByType('resource').some((e) => new URL(e.name, location.href).pathname.startsWith(`/assets/${name}-`)),
          ),
        null,
        { timeout: 6000 },
      )
      .then(() => true, () => false);
    await ctx.close();

    // Part B: tapping the RAW-only cell immediately (before idle timeout) opens editor on demand
    const ctxOpen = await browser.newContext({ viewport: { width: 390, height: 844 }, hasTouch: true, isMobile: true });
    const pageOpen = await ctxOpen.newPage();
    pageOpen.on('pageerror', (e) => pageErrors.push(`open:${e.message}`));
    await pageOpen.goto(`${server.url}/`, { waitUntil: 'domcontentloaded', timeout: 20000 });
    await pageOpen.locator('.app[data-screen="start"][data-core="1"] .start button.btn.primary').waitFor({ state: 'visible', timeout: 20000 });
    await pageOpen.locator('input.filepick:not(.calibpick)').first().setInputFiles([manifest.rawOnlyDng]);
    await pageOpen.getByText('只有 RAW', { exact: true }).waitFor({ state: 'visible', timeout: 20000 });
    await pageOpen.locator('.cell').first().click();
    const onDemandEditorOpened = await pageOpen
      .locator('.editor .stage')
      .waitFor({ state: 'visible', timeout: 20000 })
      .then(() => true, () => false);
    await ctxOpen.close();

    return { early, settledPrewarmed, onDemandEditorOpened, pageErrors };
  } finally {
    await browser.close();
    await server.close();
  }
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

async function runGridFailRecovery() {
  const { launchPinnedChromium } = await loadRuntime();
  const server = await startStaticServer(manifest.distDir);
  const browser = await launchPinnedChromium({ args: CHROMIUM_ARGS });
  try {
    // Path A: abort grid -> stay on start + visible error -> unblock -> re-pick photo -> grid recovers
    const ctxRepick = await browser.newContext({ viewport: { width: 390, height: 844 }, hasTouch: true, isMobile: true });
    const pageA = await ctxRepick.newPage();
    const errorsA = [];
    pageA.on('pageerror', (e) => errorsA.push(e.message));
    let blockGridA = true;
    let gridAttemptsA = 0;
    await pageA.route(/\/assets\/grid-.*\.js(?:\?|$)/, (route) => {
      gridAttemptsA += 1;
      return blockGridA ? route.abort() : route.continue();
    });
    await pageA.goto(`${server.url}/`, { waitUntil: 'domcontentloaded', timeout: 20000 });
    await pageA.locator('.app[data-screen="start"][data-core="1"] .start button.btn.primary').waitFor({ state: 'visible', timeout: 20000 });
    await pageA.locator('input.filepick:not(.calibpick)').first().setInputFiles([manifest.jpgFile]);
    await pageA.waitForTimeout(500);
    const afterFailA = {
      screen: await pageA.locator('.app').getAttribute('data-screen'),
      startVisible: await pageA.locator('.start').isVisible(),
      alertVisible: await pageA.locator('[role="alert"]').isVisible(),
      retryVisible: await pageA.locator('[data-act="retry-grid"]').isVisible(),
      reloadVisible: await pageA.locator('[data-act="reload-app"]').isVisible(),
    };
    blockGridA = false;
    await pageA.locator('input.filepick:not(.calibpick)').first().setInputFiles([manifest.jpgFile]);
    const recoveredRepick = await pageA
      .locator('.app[data-screen="grid"] .cell canvas[data-drawn]')
      .first()
      .waitFor({ state: 'visible', timeout: 15000 })
      .then(() => true, () => false);
    const repickResult = { afterFailA, gridAttemptsA, recoveredRepick, pageErrors: errorsA };
    await ctxRepick.close();

    // Path B: abort grid -> unblock -> click [data-act="retry-grid"] -> grid recovers without re-picking
    const ctxRetry = await browser.newContext({ viewport: { width: 390, height: 844 }, hasTouch: true, isMobile: true });
    const pageB = await ctxRetry.newPage();
    const errorsB = [];
    pageB.on('pageerror', (e) => errorsB.push(e.message));
    let blockGridB = true;
    let gridAttemptsB = 0;
    await pageB.route(/\/assets\/grid-.*\.js(?:\?|$)/, (route) => {
      gridAttemptsB += 1;
      return blockGridB ? route.abort() : route.continue();
    });
    await pageB.goto(`${server.url}/`, { waitUntil: 'domcontentloaded', timeout: 20000 });
    await pageB.locator('.app[data-screen="start"][data-core="1"] .start button.btn.primary').waitFor({ state: 'visible', timeout: 20000 });
    await pageB.locator('input.filepick:not(.calibpick)').first().setInputFiles([manifest.jpgFile]);
    await pageB.waitForTimeout(500);
    const retryBtnB = pageB.locator('[data-act="retry-grid"]');
    const hasRetryB = await retryBtnB.isVisible();
    let recoveredRetryBtn = false;
    if (hasRetryB) {
      blockGridB = false;
      await retryBtnB.click();
      recoveredRetryBtn = await pageB
        .locator('.app[data-screen="grid"] .cell canvas[data-drawn]')
        .first()
        .waitFor({ state: 'visible', timeout: 15000 })
        .then(() => true, () => false);
    }
    const retryBtnResult = { hasRetryB, gridAttemptsB, recoveredRetryBtn, pageErrors: errorsB };
    await ctxRetry.close();

    return { repickResult, retryBtnResult };
  } finally {
    await browser.close();
    await server.close();
  }
}

async function runExportRetryAndReuse() {
  const { launchPinnedChromium } = await loadRuntime();
  const server = await startStaticServer(manifest.distDir);
  const browser = await launchPinnedChromium({ args: CHROMIUM_ARGS });
  const exportJsRe = /\/assets\/export_ui-[^/?]+\.js(?:\?|$)/;
  const exportCssRe = /\/assets\/export_ui-[^/?]+\.css(?:\?|$)/;
  try {
    // Part A: 仅拦截首次 export_ui-*.js 请求；编辑器不得卡死，点「导出选项」自动/重试恢复，多次重开复用同一模块与同一 ExportFlow
    const ctxJs = await browser.newContext({ viewport: { width: 390, height: 844 }, hasTouch: true, isMobile: true });
    const pageJs = await ctxJs.newPage();
    const jsErrors = [];
    pageJs.on('pageerror', (e) => jsErrors.push(e.message));
    const jsRequests = [];
    await pageJs.route(exportJsRe, (route) => {
      const search = new URL(route.request().url()).search;
      jsRequests.push(search);
      return jsRequests.length === 1 ? route.abort() : route.continue();
    });
    await pageJs.goto(`${server.url}/`, { waitUntil: 'domcontentloaded', timeout: 20000 });
    await pageJs.locator('.app[data-screen="start"][data-core="1"] .start button.btn.primary').waitFor({ state: 'visible', timeout: 20000 });
    await pageJs.locator('input.filepick:not(.calibpick)').first().setInputFiles([manifest.jpgFile]);
    await pageJs.locator('.cell:has(canvas[data-drawn])').first().click({ timeout: 20000 });
    const stageA = pageJs.locator('.editor .stage');
    const retryEditorA = pageJs.locator('[data-act="retry-editor"]');
    await stageA.or(retryEditorA).waitFor({ state: 'visible', timeout: 15000 });
    let editorOpenedA = await stageA.isVisible();
    if (!editorOpenedA && (await retryEditorA.isVisible())) {
      await retryEditorA.click();
      editorOpenedA = await stageA.waitFor({ state: 'visible', timeout: 10000 }).then(() => true, () => false);
    }
    let optionsOpensA = 0;
    let requestsAfterFirstOpenA = 0;
    let requestsAfterReopensA = 0;
    let liveExportModulesA = [];
    if (editorOpenedA) {
      for (let i = 0; i < 3; i += 1) {
        await pageJs.locator('[data-act="editor-more"]').click();
        await pageJs.locator('.ms[data-state="editor-more"] [data-act="more-export-options"]').click();
        const sheet = pageJs.locator('.ms.export[data-state="export-sheet"]');
        const alert = pageJs.locator('.ms[data-state="editor-more"] [role="alert"]');
        await sheet.or(alert).waitFor({ state: 'visible', timeout: 10000 });
        if (!(await sheet.isVisible()) && (await pageJs.locator('.ms[data-state="editor-more"] [data-act="more-load-retry"]').isVisible())) {
          await pageJs.locator('.ms[data-state="editor-more"] [data-act="more-load-retry"]').click();
          await sheet.waitFor({ state: 'visible', timeout: 10000 }).catch(() => undefined);
        }
        if (await sheet.isVisible()) {
          optionsOpensA += 1;
          if (i === 0) requestsAfterFirstOpenA = jsRequests.length;
          await sheet.getByRole('button', { name: '取消' }).click();
          await sheet.waitFor({ state: 'hidden', timeout: 5000 }).catch(() => undefined);
        }
      }
      requestsAfterReopensA = jsRequests.length;
      liveExportModulesA = await pageJs.evaluate(async () => {
        const shell = document.querySelector('.app');
        const id = document.querySelector('.editor')?.dataset.photo?.toLowerCase() ?? '';
        const paths = [
          ...new Set(
            performance
              .getEntriesByType('resource')
              .map((e) => e.name)
              .filter((name) => /^\/assets\/export_ui-[^/]+\.js$/.test(new URL(name).pathname)),
          ),
        ];
        const mods = await Promise.allSettled(paths.map((p) => import(p)));
        const out = [];
        for (let i = 0; i < mods.length; i += 1) {
          if (mods[i].status !== 'fulfilled') continue;
          const ok = mods[i].value.openExportOptions(shell, [id]);
          if (ok) document.querySelector('.ms.export[data-state="export-sheet"] button.nb')?.click();
          out.push({ query: new URL(paths[i]).search, controlsFlow: ok });
        }
        return out;
      });
    }
    const jsFailResult = {
      editorOpened: editorOpenedA,
      optionsOpens: optionsOpensA,
      jsRequests: jsRequests.slice(),
      requestsAfterFirstOpen: requestsAfterFirstOpenA,
      requestsAfterReopens: requestsAfterReopensA,
      liveExportModules: liveExportModulesA,
      pageErrors: jsErrors,
    };
    await ctxJs.close();

    // Part B: 持续拦截 export_ui-*.js 直到「导出选项」首次点击显示错误与重试，放行后点「重试」用 ?retry=N 恢复并可多次重开
    const ctxRetry = await browser.newContext({ viewport: { width: 390, height: 844 }, hasTouch: true, isMobile: true });
    const pageRetry = await ctxRetry.newPage();
    const retryErrors = [];
    pageRetry.on('pageerror', (e) => retryErrors.push(e.message));
    let blockExportB = true;
    const retryRequestsB = [];
    await pageRetry.route(exportJsRe, (route) => {
      retryRequestsB.push(new URL(route.request().url()).search);
      return blockExportB ? route.abort() : route.continue();
    });
    await pageRetry.goto(`${server.url}/`, { waitUntil: 'domcontentloaded', timeout: 20000 });
    await pageRetry.locator('.app[data-screen="start"][data-core="1"] .start button.btn.primary').waitFor({ state: 'visible', timeout: 20000 });
    await pageRetry.locator('input.filepick:not(.calibpick)').first().setInputFiles([manifest.jpgFile]);
    await pageRetry.locator('.cell:has(canvas[data-drawn])').first().click({ timeout: 20000 });
    const stageB = pageRetry.locator('.editor .stage');
    const retryEditorB = pageRetry.locator('[data-act="retry-editor"]');
    await stageB.or(retryEditorB).waitFor({ state: 'visible', timeout: 15000 });
    let alertB = false;
    let hasRetryB = false;
    let recoveredOpensB = 0;
    let countAfterRetryB = 0;
    if (await stageB.isVisible()) {
      await pageRetry.locator('[data-act="editor-more"]').click();
      await pageRetry.locator('.ms[data-state="editor-more"] [data-act="more-export-options"]').click();
      alertB = await pageRetry.locator('.ms[data-state="editor-more"] [role="alert"]').waitFor({ state: 'visible', timeout: 10000 }).then(() => true, () => false);
      const retryBtnB = pageRetry.locator('.ms[data-state="editor-more"] [data-act="more-load-retry"]');
      hasRetryB = await retryBtnB.isVisible();
      if (hasRetryB) {
        blockExportB = false;
        await retryBtnB.click();
        const sheetB = pageRetry.locator('.ms.export[data-state="export-sheet"]');
        if (await sheetB.waitFor({ state: 'visible', timeout: 10000 }).then(() => true, () => false)) {
          recoveredOpensB += 1;
          countAfterRetryB = retryRequestsB.length;
          await sheetB.getByRole('button', { name: '取消' }).click();
          await sheetB.waitFor({ state: 'hidden', timeout: 5000 }).catch(() => undefined);
          for (let i = 0; i < 2; i += 1) {
            await pageRetry.locator('[data-act="editor-more"]').click();
            await pageRetry.locator('.ms[data-state="editor-more"] [data-act="more-export-options"]').click();
            if (await sheetB.waitFor({ state: 'visible', timeout: 10000 }).then(() => true, () => false)) {
              recoveredOpensB += 1;
              await sheetB.getByRole('button', { name: '取消' }).click();
              await sheetB.waitFor({ state: 'hidden', timeout: 5000 }).catch(() => undefined);
            }
          }
        }
      }
    }
    const explicitRetryResult = {
      alertVisible: alertB,
      hasRetryBtn: hasRetryB,
      recoveredOpens: recoveredOpensB,
      requests: retryRequestsB.slice(),
      countAfterRetry: countAfterRetryB,
      countAfterReopens: retryRequestsB.length,
      pageErrors: retryErrors,
    };
    await ctxRetry.close();

    // Part C: 仅拦截首次 export_ui-*.css 请求（JS 已成功），重试后不得产生第二份 export_ui JS 实例，且「导出选项」可直接打开并加载样式
    const ctxCss = await browser.newContext({ viewport: { width: 390, height: 844 }, hasTouch: true, isMobile: true });
    const pageCss = await ctxCss.newPage();
    const cssErrors = [];
    pageCss.on('pageerror', (e) => cssErrors.push(e.message));
    const cssRequestsC = [];
    const jsRequestsC = [];
    await pageCss.route(exportCssRe, (route) => {
      cssRequestsC.push(new URL(route.request().url()).search);
      return cssRequestsC.length === 1 ? route.abort() : route.continue();
    });
    await pageCss.route(exportJsRe, (route) => {
      jsRequestsC.push(new URL(route.request().url()).search);
      return route.continue();
    });
    await pageCss.goto(`${server.url}/`, { waitUntil: 'domcontentloaded', timeout: 20000 });
    await pageCss.locator('.app[data-screen="start"][data-core="1"] .start button.btn.primary').waitFor({ state: 'visible', timeout: 20000 });
    await pageCss.locator('input.filepick:not(.calibpick)').first().setInputFiles([manifest.rawOnlyDng]);
    await pageCss.getByText('只有 RAW', { exact: true }).waitFor({ state: 'visible', timeout: 20000 });
    await pageCss.locator('.cell').first().click();
    const stageC = pageCss.locator('.editor .stage');
    const retryEditorC = pageCss.locator('[data-act="retry-editor"]');
    await stageC.or(retryEditorC).waitFor({ state: 'visible', timeout: 15000 });
    let editorOpenedC = await stageC.isVisible();
    if (!editorOpenedC && (await retryEditorC.isVisible())) {
      await retryEditorC.click();
      editorOpenedC = await stageC.waitFor({ state: 'visible', timeout: 10000 }).then(() => true, () => false);
    }
    let optionsOpenedC = false;
    let cssRuleCountC = 0;
    let liveExportModulesC = [];
    if (editorOpenedC) {
      await pageCss.locator('[data-act="editor-more"]').click();
      await pageCss.locator('.ms[data-state="editor-more"] [data-act="more-export-options"]').click();
      const sheetC = pageCss.locator('.ms.export[data-state="export-sheet"]');
      const alertC = pageCss.locator('.ms[data-state="editor-more"] [role="alert"]');
      await sheetC.or(alertC).waitFor({ state: 'visible', timeout: 10000 });
      if (!(await sheetC.isVisible()) && (await pageCss.locator('.ms[data-state="editor-more"] [data-act="more-load-retry"]').isVisible())) {
        await pageCss.locator('.ms[data-state="editor-more"] [data-act="more-load-retry"]').click();
        await sheetC.waitFor({ state: 'visible', timeout: 10000 }).catch(() => undefined);
      }
      optionsOpenedC = await sheetC.isVisible();
      if (optionsOpenedC) {
        await sheetC.getByRole('button', { name: '取消' }).click();
        await sheetC.waitFor({ state: 'hidden', timeout: 5000 }).catch(() => undefined);
      }
      const cssProbe = await pageCss.evaluate(async () => {
        const links = [...document.querySelectorAll('link[rel="stylesheet"]')].filter((l) => /\/assets\/export_ui-[^/?]+\.css/.test(l.href));
        let rules = 0;
        for (const l of links) {
          try {
            if (l.sheet) rules = Math.max(rules, l.sheet.cssRules.length);
          } catch {}
        }
        const shell = document.querySelector('.app');
        const id = document.querySelector('.editor')?.dataset.photo?.toLowerCase() ?? '';
        const paths = [
          ...new Set(
            performance
              .getEntriesByType('resource')
              .map((e) => e.name)
              .filter((name) => /^\/assets\/export_ui-[^/]+\.js$/.test(new URL(name).pathname)),
          ),
        ];
        const mods = await Promise.allSettled(paths.map((p) => import(p)));
        const live = [];
        for (let i = 0; i < mods.length; i += 1) {
          if (mods[i].status !== 'fulfilled') continue;
          const ok = mods[i].value.openExportOptions(shell, [id]);
          if (ok) document.querySelector('.ms.export[data-state="export-sheet"] button.nb')?.click();
          live.push({ query: new URL(paths[i]).search, controlsFlow: ok });
        }
        return { rules, live };
      });
      cssRuleCountC = cssProbe.rules;
      liveExportModulesC = cssProbe.live;
    }
    const cssFailResult = {
      editorOpened: editorOpenedC,
      optionsOpened: optionsOpenedC,
      cssRequests: cssRequestsC.slice(),
      jsRequests: jsRequestsC.slice(),
      cssRuleCount: cssRuleCountC,
      liveExportModules: liveExportModulesC,
      pageErrors: cssErrors,
    };
    await ctxCss.close();

    return { jsFailResult, explicitRetryResult, cssFailResult };
  } finally {
    await browser.close();
    await server.close();
  }
}

async function runReopenSingleInstance() {
  const { launchPinnedChromium } = await loadRuntime();
  const server = await startStaticServer(manifest.distDir);
  const browser = await launchPinnedChromium({ args: CHROMIUM_ARGS });
  try {
    // Part A: 关机图失败 1 次 -> 重试成功 -> 关闭并连续重开 3 次，只能有 1 个重试 URL、0 次额外请求
    const ctxOff = await browser.newContext({ viewport: { width: 390, height: 844 }, hasTouch: true, isMobile: true });
    const pageOff = await ctxOff.newPage();
    const offErrors = [];
    pageOff.on('pageerror', (e) => offErrors.push(e.message));
    const offRequests = [];
    await pageOff.route(/\/assets\/off_image_sheet-[^/?]+\.js(?:\?|$)/, (route) => {
      offRequests.push(new URL(route.request().url()).search);
      return offRequests.length === 1 ? route.abort() : route.continue();
    });
    await openEditorOnPhoto(pageOff, server.url, manifest.jpgFile);
    await pageOff.locator('[data-act="editor-more"]').click();
    await pageOff.locator('.ms[data-state="editor-more"] [data-act="more-off-image"]').click();
    await pageOff.locator('.ms[data-state="editor-more"] [role="alert"]').waitFor({ state: 'visible', timeout: 10000 });
    await pageOff.locator('.ms[data-state="editor-more"] [data-act="more-load-retry"]').click();
    await pageOff.locator('[data-state="off-image"]').waitFor({ state: 'visible', timeout: 15000 });
    await pageOff.locator('[data-state="off-image"]').getByRole('button', { name: '关闭' }).click();
    await pageOff.locator('[data-state="off-image"]').waitFor({ state: 'hidden', timeout: 5000 }).catch(() => undefined);

    let reopenCount = 0;
    for (let i = 0; i < 3; i += 1) {
      await pageOff.locator('[data-act="editor-more"]').click();
      await pageOff.locator('.ms[data-state="editor-more"] [data-act="more-off-image"]').click();
      if (await pageOff.locator('[data-state="off-image"]').waitFor({ state: 'visible', timeout: 10000 }).then(() => true, () => false)) {
        reopenCount += 1;
        await pageOff.locator('[data-state="off-image"]').getByRole('button', { name: '关闭' }).click();
        await pageOff.locator('[data-state="off-image"]').waitFor({ state: 'hidden', timeout: 5000 }).catch(() => undefined);
      }
    }
    const offIdentity = await pageOff.evaluate(async () => {
      const paths = [
        ...new Set(
          performance
            .getEntriesByType('resource')
            .map((e) => e.name)
            .filter((name) => /^\/assets\/off_image_sheet-[^/]+\.js$/.test(new URL(name).pathname) && new URL(name).searchParams.has('retry')),
        ),
      ];
      const mods = await Promise.all(paths.map((p) => import(p)));
      return { uniqueRetryUrls: paths.length, sameModule: mods.length === 1 || mods.every((m) => m === mods[0]) };
    });
    const offResult = { requests: offRequests.slice(), reopenCount, ...offIdentity, pageErrors: offErrors };
    await ctxOff.close();

    // Part B: grid 分包失败 1 次 -> 重试成功 -> 进出编辑器 2 次，不再重发 grid 请求
    const ctxGrid = await browser.newContext({ viewport: { width: 390, height: 844 }, hasTouch: true, isMobile: true });
    const pageGrid = await ctxGrid.newPage();
    const gridErrors = [];
    pageGrid.on('pageerror', (e) => gridErrors.push(e.message));
    let blockGrid = true;
    const gridRequests = [];
    await pageGrid.route(/\/assets\/grid-[^/?]+\.js(?:\?|$)/, (route) => {
      gridRequests.push(new URL(route.request().url()).search);
      return blockGrid ? route.abort() : route.continue();
    });
    await pageGrid.goto(`${server.url}/`, { waitUntil: 'domcontentloaded', timeout: 20000 });
    await pageGrid.locator('.app[data-screen="start"][data-core="1"] .start button.btn.primary').waitFor({ state: 'visible', timeout: 20000 });
    await pageGrid.locator('input.filepick:not(.calibpick)').first().setInputFiles([manifest.jpgFile]);
    await pageGrid.locator('[data-act="retry-grid"]').waitFor({ state: 'visible', timeout: 10000 });
    blockGrid = false;
    const beforeRetryCount = gridRequests.length;
    await pageGrid.locator('[data-act="retry-grid"]').click();
    await pageGrid.locator('.app[data-screen="grid"] .cell:has(canvas[data-drawn])').first().waitFor({ state: 'visible', timeout: 15000 });
    for (let i = 0; i < 2; i += 1) {
      await pageGrid.locator('.cell:has(canvas[data-drawn])').first().click();
      await pageGrid.locator('.editor .stage').waitFor({ state: 'visible', timeout: 15000 });
      await pageGrid.locator('button.nav-back').click();
      await pageGrid.locator('.app[data-screen="grid"] .cell:has(canvas[data-drawn])').first().waitFor({ state: 'visible', timeout: 15000 });
    }
    const gridResult = {
      requests: gridRequests.slice(),
      beforeRetryCount,
      afterRoundTripsCount: gridRequests.length,
      pageErrors: gridErrors,
    };
    await ctxGrid.close();

    return { offResult, gridResult };
  } finally {
    await browser.close();
    await server.close();
  }
}

let output;
if (mode === 'menu-race-and-fail') output = await runMenuRaceAndFail();
else if (mode === 'raw-only-prewarm') output = await runRawOnlyPrewarm();
else if (mode === 'pool-idle-cold-slot') output = await runPoolIdleColdSlot();
else if (mode === 'grid-fail-recovery') output = await runGridFailRecovery();
else if (mode === 'export-retry-and-reuse') output = await runExportRetryAndReuse();
else if (mode === 'reopen-single-instance') output = await runReopenSingleInstance();
else throw new Error(`unknown mode: ${mode}`);

fs.writeFileSync(manifest.out, JSON.stringify(output, null, 2));
process.stdout.write('done\n');
