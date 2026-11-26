// 电脑浏览器上「存」就是下载（2026-11-26）：真实照片在 Chromium（没有触摸点、指针精确）里一键导出、点「下载」，
// 收下浏览器的下载；同一张、同样的选项在「手机」Chromium（一个触摸点 + 假共享面板）里走共享，两边文件逐字节比
// （Python 那边比）。WebKit 手机和 iPad（有触摸）尺寸下文案不变、仍调共享面板。
// 隐私：stdout 只有一行 JSON（计数和布尔），文件名只写进 manifest.outDir；不打印照片名、拍摄时间或 EXIF。

import { existsSync, readdirSync, readFileSync, writeFileSync } from 'node:fs';
import path from 'node:path';
import { pathToFileURL } from 'node:url';
import { cellOf, exportFromGrid, pickAll, sharedFiles, waitPhase } from './mobile_ui_export_parity.mjs';
import { phoneTouch } from './phone_touch.mjs';

const [runtime, url, manifestPath] = process.argv.slice(2);
const manifest = JSON.parse(readFileSync(manifestPath, 'utf8'));
const { launchPinnedChromium, pinnedRuntime, verifyPinnedRuntime } = await import(pathToFileURL(runtime).href);
verifyPinnedRuntime();
// launchPinnedChromium 会把 PLAYWRIGHT_BROWSERS_PATH 改成 Codex 那份（里面没有 WebKit）：先记下 test-env 给的位置。
const BROWSER_DIRS = [process.env.PLAYWRIGHT_BROWSERS_PATH, pinnedRuntime.browsersPath].filter(Boolean);

const out = { pageErrors: [], steps: {}, error: null };
const GPU_ARGS = ['--enable-unsafe-webgpu', '--enable-unsafe-swiftshader', '--use-angle=swiftshader', '--use-webgpu-adapter=swiftshader'];
const IPAD_UA = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/26.0 Safari/605.1.15';

/** 假共享面板：记下每次 share() 的文件和 canShare 被问了几次。电脑上它也在（Mac 的 Safari、Chrome 都有共享），但不该被调。 */
function fakeShare() {
  const scope = window;
  scope.__shareLog = { canShare: 0, calls: [] };
  Object.defineProperty(navigator, 'canShare', {
    configurable: true,
    value: (data) => {
      scope.__shareLog.canShare += 1;
      return Array.isArray(data?.files) && data.files.length > 0;
    },
  });
  Object.defineProperty(navigator, 'share', {
    configurable: true,
    value: (data) => {
      scope.__shareLog.calls.push({ active: navigator.userActivation ? navigator.userActivation.isActive : null, files: data.files });
      return Promise.resolve();
    },
  });
}

/** iPadOS Safari：Mac 的 UA，但有 5 个触摸点。 */
function ipadTouch() {
  Object.defineProperty(Navigator.prototype, 'maxTouchPoints', { configurable: true, get: () => 5 });
  Object.defineProperty(Navigator.prototype, 'platform', { configurable: true, get: () => 'MacIntel' });
}

async function settle(page) {
  for (let attempt = 0; attempt < 5; attempt += 1) {
    await page.waitForSelector('.app[data-core="1"]', { timeout: 60_000 });
    const reloaded = await page.waitForEvent('framenavigated', { timeout: 4_000 }).then(() => true).catch(() => false);
    if (!reloaded) return;
  }
  throw new Error('page kept reloading');
}

async function openPage(browser, label, options, inits) {
  const ctx = await browser.newContext({ ...options, acceptDownloads: true });
  for (const init of inits) await ctx.addInitScript(init);
  await ctx.addInitScript(fakeShare);
  const page = await ctx.newPage();
  page.on('pageerror', (err) => out.pageErrors.push(`${label}: ${err.message.slice(0, 200)}`));
  const downloads = [];
  page.on('download', (dl) => downloads.push(dl));
  await page.goto(url);
  await settle(page);
  await page.evaluate(() => localStorage.clear());
  await pickAll(page, manifest, manifest.files);
  return { ctx, page, downloads };
}

async function openPhoto(page, stem) {
  await page.locator(cellOf(stem)).evaluate((node) => node.click());
  await page.waitForFunction((want) => document.querySelector('.editor')?.dataset.photo === want, stem, { timeout: 120_000 });
  await page.locator('.editor .toolbar [data-act="export"]').waitFor({ state: 'visible', timeout: 60_000 });
}

async function backToGrid(page) {
  await page.getByRole('button', { name: '照片', exact: true }).click();
  await page.waitForSelector('[data-screen="grid"]');
}

async function envFacts(page) {
  return page.evaluate(() => ({ touch: navigator.maxTouchPoints, fine: matchMedia('(pointer: fine)').matches, layout: document.querySelector('.app')?.dataset.layout ?? '' }));
}

async function saveButton(page) {
  const btn = page.locator('.editor .toolbar [data-act="export"]');
  return { text: (await btn.innerText()).trim(), aria: (await btn.getAttribute('aria-label')) ?? '' };
}

async function readyButton(page) {
  const btn = page.locator('.pdock [data-act="share"]');
  await btn.waitFor({ state: 'visible', timeout: 60_000 });
  return { btn, text: (await btn.innerText()).trim(), aria: (await btn.getAttribute('aria-label')) ?? '' };
}

/** 等到收下 `n` 个下载，存进 outDir/<prefix><文件名>。 */
async function saveDownloads(downloads, from, n, prefix) {
  const deadline = Date.now() + 60_000;
  while (downloads.length < from + n && Date.now() < deadline) await new Promise((r) => setTimeout(r, 100));
  const names = [];
  for (const dl of downloads.slice(from)) {
    const name = dl.suggestedFilename();
    await dl.saveAs(path.join(manifest.outDir, `${prefix}${name}`));
    names.push(name);
  }
  return names;
}

/** 编辑器 ⋯ →「做成关机图」，等最终字节校验通过后点「导出」。 */
async function offImage(page) {
  await page.locator('.editor .nav [data-act="editor-more"]').click();
  await page.waitForSelector('.ms[data-state="editor-more"]', { timeout: 10_000 });
  await page.locator('.ms[data-state="editor-more"] [data-act="more-off-image"]').click();
  await page.waitForFunction(
    () => {
      const sheet = document.querySelector('.ms.off-image-sheet');
      const btn = sheet?.querySelector('[data-act="off-image-export"]');
      return sheet?.dataset.ready === '1' && btn && !btn.disabled;
    },
    null,
    { timeout: 120_000 },
  );
  await page.locator('.ms.off-image-sheet [data-act="off-image-export"]').click();
}

async function closeOffImage(page) {
  await page.keyboard.press('Escape');
  await page.waitForFunction(() => !document.querySelector('.ms.off-image-sheet'), null, { timeout: 10_000 });
}

async function shareFacts(page) {
  return page.evaluate(() => ({ calls: window.__shareLog.calls.length, canShare: window.__shareLog.canShare, files: window.__shareLog.calls.map((c) => c.files.length), active: window.__shareLog.calls.map((c) => c.active) }));
}

/** 电脑：编辑器一键导出 → 就绪条「下载」→ 浏览器下载；再在网格里导出两张，一次点击下载两个。 */
async function computer(browser) {
  const { ctx, page, downloads } = await openPage(browser, 'computer', { viewport: { width: 1280, height: 800 } }, []);
  const step = { env: await envFacts(page) };
  await openPhoto(page, manifest.stems[0]);
  step.save = await saveButton(page);
  await page.locator('.editor .toolbar [data-act="export"]').click();
  await waitPhase(page, ['ready'], 600_000);
  const ready = await readyButton(page);
  step.ready = { text: ready.text, aria: ready.aria };
  const before = downloads.length;
  await ready.btn.click();
  step.singleNames = await saveDownloads(downloads, before, 1, 'computer1-');
  await page.waitForFunction(() => !document.querySelector('.pdock'), null, { timeout: 10_000 }).catch(() => {});
  step.dockGoneAfterSingle = (await page.locator('.pdock').count()) === 0;
  step.saveAfter = await saveButton(page);

  const beforeOff = downloads.length;
  await offImage(page);
  step.offNames = await saveDownloads(downloads, beforeOff, 1, 'computerOff-');
  await closeOffImage(page);

  await backToGrid(page);
  const sheet = await exportFromGrid(page, manifest.stems, '原始尺寸', '高');
  step.sheetText = sheet.text;
  await waitPhase(page, ['ready'], 900_000);
  const batch = await readyButton(page);
  step.batchReady = { text: batch.text, aria: batch.aria };
  const beforeBatch = downloads.length;
  await batch.btn.click();
  step.batchNames = await saveDownloads(downloads, beforeBatch, manifest.stems.length, 'computerN-');
  const links = page.locator('[data-state="no-share"]');
  await links.waitFor({ state: 'visible', timeout: 10_000 }).catch(() => {});
  step.backupLinks = (await links.count()) ? await links.locator('a[download]').evaluateAll((as) => as.map((a) => a.download)) : [];
  step.backupText = (await links.count()) ? (await links.locator('p').first().innerText()).trim() : '';
  step.share = await shareFacts(page);
  out.steps.computer = step;
  await ctx.close();
}

/** 手机（Chromium + 一个触摸点）：同一张、同样的选项走共享面板，收下的文件给 Python 逐字节比。 */
async function phoneChromium(browser) {
  const { ctx, page, downloads } = await openPage(browser, 'phone', { viewport: { width: 390, height: 844 } }, [phoneTouch]);
  const step = { env: await envFacts(page) };
  await openPhoto(page, manifest.stems[0]);
  step.save = await saveButton(page);
  await page.locator('.editor .toolbar [data-act="export"]').click();
  await waitPhase(page, ['ready'], 600_000);
  const ready = await readyButton(page);
  step.ready = { text: ready.text, aria: ready.aria };
  await ready.btn.click();
  await page.waitForFunction(() => window.__shareLog.calls.length >= 1, null, { timeout: 30_000 });
  step.singleShared = await sharedFiles(page, manifest.outDir, 'phone1-', 0);
  await offImage(page);
  await page.waitForFunction(() => window.__shareLog.calls.length >= 2, null, { timeout: 30_000 });
  step.offShared = await sharedFiles(page, manifest.outDir, 'phoneOff-', 1);
  await closeOffImage(page);
  await backToGrid(page);
  const sheet = await exportFromGrid(page, manifest.stems, '原始尺寸', '高');
  step.sheetText = sheet.text;
  await waitPhase(page, ['ready'], 900_000);
  const batch = await readyButton(page);
  step.batchReady = { text: batch.text, aria: batch.aria };
  await batch.btn.click();
  await page.waitForFunction(() => window.__shareLog.calls.length >= 3, null, { timeout: 30_000 });
  step.batchShared = await sharedFiles(page, manifest.outDir, 'phoneN-', 2);
  step.share = await shareFacts(page);
  step.downloads = downloads.length;
  out.steps.phoneChromium = step;
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

/** WebKit 手机和 iPad（有触摸）：按钮的字不变，点「存进相册」仍调共享面板，不下载。 */
async function webkitTouch(browser, label, options, inits) {
  const { ctx, page, downloads } = await openPage(browser, label, { ...options, hasTouch: true, isMobile: true }, inits);
  const step = { env: await envFacts(page) };
  await openPhoto(page, manifest.stems[0]);
  step.save = await saveButton(page);
  await page.locator('.editor .toolbar [data-act="export"]').click();
  await waitPhase(page, ['ready'], 900_000);
  const ready = await readyButton(page);
  step.ready = { text: ready.text, aria: ready.aria };
  await ready.btn.click();
  await page.waitForFunction(() => window.__shareLog.calls.length >= 1, null, { timeout: 30_000 });
  step.share = await shareFacts(page);
  await page.waitForTimeout(500);
  step.downloads = downloads.length;
  out.steps[label] = step;
  await ctx.close();
}

try {
  const chromium = await launchPinnedChromium({ args: GPU_ARGS });
  try {
    await computer(chromium);
    await phoneChromium(chromium);
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
      await webkitTouch(browser, 'webkitPhone', { viewport: { width: 390, height: 844 }, deviceScaleFactor: 2 }, []);
      await webkitTouch(browser, 'webkitIpad', { viewport: { width: 1032, height: 1376 }, deviceScaleFactor: 2, userAgent: IPAD_UA }, [ipadTouch]);
    } finally {
      await browser.close();
    }
  }
} catch (err) {
  out.error = String(err?.message ?? err).split('\n')[0].slice(0, 300);
}
writeFileSync(manifest.reportPath, JSON.stringify(out));
process.stdout.write(`${JSON.stringify({ done: true, error: out.error !== null })}\n`);
