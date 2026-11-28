// 27 寸大屏版式检查驱动（2026-11-28，生产 mobile/dist，Chromium Metal）。
// 用法：node mobile_big_screen_browser.mjs <runtime.mjs> <manifest.json>
// manifest：distDir、files（照片 + 相机配方）、stem（打开的那张）、cases（[宽, 高, 设备像素比]）、out；
// 可选 heap：{ files, sizes }，大图库只量内存峰值（不在 pytest 里跑）。
// 电脑：没有触摸点、指针精确。只量盒子、字号和位图尺寸；输出只写脱敏 JSON，不写照片名、拍摄时间或 EXIF。
// HTTP 服务只绑 127.0.0.1 临时端口（避开禁用端口）。

import fs from 'node:fs';
import http from 'node:http';
import path from 'node:path';
import { pathToFileURL } from 'node:url';

const [runtimePath, manifestPath] = process.argv.slice(2);
const manifest = JSON.parse(fs.readFileSync(manifestPath, 'utf8'));
const { launchPinnedChromium, verifyPinnedRuntime } = await import(pathToFileURL(runtimePath).href);
verifyPinnedRuntime();

const FORBIDDEN_PORTS = new Set([60887, 18772, 18801, 18802, 18803, 5290]);
const MIME = { '.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8', '.css': 'text/css; charset=utf-8', '.wasm': 'application/wasm', '.zip': 'application/zip', '.ttf': 'font/ttf', '.txt': 'text/plain; charset=utf-8', '.svg': 'image/svg+xml', '.json': 'application/json', '.png': 'image/png' };
const METAL_ARGS = ['--enable-unsafe-webgpu', '--enable-gpu', '--ignore-gpu-blocklist', '--use-angle=metal', '--enable-precise-memory-info'];
const STEM = String(manifest.stem || '').toLowerCase();
const PREDICTED = '电脑预测';

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

const settle = (page, ms = 150) => page.evaluate((wait) => new Promise((done) => requestAnimationFrame(() => requestAnimationFrame(() => setTimeout(done, wait)))), ms);

async function newPage(browser, width, height, dpr) {
  const ctx = await browser.newContext({ viewport: { width, height }, deviceScaleFactor: dpr, hasTouch: false, isMobile: false, locale: 'zh-CN' });
  const page = await ctx.newPage();
  const errors = [];
  page.on('pageerror', (err) => errors.push(String(err.message).slice(0, 200)));
  return { ctx, page, errors };
}

async function load(page, url, files, timeout = 120_000) {
  await page.goto(url, { waitUntil: 'domcontentloaded' });
  await page.locator('.app[data-screen="start"][data-core="1"]').waitFor({ timeout: 60_000 });
  await page.locator('input.filepick:not(.calibpick)').first().setInputFiles(files);
  await page.locator('.app[data-screen="grid"]').waitFor({ timeout: 30_000 });
  await page.waitForFunction(() => !document.querySelector('.nav small')?.textContent?.includes('正在读取'), null, { timeout });
  await settle(page, 300);
}

/** 文字没被放大：字号原样、变换里没有缩放。 */
const TEXT_PROBE = `(node) => {
  if (!node) return null;
  const cs = getComputedStyle(node);
  const r = node.getBoundingClientRect();
  const m = cs.transform === 'none' ? [1, 0, 0, 1] : cs.transform.replace(/^matrix\\(|\\)$/g, '').split(',').map(Number);
  return { font: parseFloat(cs.fontSize), h: Math.round(r.height * 10) / 10, scaleX: m[0], scaleY: m[3], zoom: cs.zoom || '1' };
}`;

function measureGrid(page) {
  return page.evaluate((probeSrc) => {
    const probe = (0, eval)(probeSrc);
    const vis = (n) => n.getClientRects().length > 0;
    const scroller = document.querySelector('.gscroll');
    const inner = scroller?.querySelector('.inner');
    const cells = [...document.querySelectorAll('.gscroll .cell')].filter(vis);
    const thumbs = [];
    for (const cell of cells) {
      const canvas = cell.querySelector('canvas');
      if (!canvas || canvas.width <= 0) continue;
      const r = canvas.getBoundingClientRect();
      thumbs.push({ cw: canvas.width, ch: canvas.height, bw: Math.round(r.width * 10) / 10, bh: Math.round(r.height * 10) / 10 });
    }
    const first = cells[0]?.getBoundingClientRect();
    const doc = document.documentElement;
    return {
      layout: document.querySelector('.app')?.dataset.layout ?? '',
      big: matchMedia('(min-width: 2000px) and (min-height: 1100px)').matches,
      cols: Number(inner ? getComputedStyle(inner).getPropertyValue('--grid-cols') : 0),
      scrollerW: scroller?.clientWidth ?? 0,
      cellW: first ? Math.round(first.width * 10) / 10 : 0,
      cellH: first ? Math.round(first.height * 10) / 10 : 0,
      thumbs,
      badge: probe(document.querySelector('.gscroll .cell .bdg')),
      pageScroll: doc.scrollWidth > doc.clientWidth + 1 || doc.scrollHeight > doc.clientHeight + 1,
      gridOverflowX: scroller ? scroller.scrollWidth > scroller.clientWidth + 1 : true,
    };
  }, TEXT_PROBE);
}

/** 编辑器：右边栏宽、配方卡小图位图对显示尺寸，整页和边栏都不横向溢出、什么都不出窗口。 */
function measureEditor(page) {
  return page.evaluate((want) => {
    const vis = (n) => n.getClientRects().length > 0;
    const band = document.querySelector('.screen.editor > .band');
    const b = band?.getBoundingClientRect();
    const cards = [];
    for (const th of document.querySelectorAll('.ipad-strip-host .rc .th')) {
      const canvas = th.querySelector('canvas');
      if (!vis(th) || !canvas || th.dataset.source !== want) continue;
      const r = canvas.getBoundingClientRect();
      cards.push({ cw: canvas.width, ch: canvas.height, bw: Math.round(r.width * 10) / 10, bh: Math.round(r.height * 10) / 10 });
    }
    const outside = [];
    for (const node of document.querySelectorAll('.screen.editor > *')) {
      if (!vis(node)) continue;
      const r = node.getBoundingClientRect();
      if (r.left < -1 || r.top < -1 || r.right > innerWidth + 1 || r.bottom > innerHeight + 1) outside.push(node.className.split(' ')[0]);
    }
    const doc = document.documentElement;
    return {
      layout: document.querySelector('.app')?.dataset.layout ?? '',
      panelW: b ? Math.round(b.width * 10) / 10 : 0,
      cards,
      outside,
      bandOverflowX: band ? band.scrollWidth > band.clientWidth + 1 : true,
      pageScroll: doc.scrollWidth > doc.clientWidth + 1 || doc.scrollHeight > doc.clientHeight + 1,
    };
  }, PREDICTED);
}

function measureRuler(page) {
  return page.evaluate((probeSrc) => {
    const probe = (0, eval)(probeSrc);
    const ruler = [...document.querySelectorAll('.editor .ruler')].find((n) => n.getClientRects().length > 0);
    const label = ruler ? [...ruler.querySelectorAll('.tnum')].find((n) => n.textContent === '0°') : null;
    const band = document.querySelector('.screen.editor > .band');
    return { label: probe(label), bandOverflowX: band ? band.scrollWidth > band.clientWidth + 1 : true };
  }, TEXT_PROBE);
}

async function caseRow(browser, url, [width, height, dpr]) {
  const { ctx, page, errors } = await newPage(browser, width, height, dpr);
  try {
    await load(page, url, manifest.files);
    const grid = await measureGrid(page);
    await page.locator(`.gscroll .cell[data-id="${STEM}"]`).click();
    await page.waitForFunction((want) => document.querySelector('.screen.editor')?.dataset.photo?.toLowerCase() === want, STEM, { timeout: 30_000 });
    await page.waitForFunction(() => document.querySelector('.app')?.dataset.settled !== '0', null, { timeout: 60_000 });
    await page.waitForFunction(
      (want) => [...document.querySelectorAll('.ipad-strip-host .rc .th')].filter((n) => n.getClientRects().length > 0 && n.dataset.source === want && n.querySelector('canvas')).length >= 3,
      PREDICTED,
      { timeout: 120_000 },
    );
    await settle(page, 300);
    const editor = await measureEditor(page);
    await page.locator('.editor .toolbar .tbtn[data-tool="crop"]').first().click();
    await page.locator('.editor .ruler:visible').first().waitFor({ timeout: 10_000 });
    await settle(page, 400);
    const ruler = await measureRuler(page);
    return { size: `${width}x${height}@${dpr}`, dpr, grid, editor, ruler, errors };
  } finally {
    await ctx.close();
  }
}

/** 1440×900 读好照片以后把窗口拉到 2560×1440（跨过大屏线）：看得见的格子换成按大格子重解的位图（最多等 30 秒）。 */
async function resizeRow(browser, url, files, [width, height], [bigW, bigH], dpr) {
  const { ctx, page, errors } = await newPage(browser, width, height, dpr);
  try {
    await load(page, url, files);
    const before = await measureGrid(page);
    await page.setViewportSize({ width: bigW, height: bigH });
    const sharp = () => {
      const cells = [...document.querySelectorAll('.gscroll .cell canvas')].filter((n) => n.getClientRects().length > 0 && n.width > 0);
      return cells.length > 0 && cells.every((n) => {
        const r = n.getBoundingClientRect();
        return n.width >= r.width * devicePixelRatio - 0.5 && n.height >= r.height * devicePixelRatio - 0.5;
      });
    };
    const upsized = await page.waitForFunction(sharp, null, { timeout: 30_000 }).then(() => true, () => false);
    await settle(page, 300);
    const after = await measureGrid(page);
    return { size: `${width}x${height}->${bigW}x${bigH}@${dpr}`, dpr, upsized, before, after, errors };
  } finally {
    await ctx.close();
  }
}

/** 大图库：读完以后 JS 堆的峰值，和画在格子里的缩略图位图尺寸（每张照片一张同样大小的位图）。 */
async function heapRow(browser, url, files, [width, height, dpr]) {
  const { ctx, page, errors } = await newPage(browser, width, height, dpr);
  const cdp = await ctx.newCDPSession(page);
  await cdp.send('Performance.enable');
  let peak = 0;
  let stop = false;
  const sample = async () => {
    while (!stop) {
      const { metrics } = await cdp.send('Performance.getMetrics').catch(() => ({ metrics: [] }));
      const used = metrics.find((m) => m.name === 'JSHeapUsedSize')?.value ?? 0;
      peak = Math.max(peak, used);
      await new Promise((done) => setTimeout(done, 100));
    }
  };
  try {
    const t0 = Date.now();
    const sampler = sample();
    await load(page, url, files, 600_000);
    const readS = (Date.now() - t0) / 1000;
    await settle(page, 1_000);
    stop = true;
    await sampler;
    const grid = await measureGrid(page);
    const per = grid.thumbs.length ? grid.thumbs[0].cw * grid.thumbs[0].ch * 4 : 0;
    // 一屏一屏滚到底，按格子记下每张照片的缩略图位图：大屏上超过字节上限以后读的照片照旧是 360 宽。
    const seen = await page.evaluate(async () => {
      const scroller = document.querySelector('.gscroll');
      const out = {};
      if (!scroller) return out;
      for (let top = 0; ; top += scroller.clientHeight) {
        scroller.scrollTop = top;
        await new Promise((done) => requestAnimationFrame(() => requestAnimationFrame(() => setTimeout(done, 150))));
        for (const cell of scroller.querySelectorAll('.cell')) {
          const canvas = cell.querySelector('canvas');
          if (canvas && canvas.width > 0) out[cell.dataset.id] = [canvas.width, canvas.height];
        }
        if (top + scroller.clientHeight >= scroller.scrollHeight) break;
      }
      return out;
    });
    const dims = Object.values(seen);
    const widths = {};
    for (const [w] of dims) widths[w] = (widths[w] ?? 0) + 1;
    const bitmapMiB = Math.round((dims.reduce((sum, [w, h]) => sum + w * h * 4, 0) / 2 ** 20) * 10) / 10;
    const bigMiB = Math.round((dims.filter(([w]) => w > 360).reduce((sum, [w, h]) => sum + w * h * 4, 0) / 2 ** 20) * 10) / 10;
    return { size: `${width}x${height}@${dpr}`, photos: files.length, readS, peakHeapMiB: Math.round((peak / 2 ** 20) * 10) / 10, firstThumb: grid.thumbs[0] ?? null, thumbsSeen: dims.length, widths, bitmapMiB, bigMiB, perThumbKiB: Math.round(per / 1024), cols: grid.cols, errors };
  } finally {
    stop = true;
    await ctx.close();
  }
}

const report = {};
const server = await startServer(manifest.distDir);
const browser = await launchPinnedChromium({ args: METAL_ARGS });
try {
  if (manifest.heap) {
    report.heap = [];
    for (const size of manifest.heap.sizes) report.heap.push(await heapRow(browser, server.url, manifest.heap.files, size));
  } else {
    report.rows = [];
    for (const size of manifest.cases) {
      try {
        report.rows.push(await caseRow(browser, server.url, size));
      } catch (err) {
        report.rows.push({ size: size.join('x'), error: String(err?.message ?? err).replace(/\/(?:Volumes|Users|private|var)\/\S+/g, '[path]').slice(0, 600) });
      }
    }
    if (manifest.resize) {
      const { from, to, dpr, files } = manifest.resize;
      try {
        report.resize = await resizeRow(browser, server.url, files, from, to, dpr);
      } catch (err) {
        report.resize = { error: String(err?.message ?? err).replace(/\/(?:Volumes|Users|private|var)\/\S+/g, '[path]').slice(0, 600) };
      }
    }
  }
} finally {
  await browser.close();
  await server.close();
}
fs.writeFileSync(manifest.out, JSON.stringify(report));
