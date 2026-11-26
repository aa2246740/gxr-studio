// polish-3 回归驱动（固定 Playwright WebKit，没有 WebGPU，生产 mobile/dist）。
// 用法：node mobile_polish3_webkit.mjs <runtime.mjs> <manifest.json>；manifest 给 distDir、photos（raw、jpeg、pair）、cubes 和 cubeTitles（每个内置色彩空间一个，键是空间 id）、out，可选 shotsDir、shotsPrefix。
// 1. 无显卡说明条：iPad 和手机横屏里，只有 RAW（没有 JPG、没有内嵌预览）的那张和舞台说同一句；有 JPG 的照旧。
// 2. 手机横屏 LUT 色彩空间瓷砖：只显示短名，不带「· 文件已写明」，不在词中间折行，不被裁切；aria-label 仍是全文。
//    WebKit 和固定 Chromium 各查一遍（Chromium 会在连字符处折行，WebKit 不会）。
// 3. 手机横屏焦点顺序：Tab 一路走下去（WebKit 默认 Tab 不停在按钮上，这一项用固定 Chromium、去掉 navigator.gpu），
//    右边一列的顺序和画面从上到下一致；读屏走的 DOM 顺序也一样。转竖屏再转回来也对。
// 只用合成照片；输出只写脱敏 JSON。没装 WebKit 时报告 { skipped }。HTTP 服务只绑 127.0.0.1 临时端口（避开禁用端口）。

import fs from 'node:fs';
import http from 'node:http';
import path from 'node:path';
import { pathToFileURL } from 'node:url';

const [runtimePath, manifestPath] = process.argv.slice(2);
const manifest = JSON.parse(fs.readFileSync(manifestPath, 'utf8'));
const { launchPinnedChromium, pinnedRuntime, verifyPinnedRuntime } = await import(pathToFileURL(runtimePath).href);
verifyPinnedRuntime();
const { webkit } = await import(pathToFileURL(pinnedRuntime.modulePath).href);

const FORBIDDEN_PORTS = new Set([60887, 18772, 18801, 18802, 18803, 5290]);
const MIME = { '.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8', '.css': 'text/css; charset=utf-8', '.wasm': 'application/wasm', '.zip': 'application/zip', '.ttf': 'font/ttf', '.txt': 'text/plain; charset=utf-8', '.svg': 'image/svg+xml' };
const IPHONE_UA = 'Mozilla/5.0 (iPhone; CPU iPhone OS 18_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.6 Mobile/15E148 Safari/604.1';
const IPAD_UA = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/26.0 Safari/605.1.15';
const idOf = (file) => path.basename(file).replace(/\.[^.]+$/, '').toLowerCase();
const ID = { raw: idOf(manifest.photos.raw), jpeg: idOf(manifest.photos.jpeg), pair: idOf(manifest.photos.pair[0]) };
const FILES = [manifest.photos.raw, manifest.photos.jpeg, ...manifest.photos.pair];

function resolveWebKitExecutable() {
  const direct = webkit.executablePath();
  if (direct && fs.existsSync(direct)) return direct;
  const baseName = direct ? path.basename(direct) : 'pw_run.sh';
  if (fs.existsSync(pinnedRuntime.browsersPath)) {
    for (const entry of fs.readdirSync(pinnedRuntime.browsersPath)) {
      if (!entry.startsWith('webkit-')) continue;
      const candidate = path.join(pinnedRuntime.browsersPath, entry, baseName);
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

const settle = (page) => page.evaluate(() => new Promise((done) => requestAnimationFrame(() => requestAnimationFrame(() => setTimeout(done, 120)))));
const cell = (page, id) => page.locator(`.gscroll .cell[data-id="${id}"]`);

async function newPage(browser, vp, ipad) {
  const ctx = await browser.newContext({ viewport: vp, deviceScaleFactor: 2, isMobile: true, hasTouch: true, userAgent: ipad ? IPAD_UA : IPHONE_UA, locale: 'zh-CN' });
  const page = await ctx.newPage();
  const errors = [];
  page.on('pageerror', (err) => errors.push(String(err.message).slice(0, 200)));
  return { ctx, page, errors };
}

async function start(page, url) {
  await page.goto(url, { waitUntil: 'domcontentloaded' });
  await page.locator('.app[data-screen="start"][data-core="1"]').waitFor({ timeout: 60_000 });
  await page.locator('input.filepick:not(.calibpick)').first().setInputFiles(FILES);
  await page.locator('.app[data-screen="grid"]').waitFor({ timeout: 30_000 });
  for (const id of Object.values(ID)) await cell(page, id).waitFor({ timeout: 30_000 });
  await page.waitForFunction(() => !document.querySelector('.nav small')?.textContent?.includes('正在读取'), null, { timeout: 60_000 });
}

async function open(page, id) {
  await cell(page, id).click();
  await page.waitForFunction((want) => document.querySelector('.app')?.dataset.screen === 'editor' && document.querySelector('.screen.editor')?.dataset.photo?.toLowerCase() === want, id, { timeout: 30_000 });
  await page.waitForFunction(() => document.querySelector('.app')?.dataset.settled !== '0', null, { timeout: 30_000 });
  const later = page.locator('.ask-sheet button', { hasText: '先不用' });
  if (await later.waitFor({ state: 'visible', timeout: 1_500 }).then(() => true, () => false)) {
    await later.click();
    await page.waitForFunction(() => !document.querySelector('.ask-sheet'), null, { timeout: 5_000 });
  }
  await settle(page);
}

async function back(page) {
  await page.locator('button.nav-back').click();
  await page.locator('.app[data-screen="grid"]').waitFor();
}

async function resize(page, vp, layout) {
  await page.setViewportSize(vp);
  await page.waitForFunction((want) => document.querySelector('.app')?.dataset.layout === want, layout, { timeout: 10_000 });
  await settle(page);
}

async function shot(page, name, selector) {
  if (!manifest.shotsDir) return;
  const node = page.locator(selector).first();
  if (!(await node.isVisible().catch(() => false))) return;
  fs.mkdirSync(manifest.shotsDir, { recursive: true });
  await node.screenshot({ path: path.join(manifest.shotsDir, `${manifest.shotsPrefix || 'shot'}-${name}.png`), animations: 'disabled' });
}

/** 说明条、舞台占位和舞台下标签现在说的话。 */
function readNotice(page) {
  return page.evaluate(() => {
    const vis = (node) => Boolean(node) && node.getClientRects().length > 0 && getComputedStyle(node).visibility !== 'hidden';
    const box = document.querySelector('.editor .nogpu-notice');
    const ph = document.querySelector('.editor .stage [data-state="no-preview"]');
    return {
      layout: document.querySelector('.app')?.dataset.layout ?? '',
      count: document.querySelectorAll('.editor .nogpu-notice').length,
      visible: vis(box),
      title: box?.querySelector('.nogpu-title')?.textContent ?? null,
      detail: box?.querySelector('.nogpu-detail')?.textContent ?? null,
      noPreview: ph && vis(ph) ? ph.textContent : null,
    };
  });
}

async function noticeScenario(browser, url, vp, layout, ipad) {
  const { ctx, page, errors } = await newPage(browser, vp, ipad);
  try {
    await start(page, url);
    const row = {};
    await open(page, ID.raw);
    row.raw = await readNotice(page);
    await shot(page, `notice-${vp.width}x${vp.height}-raw`, ipad ? '.editor .ipad-hist-area' : '.editor .nogpu-notice');
    await back(page);
    await open(page, ID.jpeg);
    row.jpg = await readNotice(page);
    await shot(page, `notice-${vp.width}x${vp.height}-jpg`, ipad ? '.editor .ipad-hist-area' : '.editor .nogpu-notice');
    if (!ipad) {
      // 转竖屏再转回来：说明条重新挂上，说的话不变
      await back(page);
      await open(page, ID.raw);
      await resize(page, { width: vp.height, height: vp.width }, 'phone-portrait');
      row.rawPortrait = await readNotice(page);
      await resize(page, vp, layout);
      row.rawBack = await readNotice(page);
    }
    row.errors = errors;
    return row;
  } finally {
    await ctx.close();
  }
}

/** 色彩空间瓷砖：看得见的字、每一行的字、有没有被裁切，以及按钮的 aria-label。 */
function readSpaceTile(page) {
  return page.evaluate(() => {
    const btn = document.querySelector('.editor .panel.lut .lut-pin [data-act="lut-space"]');
    const txt = btn?.querySelector('.lut-space-txt');
    if (!btn || !txt) return null;
    const lines = [];
    const walker = document.createTreeWalker(txt, NodeFilter.SHOW_TEXT);
    const range = document.createRange();
    for (let node = walker.nextNode(); node; node = walker.nextNode()) {
      for (let i = 0; i < node.data.length; i += 1) {
        range.setStart(node, i);
        range.setEnd(node, i + 1);
        const rects = [...range.getClientRects()].filter((r) => r.width > 0);
        if (!rects.length) continue;
        const top = Math.round(rects[0].top);
        let line = lines.find((l) => Math.abs(l.top - top) <= 2);
        if (!line) lines.push((line = { top, text: '' }));
        line.text += node.data[i];
      }
    }
    lines.sort((a, b) => a.top - b.top);
    const tb = txt.getBoundingClientRect();
    const bb = btn.getBoundingClientRect();
    return {
      layout: document.querySelector('.app')?.dataset.layout ?? '',
      shown: txt.innerText.replace(/\s+/g, ' ').trim(),
      lines: lines.map((l) => l.text.trim()),
      clipped: txt.scrollHeight - txt.clientHeight > 1 || txt.scrollWidth - txt.clientWidth > 1,
      inButton: tb.left >= bb.left - 0.5 && tb.right <= bb.right + 0.5 && tb.top >= bb.top - 0.5 && tb.bottom <= bb.bottom + 0.5,
      aria: btn.getAttribute('aria-label'),
      width: Math.round(bb.width * 10) / 10,
    };
  });
}

async function pickLut(page, title) {
  await page.locator('.editor .panel.lut .strip .rc', { hasText: title }).first().click();
  await page.waitForFunction((t) => [...document.querySelectorAll('.editor .panel.lut .strip .rc.on')].some((n) => n.textContent.includes(t)), title, { timeout: 10_000 });
  await settle(page);
}

async function spaceScenario(browser, url, engine) {
  const land = [{ width: 844, height: 390 }, { width: 667, height: 375 }];
  const { ctx, page, errors } = await newPage(browser, land[0], false);
  if (engine === 'chromium') await ctx.addInitScript(() => Object.defineProperty(navigator, 'gpu', { value: undefined }));
  try {
    await start(page, url);
    await open(page, ID.pair);
    await page.locator('.editor .panel.recipe > .mode-switch [data-mode="lut"]').click();
    await page.waitForFunction(() => !document.querySelector('.editor .panel.lut')?.hidden, null, { timeout: 10_000 });
    await page.locator('input.lutpick').setInputFiles(Object.values(manifest.cubes));
    await page.locator('.editor .panel.lut .strip .rc', { hasText: manifest.cubeTitles.srgb }).first().waitFor({ timeout: 30_000 });
    const row = { tiles: {}, errors };
    for (const [key, title] of Object.entries(manifest.cubeTitles)) {
      await pickLut(page, title);
      for (const vp of land) {
        await resize(page, vp, 'phone-landscape');
        row.tiles[`${key}-${vp.width}x${vp.height}`] = await readSpaceTile(page);
        await shot(page, `space-${engine}-${key}-${vp.width}x${vp.height}`, '.editor .panel.lut .lut-pin');
      }
    }
    await resize(page, { width: 390, height: 844 }, 'phone-portrait');
    row.portraitMenu = await page.evaluate(() => {
      const tile = document.querySelector('.editor .panel.lut .lut-pin [data-act="lut-menu"]');
      return tile ? { text: tile.textContent.replace(/\s+/g, ' ').trim(), aria: tile.getAttribute('aria-label') } : null;
    });
    return row;
  } finally {
    await ctx.close();
  }
}

/** 从页面开头按 Tab，记下每个落到的控件（在编辑器里、看得见的），直到焦点离开编辑器或绕回来。 */
async function tabWalk(page) {
  await page.evaluate(() => {
    document.activeElement?.blur?.();
    window.scrollTo(0, 0);
  });
  const seen = [];
  for (let i = 0; i < 120; i += 1) {
    await page.keyboard.press('Tab');
    const at = await page.evaluate(() => {
      const node = document.activeElement;
      if (!node || node === document.body) return { out: true };
      const screen = document.querySelector('.screen.editor');
      if (!screen?.contains(node)) return { out: true, outside: node.className || node.tagName };
      if (node.dataset.p3seen) return { loop: true };
      node.dataset.p3seen = '1';
      const r = node.getBoundingClientRect();
      const name = node.getAttribute('aria-label') || node.dataset.act || node.dataset.tool || node.dataset.mode || node.textContent.trim().slice(0, 12);
      const region = node.closest('.screen.editor > *')?.classList[0] ?? '';
      return { name, region, x: Math.round(r.left), y: Math.round(r.top), w: Math.round(r.width), h: Math.round(r.height) };
    });
    if (at.loop) break;
    if (at.out) {
      if (seen.length) break;
      continue;
    }
    seen.push(at);
  }
  await page.evaluate(() => {
    for (const node of document.querySelectorAll('[data-p3seen]')) delete node.dataset.p3seen;
  });
  return seen;
}

/** 编辑器里看得见、能拿到焦点的控件，按 DOM 顺序（读屏的顺序）。 */
function domOrder(page) {
  return page.evaluate(() => {
    const screen = document.querySelector('.screen.editor');
    const sel = 'button:not([disabled]), input:not([disabled]):not([type="hidden"]), select, textarea, a[href], [tabindex]:not([tabindex="-1"])';
    return [...(screen?.querySelectorAll(sel) ?? [])]
      .filter((node) => node.getClientRects().length > 0 && getComputedStyle(node).visibility !== 'hidden' && !node.closest('[hidden], [inert], [aria-hidden="true"]'))
      .map((node) => {
        const r = node.getBoundingClientRect();
        const name = node.getAttribute('aria-label') || node.dataset.act || node.dataset.tool || node.dataset.mode || node.textContent.trim().slice(0, 12);
        const region = node.closest('.screen.editor > *')?.classList[0] ?? '';
        return { name, region, x: Math.round(r.left), y: Math.round(r.top), w: Math.round(r.width), h: Math.round(r.height) };
      })
      .filter((step) => step.w > 0 && step.h > 0 && step.y < window.innerHeight && step.x < window.innerWidth);
  });
}

async function focusScenario(url) {
  const vp = { width: 844, height: 390 };
  const browser = await launchPinnedChromium({ args: ['--disable-gpu'] });
  try {
    const { ctx, page, errors } = await newPage(browser, vp, false);
    await ctx.addInitScript(() => Object.defineProperty(navigator, 'gpu', { value: undefined }));
    await start(page, url);
    await open(page, ID.jpeg);
    const row = { errors };
    row.stage = await page.evaluate(() => {
      const r = document.querySelector('.screen.editor > .stage').getBoundingClientRect();
      return { x: Math.round(r.left), right: Math.round(r.right) };
    });
    row.landscape = await tabWalk(page);
    row.landscapeDom = await domOrder(page);
    await resize(page, { width: 390, height: 844 }, 'phone-portrait');
    row.portrait = await tabWalk(page);
    row.portraitDom = await domOrder(page);
    await resize(page, vp, 'phone-landscape');
    row.landscapeAgain = await tabWalk(page);
    row.landscapeAgainDom = await domOrder(page);
    await ctx.close();
    return row;
  } finally {
    await browser.close();
  }
}

const executablePath = resolveWebKitExecutable();
if (!executablePath) {
  fs.writeFileSync(manifest.out, JSON.stringify({ skipped: 'Playwright WebKit 未安装' }));
  process.exit(0);
}
const server = await startServer(manifest.distDir);
const browser = await webkit.launch({ headless: true, executablePath });
const report = { scenarios: {} };
const run = async (name, fn) => {
  try {
    report.scenarios[name] = await fn();
  } catch (err) {
    report.scenarios[name] = { error: String(err?.stack || err).replace(/\/(?:Volumes|Users|private|var\/folders|tmp)\/\S+/g, '[path]').slice(0, 600) };
  }
};
try {
  await run('noticeIpadLandscape', () => noticeScenario(browser, server.url, { width: 1180, height: 820 }, 'ipad-landscape', true));
  await run('noticeIpadPortrait', () => noticeScenario(browser, server.url, { width: 820, height: 1180 }, 'ipad-portrait', true));
  await run('noticeLandscape844', () => noticeScenario(browser, server.url, { width: 844, height: 390 }, 'phone-landscape', false));
  await run('noticeLandscape667', () => noticeScenario(browser, server.url, { width: 667, height: 375 }, 'phone-landscape', false));
  await run('spaceTile', () => spaceScenario(browser, server.url, 'webkit'));
  await run('spaceTileChromium', async () => {
    const chromium = await launchPinnedChromium({ args: ['--disable-gpu'] });
    try {
      return await spaceScenario(chromium, server.url, 'chromium');
    } finally {
      await chromium.close();
    }
  });
  await run('focus', () => focusScenario(server.url));
} finally {
  await browser.close();
  await server.close();
}
fs.writeFileSync(manifest.out, JSON.stringify(report, null, 2));
