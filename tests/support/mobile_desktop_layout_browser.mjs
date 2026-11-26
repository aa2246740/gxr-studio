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

/** 看得见、但被不能滚动的祖先或视口裁掉的控件（能滚到的不算）；页面本身不该有滚动。只写类名和可见文字，不写照片信息。 */
function measureClip(page) {
  return page.evaluate(() => {
    const vw = innerWidth;
    const vh = innerHeight;
    const vis = (n) => {
      const r = n.getBoundingClientRect();
      const cs = getComputedStyle(n);
      return r.width > 0 && r.height > 0 && cs.visibility !== 'hidden' && Number(cs.opacity) > 0.05 && !n.closest('.sr-only, [hidden]');
    };
    const desc = (n) => `${n.tagName.toLowerCase()}.${String(n.className || '').trim().split(/\s+/).slice(0, 2).join('.')}${n.dataset.tool ? `[${n.dataset.tool}]` : ''}`;
    const controls = [...document.querySelectorAll('.app button, .app input:not([type="file"]), .app [role="slider"], .app [role="tab"], .app [tabindex="0"]')].filter(vis);
    const clipped = [];
    for (const n of controls) {
      const r = n.getBoundingClientRect();
      let box = { l: 0, t: 0, r: vw, b: vh };
      let scrollX = false;
      let scrollY = false;
      for (let p = n.parentElement; p && p !== document.body; p = p.parentElement) {
        const cs = getComputedStyle(p);
        const pr = p.getBoundingClientRect();
        if (/(auto|scroll)/.test(cs.overflowX)) scrollX = true;
        else if (cs.overflowX !== 'visible' && !scrollX) box = { ...box, l: Math.max(box.l, pr.left), r: Math.min(box.r, pr.right) };
        if (/(auto|scroll)/.test(cs.overflowY)) scrollY = true;
        else if (cs.overflowY !== 'visible' && !scrollY) box = { ...box, t: Math.max(box.t, pr.top), b: Math.min(box.b, pr.bottom) };
      }
      const outX = !scrollX && (r.left < box.l - 1 || r.right > box.r + 1);
      const outY = !scrollY && (r.top < box.t - 1 || r.bottom > box.b + 1);
      if (outX || outY) clipped.push(`${desc(n)} ${(n.getAttribute('aria-label') || n.textContent || '').trim().slice(0, 12)}`);
    }
    const strip = (sel) => {
      const n = document.querySelector(sel);
      if (!n || !vis(n)) return null;
      const r = n.getBoundingClientRect();
      return { x: Math.round(r.left), y: Math.round(r.top), w: Math.round(r.width), h: Math.round(r.height), inside: r.left >= -1 && r.top >= -1 && r.right <= vw + 1 && r.bottom <= vh + 1 };
    };
    const film = document.querySelector('.editor .ipad-filmstrip');
    const filmCells = film && vis(film) ? [...film.querySelectorAll('.cell')].filter(vis).map((c) => Math.round(c.getBoundingClientRect().height)) : [];
    return {
      layout: document.querySelector('.app')?.dataset.layout ?? '',
      screen: document.querySelector('.app')?.dataset.screen ?? '',
      pageScroll: document.documentElement.scrollWidth > vw + 1 || document.documentElement.scrollHeight > vh + 1,
      clipped: clipped.slice(0, 12),
      recipeStrip: strip('.editor .ipad-recipe-strip'),
      filmstrip: strip('.editor .ipad-filmstrip'),
      filmCells,
      // 侧边面板不用滚动就能看到的完整控件数，以及舞台的高度：太矮时面板只剩直方图、照片很小。
      panel: (() => {
        const body = document.querySelector('.editor .band .band-body');
        if (!body || !vis(body)) return null;
        const br = body.getBoundingClientRect();
        const inView = [...body.querySelectorAll('button, input[type="range"], [role="slider"]')].filter(vis).filter((n) => {
          const r = n.getBoundingClientRect();
          return r.top >= br.top - 1 && r.bottom <= br.bottom + 1;
        }).length;
        return { h: Math.round(br.height), scrollH: body.scrollHeight, inView };
      })(),
      stageH: Math.round(document.querySelector('.editor .stage')?.getBoundingClientRect().height ?? 0),
      gridCols: Number(document.querySelector('.gscroll [style*="--grid-cols"]')?.style.getPropertyValue('--grid-cols') || 0),
    };
  });
}

async function closeSheets(page) {
  for (const sel of ['.ms.export [data-act="close"]', '.ms.export .nb', '.ms .hd .nb']) {
    const btn = page.locator(sel).first();
    if (await btn.isVisible().catch(() => false)) {
      await btn.click();
      await settle(page, 300);
      return;
    }
  }
}

/** L2：每个尺寸量网格、编辑器（配方、调整、裁剪、导出选项）和胶片条；在胶片条上点第二张能换照片。 */
async function shortGroup(browser, url, sizes) {
  const rows = [];
  const { ctx, page, errors } = await newPage(browser, 'metal', 'computer', { width: sizes[0][0], height: sizes[0][1] });
  try {
    await start(page, url);
    for (const [width, height] of sizes) {
      const size = `${width}x${height}`;
      await resize(page, { width, height });
      const row = { size, grid: await measureClip(page) };
      if (manifest.shotsDir && manifest.shotSizes?.includes(size)) await shot(page, `short-${size}-grid`);
      await open(page, 'metal');
      for (const id of ['recipe', 'adjust', 'crop']) {
        await tool(page, id);
        row[id] = await measureClip(page);
        if (manifest.shotsDir && manifest.shotSizes?.includes(size)) await shot(page, `short-${size}-${id}`);
      }
      await tool(page, 'recipe');
      await page.locator('.editor .nav [data-act="editor-more"]').click();
      await page.locator('[data-act="more-export-options"]').click();
      await page.locator('.ms.export').first().waitFor({ timeout: 10_000 });
      await settle(page, 300);
      row.exportOptions = await measureClip(page);
      await closeSheets(page);
      const filmCells = page.locator('.editor .ipad-filmstrip .cell');
      row.filmSwitch = null;
      if ((await filmCells.count()) >= 2 && (await filmCells.nth(1).isVisible())) {
        const before = await page.evaluate(() => document.querySelector('.screen.editor')?.dataset.photo ?? '');
        await filmCells.nth(1).click();
        row.filmSwitch = await page
          .waitForFunction((was) => (document.querySelector('.screen.editor')?.dataset.photo ?? '') !== was, before, { timeout: 15_000 })
          .then(() => true, () => false);
        await settle(page, 400);
      }
      await page.locator('button.nav-back').click();
      await page.locator('.app[data-screen="grid"]').waitFor({ timeout: 10_000 });
      rows.push(row);
    }
  } finally {
    await ctx.close();
  }
  return { rows, errors };
}

/** 只量版式名：电脑（没有触摸）和手机/iPad（有触摸）在给定尺寸上分到哪种版式。 */
async function classifyGroup(browser, url, kind, sizes) {
  const { ctx, page, errors } = await newPage(browser, 'metal', kind, { width: sizes[0][0], height: sizes[0][1] });
  const out = {};
  try {
    await page.goto(url, { waitUntil: 'domcontentloaded' });
    await page.locator('.app[data-core="1"]').waitFor({ timeout: 60_000 });
    for (const [width, height] of sizes) {
      await resize(page, { width, height });
      out[`${width}x${height}`] = await page.evaluate(() => document.querySelector('.app')?.dataset.layout ?? '');
    }
  } finally {
    await ctx.close();
  }
  return { layouts: out, errors };
}

/** L5：悬停前后比计算样式（底色、颜色、滤镜、阴影、透明度）和光标。sub 是真正变亮的子节点（配方卡的缩略图）。 */
const HOVER_TARGETS = {
  recipe: [
    ['tab', '.editor .toolbar .tbtn[data-tool="adjust"]'],
    ['download', '.editor .toolbar [data-act="export"]'],
    ['more', '.editor .nav [data-act="editor-more"]'],
    ['back', '.editor .nav .nav-back'],
    ['recipeCard', '.editor .ipad-strip-host .recipe-strip-wrap .rc.on + .rc', '.th'],
    ['stripSwitch', '.editor .ipad-recipe-strip .vseg-btn:not(.on)'],
    ['filmCell', '.editor .ipad-filmstrip .cell:not(.cur)'],
  ],
  adjust: [['chip', '.editor .panel.adjust .adjust-row .chip:not(.on)']],
  crop: [
    ['cornerTL', '.editor .crop-overlay .ch.corner.tl'],
    ['cornerTR', '.editor .crop-overlay .ch.corner.tr'],
    ['edgeT', '.editor .crop-overlay .ch.edge.t'],
    ['edgeL', '.editor .crop-overlay .ch.edge.l'],
    ['ruler', '.editor .ruler'],
  ],
};

async function hoverStyle(page, sel, sub) {
  const loc = page.locator(sel).first();
  if (!(await loc.isVisible().catch(() => false))) return null;
  const read = () =>
    loc.evaluate((node, subSel) => {
      const target = (subSel && node.querySelector(subSel)) || node;
      const cs = getComputedStyle(target);
      return { cursor: getComputedStyle(node).cursor, sig: [cs.backgroundColor, cs.color, cs.filter, cs.boxShadow, cs.opacity].join('|') };
    }, sub ?? null);
  await page.mouse.move(2, Math.round((await page.evaluate(() => innerHeight)) / 2));
  await settle(page, 80);
  const before = await read();
  const box = await loc.boundingBox();
  await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
  await settle(page, 80);
  const after = await read();
  const hit = await loc.evaluate((node, [x, y]) => {
    const top = document.elementFromPoint(x, y);
    return top === node || node.contains(top) ? 'self' : `${top?.tagName.toLowerCase()}.${String(top?.className || '').split(' ')[0]}`;
  }, [box.x + box.width / 2, box.y + box.height / 2]);
  await page.mouse.move(2, Math.round((await page.evaluate(() => innerHeight)) / 2));
  return { cursor: after.cursor, restCursor: before.cursor, changed: before.sig !== after.sig, hit, cls: await loc.getAttribute('class') };
}

async function hoverGroup(browser, url, kind, vp, engine = 'metal') {
  const { ctx, page, errors } = await newPage(browser, engine, kind, vp);
  const out = {};
  try {
    await start(page, url);
    out.media = await page.evaluate(() => ({ hover: matchMedia('(hover: hover)').matches, fine: matchMedia('(pointer: fine)').matches }));
    out.gridCellCursor = await page.locator('.gscroll .cell').first().evaluate((n) => getComputedStyle(n).cursor);
    await open(page, engine);
    for (const [toolId, targets] of Object.entries(HOVER_TARGETS)) {
      await tool(page, toolId);
      for (const [name, sel, sub] of targets) out[name] = await hoverStyle(page, sel, sub);
    }
  } finally {
    await ctx.close();
  }
  return { ...out, errors };
}

/** L7 / 曲线：没有显卡预览时配方微调、调整滑块和曲线入口一起停用，说同一句话；配方卡照样能选。 */
function readLocks(page) {
  return page.evaluate(() => {
    const vis = (n) => Boolean(n) && n.getClientRects().length > 0 && getComputedStyle(n).visibility !== 'hidden';
    const reason = (n) => {
      const id = n?.getAttribute('aria-describedby');
      return id ? (document.getElementById(id)?.textContent ?? '') : '';
    };
    const knobs = [...document.querySelectorAll('.editor .panel.recipe .knobs input[type="range"]')];
    const note = document.querySelector('.editor .panel.recipe .knobs .adj-disabled-note');
    return {
      knobCount: knobs.length,
      knobsDisabled: knobs.filter((n) => n.disabled).length,
      knobReasons: [...new Set(knobs.map(reason))],
      knobNote: note && vis(note) ? note.textContent : null,
      knobOpacity: knobs[0] ? getComputedStyle(knobs[0]).opacity : null,
    };
  });
}

async function lockGroup(browser, url, engine, kind, vp) {
  const { ctx, page, errors } = await newPage(browser, engine, kind, vp);
  const out = {};
  try {
    await start(page, url);
    await open(page, engine);
    const ipad = await page.evaluate(() => document.querySelector('.app')?.dataset.layout?.startsWith('ipad') ?? false);
    if (!ipad) {
      // 手机：再点一下选中的配方卡进微调。
      await page.locator('.editor .band .rc.on').first().click();
      await settle(page, 300);
    }
    out.recipe = await readLocks(page);
    out.recipeClip = await measureClip(page);
    out.knobGeom = await page.evaluate(() => {
      const r = (sel) => {
        const n = document.querySelector(sel);
        if (!n) return null;
        const b = n.getBoundingClientRect();
        return [Math.round(b.top), Math.round(b.height)];
      };
      return { knobs: r('.editor .panel.recipe .knobs'), row: r('.editor .panel.recipe .knobs .krow:not([hidden])'), input: r('.editor .panel.recipe .knobs .krow:not([hidden]) input'), fine: r('.editor .panel.recipe .recipe-fine') };
    });
    if (manifest.shotsDir) await shot(page, `lock-${engine}-${kind}-${vp.width}x${vp.height}-recipe`);
    const knob = page.locator('.editor .panel.recipe .knobs .krow:not([hidden]) input[type="range"]').first();
    if (await knob.isVisible().catch(() => false)) {
      const before = await knob.inputValue();
      const box = await knob.boundingBox();
      await page.mouse.click(box.x + box.width * 0.9, box.y + box.height / 2);
      await settle(page, 200);
      out.knobMoved = (await knob.inputValue()) !== before;
    }
    if (ipad) {
      const cardSel = '.editor .ipad-strip-host .recipe-strip-wrap .rc';
      const was = await page.locator(`${cardSel}.on .nm`).first().textContent();
      await page.locator(`${cardSel}.on + .rc`).first().click();
      await settle(page, 600);
      await dismissAsk(page);
      out.cardSwitched = (await page.locator(`${cardSel}.on .nm`).first().textContent()) !== was;
    }
    await tool(page, 'adjust');
    out.adjust = await page.evaluate(() => {
      const reason = (n) => (n?.getAttribute('aria-describedby') ? (document.getElementById(n.getAttribute('aria-describedby'))?.textContent ?? '') : '');
      const input = document.querySelector('.editor .panel.adjust .big input[type="range"]');
      const curves = document.querySelector('.editor .panel.adjust [data-act="open-curves"]');
      return { sliderDisabled: Boolean(input?.disabled), sliderReason: reason(input), curvesDisabled: Boolean(curves?.disabled), curvesReason: reason(curves) };
    });
    await tool(page, 'more');
    await page.locator('[data-more="curves"]:visible').first().waitFor({ timeout: 10_000 });
    out.moreCurves = await page.locator('[data-more="curves"]:visible').first().evaluate((n) => ({ disabled: n.disabled, text: n.textContent }));
    if (manifest.shotsDir) await shot(page, `lock-${engine}-${kind}-${vp.width}x${vp.height}-more`);
  } finally {
    await ctx.close();
  }
  return { ...out, errors };
}

/** L11：调整里每一项的滑块，无障碍名称等于看得见的名字，aria-valuetext 是看得见的数值（曝光带 EV）。 */
async function namesGroup(browser, url, kind, vp) {
  const { ctx, page, errors } = await newPage(browser, 'metal', kind, vp);
  const rows = [];
  try {
    await start(page, url);
    await open(page, 'metal');
    await tool(page, 'adjust');
    const chips = page.locator('.editor .panel.adjust .adjust-row .chip[data-key]');
    const n = await chips.count();
    for (let i = 0; i < n; i += 1) {
      await chips.nth(i).click();
      await settle(page, 100);
      const slider = page.locator('.editor .panel.adjust .big input[type="range"]');
      await slider.focus();
      await page.keyboard.press('ArrowRight');
      await settle(page, 100);
      const row = await page.evaluate(() => {
        const vis = (q) => [...document.querySelectorAll(q)].find((x) => x.getClientRects().length > 0)?.textContent?.trim() ?? null;
        const input = document.querySelector('.editor .panel.adjust .big input[type="range"]');
        return {
          key: input?.dataset.key ?? '',
          label: vis('.editor .adj-l1 b, .editor .band-adjust-hd .adj-lab'),
          value: vis('.editor .adj-l1 .val, .editor .band-adjust-hd .val'),
          ariaLabel: input?.getAttribute('aria-label') ?? null,
          valueText: input?.getAttribute('aria-valuetext') ?? null,
        };
      });
      row.roleCount = await page.getByRole('slider', { name: row.label ?? '', exact: true }).count();
      rows.push(row);
      await page.keyboard.press('ArrowLeft');
    }
  } finally {
    await ctx.close();
  }
  return { rows, errors };
}

/** L12：拉直尺上的度数，看得见的都整个落在尺子里；转几度后再看一次。 */
function readRuler(page) {
  return page.evaluate(() => {
    const ruler = [...document.querySelectorAll('.editor .ruler')].find((n) => n.getClientRects().length > 0);
    if (!ruler) return null;
    const rr = ruler.getBoundingClientRect();
    const cut = [];
    const shown = [];
    for (const label of ruler.querySelectorAll('.tnum')) {
      const r = label.getBoundingClientRect();
      if (getComputedStyle(label).visibility === 'hidden' || r.width === 0) continue;
      if (r.right <= rr.left || r.left >= rr.right) continue;
      shown.push(label.textContent);
      if (r.left < rr.left - 0.5 || r.right > rr.right + 0.5) cut.push(label.textContent);
    }
    return { width: Math.round(rr.width), shown, cut, angle: ruler.getAttribute('aria-valuenow') };
  });
}

async function rulerGroup(browser, url, kind, sizes) {
  const rows = [];
  const { ctx, page, errors } = await newPage(browser, 'metal', kind, { width: sizes[0][0], height: sizes[0][1] });
  try {
    await start(page, url);
    await open(page, 'metal');
    await tool(page, 'crop');
    for (const [width, height] of sizes) {
      await resize(page, { width, height });
      const size = `${width}x${height}`;
      const rest = await readRuler(page);
      if (manifest.shotsDir) await page.locator('.editor .ruler:visible').first().screenshot({ path: path.join(manifest.shotsDir, `${manifest.shotsPrefix || 'shot'}-ruler-${kind}-${size}.png`) });
      const ruler = page.locator('.editor .ruler:visible').first();
      await ruler.focus();
      for (let i = 0; i < 4; i += 1) await page.keyboard.press('ArrowRight');
      await settle(page, 200);
      const turned = await readRuler(page);
      for (let i = 0; i < 4; i += 1) await page.keyboard.press('ArrowLeft');
      await settle(page, 200);
      rows.push({ size, rest, turned });
    }
  } finally {
    await ctx.close();
  }
  return { rows, errors };
}

/** L14：首屏上按钮和上面内容之间的空档，以及最后一行离首屏底边多远。 */
async function startGroup(browser, url, kind, sizes) {
  const rows = [];
  const { ctx, page, errors } = await newPage(browser, 'metal', kind, { width: sizes[0][0], height: sizes[0][1] });
  try {
    await page.goto(url, { waitUntil: 'domcontentloaded' });
    await page.locator('.app[data-screen="start"][data-core="1"]').waitFor({ timeout: 60_000 });
    for (const [width, height] of sizes) {
      await resize(page, { width, height });
      const size = `${width}x${height}`;
      const row = await page.evaluate(() => {
        const start = document.querySelector('.start');
        const cta = start?.querySelector('.cta');
        const prev = cta?.previousElementSibling;
        const help = start?.querySelector(':scope > .help');
        if (!start || !cta || !prev || !help) return null;
        const s = start.getBoundingClientRect();
        const c = cta.getBoundingClientRect();
        return {
          layout: document.querySelector('.app')?.dataset.layout ?? '',
          gap: Math.round(c.top - prev.getBoundingClientRect().bottom),
          ctaTop: Math.round(c.top),
          bottomSpace: Math.round(s.bottom - help.getBoundingClientRect().bottom),
          topSpace: Math.round(start.querySelector('.hero').getBoundingClientRect().top - s.top),
          scrolls: start.scrollHeight > start.clientHeight + 1,
        };
      });
      if (manifest.shotsDir) await shot(page, `start-${kind}-${size}`);
      rows.push({ size, ...row });
    }
  } finally {
    await ctx.close();
  }
  return { rows, errors };
}

/** P2-6：竖向滚轮在配方条和（溢出时的）胶片条上变成左右滚；滚到头或不溢出时不拦；电脑上露细滚动条。 */
function wheelProbe(page, sel, deltaY) {
  return page.evaluate(
    ([q, dy]) => {
      const strip = [...document.querySelectorAll(q)].find((n) => n.getClientRects().length > 0);
      if (!strip) return null;
      const before = strip.scrollLeft;
      const ev = new WheelEvent('wheel', { deltaY: dy, bubbles: true, cancelable: true });
      (strip.querySelector('button') ?? strip).dispatchEvent(ev);
      return {
        overflow: strip.scrollWidth - strip.clientWidth,
        before: Math.round(before),
        after: Math.round(strip.scrollLeft),
        prevented: ev.defaultPrevented,
        scrollbar: getComputedStyle(strip).scrollbarWidth,
      };
    },
    [sel, deltaY],
  );
}

async function wheelGroup(browser, url, kind, vp, engine = 'metal') {
  const { ctx, page, errors } = await newPage(browser, engine, kind, vp);
  const out = {};
  try {
    await start(page, url);
    await open(page, engine);
    const strip = '.editor .ipad-strip-host .recipe-strip-wrap .strip';
    await page.evaluate((q) => {
      const n = document.querySelector(q);
      if (n) n.scrollLeft = 0;
    }, strip);
    out.recipeDown = await wheelProbe(page, strip, 240);
    out.recipeUpAtStart = await (async () => {
      await page.evaluate((q) => {
        const n = document.querySelector(q);
        if (n) n.scrollLeft = 0;
      }, strip);
      return wheelProbe(page, strip, -240);
    })();
    await page.evaluate((q) => {
      const n = document.querySelector(q);
      if (n) n.scrollLeft = n.scrollWidth;
    }, strip);
    out.recipeDownAtEnd = await wheelProbe(page, strip, 240);
    // 真鼠标滚轮：停在配方条上滚一下。
    await page.evaluate((q) => {
      const n = document.querySelector(q);
      if (n) n.scrollLeft = 0;
    }, strip);
    const box = await page.locator(strip).first().boundingBox();
    await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
    await page.mouse.wheel(0, 300);
    await settle(page, 200);
    out.realWheel = await page.evaluate((q) => Math.round(document.querySelector(q)?.scrollLeft ?? -1), strip);
    out.filmNoOverflow = await wheelProbe(page, '.editor .ipad-filmstrip', 240);
    await page.evaluate(() => {
      const n = document.querySelector('.editor .ipad-filmstrip');
      if (n) n.style.maxWidth = '160px';
    });
    out.filmOverflow = await wheelProbe(page, '.editor .ipad-filmstrip', 240);
  } finally {
    await ctx.close();
  }
  return { ...out, errors };
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
  async short(url) {
    return withBrowser('metal', async (browser) => ({
      ...(await shortGroup(browser, url, manifest.shortSizes)),
      computer: await classifyGroup(browser, url, 'computer', manifest.classifySizes),
      phone: await classifyGroup(browser, url, 'phone', manifest.classifySizes),
    }));
  },
  async hover(url) {
    return withBrowser('metal', async (browser) => ({
      computer: await hoverGroup(browser, url, 'computer', { width: 1440, height: 900 }),
      ipad: await hoverGroup(browser, url, 'ipad', { width: 1180, height: 820 }),
      webkit: await withBrowser('webkit', (wk) => hoverGroup(wk, url, 'computer', { width: 1440, height: 900 }, 'webkit')),
    }));
  },
  async lock(url) {
    const out = {};
    out.metal = await withBrowser('metal', (b) => lockGroup(b, url, 'metal', 'computer', { width: 1440, height: 900 }));
    out.nogpu = await withBrowser('nogpu', (b) => lockGroup(b, url, 'nogpu', 'computer', { width: 1440, height: 900 }));
    out.webkit = await withBrowser('webkit', (b) => lockGroup(b, url, 'webkit', 'computer', { width: 1440, height: 900 }));
    out.webkitPhone = await withBrowser('webkit', (b) => lockGroup(b, url, 'webkit', 'phone', { width: 390, height: 844 }));
    return out;
  },
  async names(url) {
    return withBrowser('metal', async (browser) => ({
      computer: await namesGroup(browser, url, 'computer', { width: 1440, height: 900 }),
      phone: await namesGroup(browser, url, 'phone', { width: 390, height: 844 }),
    }));
  },
  async ruler(url) {
    return withBrowser('metal', async (browser) => ({
      computer: await rulerGroup(browser, url, 'computer', [[1440, 900], [1024, 768], [2560, 1440]]),
      ipad: await rulerGroup(browser, url, 'ipad', [[1180, 820], [1024, 1366]]),
      phone: await rulerGroup(browser, url, 'phone', [[390, 844], [844, 390]]),
    }));
  },
  async start(url) {
    return withBrowser('metal', async (browser) => ({
      computer: await startGroup(browser, url, 'computer', [[2560, 1440], [1920, 1080], [1440, 900], [1024, 768], [1280, 600], [1093, 614], [900, 590]]),
      ipad: await startGroup(browser, url, 'ipad', [[1366, 1024], [1180, 820], [1024, 1366]]),
      phone: await startGroup(browser, url, 'phone', [[390, 844], [440, 956]]),
    }));
  },
  async wheel(url) {
    return withBrowser('metal', async (browser) => ({
      computer: await wheelGroup(browser, url, 'computer', { width: 1440, height: 900 }),
      webkit: await withBrowser('webkit', (wk) => wheelGroup(wk, url, 'computer', { width: 1440, height: 900 }, 'webkit')),
      ipad: await wheelGroup(browser, url, 'ipad', { width: 1180, height: 820 }),
    }));
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
