// r5：只选 GXRCE.DAT 时 06d 表写出核心的缺库原话；补上 GXRFX.BIN（另一次选择，或同一次）后读出 24×552，
// 列表和一次 resolve 与桌面同一对文件。照片字节不打到 stdout。结果写进 manifest.out。

import { readFileSync, writeFileSync } from 'node:fs';
import { pathToFileURL } from 'node:url';

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

async function newPage() {
  const page = await browser.newPage({ viewport: { width: 390, height: 844 } });
  page.on('pageerror', (error) => out.pageErrors.push(error.message));
  await page.goto(url);
  await settle(page);
  return page;
}

async function loaded(page) {
  return page.evaluate(async () => {
    const book = window.__gxrBook;
    const recipe = { kind: 'builtin', unit: 'jlaunch', slot: 1, levels: {} };
    const got = await book.io.resolve(
      book.dat.read.bytes.slice(0),
      JSON.stringify(recipe),
      book.dat.library ? book.dat.library.slice(0) : null,
    );
    const bytes = got.entry;
    let text = '';
    for (let i = 0; i < bytes.length; i += 0x8000) text += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
    return {
      entries: book.dat.read.entries.length,
      generation: book.dat.generation,
      library: book.dat.library ? book.dat.library.byteLength : 0,
      listing: book.dat.listing.units,
      entry: btoa(text),
      sha: got.entrySha256,
      levels: got.levels,
    };
  });
}

try {
  const page = await newPage();
  await page.locator('input.filepick:not(.calibpick)').setInputFiles([manifest.dng, manifest.jpg, manifest.dat]);
  await page.waitForFunction(() => {
    const sub = document.querySelector('.nav small');
    const book = window.__gxrBook;
    return sub && !sub.textContent.includes('正在读取') && document.querySelector('.cell canvas') && book && !book.datBusy && book.datError;
  }, null, { timeout: 120_000 });
  await page.locator('.cell').first().evaluate((node) => node.click());
  await page.waitForFunction(() => document.querySelector('.editor .lbl')?.textContent, null, { timeout: 120_000 });
  await page.locator('.editor .tabs button', { hasText: '配方' }).click();
  await page.waitForSelector('.ask-sheet[data-state="no-recipe-file"]');
  const lead = page.locator('.ask-sheet p.lead').first();
  out.sheet = {
    text: await page.locator('.ask-sheet').innerText(),
    panel: await page.locator('.editor .panel.recipe').innerText(),
    hidden: await lead.evaluate((node) => node.hidden),
  };
  await page.locator('input.filepick:not(.calibpick)').setInputFiles(manifest.bin);
  await page.waitForFunction(() => window.__gxrBook?.dat?.read?.entries?.length === 13248, null, { timeout: 60_000 });
  out.second = await loaded(page);
  await page.close();

  const again = await newPage();
  await again.locator('input.filepick:not(.calibpick)').setInputFiles(manifest.files);
  await again.waitForFunction(() => window.__gxrBook?.dat?.read?.entries?.length === 13248 && !window.__gxrBook.datBusy, null, { timeout: 60_000 });
  out.loaded = await loaded(again);
  await again.close();
} catch (err) {
  out.error = line(err);
} finally {
  writeFileSync(manifest.out, JSON.stringify(out));
  await browser.close();
}
console.log('done');
