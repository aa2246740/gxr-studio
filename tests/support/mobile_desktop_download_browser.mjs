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

/** 兜底链接表上列着的文件名（没有这张表就是空）。 */
async function backupNames(page) {
  const box = page.locator('.links[data-state="download-backup"]');
  if (!(await box.count())) return [];
  return box.locator('a[download]').evaluateAll((as) => as.map((a) => a.download));
}

async function shareFacts(page) {
  return page.evaluate(() => ({ calls: window.__shareLog.calls.length, canShare: window.__shareLog.canShare, files: window.__shareLog.calls.map((c) => c.files.length), active: window.__shareLog.calls.map((c) => c.active) }));
}

/** 电脑：编辑器一键导出 → 就绪条「下载」→ 浏览器下载；再在网格里导出两张，一次点击下载两个。 */
async function computer(browser) {
  const { ctx, page, downloads } = await openPage(browser, 'computer', { viewport: { width: 1280, height: 800 } }, []);
  const step = { env: await envFacts(page) };
  out.steps.computer = step; // 中途出错时，已经量到的也在报告里
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
  // 和共享成功一样，这一次点击就收尾：就绪条收起，兜底链接表自己列着这几个文件。
  await page.waitForFunction(() => !document.querySelector('.pdock'), null, { timeout: 10_000 }).catch(() => {});
  step.dockGoneAfterBatch = (await page.locator('.pdock').count()) === 0;
  step.backupLinks = await backupNames(page);
  step.backupText = (await page.locator('.links[data-state="download-backup"] p').count())
    ? (await page.locator('.links[data-state="download-backup"] p').first().innerText()).trim()
    : '';
  await page.keyboard.press('Escape');
  step.backupClosedByEsc = (await page.locator('[data-state="download-backup"]').count()) === 0;

  // 一批停在内存上限（held）：点一次「下载」就放掉这一批、接着做排着的那张，不用再点「关闭」。
  await page.evaluate((n) => { window.__devExport.holdAfter = n; }, manifest.stems.length);
  await exportFromGrid(page, manifest.heldStems, '小', '低');
  await waitPhase(page, ['held'], 300_000);
  const held = await readyButton(page);
  step.heldReady = { text: held.text, phase: await page.locator('.pdock').getAttribute('data-phase') };
  const beforeHeld = downloads.length;
  await held.btn.click();
  step.heldNames = await saveDownloads(downloads, beforeHeld, manifest.stems.length, 'computerH-');
  step.phaseAfterHeldTap = await page.evaluate(() => window.__devExport.flow.current()?.phase ?? 'none');
  step.backupDuringResume = await backupNames(page);
  await waitPhase(page, ['ready'], 900_000);
  const rest = await readyButton(page);
  step.restReady = { text: rest.text };
  step.backupAfterResume = await backupNames(page);
  await page.locator('.scrim.export-details-scrim').last().click({ position: { x: 5, y: 5 } });
  step.backupClosedByScrim = (await page.locator('[data-state="download-backup"]').count()) === 0;
  const beforeRest = downloads.length;
  await rest.btn.click();
  step.restNames = await saveDownloads(downloads, beforeRest, 1, 'computerR-');
  await page.waitForFunction(() => !document.querySelector('.pdock'), null, { timeout: 10_000 }).catch(() => {});
  step.dockGoneAfterRest = (await page.locator('.pdock').count()) === 0;
  await page.evaluate(() => { delete window.__devExport.holdAfter; });
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

/** 舞台上 HDR 相关的字：「HDR 预览」小标签在不在，「这块屏幕显示不了 HDR」那行在不在、在不在舞台里。 */
async function hdrFacts(page) {
  return page.evaluate(() => {
    const stage = document.querySelector('.editor .stage');
    const note = stage?.querySelector('[data-hdr-screen-note]');
    const sr = stage?.getBoundingClientRect();
    const nr = note?.getBoundingClientRect();
    return {
      preview: Boolean(document.querySelector('.editor [data-hdr-preview]')),
      note: note ? note.textContent : null,
      noteVisible: Boolean(note && nr && sr && nr.width > 0 && nr.height > 0 && nr.top >= sr.top && nr.bottom <= sr.bottom && getComputedStyle(note).visibility !== 'hidden'),
      exportHdr: document.querySelector('.editor .nav [data-act="hdr-toggle"]')?.getAttribute('aria-checked') ?? null,
      reloads: performance.getEntriesByType('navigation').length,
    };
  });
}

/** 裁剪时那行字收起来，不压在裁剪框下沿的手柄上；退出裁剪后回来。按 `sizes` 逐个视口量。 */
async function cropNote(page, sizes) {
  const rows = [];
  const shown = () => page.evaluate(() => {
    const note = document.querySelector('.editor .stage [data-hdr-screen-note]');
    return Boolean(note && getComputedStyle(note).display !== 'none' && note.getBoundingClientRect().height > 0);
  });
  for (const [width, height] of sizes) {
    await page.setViewportSize({ width, height });
    // 无头 Chrome 改视口后不发 resize、也不回调 ResizeObserver：手动发一次，让版式跟上。
    await page.evaluate(() => window.dispatchEvent(new Event('resize')));
    await page.waitForFunction(([w, h]) => document.querySelector('.app')?.clientWidth === w && document.querySelector('.app')?.clientHeight === h, [width, height], { timeout: 10_000 });
    await page.waitForFunction(() => Boolean(document.querySelector('.editor .stage [data-hdr-screen-note]')), null, { timeout: 30_000 }).catch(() => {});
    await page.waitForTimeout(300);
    const layout = await page.evaluate(() => document.querySelector('.app')?.dataset.layout ?? '');
    const before = await shown();
    await page.locator('.editor .toolbar [data-tool="crop"]').evaluate((node) => node.click());
    await page.waitForFunction(() => document.querySelector('.editor .crop-overlay')?.hidden === false, null, { timeout: 30_000 });
    const cropping = await shown();
    await page.locator('.editor .toolbar [data-tool="recipe"]').evaluate((node) => node.click());
    await page.waitForFunction(() => document.querySelector('.editor .crop-overlay')?.hidden !== false, null, { timeout: 30_000 });
    rows.push({ layout, before, cropping, after: await shown() });
  }
  return rows;
}

/** 电脑上 HDR 跟着屏幕走：页面在 SDR 屏上打开，再「挪到 HDR 屏」，不重载预览就打开；挪回来就关、那行字回来。 */
async function hdrScreen(browser) {
  const { ctx, page } = await openPage(browser, 'hdr', { viewport: { width: 1280, height: 800 } }, []);
  const step = {};
  await openPhoto(page, manifest.stems[0]);
  await page.waitForFunction(() => document.querySelector('.app')?.dataset.frame === '1', null, { timeout: 120_000 });
  const marker = await page.evaluate(() => { window.__noReload = 1; return 1; });
  await page.waitForFunction(() => Boolean(document.querySelector('.editor .stage [data-hdr-screen-note]')), null, { timeout: 30_000 }).catch(() => {});
  step.sdrScreen = await hdrFacts(page);
  step.crop = await cropNote(page, [[1280, 800], [390, 844], [844, 390]]);
  await page.setViewportSize({ width: 1280, height: 800 });
  await page.evaluate(() => window.dispatchEvent(new Event('resize')));
  await page.waitForFunction(() => document.querySelector('.app')?.dataset.layout === 'ipad-landscape', null, { timeout: 10_000 });
  // 和页面里的应用是同一个模块（Vite 开发服务器同一个 URL）。
  await page.evaluate(async () => (await import('/src/core/hdr_display.ts')).debugForceHdrDisplay(true));
  await page.waitForFunction(() => Boolean(document.querySelector('.editor [data-hdr-preview]')), null, { timeout: 120_000 }).catch(() => {});
  step.hdrScreen = await hdrFacts(page);
  await page.evaluate(async () => (await import('/src/core/hdr_display.ts')).debugForceHdrDisplay(false));
  await page.waitForFunction(() => !document.querySelector('.editor [data-hdr-preview]'), null, { timeout: 30_000 }).catch(() => {});
  step.backToSdr = await hdrFacts(page);
  await page.locator('.editor .nav [data-act="hdr-toggle"]').click();
  await page.waitForFunction(() => !document.querySelector('.editor .stage [data-hdr-screen-note]'), null, { timeout: 10_000 }).catch(() => {});
  step.hdrOff = await hdrFacts(page);
  step.sameDocument = marker === 1 && (await page.evaluate(() => window.__noReload === 1));
  out.steps.hdr = step;
  await ctx.close();
}

try {
  const chromium = await launchPinnedChromium({ args: GPU_ARGS });
  try {
    if (manifest.mode === 'hdr') {
      await hdrScreen(chromium);
    } else {
      await computer(chromium);
      await phoneChromium(chromium);
    }
  } finally {
    await chromium.close();
  }
  if (manifest.mode === 'hdr') throw Object.assign(new Error('hdr only'), { done: true });
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
  if (!err?.done) out.error = String(err?.message ?? err).split('\n')[0].slice(0, 300);
}
writeFileSync(manifest.reportPath, JSON.stringify(out));
process.stdout.write(`${JSON.stringify({ done: true, error: out.error !== null })}\n`);
