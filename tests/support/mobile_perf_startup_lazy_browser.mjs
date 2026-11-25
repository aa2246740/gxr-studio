// 两个懒加载分包（grid、editor_chunk）的失败恢复与竞态回归驱动（生产 mobile/dist）。
// 用法：node mobile_perf_startup_lazy_browser.mjs <runtime.mjs> <manifest.json>；manifest 给 distDir、照片和要跑的 modes。
// 每个模式一个新的浏览器上下文；HTTP 服务只绑 127.0.0.1 临时端口（避开禁用端口），结束时关掉浏览器和服务。

import fs from 'node:fs';
import http from 'node:http';
import path from 'node:path';
import { pathToFileURL } from 'node:url';

const [runtimePath, manifestPath] = process.argv.slice(2);
const manifest = JSON.parse(fs.readFileSync(manifestPath, 'utf8'));
const { jpeg, jpeg2, bad, raw } = manifest.photos;

const FORBIDDEN_PORTS = new Set([60887, 18772, 18801, 18802, 18803, 5290]);
const CHROMIUM_ARGS = ['--enable-unsafe-webgpu', '--enable-unsafe-swiftshader', '--use-angle=swiftshader', '--use-webgpu-adapter=swiftshader'];
const MIME = { '.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8', '.css': 'text/css; charset=utf-8', '.wasm': 'application/wasm', '.zip': 'application/zip', '.ttf': 'font/ttf', '.txt': 'text/plain; charset=utf-8', '.svg': 'image/svg+xml' };
const GRID = /\/assets\/grid-[^/?]+\.js(?:\?|$)/;
const EDITOR = /\/assets\/editor_chunk-[^/?]+\.js(?:\?|$)/;
const DIALOG = '[data-state="chunk-load-error"]';
const EXPORT_SHEET = '.ms.export[data-state="export-sheet"]';

function startServer(rootDir) {
  const root = path.resolve(rootDir);
  const server = http.createServer((req, res) => {
    const clean = decodeURIComponent((req.url || '/').split('?')[0]);
    const full = path.resolve(root, clean === '/' ? 'index.html' : clean.replace(/^\/+/, ''));
    if (!full.startsWith(root + path.sep) || !fs.existsSync(full) || !fs.statSync(full).isFile()) {
      res.statusCode = 404;
      res.end('not found');
      return;
    }
    res.setHeader('Content-Type', MIME[path.extname(full).toLowerCase()] || 'application/octet-stream');
    res.setHeader('Cache-Control', clean.startsWith('/assets/') ? 'public, max-age=31536000, immutable' : 'no-cache');
    res.end(fs.readFileSync(full));
  });
  return new Promise((resolve, reject) => {
    const listen = () =>
      server.listen(0, '127.0.0.1', () => {
        const { port } = server.address();
        if (FORBIDDEN_PORTS.has(port)) return server.close(listen);
        resolve({ url: `http://127.0.0.1:${port}`, close: () => new Promise((done) => server.close(done)) });
      });
    server.on('error', reject);
    listen();
  });
}

const picker = (page) => page.locator('input.filepick:not(.calibpick)').first();
const drawnCells = (page) => page.locator('.gscroll .cell:has(canvas[data-drawn])');
const screen = (page) => page.locator('.app').getAttribute('data-screen');
const editorPhoto = (page) => page.evaluate(() => document.querySelector('.screen.editor')?.dataset.photo ?? null);
const nextFrames = (page) => page.evaluate(() => new Promise((done) => requestAnimationFrame(() => requestAnimationFrame(() => setTimeout(done, 50)))));

async function start(page, url) {
  await page.goto(url, { waitUntil: 'domcontentloaded' });
  await page.locator('.app[data-screen="start"][data-core="1"] .start button.btn.primary').waitFor();
}

/** 记下一个分包的每次请求（查询串）和每次成功响应；`gate(n)` 决定第 n 次请求：'ok'、'abort' 或一个先等着的 Promise。 */
async function watchChunk(page, pattern, gate) {
  const seen = { requests: [], ok: 0 };
  await page.route(pattern, async (route) => {
    const n = seen.requests.push(new URL(route.request().url()).search);
    let decision = gate(n);
    if (decision && typeof decision.then === 'function') decision = await decision;
    if (decision === 'abort') return route.abort();
    seen.ok += 1;
    return route.continue();
  });
  return seen;
}

function held() {
  let release;
  const promise = new Promise((done) => {
    release = done;
  });
  return { promise, release };
}

async function clearPhotos(page) {
  await page.getByRole('button', { name: '更多', exact: true }).click();
  await page.getByText('清空照片，重新选', { exact: true }).click();
  await page.locator('.app[data-screen="start"] .start').waitFor();
}

async function openExportOptions(page) {
  await page.locator('[data-act="editor-more"]').click();
  await page.locator('[data-state="editor-more"] [data-act="more-export-options"]').click();
  await page.locator(EXPORT_SHEET).waitFor();
}

const modes = {
  /** 网格分包失败：停在开始页，给「重试」和「刷新页面」；没恢复前重选照片也只是再提示一次；恢复后往返不再请求。 */
  async 'grid-fail'(page, url) {
    let blocked = true;
    const grid = await watchChunk(page, GRID, () => (blocked ? 'abort' : 'ok'));
    await start(page, url);
    await page.waitForTimeout(600);
    await picker(page).setInputFiles([jpeg]);
    const dialog = page.locator(`${DIALOG}[data-chunk="grid"]`);
    await dialog.waitFor();
    const onError = {
      screen: await screen(page),
      startVisible: await page.locator('.start').isVisible(),
      retry: await dialog.locator('[data-act="retry-chunk"]').isVisible(),
      reload: await dialog.locator('[data-act="reload-app"]').isVisible(),
    };
    const before = grid.requests.length;
    await picker(page).setInputFiles([jpeg, jpeg2]);
    for (let i = 0; i < 50 && grid.requests.length === before; i += 1) await page.waitForTimeout(50);
    await dialog.waitFor();
    await nextFrames(page);
    const afterRepick = { screen: await screen(page), dialogs: await page.locator(DIALOG).count(), requests: grid.requests.length };
    blocked = false;
    await dialog.locator('[data-act="retry-chunk"]').click();
    await drawnCells(page).nth(1).waitFor();
    const atRecovery = grid.requests.slice();
    await drawnCells(page).first().click();
    await page.locator('.editor .stage').waitFor();
    await page.locator('button.nav-back').click();
    await drawnCells(page).first().waitFor();
    return { onError, afterRepick, atRecovery, cells: await page.locator('.gscroll .cell').count(), requests: grid.requests, ok: grid.ok };
  },

  /** 编辑器分包：预取失败不提示；点开时再失败才提示，重试成功后只有一份模块，导出选项和关机图都不再请求分包。 */
  async 'editor-fail'(page, url) {
    let blocked = true;
    const editor = await watchChunk(page, EDITOR, () => (blocked ? 'abort' : 'ok'));
    await start(page, url);
    await picker(page).setInputFiles([jpeg]);
    await drawnCells(page).first().waitFor();
    for (let i = 0; i < 50 && editor.requests.length < 1; i += 1) await page.waitForTimeout(50);
    await page.waitForTimeout(200);
    const prewarmRequests = editor.requests.slice();
    const dialogAfterPrewarm = await page.locator(DIALOG).count();
    await drawnCells(page).first().click();
    const dialog = page.locator(`${DIALOG}[data-chunk="editor"]`);
    await dialog.waitFor();
    const onError = { screen: await screen(page), retry: await dialog.locator('[data-act="retry-chunk"]').isVisible(), reload: await dialog.locator('[data-act="reload-app"]').isVisible() };
    blocked = false;
    await dialog.locator('[data-act="retry-chunk"]').click();
    await page.locator('.editor .stage').waitFor();
    const atRecovery = editor.requests.length;
    for (let i = 0; i < 3; i += 1) {
      await openExportOptions(page);
      await page.locator(EXPORT_SHEET).getByRole('button', { name: '取消' }).click();
    }
    await page.locator('[data-act="editor-more"]').click();
    await page.locator('[data-state="editor-more"] [data-act="more-off-image"]').click();
    await page.locator('[data-state="off-image"]').waitFor();
    const navigations = await page.evaluate(() => performance.getEntriesByType('navigation').length);
    return { prewarmRequests, dialogAfterPrewarm, onError, atRecovery, requests: editor.requests, ok: editor.ok, navigations };
  },

  /** 预取失败（网络已恢复）：点开照片直接用新地址拿到分包，不弹错误。 */
  async 'editor-prewarm-fail'(page, url) {
    const editor = await watchChunk(page, EDITOR, (n) => (n === 1 ? 'abort' : 'ok'));
    await start(page, url);
    await picker(page).setInputFiles([jpeg]);
    await drawnCells(page).first().waitFor();
    for (let i = 0; i < 50 && editor.requests.length < 1; i += 1) await page.waitForTimeout(100);
    await page.waitForTimeout(200);
    await drawnCells(page).first().click();
    await page.locator('.editor .stage').waitFor();
    return { requests: editor.requests, ok: editor.ok, dialogs: await page.locator(DIALOG).count() };
  },

  /** 编辑器分包在路上时先点 A 再点 B：只打开 B。 */
  async 'open-other'(page, url) {
    const gate = held();
    const editor = await watchChunk(page, EDITOR, () => gate.promise);
    await start(page, url);
    await picker(page).setInputFiles([jpeg, jpeg2]);
    await drawnCells(page).nth(1).waitFor();
    const cells = page.locator('.gscroll .cell');
    const want = (await cells.nth(1).getAttribute('data-id'))?.toUpperCase() ?? null;
    await cells.nth(0).click();
    await cells.nth(1).click();
    const screenWhileLoading = await screen(page);
    gate.release('ok');
    await page.locator('.editor .stage').waitFor();
    await nextFrames(page);
    return { screenWhileLoading, want, photo: await editorPhoto(page), editors: await page.locator('.screen.editor').count(), dialogs: await page.locator(DIALOG).count(), requests: editor.requests };
  },

  /** 编辑器分包在路上时点开照片、再清空照片：分包到了（成功或失败）也停在开始页，不开编辑器、不弹错误。 */
  async 'open-clear'(page, url, variant) {
    const gate = held();
    await watchChunk(page, EDITOR, () => gate.promise);
    await start(page, url);
    await picker(page).setInputFiles([jpeg]);
    await drawnCells(page).first().waitFor();
    await drawnCells(page).first().click();
    await clearPhotos(page);
    gate.release(variant === 'fail' ? 'abort' : 'ok');
    await page.waitForTimeout(500);
    await nextFrames(page);
    return { screen: await screen(page), editors: await page.locator('.screen.editor').count(), dialogs: await page.locator(DIALOG).count(), startVisible: await page.locator('.start').isVisible() };
  },

  /** 图库多选导出时编辑器分包在路上、随后清空照片：分包到了也不弹导出面板或错误（r5 P2）。 */
  async 'export-clear'(page, url, variant) {
    const gate = held();
    await watchChunk(page, EDITOR, () => gate.promise);
    await start(page, url);
    await picker(page).setInputFiles([jpeg, jpeg2]);
    await drawnCells(page).nth(1).waitFor();
    await page.locator('button.nb.bold', { hasText: '选择' }).click();
    await page.locator('.gscroll .cell').first().click();
    await page.locator('[data-act="export"]').click();
    await page.getByRole('button', { name: '取消', exact: true }).first().click();
    await clearPhotos(page);
    gate.release(variant === 'fail' ? 'abort' : 'ok');
    await page.waitForTimeout(500);
    await nextFrames(page);
    return { screen: await screen(page), exportSheets: await page.locator(EXPORT_SHEET).count(), dialogs: await page.locator(DIALOG).count() };
  },

  /** 图库多选导出时编辑器分包还没到：等它到了，导出面板照常打开，选的是那几张。 */
  async 'export-wait'(page, url) {
    const gate = held();
    const editor = await watchChunk(page, EDITOR, () => gate.promise);
    await start(page, url);
    await picker(page).setInputFiles([jpeg, jpeg2]);
    await drawnCells(page).nth(1).waitFor();
    await page.locator('button.nb.bold', { hasText: '选择' }).click();
    await page.locator('.gscroll .cell').nth(1).click();
    await page.locator('[data-act="export"]').click();
    const sheetsWhileLoading = await page.locator(EXPORT_SHEET).count();
    gate.release('ok');
    await page.locator(EXPORT_SHEET).waitFor();
    return { sheetsWhileLoading, sheets: await page.locator(EXPORT_SHEET).count(), screen: await screen(page), requests: editor.requests };
  },

  /** 只有 RAW、没有能画的缩略图：导入时不预取编辑器分包，落定空闲后才预取；点开照片随时按需加载。 */
  async 'raw-only'(page, url) {
    const editor = await watchChunk(page, EDITOR, () => 'ok');
    await start(page, url);
    await picker(page).setInputFiles([raw]);
    await page.getByText('只有 RAW', { exact: true }).waitFor();
    await page.waitForTimeout(700);
    const early = { drawn: await page.locator('.cell canvas[data-drawn]').count(), requests: editor.requests.length };
    for (let i = 0; i < 60 && editor.requests.length < 1; i += 1) await page.waitForTimeout(100);
    const settled = editor.requests.length;
    await page.locator('.gscroll .cell').first().click();
    await page.locator('.editor .stage').waitFor();
    return { early, settled, requests: editor.requests };
  },

  /** 网格分包在路上时选了一张好图和一张坏图：分包到之前不读照片，到了以后照常提示「读不了」一次（r4）。 */
  async 'unreadable-hold-grid'(page, url) {
    await page.addInitScript(() => {
      const Builtin = window.Worker;
      window.reviewThumbs = 0;
      window.Worker = class extends Builtin {
        constructor(...args) {
          super(...args);
          this.addEventListener('message', ({ data }) => {
            if (data?.kind === 'thumb') window.reviewThumbs += 1;
          });
        }
      };
    });
    const gate = held();
    await watchChunk(page, GRID, () => gate.promise);
    await start(page, url);
    await picker(page).setInputFiles([jpeg, bad]);
    await page.waitForTimeout(600);
    const beforeGrid = { screen: await screen(page), thumbs: await page.evaluate(() => window.reviewThumbs) };
    gate.release('ok');
    const notice = page.getByText('R9990004 读不了', { exact: true });
    await notice.waitFor();
    await drawnCells(page).first().waitFor();
    await nextFrames(page);
    return { beforeGrid, screen: await screen(page), notices: await notice.count(), drawn: await page.locator('.gscroll .cell canvas[data-drawn]').count() };
  },

  /** iPad：导出选项随点随开（同一分包），换照片以后再打开的导出选项、导出的文件都是当前这张（r5 P1）。 */
  async 'ipad-switch-export'(page, url) {
    await page.addInitScript(() => {
      window.reviewShared = [];
      Object.defineProperty(navigator, 'canShare', { value: () => true, configurable: true });
      Object.defineProperty(navigator, 'share', {
        value: async ({ files }) => {
          window.reviewShared.push(...files.map((file) => file.name));
        },
        configurable: true,
      });
    });
    await start(page, url);
    await picker(page).setInputFiles([jpeg, jpeg2]);
    await drawnCells(page).nth(1).waitFor();
    await drawnCells(page).first().click();
    await page.locator('.editor .stage').waitFor();
    const first = await editorPhoto(page);
    await page.locator('.editor [data-tool="more"]:visible').click();
    await page.locator('.more-panel [data-more="export"]').click();
    const sheetAtOnce = await page.locator(EXPORT_SHEET).count();
    // iPad 上导出选项是弹出框，关闭键是「好」。
    await page.locator(EXPORT_SHEET).getByRole('button', { name: '好', exact: true }).click();
    await page.locator('.ipad-filmstrip button.cell:not(.cur)').first().click();
    await page.waitForFunction((id) => document.querySelector('.screen.editor')?.dataset.photo !== id, first);
    const second = await editorPhoto(page);
    await page.locator('.editor [data-tool="more"]:visible').click();
    await page.locator('.more-panel [data-more="export"]').click();
    await page.locator(EXPORT_SHEET).waitFor();
    await page.locator(`${EXPORT_SHEET} .go`).click();
    await page.locator('.pdock [data-act="share"]').click({ timeout: 60000 });
    await page.waitForFunction(() => window.reviewShared.length > 0, null, { timeout: 30000 });
    return { first, second, sheetAtOnce, shared: await page.evaluate(() => window.reviewShared), photo: await editorPhoto(page) };
  },
};

const { launchPinnedChromium, verifyPinnedRuntime } = await import(pathToFileURL(runtimePath).href);
verifyPinnedRuntime();
const server = await startServer(manifest.distDir);
const browser = await launchPinnedChromium({ args: CHROMIUM_ARGS });
const out = {};
try {
  for (const name of manifest.modes) {
    const [mode, variant] = name.split(':');
    const ipad = mode.startsWith('ipad');
    const context = await browser.newContext({ viewport: ipad ? { width: 1194, height: 834 } : { width: 390, height: 844 }, hasTouch: true, isMobile: true, deviceScaleFactor: 2 });
    const page = await context.newPage();
    page.setDefaultTimeout(20000);
    const pageErrors = [];
    page.on('pageerror', (err) => pageErrors.push(err.message));
    try {
      out[name] = { ...(await modes[mode](page, `${server.url}/`, variant)), pageErrors };
    } catch (err) {
      out[name] = { error: String(err?.stack || err).slice(0, 1500), pageErrors };
    } finally {
      await context.close();
    }
  }
} finally {
  await browser.close();
  await server.close();
}
fs.writeFileSync(manifest.out, JSON.stringify(out, null, 2));
process.stdout.write('done\n');
