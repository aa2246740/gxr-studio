// r5 两条提示：已有合法配方时，坏的 GXRFX.BIN 仍要在面板上写出 book.datError；
// 06d 弹层打开之后才返回缺库错误，弹层要换成这句原话，DAT 读成功后再关掉。
// 配方和照片都是合成的。stdout 只打 done。

import { readFileSync, writeFileSync } from 'node:fs';
import { pathToFileURL } from 'node:url';

const [runtime, url, manifestPath] = process.argv.slice(2);
const manifest = JSON.parse(readFileSync(manifestPath, 'utf8'));
const { launchPinnedChromium, verifyPinnedRuntime } = await import(pathToFileURL(runtime).href);
verifyPinnedRuntime();

const browser = await launchPinnedChromium();
const out = { pageErrors: [] };
const line = (err) => (err instanceof Error ? err.message : String(err)).split('\n')[0].slice(0, 300);

try {
  const page = await browser.newPage({ viewport: { width: 390, height: 844 } });
  page.on('pageerror', (error) => out.pageErrors.push(error.message));
  await page.goto(url);
  for (let attempt = 0; attempt < 5; attempt += 1) {
    await page.waitForSelector('.app[data-core="1"]', { timeout: 60_000 });
    const reloaded = await page.waitForEvent('framenavigated', { timeout: 4_000 }).then(() => true).catch(() => false);
    if (!reloaded) break;
  }
  if (manifest.real) {
    const picker = page.locator('input.filepick:not(.calibpick)');
    await picker.setInputFiles([manifest.dng, manifest.jpg, manifest.dat, manifest.bin]);
    await page.waitForFunction(() => {
      const book = window.__gxrBook;
      return book?.dat?.read?.entries?.length === 13248 && !book.datBusy && Boolean(document.querySelector('.cell canvas'));
    }, null, { timeout: 120_000 });
    await page.locator('.cell').first().evaluate((node) => node.click());
    await page.waitForFunction(() => Boolean(document.querySelector('.editor .lbl')?.textContent), null, { timeout: 120_000 });
    await page.locator('.editor .tabs button', { hasText: '配方' }).click();
    await page.waitForSelector('.editor .strip[data-recipes] .rc');
    await picker.setInputFiles({ name: 'GXRFX.BIN', mimeType: 'application/octet-stream', buffer: Buffer.alloc(64) });
    await page.waitForFunction(() => Boolean(window.__gxrBook && !window.__gxrBook.datBusy && window.__gxrBook.datError), null, { timeout: 60_000 });
    out.real = await page.evaluate(() => {
      const book = window.__gxrBook;
      const panel = document.querySelector('.editor .panel.recipe')?.innerText ?? '';
      const strip = document.querySelector('.editor .strip[data-recipes]')?.innerText ?? '';
      return {
        coreError: book.datError ?? null,
        hasPreviousDat: Boolean(book.dat),
        entries: book.dat?.read?.entries?.length ?? 0,
        errorVisible: Boolean(book.datError && panel.includes(book.datError)),
        keptRecipe: strip.includes('原厂标准'),
      };
    });
  } else {
    out.cases = await page.evaluate(async () => {
      const { RecipeView } = await import('/src/app/recipe_view.ts');
      const { zeroKnobs } = await import('/src/app/model.ts');
      const mismatch = '无法读取相机配方文件：GXRFX.BIN 与第 178 代 GXRCE.DAT 不匹配或已损坏，请重新复制同一套两个文件。';
      const missing = '无法读取相机配方文件：缺少 GXRFX.BIN。请将 GXRCE.DAT 和 GXRFX.BIN 一起从存储卡复制到同一文件夹，再同时导入。';
      const control = { name: 'contrast', label: '对比度', min: -2, max: 4, default: 0, cap_lower: false, cap_upper: false, amount_step: null, levels: [-2, -1, 0, 1, 2, 3, 4] };
      const listing = { units: [{ id: 'jlaunch', recipes: Array.from({ length: 4 }, (_, i) => ({ slot: i + 1, controls: [control] })), looks: [], users: [] }] };
      const photo = () => ({
        id: 'p', stem: 'p', version: 1, rev: 0, dng: new File(['synthetic'], 'p.DNG'), jpg: null, lastModified: 0, captureMs: null,
        status: 'ready', gone: false, cached: null, checked: null, bitmap: null, neutral: null, jpgMeta: null, dngMeta: null,
        choice: null, levels: new Map(), memory: new Map(), knobs: zeroKnobs(), looks: new Map(),
      });
      const hooks = { mode: () => 'raw', look() {}, dragging: () => false, recipeTab: () => true, geometry: async () => ({ geometry: null, cropped: false }) };

      function mount(book) {
        const listeners = [];
        book.onChange = (fn) => {
          listeners.push(fn);
          return () => {};
        };
        const api = { photo: () => null, photos: () => [], session: () => 1, book: () => book, pickDat() {}, pickCalib() {} };
        const view = new RecipeView(api, null, photo(), hooks);
        const host = document.createElement('div');
        host.className = 'app';
        host.append(view.stripEl(), view.panelEl());
        document.body.append(host);
        return { view, host, notify: () => listeners.forEach((fn) => fn()) };
      }

      const loaded = {
        dat: { listing, generation: 178 },
        datError: mismatch,
        datNote: null,
        unitState: () => ({ state: 'ready' }),
        bakeKey: () => null,
        ready: () => false,
        bake: () => new Promise(() => {}),
        gpuKey: () => null,
        pin() {},
      };
      const first = mount(loaded);
      const panel = first.view.panelEl().innerText;
      const mismatchCase = { errorVisible: panel.includes(mismatch), keptRecipe: panel.includes('原厂标准') };
      first.host.remove();
      first.view.destroy();

      const late = { dat: null, datError: null, datNote: null, unitState: () => ({ state: 'none' }), bakeKey: () => null, ready: () => false, bake: () => new Promise(() => {}), gpuKey: () => null, pin() {} };
      const second = mount(late);
      second.view.tabOpened();
      const opened = second.host.querySelector('.ask-sheet')?.innerText ?? '';
      late.datError = missing;
      second.notify();
      const updated = second.host.querySelector('.ask-sheet')?.innerText ?? '';
      late.dat = { listing, generation: 178 };
      late.datError = null;
      second.notify();
      const delayed = {
        sheetOpen: opened.length > 0,
        sheetShowsCoreError: updated.includes(missing),
        closedAfterLoad: second.host.querySelector('.ask-sheet') === null,
      };
      second.host.remove();
      second.view.destroy();
      return { mismatch: mismatchCase, delayed };
    });
  }
} catch (err) {
  out.error = line(err);
} finally {
  writeFileSync(manifest.out, JSON.stringify(out));
  await browser.close();
}
console.log('done');
