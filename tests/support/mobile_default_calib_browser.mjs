// 内置的默认校准（2026-11-24）在无头 Chrome 里走一遍。manifest.mode：
// - `bundled`：开发服务器照常发 public/ 里的默认校准包。起始页没有校准这一步；新的配置文件只选照片和相机配方文件就按
//   默认校准画；导出的字节和另一个配置文件手动导入同一个 zip 一样；导入别的包会盖过默认校准，恢复默认又回来；整次会话
//   只下载一次。
// - `missing` / `tampered`：默认校准下载不下来 / 文件被改过：中性转换，「未校准」写明原因，页面不抛错。`missing` 这一轮
//   顺便在页面里 import 真的 Book，用手里攥着的回复造默认校准和导入、删除的先后时序。
// 结果写进 manifest.out；stdout 只打 done。照片字节和文件名不打出来。

import { readFileSync, writeFileSync } from 'node:fs';
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
async function freshPage(label) {
  const ctx = await browser.newContext({ viewport: { width: 390, height: 844 }, deviceScaleFactor: 2 });
  const page = await ctx.newPage();
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

try {
  if (manifest.mode === 'bundled') await runBundled();
  else await runBroken();
} catch (err) {
  out.error = line(err);
} finally {
  writeFileSync(manifest.out, JSON.stringify(out));
  await browser.close();
}
console.log('done');
