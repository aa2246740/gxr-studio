// 电脑上一批下载打成一个 ZIP（2026-11-26，desktop-accept F1 / F2 / F9 / F10 / F11）。真实照片，1440×900、没有触摸点。
// - zip：Chromium 里每张单独导出一次（1 个 JPG），再 11 张、14 张各导出一批，点一次「下载」只有 1 个 .zip；WebKit 电脑同样 14 张。
// - held：内存上限停住的一批写「先下载这 N 张，剩下 M 张接着做」，点一次下 1 个 ZIP 并接着做；拆包「下载第 1/2 包」；
//   WebKit 手机和 iPad（有触摸）照旧走共享面板，只有 held 那句换了。
// - hdr：没对齐的 RAW+JPG（hdr_reason = not_aligned）在详情里有自己的一句。
// - repick：文件读不了以后详情的按钮是「重新选这张」，打开选照片，选回来以后接着做成。
// 隐私：stdout 只有一行 JSON；文件名只写进 manifest.outDir 和报告（Python 只比字节，不打印）。

import { copyFileSync, existsSync, readdirSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import path from 'node:path';
import { pathToFileURL } from 'node:url';
import { cellOf, exportFromGrid, pickAll, waitPhase } from './mobile_ui_export_parity.mjs';
import { phoneTouch } from './phone_touch.mjs';

const [runtime, url, manifestPath] = process.argv.slice(2);
const manifest = JSON.parse(readFileSync(manifestPath, 'utf8'));
const { launchPinnedChromium, pinnedRuntime, verifyPinnedRuntime } = await import(pathToFileURL(runtime).href);
verifyPinnedRuntime();
const BROWSER_DIRS = [process.env.PLAYWRIGHT_BROWSERS_PATH, pinnedRuntime.browsersPath].filter(Boolean);

const out = { pageErrors: [], steps: {}, error: null };
const GPU_ARGS = ['--enable-unsafe-webgpu', '--enable-unsafe-swiftshader', '--use-angle=swiftshader', '--use-webgpu-adapter=swiftshader'];
const IPAD_UA = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/26.0 Safari/605.1.15';
const DESK = { viewport: { width: 1440, height: 900 }, deviceScaleFactor: 2, hasTouch: false, isMobile: false };
/** 点「下载」以后多等这么久，确认没有第二个下载跟上来。 */
const QUIET_MS = 2_500;

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
      scope.__shareLog.calls.push({ files: data.files.map((f) => f.name) });
      return Promise.resolve();
    },
  });
}

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

async function openPage(browser, label, options, inits, files = manifest.files, count = manifest.count) {
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
  await pickAll(page, { ...manifest, count }, files);
  return { ctx, page, downloads };
}

async function envFacts(page) {
  return page.evaluate(() => ({ touch: navigator.maxTouchPoints, fine: matchMedia('(pointer: fine)').matches, layout: document.querySelector('.app')?.dataset.layout ?? '' }));
}

async function shot(page, name) {
  if (!manifest.shotsDir) return;
  await page.screenshot({ path: path.join(manifest.shotsDir, `${name}.png`) });
}

async function readyButton(page) {
  const btn = page.locator('.pdock [data-act="share"]');
  await btn.waitFor({ state: 'visible', timeout: 60_000 });
  return { btn, text: (await btn.innerText()).trim(), aria: (await btn.getAttribute('aria-label')) ?? '' };
}

/** 等第一个下载，再静等 QUIET_MS：这次点击一共引出几个下载，存进 outDir/<prefix><名字>。 */
async function takeDownloads(downloads, from, prefix) {
  const deadline = Date.now() + 60_000;
  while (downloads.length <= from && Date.now() < deadline) await new Promise((r) => setTimeout(r, 100));
  await new Promise((r) => setTimeout(r, QUIET_MS));
  const names = [];
  for (const dl of downloads.slice(from)) {
    const name = dl.suggestedFilename();
    await dl.saveAs(path.join(manifest.outDir, `${prefix}${name}`));
    names.push(name);
  }
  return names;
}

async function dockGone(page) {
  await page.waitForFunction(() => !document.querySelector('.pdock'), null, { timeout: 15_000 }).catch(() => {});
  return (await page.locator('.pdock').count()) === 0;
}

/** 兜底表：头一句、「单张下载」折叠、链接、有没有旧的那句「哪个文件没下下来」。 */
async function backupFacts(page) {
  return page.evaluate(() => {
    const box = document.querySelector('.links[data-state="download-backup"]');
    if (!box) return null;
    const fold = box.querySelector('details');
    return {
      text: box.querySelector('p')?.textContent ?? '',
      all: box.textContent ?? '',
      summary: fold?.querySelector('summary')?.textContent ?? null,
      foldOpen: fold ? fold.open : null,
      links: [...box.querySelectorAll('a[download]')].map((a) => a.download),
      linksInFold: fold ? fold.querySelectorAll('a[download]').length : 0,
    };
  });
}

async function closeBackup(page) {
  await page.keyboard.press('Escape');
  await page.waitForFunction(() => !document.querySelector('[data-state="download-backup"]'), null, { timeout: 10_000 }).catch(() => {});
}

/** 选中 `stems` 导出（小 / 低，不开 HDR），等到就绪或停住。 */
async function exportSet(page, stems, phases = ['ready']) {
  await exportFromGrid(page, stems, '小', '低', null, { hdr: false });
  await waitPhase(page, phases, 900_000);
}

/** 一批：标签、点一次的下载数、兜底表；可选点开折叠、点一个链接看 ✓。 */
async function zipBatch(page, downloads, stems, prefix, { shots = '', clickLink = false } = {}) {
  await exportSet(page, stems);
  const ready = await readyButton(page);
  const row = { label: ready.text, aria: ready.aria };
  if (shots) await shot(page, `${shots}-ready-bar`);
  const before = downloads.length;
  await ready.btn.click();
  row.names = await takeDownloads(downloads, before, prefix);
  row.dockGone = await dockGone(page);
  row.backup = await backupFacts(page);
  if (shots) await shot(page, `${shots}-after-download`);
  if (clickLink && row.backup) {
    await page.locator('.links[data-state="download-backup"] summary').click();
    if (shots) await shot(page, `${shots}-fold-open`);
    const link = page.locator('.links[data-state="download-backup"] a[download]').first();
    const b2 = downloads.length;
    await link.click();
    row.linkDownloads = (await takeDownloads(downloads, b2, `${prefix}link-`)).length;
    row.linkText = (await link.innerText()).trim();
    row.linkMarked = (await link.getAttribute('data-clicked')) === '1';
  }
  await closeBackup(page);
  return row;
}

async function crcFacts(page) {
  return page.evaluate(async () => {
    const zw = await import('/src/lib/zip_write.ts');
    return { ...zw.crcStats };
  });
}

/** 每张单独导出、点一次「下载」（今天的单张下载，参照字节），存成 `<prefix><名字>`。 */
async function singles(page, downloads, prefix) {
  const rows = [];
  for (const [i, stem] of manifest.stems14.entries()) {
    await exportSet(page, [stem]);
    const ready = await readyButton(page);
    const before = downloads.length;
    await ready.btn.click();
    const names = await takeDownloads(downloads, before, prefix);
    rows.push({ label: i === 0 ? ready.text : undefined, names, dockGone: await dockGone(page) });
  }
  return rows;
}

/** Chromium 电脑：每张单独导出（参照字节），再 11 张一批、14 张一批。 */
async function chromiumZip(browser) {
  const { ctx, page, downloads } = await openPage(browser, 'chromium', DESK, []);
  const step = { env: await envFacts(page) };
  out.steps.chromium = step;
  step.singles = await singles(page, downloads, 'single-');
  step.b11 = await zipBatch(page, downloads, manifest.stems11, 'zip11-', { shots: 'chromium-11', clickLink: true });
  step.b14 = await zipBatch(page, downloads, manifest.stems14, 'zip14-', { shots: 'chromium-14' });
  step.crc = await crcFacts(page);
  step.share = await page.evaluate(() => window.__shareLog.calls.length);
  await ctx.close();
}

/**
 * WebKit 电脑（Safari 内核，一次点击只放行 1 个下载）：先逐张单独导出（WebKit 自己的参照字节：相机 JPG 由浏览器解码，
 * 两个内核解出来的像素可以差一点，和 ZIP 无关），再 14 张一批。
 */
async function webkitZip(browser) {
  const { ctx, page, downloads } = await openPage(browser, 'webkit', DESK, []);
  const step = { env: await envFacts(page) };
  out.steps.webkit = step;
  step.singles = await singles(page, downloads, 'wksingle-');
  step.b14 = await zipBatch(page, downloads, manifest.stems14, 'wk14-', { shots: 'webkit-14' });
  step.share = await page.evaluate(() => window.__shareLog.calls.length);
  await ctx.close();
}

/** 停在内存上限：held 那句、点一次 1 个 ZIP 并接着做、最后一包是剩下的；再用开发钩子拆包看「第 1/2 包」。 */
async function chromiumHeld(browser) {
  const { ctx, page, downloads } = await openPage(browser, 'held', DESK, []);
  const step = { env: await envFacts(page) };
  out.steps.held = step;
  await page.evaluate((n) => { window.__devExport.holdAfter = n; }, manifest.holdAfter);
  await exportSet(page, manifest.heldStems, ['held', 'ready', 'stopped']);
  step.phase = await page.locator('.pdock').getAttribute('data-phase');
  const held = await readyButton(page);
  step.heldLabel = held.text;
  step.heldAria = held.aria;
  await shot(page, 'chromium-held-bar');
  const before = downloads.length;
  await held.btn.click();
  step.heldNames = await takeDownloads(downloads, before, 'held1-');
  step.phaseAfterTap = await page.evaluate(() => window.__devExport.flow.current()?.phase ?? 'none');
  step.heldBackup = await backupFacts(page);
  await closeBackup(page);
  await page.evaluate(() => { delete window.__devExport.holdAfter; });
  await waitPhase(page, ['ready'], 900_000);
  const rest = await readyButton(page);
  step.restLabel = rest.text;
  const b2 = downloads.length;
  await rest.btn.click();
  step.restNames = await takeDownloads(downloads, b2, 'held2-');
  step.dockGone = await dockGone(page);
  await closeBackup(page);

  // 拆包（开发钩子把一包的上限降到 2 个文件）：4 个文件 → 两包，各点一次，各 1 个 ZIP。
  await page.evaluate(() => { window.__devExport.chunkFiles = 2; });
  await exportSet(page, manifest.partStems);
  step.parts = [];
  for (let k = 0; k < 3; k += 1) {
    const btn = page.locator('.pdock [data-act="share"]');
    if (!(await btn.count())) break;
    const label = (await btn.innerText()).trim();
    const b3 = downloads.length;
    await btn.click();
    step.parts.push({ label, names: await takeDownloads(downloads, b3, `part${k + 1}-`) });
    await closeBackup(page);
  }
  await page.evaluate(() => { delete window.__devExport.chunkFiles; });
  step.share = await page.evaluate(() => window.__shareLog.calls.length);
  await ctx.close();
}

/** WebKit 手机 / iPad（有触摸）：一批 2 张仍走共享面板；held 时那句换成「先把这 N 张存进相册，…」，其余照旧。 */
async function webkitTouch(browser, label, options, inits) {
  const { ctx, page, downloads } = await openPage(browser, label, { ...options, hasTouch: true, isMobile: true }, inits);
  const step = { env: await envFacts(page) };
  out.steps[label] = step;
  await exportSet(page, manifest.partStems.slice(0, 2));
  const ready = await readyButton(page);
  step.batch = { label: ready.text, aria: ready.aria };
  await ready.btn.click();
  await page.waitForFunction(() => window.__shareLog.calls.length >= 1, null, { timeout: 30_000 });
  step.batch.shared = await page.evaluate(() => window.__shareLog.calls[0].files.length);
  await dockGone(page);
  await page.evaluate((n) => { window.__devExport.holdAfter = n; }, manifest.holdAfter);
  await exportSet(page, manifest.heldStems, ['held', 'ready', 'stopped']);
  step.phase = await page.locator('.pdock').getAttribute('data-phase');
  const held = await readyButton(page);
  step.heldLabel = held.text;
  if (label === 'webkitPhone') await shot(page, 'webkit-phone-held-bar');
  await held.btn.click();
  await page.waitForFunction(() => window.__shareLog.calls.length >= 2, null, { timeout: 30_000 });
  step.heldShared = await page.evaluate(() => window.__shareLog.calls[1].files.length);
  await page.evaluate(() => { delete window.__devExport.holdAfter; });
  await waitPhase(page, ['ready'], 900_000);
  step.restLabel = (await readyButton(page)).text;
  await page.waitForTimeout(500);
  step.downloads = downloads.length;
  await ctx.close();
}

/** HDR 开着导出 card250 的几对：没对齐的那几张（not_aligned）在详情里写自己的那句。 */
async function hdrReasons(browser) {
  const { ctx, page } = await openPage(browser, 'hdr', DESK, []);
  const step = {};
  out.steps.hdr = step;
  await exportFromGrid(page, manifest.stems11, '小', '低', null, { hdr: true });
  await waitPhase(page, ['ready'], 1_500_000);
  step.reasons = await page.evaluate(() => {
    const batch = window.__devExport.flow.current();
    return [...batch.results.values()].map((r) => (r.hdr === true ? 'hdr' : String(r.hdr_reason ?? '')));
  });
  await page.locator('.pdock [data-act="toggle-details"]').click();
  await page.waitForSelector('.ms[data-state="export-details"]', { timeout: 10_000 });
  step.notes = await page.locator('.ms[data-state="export-details"] .hdrnotes li').evaluateAll((li) => li.map((n) => n.textContent.replace(/^.*?：/, '')));
  await ctx.close();
}

/** 文件读不了：删掉那一对的副本再导出；详情的按钮是「重新选这张」，点了打开选照片，选回副本后接着做成。 */
async function repick(browser) {
  const files = [...manifest.files.filter((f) => !manifest.copyFrom.includes(f)), ...manifest.copyTo];
  for (const [i, src] of manifest.copyFrom.entries()) copyFileSync(src, manifest.copyTo[i]);
  const { ctx, page } = await openPage(browser, 'repick', DESK, [], files, manifest.count);
  const step = {};
  out.steps.repick = step;
  for (const p of manifest.copyTo) rmSync(p);
  await exportFromGrid(page, manifest.repickStems, '小', '低', null, { hdr: false });
  await waitPhase(page, ['ready'], 900_000);
  step.failures = await page.evaluate(() => [...window.__devExport.flow.current().failures.values()].map((f) => f.kind));
  // 「存储卡被拔出了」那个框（F3，别的单元在改）：有就先用读进来的。
  const keep = page.getByRole('button', { name: '先用读进来的' });
  if (await keep.count()) await keep.first().click();
  await page.locator('.pdock [data-act="toggle-details"]').click();
  await page.waitForSelector('.ms[data-state="export-details"]', { timeout: 10_000 });
  const acts = page.locator('.ms[data-state="export-details"] .es-fail-acts button');
  step.buttons = await acts.evaluateAll((bs) => bs.map((b) => b.textContent.trim()));
  await shot(page, 'chromium-repick-details');
  for (const [i, src] of manifest.copyFrom.entries()) copyFileSync(src, manifest.copyTo[i]);
  const first = acts.first();
  const chooser = page.waitForEvent('filechooser', { timeout: 10_000 }).catch(() => null);
  await first.click();
  const fc = await chooser;
  step.chooserOpened = fc !== null;
  if (fc) {
    await fc.setFiles(manifest.copyTo);
    await page.waitForFunction(() => {
      const b = window.__devExport.flow.current();
      return b && b.phase !== 'running' && b.failures.size === 0;
    }, null, { timeout: 300_000 }).catch(() => {});
  }
  step.after = await page.evaluate(() => {
    const b = window.__devExport.flow.current();
    return b ? { phase: b.phase, failures: b.failures.size, ok: b.okCount() } : null;
  });
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

async function withWebkit(fn) {
  const { webkit } = await import(pathToFileURL(pinnedRuntime.modulePath).href);
  const executablePath = webkitExecutable(webkit);
  if (!executablePath) {
    out.steps.webkitMissing = true;
    return;
  }
  const browser = await webkit.launch({ headless: true, executablePath });
  try {
    await fn(browser);
  } finally {
    await browser.close();
  }
}

try {
  const mode = manifest.mode;
  if (mode !== 'touch') {
    const chromium = await launchPinnedChromium({ args: GPU_ARGS });
    try {
      if (mode === 'zip') await chromiumZip(chromium);
      if (mode === 'held') await chromiumHeld(chromium);
      if (mode === 'hdr') await hdrReasons(chromium);
      if (mode === 'repick') await repick(chromium);
    } finally {
      await chromium.close();
    }
  }
  if (mode === 'zip') await withWebkit((b) => webkitZip(b));
  if (mode === 'touch') {
    await withWebkit(async (b) => {
      await webkitTouch(b, 'webkitPhone', { viewport: { width: 390, height: 844 }, deviceScaleFactor: 2 }, []);
      await webkitTouch(b, 'webkitIpad', { viewport: { width: 1032, height: 1376 }, deviceScaleFactor: 2, userAgent: IPAD_UA }, [ipadTouch]);
    });
  }
} catch (err) {
  out.error = String(err?.message ?? err).split('\n')[0].slice(0, 300);
}
writeFileSync(manifest.reportPath, JSON.stringify(out));
process.stdout.write(`${JSON.stringify({ done: true, error: out.error !== null })}\n`);
