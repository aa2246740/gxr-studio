// 电脑浏览器上的导入（2026-11-26）：把真实照片和相机配方文件拖进窗口（CDP Input.dispatchDragEvent，带真实文件路径），
// 拖动时的全窗提示，编辑器里不收，文字拖动不拦；有照片时刷新先问一声（只在电脑上）；没用上的文件说一句；
// 只选 GXRFX.BIN 的提示；看不见的选择框不进 Tab 顺序。WebKit 手机和 iPad（有触摸）不挂 beforeunload。
// 隐私：stdout 只有一行 JSON；报告里只有计数、布尔和界面上的固定文案，没有照片名、拍摄时间或 EXIF。
// 截图只拍开始页和提示条本身（manifest.shotsDir 有值才拍）。

import { existsSync, readdirSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import path from 'node:path';
import { pathToFileURL } from 'node:url';
import { cellOf, exportFromGrid, waitPhase } from './mobile_ui_export_parity.mjs';

const [runtime, url, manifestPath] = process.argv.slice(2);
const manifest = JSON.parse(readFileSync(manifestPath, 'utf8'));
const { launchPinnedChromium, pinnedRuntime, verifyPinnedRuntime } = await import(pathToFileURL(runtime).href);
verifyPinnedRuntime();
const BROWSER_DIRS = [process.env.PLAYWRIGHT_BROWSERS_PATH, pinnedRuntime.browsersPath].filter(Boolean);

const out = { pageErrors: [], steps: {}, flowErrors: {}, error: null };
const GPU_ARGS = ['--enable-unsafe-webgpu', '--enable-unsafe-swiftshader', '--use-angle=swiftshader', '--use-webgpu-adapter=swiftshader'];
const IPAD_UA = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/26.0 Safari/605.1.15';
const W = 1440;
const H = 900;

async function settle(page) {
  for (let attempt = 0; attempt < 5; attempt += 1) {
    await page.waitForSelector('.app[data-core="1"]', { timeout: 60_000 });
    const reloaded = await page.waitForEvent('framenavigated', { timeout: 4_000 }).then(() => true).catch(() => false);
    if (!reloaded) return;
  }
  throw new Error('page kept reloading');
}

/**
 * 有没有挂着会拦下离开的 beforeunload：发一个合成的 beforeunload，看有没有谁取消它。
 * 不数 addEventListener：Vite 的开发客户端自己也挂一个（只记一下要卸载，不拦）。
 */
const unloadBlocked = (page) => page.evaluate(() => {
  const event = new Event('beforeunload', { cancelable: true });
  window.dispatchEvent(event);
  return event.defaultPrevented;
});

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

/** 页面里记下 dragover / drop 有没有被取消（监听挂在应用之后，看得到应用的 preventDefault）。 */
async function watchDrags(page) {
  await page.evaluate(() => {
    window.__drags = [];
    for (const type of ['dragover', 'drop']) {
      window.addEventListener(type, (e) => window.__drags.push({ type, files: Array.from(e.dataTransfer?.types ?? []).includes('Files'), prevented: e.defaultPrevented }));
    }
  });
}
const drags = (page) => page.evaluate(() => window.__drags.splice(0));

/** CDP 的真实文件拖动：进窗、悬停、（可选）松开。 */
async function cdpDrag(cdp, files, steps = ['dragEnter', 'dragOver', 'drop']) {
  const data = { items: [], files, dragOperationsMask: 1 };
  for (const type of steps) await cdp.send('Input.dispatchDragEvent', { type, x: W / 2, y: H / 2, data });
}

/** 页面里合成的拖动事件。`kind`：files（带一个 File）、text、link。 */
async function synthDrag(page, type, kind) {
  return page.evaluate(([t, k]) => {
    const dt = new DataTransfer();
    if (k === 'files') dt.items.add(new File(['x'], 'R0000001.JPG', { type: 'image/jpeg' }));
    else if (k === 'text') dt.setData('text/plain', 'hello');
    else dt.setData('text/uri-list', 'https://example.com/');
    const event = new DragEvent(t, { dataTransfer: dt, bubbles: true, cancelable: true });
    document.querySelector('.view').dispatchEvent(event);
    return event.defaultPrevented;
  }, [type, kind]);
}

const overlay = (page) => page.evaluate(() => {
  const node = document.querySelector('.filedrop');
  return {
    on: document.querySelector('.app')?.dataset.filedrop === '1',
    visible: Boolean(node && getComputedStyle(node).display !== 'none' && node.getBoundingClientRect().width >= window.innerWidth - 1),
    text: node?.textContent ?? null,
  };
});

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
const toastText = (page) => page.evaluate(() => document.querySelector('.ftoast')?.textContent ?? null);
async function waitToast(page, timeout = 5_000) {
  await page.waitForFunction(() => Boolean(document.querySelector('.ftoast')), null, { timeout }).catch(() => {});
  return toastText(page);
}
async function shoot(page, name, locator = null) {
  if (!manifest.shotsDir) return;
  const file = path.join(manifest.shotsDir, name);
  if (locator) await locator.screenshot({ path: file }).catch(() => {});
  else await page.screenshot({ path: file });
}

/** 开始页拖进 2 对 + 两个相机配方文件；网格上再拖（不重复）；编辑器里拖（不收）；提示的显示和收起；文字拖动不拦。 */
async function dropFlow(browser) {
  const { ctx, page } = await openPage(browser, 'drop', { viewport: { width: W, height: H } });
  const step = {};
  out.steps.drop = step;
  await watchDrags(page);
  const cdp = await ctx.newCDPSession(page);
  const navs = [];
  page.on('framenavigated', (frame) => { if (frame === page.mainFrame()) navs.push(1); });

  // 开始页：悬停时铺满窗口的提示。
  await cdpDrag(cdp, manifest.startDrop, ['dragEnter', 'dragOver']);
  step.startOverlay = await overlay(page);
  await shoot(page, 'drop-overlay-start.png');
  await cdpDrag(cdp, manifest.startDrop, ['drop']);
  step.startDrags = await drags(page);
  step.overlayAfterDrop = await overlay(page);
  await waitCells(page, 2).catch(() => {});
  step.startCells = await cellCount(page);
  step.screenAfterStart = await page.evaluate(() => document.querySelector('.app')?.dataset.screen);
  // 拖放没导入（改动之前的代码）：用选择框补上，后面几步照样能跑，各自报自己的结果。
  if (step.startCells < 2) {
    await page.locator('input.filepick:not(.calibpick)').setInputFiles(manifest.startDrop);
    await waitCells(page, 2);
  }
  step.ids = await page.evaluate(() => [...document.querySelectorAll('.cell')].map((c) => c.dataset.id));
  await page.waitForFunction(() => window.__gxrBook?.dat && !window.__gxrBook.datBusy, null, { timeout: 60_000 }).catch(() => {});
  step.camfiles = await page.evaluate(() => ({ generation: window.__gxrBook?.dat?.generation ?? null, library: Boolean(window.__gxrBook?.dat?.library), saved: window.__gxrBook?.dat?.savedAt != null }));

  // 网格：再拖 1 对旧的 + 1 对新的，只多一张。
  await cdpDrag(cdp, manifest.gridDrop, ['dragEnter', 'dragOver']);
  step.gridOverlay = await overlay(page);
  await cdpDrag(cdp, manifest.gridDrop, ['drop']);
  step.gridDrags = await drags(page);
  await waitCells(page, 3).catch(() => {});
  await page.waitForTimeout(500);
  step.gridCells = await cellCount(page);

  // 提示收起：拖出窗口（dragleave）和 Esc。CDP 没有 dragLeave，用页面里合成的事件。
  await synthDrag(page, 'dragenter', 'files');
  await synthDrag(page, 'dragover', 'files');
  const shownBeforeLeave = (await overlay(page)).on;
  await synthDrag(page, 'dragleave', 'files');
  await page.waitForTimeout(400);
  step.leave = { shownBeforeLeave, afterLeave: (await overlay(page)).on };
  await synthDrag(page, 'dragover', 'files');
  const shownBeforeEsc = (await overlay(page)).on;
  await page.keyboard.press('Escape');
  step.esc = { shownBeforeEsc, afterEsc: (await overlay(page)).on };
  // 指针在页面里的元素之间挪（dragleave 紧跟着 dragover）：提示不闪掉。
  await synthDrag(page, 'dragover', 'files');
  await synthDrag(page, 'dragleave', 'files');
  await synthDrag(page, 'dragover', 'files');
  await page.waitForTimeout(400);
  step.innerMoveKeepsOverlay = (await overlay(page)).on;
  await page.keyboard.press('Escape');
  await drags(page);

  // 文字、链接的拖动不拦（合成事件，和 CDP 的文字拖动）。
  step.text = {
    synthOverText: await synthDrag(page, 'dragover', 'text'),
    synthDropText: await synthDrag(page, 'drop', 'text'),
    synthOverLink: await synthDrag(page, 'dragover', 'link'),
    overlayOnText: (await overlay(page)).on,
  };
  await drags(page);
  const textData = { items: [{ mimeType: 'text/plain', data: 'hello' }], dragOperationsMask: 1 };
  for (const type of ['dragEnter', 'dragOver', 'drop']) await cdp.send('Input.dispatchDragEvent', { type, x: W / 2, y: H / 2, data: textData });
  step.text.cdp = await drags(page);

  // 编辑器：拖进来不导入，取消默认动作，提示回到照片列表。
  await page.locator(cellOf(manifest.openStem)).evaluate((node) => node.click());
  await page.waitForFunction((want) => document.querySelector('.editor')?.dataset.photo === want, manifest.openStem, { timeout: 120_000 });
  await page.evaluate(() => document.querySelectorAll('.ftoast').forEach((n) => n.remove()));
  await cdpDrag(cdp, manifest.editorDrop, ['dragEnter', 'dragOver']);
  step.editorOverlay = await overlay(page);
  await cdpDrag(cdp, manifest.editorDrop, ['drop']);
  step.editorDrags = await drags(page);
  step.editorToast = await waitToast(page);
  await shoot(page, 'drop-editor-toast.png', page.locator('.ftoast'));
  await page.waitForTimeout(800);
  step.editorScreen = await page.evaluate(() => document.querySelector('.app')?.dataset.screen);
  await page.getByRole('button', { name: '照片', exact: true }).click();
  await page.waitForSelector('[data-screen="grid"]');
  await page.waitForTimeout(500);
  step.cellsAfterEditorDrop = await cellCount(page);
  step.navigations = navs.length;
  step.sameApp = await page.evaluate(() => Boolean(document.querySelector('.app')) && location.protocol.startsWith('http'));
  await ctx.close();
}

/** 刷新时先问一声：有照片时弹 beforeunload，清空以后、没有照片时都不弹。 */
async function unloadFlow(browser) {
  const { ctx, page } = await openPage(browser, 'unload', { viewport: { width: W, height: H } });
  const step = {};
  out.steps.unload = step;
  const dialogs = [];
  page.on('dialog', (dialog) => {
    dialogs.push(dialog.type());
    void dialog.dismiss().catch(() => {});
  });
  const reloadAsk = async () => {
    const before = dialogs.length;
    await page.mouse.click(W / 2, 20); // 有过用户操作，Chrome 才会弹 beforeunload
    await page.reload({ timeout: 4_000 }).catch(() => {});
    await page.waitForTimeout(300);
    return dialogs.slice(before);
  };
  step.emptyStart = await reloadAsk();
  await settle(page);
  await page.locator('input.filepick:not(.calibpick)').setInputFiles(manifest.pairs);
  await waitCells(page, 2);
  step.blocksLoaded = await unloadBlocked(page);
  step.loaded = await reloadAsk();
  step.stayed = (await cellCount(page)) >= 2;
  await page.getByRole('button', { name: '更多', exact: true }).click();
  await page.getByText('清空照片，重新选', { exact: true }).click();
  await page.locator('.app[data-screen="start"] .start').waitFor();
  step.blocksCleared = await unloadBlocked(page);
  step.afterClear = await reloadAsk();
  await ctx.close();
}

/** 没用上的文件、只选 GXRFX.BIN、Tab 顺序（开始页）。每一段各开一个新页面，一段出错不挡后面几段。 */
async function unusedPicks(browser, step) {
  const { ctx, page } = await openPage(browser, 'picks', { viewport: { width: W, height: H } });
  const picker = page.locator('input.filepick:not(.calibpick)');
  await picker.setInputFiles([manifest.txt]);
  step.nothingToast = await waitToast(page);
  step.nothingScreen = await page.evaluate(() => document.querySelector('.app')?.dataset.screen);
  await shoot(page, 'note-nothing-usable.png');
  await page.evaluate(() => document.querySelectorAll('.ftoast').forEach((n) => n.remove()));
  await picker.setInputFiles([...manifest.pairs, ...manifest.renamed]);
  await waitCells(page, 2).catch(() => {});
  step.someToast = await waitToast(page);
  step.someCells = await cellCount(page);
  await shoot(page, 'note-two-unused.png', page.locator('.ftoast'));
  await ctx.close();
}

async function binOnlyStart(browser, step) {
  const { ctx, page } = await openPage(browser, 'binOnly', { viewport: { width: W, height: H } });
  const picker = page.locator('input.filepick:not(.calibpick)');
  const startErr = () => page.evaluate(() => document.querySelector('[data-state="start-dat-error"]')?.textContent ?? null);
  await picker.setInputFiles([manifest.badDat]);
  await page.waitForFunction(() => Boolean(document.querySelector('[data-state="start-dat-error"]')), null, { timeout: 30_000 }).catch(() => {});
  step.badDatError = await startErr();
  await picker.setInputFiles([manifest.bin]);
  await page.waitForTimeout(800);
  step.binOnlyStart = await startErr();
  await shoot(page, 'start-bin-only.png');
  await picker.setInputFiles([manifest.dat]);
  await page.waitForFunction(() => window.__gxrBook && !window.__gxrBook.datBusy, null, { timeout: 30_000 }).catch(() => {});
  await page.waitForTimeout(300);
  step.datAfterBin = await page.evaluate(() => ({ generation: window.__gxrBook?.dat?.generation ?? null, library: Boolean(window.__gxrBook?.dat?.library) }));
  await ctx.close();
}

async function datOnly(browser, step) {
  const { ctx, page } = await openPage(browser, 'datOnly', { viewport: { width: W, height: H } });
  const picker = page.locator('input.filepick:not(.calibpick)');
  await picker.setInputFiles([manifest.dat]);
  await page.waitForFunction(() => Boolean(document.querySelector('[data-state="start-dat-error"]')), null, { timeout: 30_000 }).catch(() => {});
  step.datOnlyStart = await page.evaluate(() => document.querySelector('[data-state="start-dat-error"]')?.textContent ?? null);
  await picker.setInputFiles(manifest.pairs);
  await waitCells(page, 2);
  step.datOnlyGrid = await page.evaluate(() => document.querySelector('.datline')?.textContent ?? null);
  await ctx.close();
}

async function binOnlyGrid(browser, step) {
  const { ctx, page } = await openPage(browser, 'binGrid', { viewport: { width: W, height: H } });
  const picker = page.locator('input.filepick:not(.calibpick)');
  await picker.setInputFiles(manifest.pairs);
  await waitCells(page, 2);
  await picker.setInputFiles([manifest.bin]);
  await page.waitForTimeout(800);
  step.binOnlyGrid = await page.evaluate(() => {
    const line = document.querySelector('.datline');
    return line && !line.hidden ? line.textContent : null;
  });
  await ctx.close();
}

async function tabOrder(browser, step) {
  const { ctx, page } = await openPage(browser, 'tab', { viewport: { width: W, height: H } });
  step.inputs = await page.evaluate(() => [...document.querySelectorAll('input[type="file"]')].map((i) => ({ tab: i.tabIndex, hidden: i.getAttribute('aria-hidden') })));
  const order = [];
  for (let n = 0; n < 16; n += 1) {
    await page.keyboard.press('Tab');
    order.push(await page.evaluate(() => {
      const a = document.activeElement;
      return a ? `${a.tagName.toLowerCase()}${a.type ? `:${a.type}` : ''}${a.dataset?.act ? `#${a.dataset.act}` : ''}` : 'none';
    }));
  }
  step.tabOrder = order;
  const chooser = async (act) => {
    const wait = page.waitForEvent('filechooser', { timeout: 5_000 }).then((fc) => fc.isMultiple()).catch(() => null);
    await page.locator(`[data-act="${act}"]`).click();
    return wait;
  };
  step.choosers = { camfiles: await chooser('pick-camfiles'), photos: await chooser('pick-photos') };
  await ctx.close();
}

/** 同名不同照片（两张卡）：先后选两张不同的同名 JPG 得到 2 格；再选同一个文件不多出一格。 */
async function sameName(browser) {
  const { ctx, page } = await openPage(browser, 'sameName', { viewport: { width: W, height: H } });
  const step = {};
  out.steps.sameName = step;
  const picker = page.locator('input.filepick:not(.calibpick)');
  await picker.setInputFiles([manifest.sameA]);
  await waitCells(page, 1);
  await picker.setInputFiles([manifest.sameA]);
  await page.waitForTimeout(800);
  step.afterRepickA = await cellCount(page);
  await picker.setInputFiles([manifest.sameB]);
  await waitCells(page, 2).catch(() => {});
  await page.waitForTimeout(800);
  step.afterB = await cellCount(page);
  await picker.setInputFiles([manifest.sameB]);
  await page.waitForTimeout(800);
  step.afterRepickB = await cellCount(page);
  step.drawn = await page.evaluate(() => document.querySelectorAll('.cell canvas[data-drawn]').length);
  await ctx.close();
}

const cardFacts = (page) => page.evaluate(() => ({
  modal: Boolean(document.querySelector('.dlg[data-state="card-removed"]')),
  gone: document.querySelectorAll('.cell.gone').length,
  err: [...document.querySelectorAll('.cell.err')].map((c) => c.dataset.id),
  cells: document.querySelectorAll('.cell').length,
}));

/** 导出时一个文件被删了：只标这一张读不了，不弹「存储卡被拔出了」；整个文件夹都没了才按拔卡处理。 */
async function lostFile(browser) {
  const { ctx, page } = await openPage(browser, 'lostFile', { viewport: { width: W, height: H } });
  const step = {};
  out.steps.lostFile = step;
  await page.locator('input.filepick:not(.calibpick)').setInputFiles(manifest.cardFiles);
  await waitCells(page, manifest.cardStems.length);
  for (const file of manifest.lostFiles) rmSync(file);
  const [lost, a, b, c, d] = manifest.cardStems;
  await exportFromGrid(page, [lost, a, b], '小', '低');
  await waitPhase(page, ['ready', 'stopped', 'held'], 600_000);
  await page.waitForTimeout(1500);
  step.oneFile = await cardFacts(page);
  step.lostId = lost.toLowerCase();
  if (await page.locator('.ms[data-state="unreadable"]').count()) await page.locator('.scrim').last().click({ position: { x: 5, y: 5 } });
  await page.evaluate(() => window.__devExport?.flow?.clear());
  rmSync(manifest.cardDir, { recursive: true, force: true });
  await exportFromGrid(page, [c, d], '小', '低');
  await page.waitForFunction(() => Boolean(document.querySelector('.dlg[data-state="card-removed"]')) || ['ready', 'stopped'].includes(document.querySelector('.pdock')?.dataset.phase), null, { timeout: 600_000 });
  await page.waitForTimeout(1500);
  step.wholeFolder = await cardFacts(page);
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

/** WebKit 手机和 iPad（有触摸）：选了照片也不挂 beforeunload。 */
async function webkitTouch(browser, label, options, inits) {
  const { ctx, page } = await openPage(browser, label, { ...options, hasTouch: true, isMobile: true }, inits);
  await page.locator('input.filepick:not(.calibpick)').setInputFiles(manifest.pairs);
  await waitCells(page, 2);
  out.steps[label] = {
    touch: await page.evaluate(() => navigator.maxTouchPoints),
    fine: await page.evaluate(() => matchMedia('(pointer: fine)').matches),
    blocks: await unloadBlocked(page),
    onbeforeunload: await page.evaluate(() => window.onbeforeunload === null),
    cells: await cellCount(page),
  };
  await ctx.close();
}

try {
  const chromium = await launchPinnedChromium({ args: GPU_ARGS });
  try {
    // 每一段各记各的错（改动之前的代码在半路就会卡住），后面几段照样跑。
    const run = async (name, fn) => {
      try {
        await fn();
      } catch (err) {
        out.flowErrors[name] = String(err?.message ?? err).split('\n')[0].slice(0, 200);
      }
    };
    await run('drop', () => dropFlow(chromium));
    await run('unload', () => unloadFlow(chromium));
    await run('sameName', () => sameName(chromium));
    await run('lostFile', () => lostFile(chromium));
    out.steps.picks = {};
    for (const fn of [unusedPicks, binOnlyStart, datOnly, binOnlyGrid, tabOrder]) await run(fn.name, () => fn(chromium, out.steps.picks));
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
