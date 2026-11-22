// 手机浏览器真机链路：载入 g262 卡包 + 带 Look 非零微调标签的合成 RAW+JPG，
// 核对拍摄配方卡片、微调旋钮值与符号，并导出成片写入 manifest.outDir 与桌面逐字节比对。
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

  await pickAll(page, { pack: manifest.pack, count: 1 }, manifest.files);
  await page.waitForFunction(
    () => window.__gxrBook?.dat?.read?.entries?.length === 13248 && !window.__gxrBook.datBusy,
    null,
    { timeout: 120_000 },
  );

  await page.locator(cellOf(manifest.stem)).evaluate((node) => node.click());
  await page.waitForFunction((want) => document.querySelector('.editor')?.dataset.photo === want, manifest.stem, { timeout: 120_000 });
  await page.waitForFunction((want) => document.querySelector('.app')?.dataset.settled === want, manifest.stem, { timeout: 120_000 });

  await page.locator('.editor .toolbar [data-tool="recipe"]').click();
  await page.waitForFunction(
    (want) => document.querySelector('.editor .recipe-head .rname')?.textContent?.trim() === want,
    manifest.expectedName,
    { timeout: 60_000 },
  );
  await page.waitForFunction(
    () => (document.querySelector('.app')?.getAttribute('data-recipe-key') || '').startsWith('bake:'),
    null,
    { timeout: 60_000 },
  );

  const cardInfo = await page.evaluate((stem) => {
    const on = document.querySelector('.strip[data-recipes] .rc.on');
    const row = window.__gxrEdits?.().find((item) => item.stem === stem) ?? null;
    return {
      topRecipe: document.querySelector('.editor .nav .t b')?.textContent?.trim() ?? '',
      heading: document.querySelector('.editor .recipe-head .rname')?.textContent?.trim() ?? '',
      tag: document.querySelector('.editor .recipe-head .neutral-tag')?.textContent?.trim() ?? '',
      stripOnName: on?.querySelector('.nm')?.textContent?.trim() ?? '',
      stripOnCard: on?.getAttribute('data-card') ?? null,
      stageKey: document.querySelector('.app')?.getAttribute('data-recipe-key') ?? '',
      choice: row?.choice ?? null,
    };
  }, manifest.stem);

  // 再点已选配方卡进入微调子页，读取每一项旋钮标签与显示值。
  const activeCard = page.locator('.strip[data-recipes] .rc.on');
  await activeCard.scrollIntoViewIfNeeded();
  await activeCard.click();
  await page.waitForSelector('.editor .recipe-fine:not([hidden]) .knobs .krow', { timeout: 30_000 });
  const knobs = await page.evaluate(() => {
    const outKnobs = {};
    for (const row of document.querySelectorAll('.editor .recipe-fine .knobs .krow')) {
      const key = row.dataset.knob || '';
      if (!key) continue;
      outKnobs[key] = {
        label: row.querySelector('.lab')?.textContent?.trim() ?? '',
        value: row.querySelector('.val')?.textContent?.trim() ?? '',
      };
    }
    return outKnobs;
  });

  const side = await page.evaluate(async (stem) => (await window.__gxrRequest(stem))?.side ?? null, manifest.stem);
  out.card = { ...cardInfo, knobs, sideRecipe: side?.recipe ?? null };

  await page.getByRole('button', { name: '照片', exact: true }).click();
  await page.waitForSelector('[data-screen="grid"]');
  await exportFromGrid(page, [manifest.stem], '原始尺寸', '高', null, { hdr: false });
  await waitPhase(page, ['running', 'ready']);
  await waitPhase(page, ['ready']);
  const facts = await batchFacts(page);
  await shareAll(page);
  const files = await sharedFiles(page, manifest.outDir, 'phone-');
  out.export = { facts, fileCount: files.reduce((n, row) => n + row.files.length, 0) };
  await page.close();
} catch (err) {
  out.error = line(err);
} finally {
  writeFileSync(manifest.out, JSON.stringify(out));
  await browser.close();
}
console.log('done');
