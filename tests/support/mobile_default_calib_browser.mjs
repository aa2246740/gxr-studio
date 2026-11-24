// 内置的默认校准（2026-11-24）在无头 Chrome 里走一遍。manifest.mode：
// - `bundled`：开发服务器照常发 public/ 里的默认校准包。起始页没有校准这一步；新的配置文件只选照片和相机配方文件就按
//   默认校准画；导出的字节和另一个配置文件手动导入同一个 zip 一样；导入别的包会盖过默认校准，恢复默认又回来；整次会话
//   只下载一次。
// - `missing` / `tampered`：默认校准下载不下来 / 文件被改过：中性转换，「未校准」写明原因，页面不抛错。`missing` 这一轮
//   顺便在页面里 import 真的 Book，用手里攥着的回复造默认校准和导入、删除的先后时序。
// - `held`：开发服务器压着默认校准不回（`hold:<文件夹>`），驱动写 `<文件夹>/release` 才放行。默认校准还在路上时点导出：
//   A 等它落定、按它导出，和落定后再导出的字节一样；B 一直不放行：等满几秒照旧开始（中性转换），不会卡住。
// 结果写进 manifest.out；stdout 只打 done。照片字节和文件名不打出来。

import { readFileSync, rmSync, writeFileSync } from 'node:fs';
import { pathToFileURL } from 'node:url';
import { cellOf, exportFromGrid, pickAll, shareAll, sharedFiles, waitPhase } from './mobile_ui_export_parity.mjs';
import { bookCases } from './mobile_default_calib_book.mjs';

const [runtime, url, manifestPath] = process.argv.slice(2);
const manifest = JSON.parse(readFileSync(manifestPath, 'utf8'));
const { launchPinnedChromium, verifyPinnedRuntime } = await import(pathToFileURL(runtime).href);
verifyPinnedRuntime();

const browser = await launchPinnedChromium({
  args: ['--enable-unsafe-webgpu', '--enable-unsafe-swiftshader', '--use-angle=swiftshader', '--use-webgpu-adapter=swiftshader'],
});
const out = { pageErrors: [], steps: {} };
const line = (err) => (err instanceof Error ? err.message : String(err)).split('\n')[0].slice(0, 300);
const DEFAULT_PATH = '/calib/gxr-default-calib.zip';

function fakeShare() {
  window.__shareLog = { canShare: 0, calls: [] };
  Object.defineProperty(navigator, 'canShare', {
    configurable: true,
    value: (data) => Array.isArray(data?.files) && data.files.length > 0 && data.files.every((f) => f instanceof File),
  });
  Object.defineProperty(navigator, 'share', {
    configurable: true,
    value: (data) => {
      window.__shareLog.calls.push({ active: null, files: data.files });
      return Promise.resolve();
    },
  });
}

async function settle(page) {
  for (let attempt = 0; attempt < 5; attempt += 1) {
    await page.waitForSelector('.app[data-core="1"]', { timeout: 60_000 });
    const reloaded = await page.waitForEvent('framenavigated', { timeout: 4_000 }).then(() => true).catch(() => false);
    if (!reloaded) return;
  }
  throw new Error('page kept reloading');
}

/** 一个新的配置文件（自己的 IndexedDB），数一数默认校准被下载了几次。 */
async function freshPage(label, init = null) {
  const ctx = await browser.newContext({ viewport: { width: 390, height: 844 }, deviceScaleFactor: 2 });
  const page = await ctx.newPage();
  if (init) await page.addInitScript(init);
  page.on('pageerror', (error) => out.pageErrors.push(`${label}: ${line(error)}`));
  const fetches = { count: 0 };
  page.on('request', (req) => {
    if (new URL(req.url()).pathname === DEFAULT_PATH) fetches.count += 1;
  });
  await page.addInitScript(fakeShare);
  await page.goto(url);
  await settle(page);
  return { ctx, page, fetches };
}

/** Book 的状态：用的是哪份校准、默认校准为什么用不了。 */
function calibState(page) {
  return page.evaluate(() => {
    const book = window.__gxrBook;
    return { kind: book?.pack?.kind ?? null, loading: Boolean(book?.loadingDefault), error: book?.defaultError ?? null, units: (book?.pack?.units ?? []).map((row) => row.unit) };
  });
}

async function waitCalib(page, kind) {
  await page.waitForFunction(
    (want) => {
      const book = window.__gxrBook;
      if (!book || book.packBusy || book.loadingDefault) return false;
      return want === null ? book.pack === null : book.pack?.kind === want;
    },
    kind,
    { timeout: 120_000 },
  );
}

async function openPhoto(page) {
  await page.locator(cellOf(manifest.stem)).evaluate((node) => node.click());
  await page.waitForFunction((want) => document.querySelector('.editor')?.dataset.photo === want, manifest.stem, { timeout: 120_000 });
}

async function backToGrid(page) {
  await page.getByRole('button', { name: '照片', exact: true }).click();
  await page.waitForSelector('[data-screen="grid"]');
}

/** 编辑器里的配方条：状态（calibrated / uncalibrated）和状态瓷砖。 */
async function stripState(page, want) {
  await page.waitForFunction(
    (state) => {
      const strip = document.querySelector('.editor .strip[data-recipes]');
      if (strip?.dataset.state !== state) return false;
      return state !== 'calibrated' || strip.querySelectorAll('.rc[data-card^="builtin:"]:not(.lockd)').length > 0;
    },
    want,
    { timeout: 180_000 },
  );
  // 状态瓷砖：照片的 ISO、镜头和校准范围对不上时本来就有「未校准」（uncalibrated）；没有校准包才是 no-calibration。
  const tile = page.locator('.editor .panel.recipe [data-act="recipe-status"]');
  const has = (await tile.count()) > 0;
  return { state: want, tile: has ? await tile.first().innerText() : null, tileState: has ? await tile.first().getAttribute('data-state') : null };
}

async function chooseBuiltin1(page) {
  const card = page.locator('.editor .strip[data-recipes] .rc[data-card="builtin:1"]');
  await card.scrollIntoViewIfNeeded();
  await card.click();
  await page.waitForFunction(() => document.querySelector('.editor .strip[data-recipes] .rc.on')?.dataset.card === 'builtin:1', null, { timeout: 60_000 });
}

/** 「更多 → 校准数据」那张卡的状态和文字（从图库 ⋯ 打开）；`act` 给了就在卡上点那个按钮。 */
async function calibSheet(page, act = null) {
  await page.getByRole('button', { name: '更多' }).click();
  const row = page.locator('.ms .lrow', { hasText: '校准数据' });
  const detail = await row.locator('small').innerText();
  await row.click();
  await page.waitForSelector('.calib-sheet .calib[data-calib]', { timeout: 10_000 });
  const card = page.locator('.calib-sheet .calib');
  const seen = { calib: await card.getAttribute('data-calib'), text: await card.innerText(), detail };
  if (act) await card.locator('button', { hasText: act }).click();
  return { seen, close: () => page.locator('.calib-sheet button', { hasText: '完成' }).click() };
}

/** 选中这张、导出（小 / 高，不做 HDR），收下假共享面板拿到的文件，写成 `<前缀><文件名>`。 */
async function exportOne(page, prefix) {
  const before = await page.evaluate(() => window.__shareLog.calls.length);
  await exportFromGrid(page, [manifest.stem], '小', '高', null, { hdr: false });
  await waitPhase(page, ['ready']);
  await shareAll(page);
  const files = await sharedFiles(page, manifest.outDir, prefix, before);
  await page.waitForFunction(() => !document.querySelector('.pdock'), null, { timeout: 30_000 }).catch(() => undefined);
  return files.flatMap((row) => row.files.map((file) => file.name));
}

async function startScreen(page) {
  const info = await page.evaluate(() => ({
    cards: document.querySelectorAll('.start .calib').length,
    buttons: [...document.querySelectorAll('.start button')].map((node) => node.textContent.trim()),
  }));
  if (manifest.shots) await page.screenshot({ path: `${manifest.shots}/first-run-after-390x844.png`, clip: { x: 0, y: 0, width: 390, height: 844 } });
  return info;
}

async function runBundled() {
  // A：新的配置文件，不导入：只选照片和相机配方文件，按默认校准画、导出。
  const a = await freshPage('A');
  out.steps.start = await startScreen(a.page);
  await waitCalib(a.page, 'default');
  out.steps.bootState = await calibState(a.page);
  await pickAll(a.page, { count: 1 }, manifest.files);
  await a.page.waitForFunction(() => window.__gxrBook?.dat && !window.__gxrBook.datBusy, null, { timeout: 120_000 });
  await openPhoto(a.page);
  const defaultStrip = await stripState(a.page, 'calibrated');
  await chooseBuiltin1(a.page);
  await backToGrid(a.page);
  const sheet = await calibSheet(a.page);
  await sheet.close();
  const defaultFiles = await exportOne(a.page, 'default-');
  out.steps.defaultA = { strip: defaultStrip, sheet: sheet.seen, files: defaultFiles, fetches: a.fetches.count };
  await a.ctx.close();

  // B：另一个新的配置文件，手动导入同一个 zip（「换成自己的校准…」那个选择框），再导出。
  const b = await freshPage('B');
  await waitCalib(b.page, 'default');
  await pickAll(b.page, { pack: manifest.bundled, count: 1 }, manifest.files);
  await waitCalib(b.page, 'user');
  await b.page.waitForFunction(() => window.__gxrBook?.dat && !window.__gxrBook.datBusy, null, { timeout: 120_000 });
  await openPhoto(b.page);
  const handStrip = await stripState(b.page, 'calibrated');
  await chooseBuiltin1(b.page);
  await backToGrid(b.page);
  const handFiles = await exportOne(b.page, 'hand-');
  out.steps.handB = { strip: handStrip, state: await calibState(b.page), files: handFiles };

  // 换成别的包（合成的，参数不同）：盖过默认校准，导出的颜色跟着变。
  const replace = await calibSheet(b.page);
  await b.page.locator('input.calibpick').setInputFiles(manifest.other);
  await b.page.waitForFunction(() => window.__gxrBook?.pack?.kind === 'user' && window.__gxrBook.pack.name !== 'gxr-default-calib.zip' && !window.__gxrBook.packBusy, null, { timeout: 60_000 });
  const replaced = { calib: await b.page.locator('.calib-sheet .calib').getAttribute('data-calib'), text: await b.page.locator('.calib-sheet .calib').innerText() };
  await replace.close();
  const otherFiles = await exportOne(b.page, 'other-');
  out.steps.otherB = { before: replace.seen, after: replaced, state: await calibState(b.page), files: otherFiles };

  // 恢复默认校准：回到默认校准（这次会话已经核对过的那份，不再下载），导出又和 A 一样。
  const restore = await calibSheet(b.page, '恢复默认校准');
  await waitCalib(b.page, 'default');
  const restored = { calib: await b.page.locator('.calib-sheet .calib').getAttribute('data-calib'), text: await b.page.locator('.calib-sheet .calib').innerText() };
  await restore.close();
  const restoredFiles = await exportOne(b.page, 'restored-');
  out.steps.restoredB = { before: restore.seen, after: restored, state: await calibState(b.page), files: restoredFiles, fetches: b.fetches.count };
  // 重新打开页面：恢复默认把导入的包删掉了，读回来的还是默认校准。
  await b.page.reload();
  await settle(b.page);
  await waitCalib(b.page, 'default');
  out.steps.reopenedB = { state: await calibState(b.page), fetches: b.fetches.count };
  await b.ctx.close();
}

async function runBroken() {
  const p = await freshPage(manifest.mode);
  out.steps.start = await startScreen(p.page);
  await waitCalib(p.page, null);
  out.steps.bootState = await calibState(p.page);
  await pickAll(p.page, { count: 1 }, manifest.files);
  await p.page.waitForFunction(() => window.__gxrBook?.dat && !window.__gxrBook.datBusy, null, { timeout: 120_000 });
  await openPhoto(p.page);
  const strip = await stripState(p.page, 'uncalibrated');
  await p.page.locator('.editor .toolbar [data-tool="recipe"]').click();
  const tile = p.page.locator('.editor .panel.recipe [data-state="no-calibration"]');
  const tag = await tile.innerText();
  await tile.click();
  await p.page.waitForSelector('.recipe-info-sheet [data-state="no-calibration"]');
  const banner = await p.page.locator('.recipe-info-sheet [data-state="no-calibration"]').innerText();
  const reason = await p.page.locator('.recipe-info-sheet [data-state="no-calibration"]').getAttribute('data-reason');
  await p.page.locator('.recipe-info-sheet [data-act="close"]').click();
  await backToGrid(p.page);
  const sheet = await calibSheet(p.page);
  await sheet.close();
  out.steps.broken = { strip, tag, banner, reason, sheet: sheet.seen, fetches: p.fetches.count };
  if (manifest.mode === 'missing') out.steps.book = await bookCases(p.page);
  await p.ctx.close();
}

/** 点「存到相册」（小 / 高，不做 HDR），不等它做完；返回点下去之后多久这一批才真的开始（毫秒）。 */
async function clickExport(page) {
  await exportFromGrid(page, [manifest.stem], '小', '高', null, { hdr: false });
  const t0 = Date.now();
  await page.waitForFunction(() => window.__devExport?.flow?.current() != null, null, { timeout: 60_000, polling: 50 });
  return Date.now() - t0;
}

async function shareReady(page, prefix, before) {
  await waitPhase(page, ['ready']);
  await shareAll(page);
  const files = await sharedFiles(page, manifest.outDir, prefix, before);
  await page.waitForFunction(() => !document.querySelector('.pdock'), null, { timeout: 30_000 }).catch(() => undefined);
  return files.flatMap((row) => row.files.map((file) => file.name));
}

async function heldPage(label) {
  const p = await freshPage(label);
  await pickAll(p.page, { count: 1 }, manifest.files);
  await p.page.waitForFunction(() => window.__gxrBook?.dat && !window.__gxrBook.datBusy, null, { timeout: 120_000 });
  await openPhoto(p.page);
  await chooseBuiltin1(p.page);
  await backToGrid(p.page);
  return p;
}

async function runHeld() {
  const release = `${manifest.holdDir}/release`;
  rmSync(release, { force: true });
  // A：默认校准还压着时点导出，一秒后放行：这一批等它落定再开始，按默认校准做。
  const a = await heldPage('held-A');
  const before = await calibState(a.page);
  let shares = await a.page.evaluate(() => window.__shareLog.calls.length);
  await exportFromGrid(a.page, [manifest.stem], '小', '高', null, { hdr: false });
  await a.page.waitForTimeout(1000);
  const waiting = await a.page.evaluate(() => ({ batch: window.__devExport?.flow?.current() != null, loading: window.__gxrBook.loadingDefault }));
  writeFileSync(release, '');
  const heldFiles = await shareReady(a.page, 'held-', shares);
  const landed = await calibState(a.page);
  shares = await a.page.evaluate(() => window.__shareLog.calls.length);
  await clickExport(a.page);
  const afterFiles = await shareReady(a.page, 'after-', shares);
  out.steps.heldA = { before, waiting, landed, heldFiles, afterFiles };
  await a.ctx.close();

  // B：一直不放行：等满几秒照旧开始（中性转换），不卡住；放行之后再导出就是校准过的。
  rmSync(release, { force: true });
  const b = await heldPage('held-B');
  shares = await b.page.evaluate(() => window.__shareLog.calls.length);
  const startedAfter = await clickExport(b.page);
  const stillLoading = await b.page.evaluate(() => ({ loading: window.__gxrBook.loadingDefault, kind: window.__gxrBook.pack?.kind ?? null }));
  const timeoutFiles = await shareReady(b.page, 'timeout-', shares);
  writeFileSync(release, '');
  await waitCalib(b.page, 'default');
  shares = await b.page.evaluate(() => window.__shareLog.calls.length);
  await clickExport(b.page);
  const laterFiles = await shareReady(b.page, 'later-', shares);
  out.steps.heldB = { startedAfter, stillLoading, timeoutFiles, laterFiles };
  await b.ctx.close();
  await runHeldQuick();
  await runHeldOffImage();
}

/** 「存到相册」记住的选项：小 / 高，不做 HDR（快一点）。 */
function quickPrefs() {
  localStorage.setItem('gxr.mobile.export_size', 'small');
  localStorage.setItem('gxr.mobile.export_quality', 'high');
  localStorage.setItem('gxr.mobile.export_hdr', '0');
}

async function quickPage(label) {
  const p = await freshPage(label, quickPrefs);
  await pickAll(p.page, { count: 1 }, manifest.files);
  await p.page.waitForFunction(() => window.__gxrBook?.dat && !window.__gxrBook.datBusy, null, { timeout: 120_000 });
  await openPhoto(p.page);
  await chooseBuiltin1(p.page);
  return p;
}

/** 这一批：几张、阶段、结果的 render_kind、是不是还是第一批。 */
function batchNow(page) {
  return page.evaluate(() => {
    const batch = window.__devExport?.flow?.current() ?? null;
    if (!batch) return null;
    window.__firstBatch ??= batch;
    const results = [...batch.results.values()];
    return { items: batch.items.length, phase: batch.phase, kinds: results.map((r) => r?.render_kind ?? null), first: batch === window.__firstBatch };
  });
}

async function runHeldQuick() {
  const release = `${manifest.holdDir}/release`;
  const tapExport = (page) => page.locator('.editor .toolbar [data-act="export"]').click();
  // C：编辑器里连点三次「存到相册」，默认校准还压着；一秒后放行：只做一批、一张，按默认校准做。
  rmSync(release, { force: true });
  const c = await quickPage('quick-C');
  let shares = await c.page.evaluate(() => window.__shareLog.calls.length);
  for (let i = 0; i < 3; i += 1) await tapExport(c.page);
  await c.page.waitForTimeout(1000);
  const waiting = await c.page.evaluate(() => ({ batch: window.__devExport?.flow?.current() != null, loading: window.__gxrBook.loadingDefault }));
  writeFileSync(release, '');
  await waitPhase(c.page, ['ready']);
  const ready = await batchNow(c.page);
  const quickFiles = await shareReady(c.page, 'quick-held-', shares);
  await c.page.waitForTimeout(1500);
  const settled = { batch: await batchNow(c.page), shares: (await c.page.evaluate(() => window.__shareLog.calls.length)) - shares };
  // 默认校准已经落定：再点一次，这一批就是按默认校准做的，和上面那一张应该一样。
  shares = await c.page.evaluate(() => window.__shareLog.calls.length);
  await tapExport(c.page);
  const quickAfter = await shareReady(c.page, 'quick-after-', shares);
  out.steps.quickC = { waiting, ready, settled, quickFiles, quickAfter };
  await c.ctx.close();

  // D：一直不放行：连点三次只等一次，约 4 秒后开始一批一张（中性转换）；开始后再点两次不会再做；做完分享后再等几秒，
  // 也不会有排在后面的点击再做一批。
  rmSync(release, { force: true });
  const d = await quickPage('quick-D');
  shares = await d.page.evaluate(() => window.__shareLog.calls.length);
  for (let i = 0; i < 3; i += 1) await tapExport(d.page);
  const t0 = Date.now();
  await d.page.waitForFunction(() => window.__devExport?.flow?.current() != null, null, { timeout: 60_000, polling: 50 });
  const startedAfter = Date.now() - t0;
  const started = await batchNow(d.page);
  await tapExport(d.page);
  await tapExport(d.page);
  await waitPhase(d.page, ['ready']);
  const readyD = await batchNow(d.page);
  const timeoutFiles = await shareReady(d.page, 'quick-timeout-', shares);
  await d.page.waitForTimeout(9000);
  const laterD = { batch: await batchNow(d.page), shares: (await d.page.evaluate(() => window.__shareLog.calls.length)) - shares, loading: await d.page.evaluate(() => window.__gxrBook.loadingDefault) };
  // 放行之后再点一次：按默认校准做，和到时开始的那一张（中性转换）不同。
  writeFileSync(release, '');
  await waitCalib(d.page, 'default');
  shares = await d.page.evaluate(() => window.__shareLog.calls.length);
  await tapExport(d.page);
  const calibratedFiles = await shareReady(d.page, 'quick-calibrated-', shares);
  out.steps.quickD = { startedAfter, started, ready: readyD, later: laterD, timeoutFiles, calibratedFiles };
  await d.ctx.close();
}

async function offImageSheet(page) {
  await page.locator('.editor .nav [data-act="editor-more"]').click();
  await page.waitForSelector('.ms[data-state="editor-more"]', { timeout: 10_000 });
  await page.locator('.ms[data-state="editor-more"] [data-act="more-off-image"]').click();
  await page.waitForSelector('.ms.off-image-sheet', { timeout: 15_000 });
}

async function offImageExport(page, prefix) {
  await page.waitForFunction(
    () => {
      const sheet = document.querySelector('.ms.off-image-sheet');
      const btn = sheet?.querySelector('[data-act="off-image-export"]');
      return sheet?.dataset.ready === '1' && btn && !btn.disabled;
    },
    null,
    { timeout: 120_000 },
  );
  const before = await page.evaluate(() => window.__shareLog.calls.length);
  await page.locator('.ms.off-image-sheet [data-act="off-image-export"]').click();
  await page.waitForFunction((k) => window.__shareLog.calls.length > k, before, { timeout: 30_000 });
  const files = await sharedFiles(page, manifest.outDir, prefix, before);
  await page.locator('.ms.off-image-sheet [data-act="off-image-close"]').click();
  await page.waitForFunction(() => !document.querySelector('.ms.off-image-sheet'), null, { timeout: 10_000 });
  return files.flatMap((row) => row.files.map((file) => file.name));
}

async function runHeldOffImage() {
  const release = `${manifest.holdDir}/release`;
  // E：默认校准还压着时打开「做成关机图」，一秒后放行：GXROFF.JPG 按默认校准做，和落定后再做的一样。
  rmSync(release, { force: true });
  const e = await quickPage('off-E');
  await offImageSheet(e.page);
  await e.page.waitForTimeout(1000);
  const waiting = await e.page.evaluate(() => ({ ready: document.querySelector('.ms.off-image-sheet')?.dataset.ready === '1', loading: window.__gxrBook.loadingDefault }));
  writeFileSync(release, '');
  const heldFiles = await offImageExport(e.page, 'off-held-');
  await waitCalib(e.page, 'default');
  await offImageSheet(e.page);
  const afterFiles = await offImageExport(e.page, 'off-after-');
  out.steps.offE = { waiting, heldFiles, afterFiles };
  await e.ctx.close();
}

try {
  if (manifest.mode === 'bundled') await runBundled();
  else if (manifest.mode === 'held') await runHeld();
  else await runBroken();
} catch (err) {
  out.error = line(err);
} finally {
  writeFileSync(manifest.out, JSON.stringify(out));
  await browser.close();
}
console.log('done');
