// 电脑上的「选文件夹」（2026-11-28）：开始页和网格「更多」里都有，旁边一行小字；选一个像存储卡的文件夹（照片、相机配方文件、
// Mac 的 ._ 文件、.Trashes、一个无关文件），只导入照片和相机配方文件，说一句「没用上」。1280×800、1440×900 不挤版。
// WebKit 手机和 iPad（有触摸）：开始页和「更多」里都没有。
// 隐私：报告里只有计数、布尔、尺寸和界面上的固定文案，没有照片名、拍摄时间或 EXIF。截图只在 manifest.shotsDir 有值时拍开始页。

import { existsSync, readdirSync, readFileSync, writeFileSync } from 'node:fs';
import path from 'node:path';
import { pathToFileURL } from 'node:url';

const [runtime, url, manifestPath] = process.argv.slice(2);
const manifest = JSON.parse(readFileSync(manifestPath, 'utf8'));
const { launchPinnedChromium, pinnedRuntime, verifyPinnedRuntime } = await import(pathToFileURL(runtime).href);
verifyPinnedRuntime();
const BROWSER_DIRS = [process.env.PLAYWRIGHT_BROWSERS_PATH, pinnedRuntime.browsersPath].filter(Boolean);

const out = { pageErrors: [], steps: {}, flowErrors: {}, error: null };
const GPU_ARGS = ['--enable-unsafe-webgpu', '--enable-unsafe-swiftshader', '--use-angle=swiftshader', '--use-webgpu-adapter=swiftshader'];
const IPAD_UA = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/26.0 Safari/605.1.15';
const SIZES = [[1280, 800], [1440, 900]];

async function settle(page) {
  for (let attempt = 0; attempt < 5; attempt += 1) {
    await page.waitForSelector('.app[data-core="1"]', { timeout: 60_000 });
    const reloaded = await page.waitForEvent('framenavigated', { timeout: 4_000 }).then(() => true).catch(() => false);
    if (!reloaded) return;
  }
  throw new Error('page kept reloading');
}

async function openPage(browser, label, options, inits = []) {
  const ctx = await browser.newContext(options);
  for (const init of inits) await ctx.addInitScript(init);
  const page = await ctx.newPage();
  page.on('pageerror', (err) => out.pageErrors.push(`${label}: ${err.message.slice(0, 200)}`));
  await page.goto(url);
  await settle(page);
  await page.evaluate(() => localStorage.clear());
  return { ctx, page };
}

async function waitCells(page, n) {
  await page.waitForFunction(
    (want) => {
      const sub = document.querySelector('.nav small');
      return sub && !sub.textContent.includes('正在读取') && document.querySelectorAll('.cell').length >= want;
    },
    n,
    { timeout: 120_000 },
  );
}
const cellCount = (page) => page.evaluate(() => document.querySelectorAll('.cell').length);
async function waitToast(page, timeout = 5_000) {
  await page.waitForFunction(() => Boolean(document.querySelector('.ftoast')), null, { timeout }).catch(() => {});
  return page.evaluate(() => document.querySelector('.ftoast')?.textContent ?? null);
}

/** 开始页上「选文件夹」那一组：在不在、小字、和「选照片」是否并排、有没有挤出来、开始页要不要滚动。 */
const startFacts = (page) => page.evaluate(() => {
  const start = document.querySelector('.start');
  const folder = start?.querySelector('[data-act="pick-folder"]');
  const photos = start?.querySelector('[data-act="pick-photos"]');
  const note = start?.querySelector('[data-state="folder-note"]');
  const box = (node) => node?.getBoundingClientRect();
  const fits = (node) => Boolean(node) && node.scrollWidth <= node.clientWidth + 1;
  const f = box(folder);
  const p = box(photos);
  const n = box(note);
  const s = box(start);
  return {
    folders: start?.querySelectorAll('[data-act="pick-folder"]').length ?? 0,
    label: folder?.textContent ?? null,
    note: note?.textContent ?? null,
    noteLines: n && note ? Math.round(n.height / parseFloat(getComputedStyle(note).lineHeight)) : 0,
    noteInside: Boolean(n && s && n.left >= s.left - 1 && n.right <= s.right + 1),
    noteDim: note ? getComputedStyle(note).color !== getComputedStyle(start.querySelector('h1')).color : false,
    noteBelow: Boolean(n && f && n.top >= f.bottom - 1),
    sideBySide: Boolean(f && p && Math.abs(f.top - p.top) <= 1 && Math.abs(f.height - p.height) <= 1 && f.left >= p.right),
    labelsFit: fits(folder) && fits(photos),
    scrolls: start ? start.scrollHeight > start.clientHeight + 1 : null,
  };
});

async function startAtSizes(page, label) {
  const rows = [];
  for (const [width, height] of SIZES) {
    await page.setViewportSize({ width, height });
    await page.waitForTimeout(200);
    rows.push({ size: `${width}x${height}`, ...(await startFacts(page)) });
    if (manifest.shotsDir) await page.screenshot({ path: path.join(manifest.shotsDir, `folder-${label}-${width}x${height}.png`) });
  }
  return rows;
}

/** 点可见的按钮，接住选择窗口，选一个文件夹（Playwright 给 webkitdirectory 选择框传目录，带 webkitRelativePath）。 */
async function chooseFolder(page, click, dir) {
  const wait = page.waitForEvent('filechooser', { timeout: 5_000 });
  await click();
  const chooser = await wait;
  const multiple = chooser.isMultiple();
  await chooser.setFiles(dir);
  return multiple;
}

async function openMore(page) {
  await page.locator('.nav [aria-label="更多"]').click();
  await page.locator('.ms .lrow').first().waitFor({ timeout: 5_000 });
  return page.evaluate(() => [...document.querySelectorAll('.ms .lrow')].map((row) => ({ title: row.querySelector('b')?.textContent ?? '', detail: row.querySelector('small')?.textContent ?? '' })));
}

async function computerFlow(browser) {
  const { ctx, page } = await openPage(browser, 'computer', { viewport: { width: SIZES[0][0], height: SIZES[0][1] } });
  const step = {};
  out.steps.computer = step;
  step.startNoDat = await startAtSizes(page, 'start');
  step.inputsBefore = await page.evaluate(() => document.querySelectorAll('input[type="file"]').length);

  // 开始页：选一个像存储卡的文件夹。
  step.multiple = await chooseFolder(page, () => page.locator('[data-act="pick-folder"]').click(), manifest.cardDir);
  step.input = await page.evaluate(() => {
    const input = document.querySelector('input.folderpick');
    return input ? { dir: input.webkitdirectory, multiple: input.multiple, tab: input.tabIndex, hidden: input.getAttribute('aria-hidden'), visible: input.getBoundingClientRect().width > 1 } : null;
  });
  step.toast = await waitToast(page);
  await waitCells(page, manifest.cardStems.length).catch(() => {});
  await page.waitForTimeout(800); // 万一多读了 .Trashes 或 ._ 里的
  step.screen = await page.evaluate(() => document.querySelector('.app')?.dataset.screen);
  step.cells = await page.evaluate(() => [...document.querySelectorAll('.cell')].map((cell) => cell.dataset.id).sort());
  step.wantCells = manifest.cardStems.map((stem) => stem.toLowerCase()).sort();
  await page.waitForFunction(() => window.__gxrBook?.dat && !window.__gxrBook.datBusy, null, { timeout: 60_000 }).catch(() => {});
  step.camfiles = await page.evaluate(() => ({ generation: window.__gxrBook?.dat?.generation ?? null, library: Boolean(window.__gxrBook?.dat?.library) }));

  // 网格「更多」：「添加照片」下面一行「选文件夹」，小字同开始页。选另一个文件夹（一对新照片和它的 ._ 文件）：加一张，不说「没用上」。
  step.gridRows = await openMore(page);
  await page.evaluate(() => document.querySelectorAll('.ftoast').forEach((node) => node.remove()));
  const row = page.locator('.ms .lrow', { has: page.locator('b', { hasText: /^选文件夹$/ }) });
  step.gridMultiple = await chooseFolder(page, () => row.click(), manifest.moreDir);
  await waitCells(page, manifest.cardStems.length + 1).catch(() => {});
  step.gridCells = await cellCount(page);
  step.gridToast = await waitToast(page, 2_000);

  // 清空以后回到开始页（相机配方文件已记住）：「从存储卡选照片」旁边照样有「选文件夹」。
  await openMore(page);
  await page.locator('.ms .lrow', { has: page.locator('b', { hasText: '清空照片' }) }).click();
  await page.locator('.dlg[data-state="confirm-clear"] .btn.danger').click();
  await page.locator('.app[data-screen="start"]').waitFor({ timeout: 10_000 });
  step.startDat = await startAtSizes(page, 'start-dat');
  step.startDatState = await page.evaluate(() => document.querySelector('.screen')?.dataset.camfiles ?? null);
  await ctx.close();
}

function webkitExecutable(webkit) {
  const direct = webkit.executablePath();
  if (direct && existsSync(direct)) return direct;
  const base = direct ? path.basename(direct) : 'pw_run.sh';
  for (const dir of BROWSER_DIRS) {
    if (!existsSync(dir)) continue;
    for (const entry of readdirSync(dir)) {
      if (!entry.startsWith('webkit-')) continue;
      const candidate = path.join(dir, entry, base);
      if (existsSync(candidate)) return candidate;
    }
  }
  return null;
}

function ipadTouch() {
  Object.defineProperty(Navigator.prototype, 'maxTouchPoints', { configurable: true, get: () => 5 });
  Object.defineProperty(Navigator.prototype, 'platform', { configurable: true, get: () => 'MacIntel' });
}

/** WebKit 手机和 iPad：开始页、读进一对照片后的「更多」里都没有「选文件夹」，也没有目录选择框。 */
async function touchFlow(browser, label, options, inits) {
  const { ctx, page } = await openPage(browser, label, { ...options, hasTouch: true, isMobile: true }, inits);
  const step = {};
  out.steps[label] = step;
  step.touch = await page.evaluate(() => navigator.maxTouchPoints);
  step.fine = await page.evaluate(() => matchMedia('(pointer: fine)').matches);
  step.start = await page.evaluate(() => ({
    folders: document.querySelectorAll('[data-act="pick-folder"]').length,
    notes: document.querySelectorAll('[data-state="folder-note"]').length,
    photos: document.querySelectorAll('[data-act="pick-photos"]').length,
  }));
  await page.locator('input.filepick:not(.calibpick)').setInputFiles(manifest.pair);
  await waitCells(page, 1);
  step.gridRows = await openMore(page);
  step.dirInputs = await page.evaluate(() => [...document.querySelectorAll('input')].filter((input) => input.webkitdirectory).length);
  await ctx.close();
}

try {
  const chromium = await launchPinnedChromium({ args: GPU_ARGS });
  try {
    try {
      await computerFlow(chromium);
    } catch (err) {
      out.flowErrors.computer = String(err?.message ?? err).split('\n')[0].slice(0, 200);
    }
  } finally {
    await chromium.close();
  }
  const { webkit } = await import(pathToFileURL(pinnedRuntime.modulePath).href);
  const executablePath = webkitExecutable(webkit);
  if (!executablePath) {
    out.steps.webkitMissing = true;
  } else {
    const browser = await webkit.launch({ headless: true, executablePath });
    try {
      await touchFlow(browser, 'webkitPhone', { viewport: { width: 390, height: 844 }, deviceScaleFactor: 2 }, []);
      await touchFlow(browser, 'webkitIpad', { viewport: { width: 1180, height: 820 }, deviceScaleFactor: 2, userAgent: IPAD_UA }, [ipadTouch]);
    } finally {
      await browser.close();
    }
  }
} catch (err) {
  out.error = String(err?.message ?? err).split('\n')[0].slice(0, 300);
}
writeFileSync(manifest.reportPath, JSON.stringify(out));
process.stdout.write(`${JSON.stringify({ done: true, error: out.error !== null })}\n`);
