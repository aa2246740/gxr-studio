// r5 导出：校准包 + 一张真实 RAW+JPG + 这对 GXRCE.DAT/GXRFX.BIN，点 builtin:1，原始尺寸 / 高。
// 假共享面板收下的文件写进 manifest.outDir。g178 只有 4 个内置位，不要等满 8 张 builtin 卡。
// stdout 只打 done。照片字节和文件名不打出来。

import { readFileSync, writeFileSync } from 'node:fs';
import { pathToFileURL } from 'node:url';
import { batchFacts, cellOf, exportFromGrid, pickAll, shareAll, sharedFiles, waitPhase } from './mobile_ui_export_parity.mjs';

const [runtime, url, manifestPath] = process.argv.slice(2);
const manifest = JSON.parse(readFileSync(manifestPath, 'utf8'));
const { launchPinnedChromium, verifyPinnedRuntime } = await import(pathToFileURL(runtime).href);
verifyPinnedRuntime();

const browser = await launchPinnedChromium({
  args: ['--enable-unsafe-webgpu', '--enable-unsafe-swiftshader', '--use-angle=swiftshader', '--use-webgpu-adapter=swiftshader'],
});
const out = { pageErrors: [] };
const line = (err) => (err instanceof Error ? err.message : String(err)).split('\n')[0].slice(0, 300);

async function settle(page) {
  for (let attempt = 0; attempt < 5; attempt += 1) {
    await page.waitForSelector('.app[data-core="1"]', { timeout: 60_000 });
    const reloaded = await page.waitForEvent('framenavigated', { timeout: 4_000 }).then(() => true).catch(() => false);
    if (!reloaded) return;
  }
  throw new Error('page kept reloading');
}

function fakeShare() {
  const scope = window;
  scope.__shareLog = { canShare: 0, calls: [] };
  Object.defineProperty(navigator, 'canShare', {
    configurable: true,
    value: (data) => {
      scope.__shareLog.canShare += 1;
      return Array.isArray(data?.files) && data.files.length > 0 && data.files.every((f) => f instanceof File);
    },
  });
  Object.defineProperty(navigator, 'share', {
    configurable: true,
    value: (data) => {
      const active = navigator.userActivation ? navigator.userActivation.isActive : null;
      scope.__shareLog.calls.push({ active, files: data.files });
      return Promise.resolve();
    },
  });
}

try {
  const page = await browser.newPage({ viewport: { width: 390, height: 844 } });
  page.on('pageerror', (error) => out.pageErrors.push(error.message));
  await page.addInitScript(fakeShare);
  await page.goto(url);
  await settle(page);
  await page.evaluate((binding) => {
    window.__devExport ??= {};
    window.__devExport.binding = binding;
  }, manifest.binding);
  await pickAll(page, manifest, manifest.files);
  await page.waitForFunction(
    () => window.__gxrBook?.dat?.read?.entries?.length === 13248 && !window.__gxrBook.datBusy,
    null,
    { timeout: 120_000 },
  );
  await page.locator(cellOf(manifest.stem)).evaluate((node) => node.click());
  await page.waitForFunction((want) => document.querySelector('.editor')?.dataset.photo === want, manifest.stem, { timeout: 120_000 });
  await page.waitForFunction(
    () => {
      const strip = document.querySelector('.editor .strip[data-recipes]');
      const card = strip?.querySelector('.rc[data-card="builtin:1"]');
      return strip?.dataset.state === 'calibrated' && card && !card.classList.contains('lockd');
    },
    null,
    { timeout: 180_000 },
  );
  const card = page.locator('.editor .strip[data-recipes] .rc[data-card="builtin:1"]');
  await card.scrollIntoViewIfNeeded();
  await card.click();
  await page.waitForFunction(
    () => document.querySelector('.editor .strip[data-recipes] .rc.on')?.dataset.card === 'builtin:1',
    null,
    { timeout: 60_000 },
  );
  await page.getByRole('button', { name: '照片', exact: true }).click();
  await page.waitForSelector('[data-screen="grid"]');
  const sheet = await exportFromGrid(page, [manifest.stem]);
  await waitPhase(page, ['running', 'ready']);
  const ready = await waitPhase(page, ['ready']);
  const facts = await batchFacts(page);
  const dock = await page.locator('.pdock').innerText();
  let shared = null;
  let files = [];
  if (await page.locator('.pdock [data-act="share"]').count()) {
    shared = await shareAll(page);
    files = await sharedFiles(page, manifest.outDir, 'r5-');
  }
  out.export = { sheet, ready, facts, dock, shared, files };
  await page.close();
} catch (err) {
  out.error = line(err);
} finally {
  writeFileSync(manifest.out, JSON.stringify(out));
  await browser.close();
}
console.log('done');
