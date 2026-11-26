// 手机端显卡设备丢失回归驱动（生产 mobile/dist，Metal Chromium）。
// 用法：node mobile_gpu_device_loss_browser.mjs <runtime.mjs> <manifest.json>；manifest 给 distDir、photos（jpeg、jpeg2、raw）、out，可选 shots。
// 做法同 Sol 的探针：拿到引擎请求的 GPUDevice，调 destroy()，真的触发 GPUDevice.lost，再看页面落到哪个状态。
// 对照组是同一份构建、navigator.gpu 去掉的页面（没有 WebGPU）。每个场景一个新的浏览器上下文。
// 没有 WebGPU 适配器时报告 { skipped } 并以 0 退出。HTTP 服务只绑 127.0.0.1 临时端口（避开禁用端口）。

import fs from 'node:fs';
import http from 'node:http';
import path from 'node:path';
import { pathToFileURL } from 'node:url';

const [runtimePath, manifestPath] = process.argv.slice(2);
const manifest = JSON.parse(fs.readFileSync(manifestPath, 'utf8'));
const { jpeg, jpeg2, raw } = manifest.photos;
const idOf = (file) => path.basename(file).replace(/\.[^.]+$/, '').toLowerCase();
const ID = { jpeg: idOf(jpeg), jpeg2: idOf(jpeg2), raw: idOf(raw) };
const FILES = [jpeg, jpeg2, raw];

const FORBIDDEN_PORTS = new Set([60887, 18772, 18801, 18802, 18803, 5290]);
const CHROMIUM_ARGS = ['--enable-unsafe-webgpu', '--use-angle=metal'];
const MIME = { '.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8', '.css': 'text/css; charset=utf-8', '.wasm': 'application/wasm', '.zip': 'application/zip', '.ttf': 'font/ttf', '.txt': 'text/plain; charset=utf-8', '.svg': 'image/svg+xml' };
const PHONE = { viewport: { width: 390, height: 844 }, hasTouch: true, isMobile: true, deviceScaleFactor: 1 };
const LANDSCAPE = { viewport: { width: 844, height: 390 }, hasTouch: true, isMobile: true, deviceScaleFactor: 1 };
/** 设备丢了以后多久内必须落到相机 JPG（毫秒）。 */
const FALLBACK_MS = 2000;

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

/**
 * 页面里的钩子（addInitScript）：记下每个 GPUDevice（测试要 destroy 它）、requestAdapter 的次数、未处理的 Promise 拒绝和弹出的轻提示。
 * `block` 为真时 requestAdapter 抛错，等于 Engine.create() 失败。`noGpu` 为真时去掉 navigator.gpu（没有 WebGPU 的对照页）。
 */
function installHooks(noGpu) {
  const t = (window.__loss = { devices: [], adapterCalls: 0, block: false, rejections: [], toasts: [] });
  window.addEventListener('unhandledrejection', (event) => {
    const reason = event.reason;
    t.rejections.push(String(reason && reason.message ? reason.message : reason));
  });
  document.addEventListener('DOMContentLoaded', () => {
    new MutationObserver((records) => {
      for (const record of records) {
        for (const node of record.addedNodes) {
          if (node instanceof HTMLElement && node.classList.contains('ftoast')) t.toasts.push(node.textContent || '');
        }
      }
    }).observe(document.body, { childList: true, subtree: true });
  });
  if (noGpu) {
    Object.defineProperty(navigator, 'gpu', { value: undefined });
    return;
  }
  if (!navigator.gpu) return;
  const gpuProto = Object.getPrototypeOf(navigator.gpu);
  const requestAdapter = gpuProto.requestAdapter;
  gpuProto.requestAdapter = async function (...args) {
    t.adapterCalls += 1;
    if (t.block) throw new Error('test: Engine.create blocked');
    return requestAdapter.apply(this, args);
  };
  const requestDevice = GPUAdapter.prototype.requestDevice;
  GPUAdapter.prototype.requestDevice = async function (...args) {
    const device = await requestDevice.apply(this, args);
    t.devices.push(device);
    return device;
  };
}

const app = (page) => page.locator('.app');
const cell = (page, id) => page.locator(`.gscroll .cell[data-id="${id}"]`);

async function start(page, url) {
  await page.goto(url, { waitUntil: 'domcontentloaded' });
  await page.locator('.app[data-screen="start"][data-core="1"]').waitFor();
  await page.locator('input.filepick:not(.calibpick)').first().setInputFiles(FILES);
  await page.locator('.app[data-screen="grid"]').waitFor();
  for (const id of [ID.jpeg, ID.jpeg2]) await cell(page, id).locator('canvas[data-drawn]').waitFor();
  await cell(page, ID.raw).waitFor();
}

async function open(page, id) {
  await cell(page, id).click();
  await page.waitForFunction((want) => document.querySelector('.app')?.dataset.screen === 'editor' && document.querySelector('.screen.editor')?.dataset.photo?.toLowerCase() === want, id);
  await page.waitForFunction(() => document.querySelector('.app')?.dataset.settled !== '0', null, { timeout: 15000 });
  await settle(page);
}

async function back(page) {
  await page.locator('button.nav-back').click();
  await page.locator('.app[data-screen="grid"]').waitFor();
}

const settle = (page) => page.evaluate(() => new Promise((done) => requestAnimationFrame(() => requestAnimationFrame(() => setTimeout(done, 120)))));

/** 打开调整工具（手机竖屏底栏）。 */
async function openAdjust(page) {
  await page.locator('.editor .toolbar [data-tool="adjust"]').click();
  await page.locator('.panel.adjust').waitFor();
  await settle(page);
}

/** 弄丢设备：destroy() 页面拿到的每个 GPUDevice。`block` 为真时之后的 Engine.create() 一律失败。 */
async function loseDevice(page, block) {
  await page.evaluate((shouldBlock) => {
    const t = window.__loss;
    t.block = shouldBlock;
    t.adapterAtLoss = t.adapterCalls;
    for (const device of t.devices) device.destroy();
  }, block);
}

function snapshot(page) {
  return page.evaluate(() => {
    const shell = document.querySelector('.app');
    const visible = (node) => {
      if (!(node instanceof HTMLElement) || node.hidden) return false;
      const box = node.getBoundingClientRect();
      if (box.width <= 0 || box.height <= 0) return false;
      for (let at = node; at; at = at.parentElement) {
        const style = getComputedStyle(at);
        if (at.hidden || style.display === 'none' || style.visibility === 'hidden') return false;
      }
      return true;
    };
    const one = (sel) => document.querySelector(sel);
    const noPreview = one('.stage [data-state="no-preview"]');
    return {
      screen: shell?.dataset.screen ?? null,
      gpu: shell?.dataset.gpu ?? null,
      frame: shell?.dataset.frame ?? null,
      photo: one('.screen.editor')?.dataset.photo?.toLowerCase() ?? null,
      gpuVisible: visible(one('.stage canvas.gpu')),
      stillVisible: visible(one('.stage canvas.still')),
      noPreview: visible(noPreview) ? (noPreview.textContent || '').trim() : null,
      label: [...document.querySelectorAll('.editor .band-hd .lbl > span')].filter(visible).map((node) => (node.textContent || '').trim()),
      notices: [...document.querySelectorAll('.nogpu-notice, .lbl.nogpu, .adj-disabled-note')].filter(visible).map((node) => node.className).sort(),
      adapterCalls: window.__loss.adapterCalls,
      adapterSinceLoss: window.__loss.adapterCalls - (window.__loss.adapterAtLoss ?? window.__loss.adapterCalls),
    };
  });
}

/**
 * 编辑器里每个控件的状态：按（标签、class、data-*、文字）给键，同样的键按出现顺序编号。
 * 值是 disabled / aria-disabled / 看不看得见。和没有 WebGPU 的页面逐个比，不是手写清单。
 */
function controls(page) {
  return page.evaluate(() => {
    const visible = (node) => {
      if (!(node instanceof HTMLElement) || node.hidden) return false;
      const box = node.getBoundingClientRect();
      if (box.width <= 0 || box.height <= 0) return false;
      for (let at = node; at; at = at.parentElement) {
        const style = getComputedStyle(at);
        if (at.hidden || style.display === 'none' || style.visibility === 'hidden') return false;
      }
      return true;
    };
    const out = {};
    const seen = new Map();
    const sel = 'button, input, select, textarea, [role="button"], [role="slider"], [role="switch"], [role="tab"]';
    for (const node of document.querySelectorAll(`.app ${sel}`)) {
      if (node.matches('input[type="file"]')) continue;
      const data = Object.entries(node.dataset).filter(([key]) => ['act', 'key', 'tool', 'panel', 'sub', 'card'].includes(key)).map(([key, value]) => `${key}=${value}`).join(',');
      const base = `${node.tagName.toLowerCase()}.${[...node.classList].filter((c) => !['on', 'edited', 'active', 'sel'].includes(c)).sort().join('.')}|${data}|${(node.getAttribute('aria-label') || node.textContent || '').trim().slice(0, 16)}`;
      const n = (seen.get(base) ?? 0) + 1;
      seen.set(base, n);
      out[`${base}#${n}`] = {
        disabled: Boolean(node.disabled),
        ariaDisabled: node.getAttribute('aria-disabled'),
        visible: visible(node),
        opacity: visible(node) ? getComputedStyle(node).opacity : null,
      };
    }
    return out;
  });
}

/** 「改过」的痕迹：带 edited 的元素、调整的数值。动过曝光滑块以后拿它和对照页比，并且必须没改。 */
function edits(page) {
  return page.evaluate(() => ({
    edited: [...document.querySelectorAll('.app .edited')].map((node) => `${node.tagName.toLowerCase()}|${node.dataset.key || node.dataset.tool || node.className}`).sort(),
    value: document.querySelector('.band-adjust-hd .val')?.textContent ?? null,
    exposure: document.querySelector('.panel.adjust input[type="range"]')?.value ?? null,
  }));
}

/** 用户的手势拖曝光滑块：鼠标按在滑块中间、拖到右边 90% 再松开，然后按几下方向键。禁用的滑块两样都不起作用。 */
async function dragExposure(page) {
  const slider = page.locator('.panel.adjust input[type="range"]');
  const box = await slider.boundingBox();
  if (box) {
    await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
    await page.mouse.down();
    await page.mouse.move(box.x + box.width * 0.9, box.y + box.height / 2, { steps: 6 });
    await page.mouse.up();
  }
  await slider.focus().catch(() => undefined);
  for (let i = 0; i < 5; i++) await page.keyboard.press('ArrowRight');
  await settle(page);
}

/** 只在 manifest.shots 有值时存整页截图（给人看）。 */
async function shot(page, name) {
  if (manifest.shots) await page.screenshot({ path: path.join(manifest.shots, `page-${name}.png`) });
}

/** 舞台中心 24×24 的平均颜色（截图解码，GPU 画布和相机 JPG 画布都一样取）。 */
async function centre(page, name) {
  const target = page.locator('.stage .pv');
  const png = await target.screenshot();
  if (manifest.shots) fs.writeFileSync(path.join(manifest.shots, `${name}.png`), png);
  return page.evaluate(async (b64) => {
    const blob = await (await fetch(`data:image/png;base64,${b64}`)).blob();
    const bitmap = await createImageBitmap(blob);
    const canvas = new OffscreenCanvas(bitmap.width, bitmap.height);
    const ctx = canvas.getContext('2d');
    ctx.drawImage(bitmap, 0, 0);
    const x = Math.max(0, Math.floor(bitmap.width / 2) - 12);
    const y = Math.max(0, Math.floor(bitmap.height / 2) - 12);
    const data = ctx.getImageData(x, y, 24, 24).data;
    const sum = [0, 0, 0];
    for (let i = 0; i < data.length; i += 4) for (let c = 0; c < 3; c++) sum[c] += data[i + c];
    const n = data.length / 4;
    bitmap.close();
    return sum.map((v) => Math.round(v / n));
  }, png.toString('base64'));
}

/** 等到落回相机 JPG（最多 FALLBACK_MS）；返回用了多久，没等到是 null。 */
async function waitFallback(page) {
  const t0 = Date.now();
  try {
    await page.waitForFunction(
      () => {
        const shell = document.querySelector('.app');
        const still = document.querySelector('.stage canvas.still');
        const gpu = document.querySelector('.stage canvas.gpu');
        return shell?.dataset.gpu === '0' && shell.dataset.frame === undefined && still && !still.hidden && (!gpu || gpu.hidden);
      },
      null,
      { timeout: FALLBACK_MS },
    );
    return Date.now() - t0;
  } catch {
    return null;
  }
}

async function newContext(browser, opts, noGpu) {
  const context = await browser.newContext(opts);
  await context.addInitScript(installHooks, noGpu);
  const page = await context.newPage();
  page.setDefaultTimeout(15000);
  const pageErrors = [];
  page.on('pageerror', (err) => pageErrors.push(String(err.message || err).slice(0, 200)));
  return { context, page, pageErrors };
}

async function finish(run, row) {
  row.pageErrors = run.pageErrors;
  row.rejections = await run.page.evaluate(() => window.__loss.rejections).catch(() => ['<page gone>']);
  row.toasts = await run.page.evaluate(() => window.__loss.toasts).catch(() => []);
  await run.context.close();
  return row;
}

/** 没有 WebGPU 的对照页：同一张照片、同一个工具下的控件、提示和拖滑块以后的状态。 */
async function reference(browser, url, opts) {
  const run = await newContext(browser, opts, true);
  const { page } = run;
  await start(page, url);
  await open(page, ID.jpeg);
  const row = { opened: await snapshot(page) };
  await openAdjust(page);
  row.adjust = await snapshot(page);
  row.controls = await controls(page);
  await shot(page, 'reference-adjust');
  await dragExposure(page);
  row.afterDrag = await edits(page);
  row.controlsAfterDrag = await controls(page);
  await back(page);
  await open(page, ID.raw);
  row.raw = await snapshot(page);
  await shot(page, 'reference-raw');
  return finish(run, row);
}

/** 编辑中丢了：落回 JPG，控件和对照页一样，拖曝光不留「改过」；回图库开另一张、再开只有 RAW 的。 */
async function lostWhileEditing(browser, url, opts) {
  const run = await newContext(browser, opts, false);
  const { page } = run;
  await start(page, url);
  await open(page, ID.jpeg);
  await page.locator('.app[data-gpu="1"][data-frame="1"]').waitFor();
  const row = { before: await snapshot(page), beforeRgb: await centre(page, 'editing-before') };
  await loseDevice(page, true);
  row.fallbackMs = await waitFallback(page);
  await settle(page);
  row.after = await snapshot(page);
  row.afterRgb = await centre(page, 'editing-after');
  await openAdjust(page);
  row.adjust = await snapshot(page);
  row.controls = await controls(page);
  await shot(page, 'editing-adjust');
  await dragExposure(page);
  row.afterDrag = await edits(page);
  row.controlsAfterDrag = await controls(page);
  await back(page);
  await open(page, ID.jpeg2);
  row.next = await snapshot(page);
  row.nextRgb = await centre(page, 'editing-next');
  await back(page);
  await open(page, ID.raw);
  row.raw = await snapshot(page);
  row.rawRgb = await centre(page, 'editing-raw');
  await shot(page, 'editing-raw');
  return finish(run, row);
}

/** 两张照片之间丢了：先用显卡开一张，回图库，在图库上丢设备，再开另一张。 */
async function lostBetweenPhotos(browser, url, opts) {
  const run = await newContext(browser, opts, false);
  const { page } = run;
  await start(page, url);
  await open(page, ID.jpeg);
  await page.locator('.app[data-gpu="1"][data-frame="1"]').waitFor();
  await back(page);
  await loseDevice(page, true);
  await page.waitForFunction(() => document.querySelector('.app')?.dataset.gpu === '0', null, { timeout: FALLBACK_MS }).catch(() => undefined);
  const row = { grid: await snapshot(page) };
  await open(page, ID.jpeg2);
  row.next = await snapshot(page);
  row.nextRgb = await centre(page, 'between-next');
  return finish(run, row);
}

/** 第一次点开之前就丢了：第一次打开就是相机 JPG。 */
async function lostBeforeFirstTap(browser, url, opts) {
  const run = await newContext(browser, opts, false);
  const { page } = run;
  await start(page, url);
  await page.locator('.app[data-gpu="1"]').waitFor();
  await page.waitForFunction(() => window.__loss.devices.length > 0);
  await loseDevice(page, true);
  await page.waitForFunction(() => document.querySelector('.app')?.dataset.gpu === '0', null, { timeout: FALLBACK_MS }).catch(() => undefined);
  const row = { grid: await snapshot(page) };
  await open(page, ID.jpeg);
  row.first = await snapshot(page);
  row.firstRgb = await centre(page, 'before-tap-first');
  return finish(run, row);
}

/** 手机横屏（有显卡提示条的版式）编辑中丢了：提示条出现，和对照页一样。 */
async function lostLandscape(browser, url) {
  const run = await newContext(browser, LANDSCAPE, false);
  const { page } = run;
  await start(page, url);
  await open(page, ID.jpeg);
  await page.locator('.app[data-gpu="1"][data-frame="1"]').waitFor();
  await loseDevice(page, true);
  const row = { fallbackMs: await waitFallback(page) };
  await settle(page);
  row.after = await snapshot(page);
  row.controls = await controls(page);
  return finish(run, row);
}

async function referenceLandscape(browser, url) {
  const run = await newContext(browser, LANDSCAPE, true);
  const { page } = run;
  await start(page, url);
  await open(page, ID.jpeg);
  const row = { opened: await snapshot(page), controls: await controls(page) };
  await shot(page, 'reference-landscape');
  return finish(run, row);
}

/** 恢复：丢了以后下一次打开照片再建一次引擎，建好了就回到显卡路径。 */
async function recovery(browser, url, opts) {
  const run = await newContext(browser, opts, false);
  const { page } = run;
  await start(page, url);
  await open(page, ID.jpeg);
  await page.locator('.app[data-gpu="1"][data-frame="1"]').waitFor();
  await loseDevice(page, false);
  const row = { fallbackMs: await waitFallback(page) };
  await back(page);
  await open(page, ID.jpeg2);
  await page.locator('.app[data-gpu="1"][data-frame="1"]').waitFor({ timeout: 5000 }).catch(() => undefined);
  await settle(page);
  row.next = await snapshot(page);
  row.nextRgb = await centre(page, 'recovery-next');
  await back(page);
  await open(page, ID.jpeg);
  row.again = await snapshot(page);
  row.againRgb = await centre(page, 'recovery-again');
  return finish(run, row);
}

/** 恢复失败：Engine.create() 一直失败，打开 3 次以后不再试，每次都是相机 JPG。 */
async function retryLimit(browser, url, opts) {
  const run = await newContext(browser, opts, false);
  const { page } = run;
  await start(page, url);
  await open(page, ID.jpeg);
  await page.locator('.app[data-gpu="1"][data-frame="1"]').waitFor();
  await loseDevice(page, true);
  const row = { fallbackMs: await waitFallback(page), opens: [] };
  const order = [ID.jpeg2, ID.jpeg, ID.jpeg2, ID.jpeg, ID.jpeg2];
  for (const [index, id] of order.entries()) {
    await back(page);
    if (index === order.length - 1) await page.evaluate(() => { window.__loss.block = false; });
    await open(page, id);
    row.opens.push(await snapshot(page));
  }
  row.lastRgb = await centre(page, 'retry-last');
  return finish(run, row);
}

const SCENARIOS = {
  reference: (browser, url) => reference(browser, url, PHONE),
  lostWhileEditing: (browser, url) => lostWhileEditing(browser, url, PHONE),
  lostBetweenPhotos: (browser, url) => lostBetweenPhotos(browser, url, PHONE),
  lostBeforeFirstTap: (browser, url) => lostBeforeFirstTap(browser, url, PHONE),
  referenceLandscape: (browser, url) => referenceLandscape(browser, url),
  lostLandscape: (browser, url) => lostLandscape(browser, url),
  recovery: (browser, url) => recovery(browser, url, PHONE),
  retryLimit: (browser, url) => retryLimit(browser, url, PHONE),
};

async function main() {
  const { launchPinnedChromium } = await import(pathToFileURL(runtimePath).href);
  const server = await startServer(manifest.distDir);
  const browser = await launchPinnedChromium({ args: CHROMIUM_ARGS });
  const report = { scenarios: {} };
  try {
    const probe = await browser.newPage();
    await probe.goto(server.url, { waitUntil: 'domcontentloaded' });
    report.adapter = await probe.evaluate(async () => {
      if (!navigator.gpu) return null;
      const adapter = await navigator.gpu.requestAdapter({ powerPreference: 'high-performance' });
      return adapter ? { vendor: adapter.info.vendor, architecture: adapter.info.architecture, fallback: Boolean(adapter.info.isFallbackAdapter) } : null;
    });
    await probe.close();
    if (!report.adapter) {
      report.skipped = 'no WebGPU adapter in pinned Chromium';
      return;
    }
    for (const name of manifest.scenarios ?? Object.keys(SCENARIOS)) {
      try {
        report.scenarios[name] = await SCENARIOS[name](browser, server.url);
      } catch (err) {
        report.scenarios[name] = { error: String(err && err.stack ? err.stack : err).slice(0, 1200) };
      }
    }
  } finally {
    fs.writeFileSync(manifest.out, JSON.stringify(report, null, 2));
    await browser.close();
    await server.close();
  }
}

await main();
