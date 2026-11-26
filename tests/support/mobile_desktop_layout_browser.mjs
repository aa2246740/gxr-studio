// 电脑浏览器版式回归驱动（2026-11-26，生产 mobile/dist）。
// 用法：node mobile_desktop_layout_browser.mjs <runtime.mjs> <manifest.json>
// manifest：distDir、files（照片 + 相机配方）、pack（可选校准包）、stem（量照片的那张）、groups（要跑的组）、out、
// 可选 shotsDir、shotsPrefix。三种引擎：Chromium Metal（真显卡 WebGPU）、Chromium 去掉 navigator.gpu、WebKit（没有 WebGPU）。
// 电脑：没有触摸点、指针精确；iPad / 手机：有触摸（只查「不动」）。
// 只量矩形和计数；输出只写脱敏 JSON，不写照片名、拍摄时间或 EXIF。HTTP 服务只绑 127.0.0.1 临时端口（避开禁用端口）。

import fs from 'node:fs';
import http from 'node:http';
import path from 'node:path';
import { pathToFileURL } from 'node:url';

const [runtimePath, manifestPath] = process.argv.slice(2);
const manifest = JSON.parse(fs.readFileSync(manifestPath, 'utf8'));
const BROWSER_DIRS = [process.env.PLAYWRIGHT_BROWSERS_PATH].filter(Boolean);
const { launchPinnedChromium, pinnedRuntime, verifyPinnedRuntime } = await import(pathToFileURL(runtimePath).href);
verifyPinnedRuntime();
BROWSER_DIRS.push(pinnedRuntime.browsersPath);
const { webkit } = await import(pathToFileURL(pinnedRuntime.modulePath).href);

const FORBIDDEN_PORTS = new Set([60887, 18772, 18801, 18802, 18803, 5290]);
const MIME = { '.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8', '.css': 'text/css; charset=utf-8', '.wasm': 'application/wasm', '.zip': 'application/zip', '.ttf': 'font/ttf', '.txt': 'text/plain; charset=utf-8', '.svg': 'image/svg+xml', '.json': 'application/json', '.png': 'image/png' };
const METAL_ARGS = ['--enable-unsafe-webgpu', '--enable-gpu', '--ignore-gpu-blocklist', '--use-angle=metal'];
const IPHONE_UA = 'Mozilla/5.0 (iPhone; CPU iPhone OS 18_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.6 Mobile/15E148 Safari/604.1';
const IPAD_UA = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/26.0 Safari/605.1.15';
const DESKTOP_SIZES = [[1024, 768], [1440, 900], [1920, 1080], [2560, 1440]];
const STEM = manifest.stem.toLowerCase();

function resolveWebKitExecutable() {
  const direct = webkit.executablePath();
  if (direct && fs.existsSync(direct)) return direct;
  const baseName = direct ? path.basename(direct) : 'pw_run.sh';
  for (const dir of BROWSER_DIRS) {
    if (!fs.existsSync(dir)) continue;
    for (const entry of fs.readdirSync(dir)) {
      if (!entry.startsWith('webkit-')) continue;
      const candidate = path.join(dir, entry, baseName);
      if (fs.existsSync(candidate)) return candidate;
    }
  }
  return null;
}

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
    res.setHeader('Cache-Control', 'no-store');
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

function noGpu() {
  try {
    Object.defineProperty(Navigator.prototype, 'gpu', { configurable: true, get: () => undefined });
  } catch {
    /* 已经没有 */
  }
}

/** iPadOS Safari：Mac 的 UA，但有 5 个触摸点。 */
function ipadTouch() {
  Object.defineProperty(Navigator.prototype, 'maxTouchPoints', { configurable: true, get: () => 5 });
}

const settle = (page, ms = 150) => page.evaluate((wait) => new Promise((done) => requestAnimationFrame(() => requestAnimationFrame(() => setTimeout(done, wait)))), ms);
const cell = (page) => page.locator(`.gscroll .cell[data-id="${STEM}"]`);

/** kind: computer | ipad | phone。computer 没有触摸；ipad、phone 有触摸（WebKit 才能真正模拟 iPad 的 Mac UA）。 */
async function newPage(browser, engine, kind, vp) {
  const touch = kind !== 'computer';
  const opts = { viewport: vp, deviceScaleFactor: 1, hasTouch: touch, isMobile: touch, locale: 'zh-CN' };
  if (kind === 'ipad') opts.userAgent = IPAD_UA;
  if (kind === 'phone') opts.userAgent = IPHONE_UA;
  const ctx = await browser.newContext(opts);
  if (engine === 'nogpu') await ctx.addInitScript(noGpu);
  if (kind === 'ipad') await ctx.addInitScript(ipadTouch);
  const page = await ctx.newPage();
  const errors = [];
  page.on('pageerror', (err) => errors.push(String(err.message).slice(0, 200)));
  return { ctx, page, errors };
}

async function start(page, url) {
  await page.goto(url, { waitUntil: 'domcontentloaded' });
  await page.locator('.app[data-screen="start"][data-core="1"]').waitFor({ timeout: 60_000 });
  if (manifest.pack) {
    await page.locator('input.calibpick').setInputFiles(manifest.pack);
    await page.waitForFunction(() => window.__gxrBook?.pack?.kind === 'user' && !window.__gxrBook.packBusy, null, { timeout: 60_000 });
  }
  await page.locator('input.filepick:not(.calibpick)').first().setInputFiles(manifest.files);
  await page.locator('.app[data-screen="grid"]').waitFor({ timeout: 30_000 });
  await cell(page).waitFor({ timeout: 30_000 });
  await page.waitForFunction(() => !document.querySelector('.nav small')?.textContent?.includes('正在读取'), null, { timeout: 120_000 });
}

async function dismissAsk(page) {
  const later = page.locator('.ask-sheet button', { hasText: '先不用' });
  if (await later.isVisible().catch(() => false)) {
    await later.click();
    await page.waitForFunction(() => !document.querySelector('.ask-sheet'), null, { timeout: 5_000 });
  }
}

/** 打开那张照片并等它的预览画好：Metal 等 GPU 画布按照片比例定好尺寸；没有显卡等相机 JPG 的清图。 */
async function open(page, engine) {
  await cell(page).click();
  await page.waitForFunction((want) => document.querySelector('.app')?.dataset.screen === 'editor' && document.querySelector('.screen.editor')?.dataset.photo?.toLowerCase() === want, STEM, { timeout: 30_000 });
  await page.waitForFunction(() => document.querySelector('.app')?.dataset.settled !== '0', null, { timeout: 60_000 });
  const media = engine === 'metal' ? '.editor .stage .pv canvas.gpu:not([hidden])' : '.editor .stage .pv canvas.still:not([hidden])';
  await page.waitForSelector(media, { timeout: 120_000 });
  if (engine === 'metal') await page.waitForFunction(() => Boolean(document.querySelector('.editor .stage .pv canvas.gpu')?.style.width), null, { timeout: 60_000 });
  await settle(page, 1_200);
  await dismissAsk(page);
}

async function resize(page, vp) {
  await page.setViewportSize(vp);
  await page.waitForFunction(([w, h]) => innerWidth === w && innerHeight === h, [vp.width, vp.height]);
  await settle(page, 600);
}

async function tool(page, id) {
  await page.locator(`.editor .toolbar .tbtn[data-tool="${id}"]`).first().click();
  await settle(page, 400);
}

async function shot(page, name) {
  if (!manifest.shotsDir) return;
  fs.mkdirSync(manifest.shotsDir, { recursive: true });
  await page.screenshot({ path: path.join(manifest.shotsDir, `${manifest.shotsPrefix || 'shot'}-${name}.png`), animations: 'disabled' });
}

/** 舞台盒、照片实际画出来的矩形（画布盒 + object-fit: contain）、裁剪框。 */
function measureFit(page) {
  return page.evaluate(() => {
    const r4 = (r) => (r ? { x: Math.round(r.x * 10) / 10, y: Math.round(r.y * 10) / 10, w: Math.round(r.w * 10) / 10, h: Math.round(r.h * 10) / 10 } : null);
    const box = (node) => {
      if (!node || node.getClientRects().length === 0) return null;
      const b = node.getBoundingClientRect();
      return { x: b.left, y: b.top, w: b.width, h: b.height };
    };
    const pv = document.querySelector('.editor .stage .pv');
    const media = pv?.querySelector('canvas.gpu:not([hidden]), canvas.still:not([hidden])') ?? null;
    let photo = box(media);
    if (photo && media.width > 0 && media.height > 0) {
      const fit = getComputedStyle(media).objectFit;
      if (fit === 'contain') {
        const s = Math.min(photo.w / media.width, photo.h / media.height);
        const dw = media.width * s;
        const dh = media.height * s;
        photo = { x: photo.x + (photo.w - dw) / 2, y: photo.y + (photo.h - dh) / 2, w: dw, h: dh };
      }
    }
    const overlay = document.querySelector('.editor .crop-overlay');
    const cbox = overlay && !overlay.hidden ? box(overlay.querySelector('.cbox')) : null;
    return {
      layout: document.querySelector('.app')?.dataset.layout ?? '',
      kind: media ? media.className : null,
      pv: r4(box(pv)),
      photo: r4(photo),
      cbox: r4(cbox),
      aspect: media && media.height > 0 ? Math.round((media.width / media.height) * 1e4) / 1e4 : null,
    };
  });
}

/** L1：每个尺寸量一次照片（配方页），再进裁剪量一次裁剪框。 */
async function fitGroup(browser, url, engine, kind, sizes) {
  const rows = [];
  let adapter = null;
  const first = { width: sizes[0][0], height: sizes[0][1] };
  const { ctx, page, errors } = await newPage(browser, engine, kind, first);
  try {
    await start(page, url);
    adapter = await page.evaluate(async () => {
      const found = navigator.gpu ? await navigator.gpu.requestAdapter().catch(() => null) : null;
      return found ? { fallback: Boolean(found.info?.isFallbackAdapter ?? found.isFallbackAdapter), vendor: found.info?.vendor ?? '' } : null;
    });
    await open(page, engine);
    for (const [width, height] of sizes) {
      await resize(page, { width, height });
      const view = await measureFit(page);
      await tool(page, 'crop');
      await page.waitForSelector('.editor .crop-overlay:not([hidden]) .cbox', { timeout: 10_000 });
      await settle(page, 300);
      const crop = await measureFit(page);
      if ((width === 1440 && height === 900) || (width === 2560 && height === 1440)) await shot(page, `fit-${engine}-${kind}-${width}x${height}-crop`);
      await tool(page, 'recipe');
      rows.push({ size: `${width}x${height}`, view, crop });
    }
  } finally {
    await ctx.close();
  }
  return { rows, errors, adapter };
}

async function withBrowser(engine, run) {
  if (engine === 'webkit') {
    const exe = resolveWebKitExecutable();
    if (!exe) return { skipped: 'pinned Playwright WebKit is not installed' };
    const browser = await webkit.launch({ headless: true, executablePath: exe });
    try {
      return await run(browser);
    } finally {
      await browser.close();
    }
  }
  const browser = await launchPinnedChromium({ args: engine === 'metal' ? METAL_ARGS : ['--disable-gpu'] });
  try {
    return await run(browser);
  } finally {
    await browser.close();
  }
}

const GROUPS = {
  async fit(url) {
    const out = {};
    for (const engine of ['metal', 'nogpu', 'webkit']) {
      out[engine] = await withBrowser(engine, async (browser) => {
        const res = await fitGroup(browser, url, engine, 'computer', DESKTOP_SIZES);
        return res;
      });
    }
    return out;
  },
  async fitTouch(url) {
    return withBrowser('webkit', async (browser) => ({
      ipad: await fitGroup(browser, url, 'webkit', 'ipad', [[1180, 820], [1024, 1366]]),
      phone: await fitGroup(browser, url, 'webkit', 'phone', [[390, 844], [844, 390]]),
    }));
  },
};

const report = { groups: {} };
const server = await startServer(manifest.distDir);
try {
  for (const name of manifest.groups) {
    try {
      report.groups[name] = await GROUPS[name](server.url);
    } catch (err) {
      report.groups[name] = { error: String(err?.message ?? err).replace(/\/(?:Volumes|Users|private|var)\/\S+/g, '[path]').slice(0, 600) };
    }
  }
} finally {
  await server.close();
}
fs.writeFileSync(manifest.out, JSON.stringify(report));
