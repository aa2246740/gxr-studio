// g250 + XPan 4288x1584: import DNG+JPG and JPG-only with g250 DAT+BIN, verify 4288x1584 preview/finalSize/badge at original ratio (and 4288x1583 only if 65:24 is clicked), export both, and compare byte-for-byte with desktop.
// Only prints "done" to stdout; photo bytes and filenames are never printed.

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
const out = { pageErrors: [], steps: {} };
const line = (err) => (err instanceof Error ? err.message : String(err)).split('\n')[0].slice(0, 300);

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

async function settle(page) {
  for (let attempt = 0; attempt < 5; attempt += 1) {
    await page.waitForSelector('.app[data-core="1"]', { timeout: 60_000 });
    const reloaded = await page.waitForEvent('framenavigated', { timeout: 4_000 }).then(() => true).catch(() => false);
    if (!reloaded) return;
  }
  throw new Error('page kept reloading');
}

async function waitCropBadge(page, want) {
  await page.waitForFunction(
    (expected) => {
      const b = document.querySelector('.cbox .csize')?.textContent?.trim() || '';
      const l = document.querySelector('.crop-size-line')?.textContent?.trim() || '';
      const m1 = /^(\d+)\s*×\s*(\d+)$/.exec(b);
      const m2 = /^(\d+)\s*×\s*(\d+)$/.exec(l);
      return Boolean(m1 && m2 && m1[1] === String(expected[0]) && m1[2] === String(expected[1]) && m2[1] === m1[1] && m2[2] === m1[2]);
    },
    want,
    { timeout: 30_000 },
  );
  return page.evaluate(() => {
    const m = /^(\d+)\s*×\s*(\d+)$/.exec(document.querySelector('.cbox .csize')?.textContent?.trim() || '');
    return m ? [Number(m[1]), Number(m[2])] : null;
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

  const previews = [];
  for (const stem of manifest.stems) {
    await page.locator(cellOf(stem)).evaluate((node) => node.click());
    await page.waitForFunction((want) => document.querySelector('.editor')?.dataset.photo === want, stem, { timeout: 120_000 });
    await page.waitForFunction((want) => document.querySelector('.app')?.dataset.settled === want, stem, { timeout: 120_000 });
    await page.waitForFunction(() => document.querySelector('.app')?.dataset.frame === '1', null, { timeout: 120_000 });
    const layerSize = await page.evaluate(() => window.__gxrEngine?.layerSize?.() ?? null);
    const originalFinalSize = await page.evaluate(() => window.__gxrEngine?.finalSize?.() ?? null);

    await page.locator('.editor .toolbar [data-tool="crop"]').click();
    await page.waitForSelector('.crop-panel');
    const originalBadge = await waitCropBadge(page, [4288, 1584]);

    const xpanChip = page.locator('.crop-panel .chips button[data-preset="xpan-65-24"]');
    await xpanChip.scrollIntoViewIfNeeded();
    await xpanChip.click();
    const xpan6524Badge = await waitCropBadge(page, [4288, 1583]);

    const origChip = page.locator('.crop-panel .chips button[data-preset="original"]');
    await origChip.scrollIntoViewIfNeeded();
    await origChip.click();
    const restoredBadge = await waitCropBadge(page, [4288, 1584]);

    const side = await page.evaluate(async (s) => (await window.__gxrRequest(s))?.side ?? null, stem);
    previews.push({ stem, layerSize, originalFinalSize, originalBadge, xpan6524Badge, restoredBadge, side });

    await page.getByRole('button', { name: '照片', exact: true }).click();
    await page.waitForSelector('[data-screen="grid"]');
  }
  out.steps.previews = previews;

  const sheet = await exportFromGrid(page, manifest.stems, '原始尺寸', '高');
  await waitPhase(page, ['running', 'ready']);
  const ready = await waitPhase(page, ['ready']);
  const facts = await batchFacts(page);
  const shared = await shareAll(page);
  const files = await sharedFiles(page, manifest.outDir, 'xpan-');
  out.steps.export = { sheet, ready, facts, shared, files };
  await page.close();
} catch (err) {
  out.error = line(err);
} finally {
  writeFileSync(manifest.out, JSON.stringify(out));
  await browser.close();
}
console.log('done');
