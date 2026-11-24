// camfiles-once 真实浏览器驱动：覆盖持久化+重载、忘掉/换一份、两类可区分不匹配与同库兼容、
// 无配方文件时 RAW+JPG 默认相机 JPG（不弹窗）/ RAW-only 保持中性转换、双态起始页，以及「未校准」拆分。
// stdout 只打 done，完整结果写进 manifest.out；绝不输出照片文件名、拍摄时间或 EXIF。

import { readFileSync, writeFileSync } from 'node:fs';
import { pathToFileURL } from 'node:url';

const [runtime, url, manifestPath] = process.argv.slice(2);
const manifest = JSON.parse(readFileSync(manifestPath, 'utf8'));
const { launchPinnedChromium, verifyPinnedRuntime } = await import(pathToFileURL(runtime).href);
verifyPinnedRuntime();

const browser = await launchPinnedChromium({
  args: ['--enable-unsafe-webgpu', '--enable-unsafe-swiftshader', '--use-angle=swiftshader', '--use-webgpu-adapter=swiftshader'],
});

const out = { pageErrors: [], steps: {} };
const line = (err) => (err instanceof Error ? err.message : String(err)).split('\n')[0].slice(0, 300);

async function settle(page) {
  for (let attempt = 0; attempt < 5; attempt += 1) {
    await page.waitForSelector('.app[data-core="1"]', { timeout: 60_000 });
    const reloaded = await page.waitForEvent('framenavigated', { timeout: 4_000 }).then(() => true).catch(() => false);
    if (!reloaded) return;
  }
  throw new Error('page kept reloading');
}

async function freshContext(label) {
  const ctx = await browser.newContext({ viewport: { width: 390, height: 844 }, deviceScaleFactor: 2 });
  const page = await ctx.newPage();
  const uploads = [];
  page.on('pageerror', (err) => out.pageErrors.push(`${label}: ${line(err)}`));
  page.on('request', (req) => {
    const method = req.method();
    if (method !== 'GET' && method !== 'HEAD') uploads.push(`${method} ${new URL(req.url()).pathname}`);
  });
  await page.goto(url);
  await settle(page);
  return { ctx, page, uploads };
}

async function waitBookDat(page, generation) {
  await page.waitForFunction(
    (want) => {
      const b = window.__gxrBook;
      if (!b || b.datBusy) return false;
      if (want === null) return b.dat === null;
      return b.dat?.generation === want && b.dat?.read?.entries?.length === 13248;
    },
    generation,
    { timeout: 120_000 },
  );
}

async function waitGridLoaded(page) {
  await page.waitForFunction(
    () => {
      const sub = document.querySelector('.nav small');
      return Boolean(sub && !sub.textContent.includes('正在读取') && document.querySelector('.cell'));
    },
    null,
    { timeout: 120_000 },
  );
}

async function readStart(page) {
  return page.evaluate(() => ({
    screen: document.querySelector('.app')?.dataset.screen ?? null,
    camfilesAttr: document.querySelector('.screen')?.dataset.camfiles ?? null,
    text: document.querySelector('.start')?.innerText ?? '',
    buttons: [...document.querySelectorAll('.start button')].map((n) => n.textContent.trim()),
    status: document.querySelector('.start [data-state="camfiles-remembered"]')?.textContent?.trim() ?? null,
  }));
}

async function openPhoto(page, stem) {
  const id = stem.toLowerCase();
  await page.locator(`.cell[data-id="${id}"]`).evaluate((node) => node.click());
  await page.waitForFunction((want) => document.querySelector('.editor')?.dataset.photo === want, stem, { timeout: 120_000 });
  await page.waitForFunction((want) => document.querySelector('.app')?.dataset.settled === want, stem, { timeout: 120_000 });
}

async function runPersistReloadStart() {
  const { ctx, page, uploads } = await freshContext('persist');
  try {
    // 1. Fresh start screen (nothing remembered)
    await page.waitForSelector('.start button', { timeout: 30_000 });
    const beforePick = await readStart(page);
    if (manifest.shotsDir) {
      await page.screenshot({ path: `${manifest.shotsDir}/start-after-empty-390x844.png`, clip: { x: 0, y: 0, width: 390, height: 844 } });
    }

    // 2. Pick g266 DAT + KUBIK BIN on start screen (stays on start screen, updates to remembered)
    await page.locator('input.filepick:not(.calibpick)').setInputFiles(manifest.g266);
    await waitBookDat(page, 266);
    await page.waitForSelector('.start [data-state="camfiles-remembered"]', { timeout: 30_000 });
    const afterPick = await readStart(page);
    if (manifest.shotsDir) {
      await page.screenshot({ path: `${manifest.shotsDir}/start-after-remembered-390x844.png`, clip: { x: 0, y: 0, width: 390, height: 844 } });
    }

    // 3. Reload page -> camera files restored automatically from IndexedDB without re-pick
    await page.reload();
    await settle(page);
    await waitBookDat(page, 266);
    await page.waitForSelector('.start [data-state="camfiles-remembered"]', { timeout: 30_000 });
    const afterReload = await readStart(page);

    // 4. Pick photos ONLY (no DAT/BIN) -> recipes render immediately
    await page.locator('input.filepick:not(.calibpick)').setInputFiles(manifest.kubikPair);
    await waitGridLoaded(page);
    await openPhoto(page, manifest.kubikStem);
    await page.waitForFunction(
      () => document.querySelector('.editor .recipe-head .rname')?.textContent?.trim() === 'K-KINO',
      null,
      { timeout: 60_000 },
    );
    const adoptedAfterReload = {
      heading: (await page.locator('.editor .recipe-head .rname').textContent())?.trim() ?? '',
      hasDat: await page.evaluate(() => Boolean(window.__gxrBook?.dat && window.__gxrBook.dat.generation === 266)),
    };

    // 5. Clear photos -> returns to start screen, remembered files STAY intact
    await page.getByRole('button', { name: '照片', exact: true }).click();
    await page.waitForSelector('[data-screen="grid"]');
    await page.getByRole('button', { name: '更多' }).click();
    await page.locator('.ms .lrow', { hasText: '清空照片，重新选' }).click();
    await page.waitForSelector('[data-screen="start"]');
    const afterClearPhotos = await readStart(page);

    // 6. Verify Book's validation runs on restore: corrupt stored bytes in IDB -> reload rejects it cleanly
    const corruptRestored = await page.evaluate(async () => {
      const db = await new Promise((resolve, reject) => {
        const req = indexedDB.open('gxr-mobile');
        req.onsuccess = () => resolve(req.result);
        req.onerror = () => reject(req.error);
      });
      await new Promise((resolve, reject) => {
        const tx = db.transaction('camfiles', 'readwrite');
        const store = tx.objectStore('camfiles');
        const g = store.get('current');
        g.onsuccess = () => {
          const row = g.result;
          const bytes = new Uint8Array(row.dat.slice(0));
          bytes[20] ^= 0xff;
          store.put({ ...row, dat: bytes.buffer, savedAt: row.savedAt + 10 }, 'current');
          store.put(row.savedAt + 10, 'watermark');
        };
        tx.oncomplete = () => resolve();
        tx.onerror = () => reject(tx.error);
      });
      db.close();
      return true;
    });
    await page.reload();
    await settle(page);
    await page.waitForFunction(() => window.__gxrBook && !window.__gxrBook.datBusy, null, { timeout: 30_000 });
    await page.waitForTimeout(400);
    const afterCorruptReload = await page.evaluate(async () => {
      const db = await new Promise((resolve, reject) => {
        const req = indexedDB.open('gxr-mobile');
        req.onsuccess = () => resolve(req.result);
        req.onerror = () => reject(req.error);
      });
      const idbRow = await new Promise((resolve, reject) => {
        const tx = db.transaction('camfiles', 'readonly');
        const g = tx.objectStore('camfiles').get('current');
        g.onsuccess = () => resolve(g.result);
        tx.onerror = () => reject(tx.error);
      });
      db.close();
      return {
        hasDat: window.__gxrBook?.dat !== null,
        camfilesAttr: document.querySelector('.screen')?.dataset.camfiles ?? null,
        buttons: [...document.querySelectorAll('.start button')].map((n) => n.textContent.trim()),
        idbCleared: idbRow === undefined || idbRow === null || idbRow.dat === null,
      };
    });

    out.steps.persist = {
      beforePick,
      afterPick,
      afterReload,
      adoptedAfterReload,
      afterClearPhotos,
      corruptRestored,
      afterCorruptReload,
      uploads,
    };
  } finally {
    await ctx.close();
  }
}

async function runForgetAndReplace() {
  const { ctx, page } = await freshContext('forget-replace');
  try {
    // Pick photos + g266 DAT+BIN together (verifying picking together still works)
    await page.locator('input.filepick:not(.calibpick)').setInputFiles([...manifest.realPair, ...manifest.g266]);
    await waitBookDat(page, 266);
    await waitGridLoaded(page);

    // Open 更多 → 校准数据 (contains 相机配方文件 card next to calibration card)
    await page.getByRole('button', { name: '更多' }).click();
    await page.locator('.ms .lrow', { hasText: '校准数据' }).click();
    await page.waitForSelector('.calib-sheet .camfiles[data-camfiles="remembered"]', { timeout: 15_000 });
    const card266 = await page.locator('.calib-sheet .camfiles').innerText();

    // 「换一份…」: switch to g250 (or g185) DAT+BIN
    await page.locator('input.filepick:not(.calibpick)').setInputFiles(manifest.altDat);
    await waitBookDat(page, manifest.altGen);
    await page.waitForFunction(
      (gen) => (document.querySelector('.calib-sheet .camfiles')?.textContent ?? '').includes(`第 ${gen} 代`),
      manifest.altGen,
      { timeout: 30_000 },
    );
    const cardReplaced = await page.locator('.calib-sheet .camfiles').innerText();

    // Reload -> replacement persisted
    await page.reload();
    await settle(page);
    await waitBookDat(page, manifest.altGen);
    const startAfterReplace = await readStart(page);

    // Re-enter grid and tap 「忘掉」
    await page.locator('input.filepick:not(.calibpick)').setInputFiles(manifest.realPair);
    await waitGridLoaded(page);
    await page.getByRole('button', { name: '更多' }).click();
    await page.locator('.ms .lrow', { hasText: '校准数据' }).click();
    await page.waitForSelector('.calib-sheet .camfiles [data-act="camfiles-forget"]', { timeout: 15_000 });
    await page.locator('.calib-sheet .camfiles [data-act="camfiles-forget"]').click();
    await waitBookDat(page, null);
    await page.waitForSelector('.calib-sheet .camfiles[data-camfiles="none"]', { timeout: 15_000 });
    const cardForgotten = await page.locator('.calib-sheet .camfiles').innerText();

    // Reload -> forgotten persists (back to first-visit start screen)
    await page.reload();
    await settle(page);
    await page.waitForFunction(() => window.__gxrBook && !window.__gxrBook.datBusy, null, { timeout: 30_000 });
    await page.waitForTimeout(300);
    const startAfterForget = await readStart(page);

    out.steps.forgetReplace = { card266, cardReplaced, startAfterReplace, cardForgotten, startAfterForget };
  } finally {
    await ctx.close();
  }
}

async function runMismatchCase(label, rememberedFiles, rememberedGen, photoFiles, photoStem, expectMismatch, replaceFiles) {
  const { ctx, page } = await freshContext(label);
  try {
    // Remember initial DAT/BIN, then reload so it comes from IndexedDB.
    await page.locator('input.filepick:not(.calibpick)').setInputFiles(rememberedFiles);
    await waitBookDat(page, rememberedGen);
    await page.reload();
    await settle(page);
    await waitBookDat(page, rememberedGen);

    // Pick photo(s) only, open target photo.
    await page.locator('input.filepick:not(.calibpick)').setInputFiles(photoFiles);
    await waitGridLoaded(page);
    if (expectMismatch) {
      await page.evaluate(() => {
        const b = window.__gxrBook;
        window.__camfilesBakeCalls = 0;
        if (window.__gxrBakes) window.__gxrBakes.clear();
        if (b && !b.__camfilesBakeSpy) {
          const orig = b.bake.bind(b);
          b.bake = (...args) => {
            window.__camfilesBakeCalls += 1;
            return orig(...args);
          };
          b.__camfilesBakeSpy = true;
        }
      });
    }
    await openPhoto(page, photoStem);

    if (expectMismatch) {
      await page.waitForSelector('.ask-sheet[data-state="camfiles-mismatch"]', { timeout: 30_000 });
      await page.waitForTimeout(300);
      const promptOpen = await page.locator('.ask-sheet[data-state="camfiles-mismatch"]').isVisible();
      const promptText = await page.locator('.ask-sheet[data-state="camfiles-mismatch"]').innerText();
      // Dismiss mismatch prompt with 「先不用」 and verify zero card bakes, greyed cards, and no DAT card name in head.
      await page.locator('.ask-sheet[data-state="camfiles-mismatch"] [data-act="mismatch-later"]').click();
      await page.waitForFunction(() => !document.querySelector('.ask-sheet[data-state="camfiles-mismatch"]'), null, { timeout: 10_000 });
      await page.waitForTimeout(500);
      const before = await page.evaluate(() => {
        const cards = [...document.querySelectorAll('.editor .strip[data-recipes] .rc')];
        return {
          promptOpen: true,
          recipeKey: document.querySelector('.app')?.getAttribute('data-recipe-key') ?? null,
          label: document.querySelector('.editor .lbl')?.textContent?.trim() ?? '',
          headName: document.querySelector('.editor .recipe-head .rname')?.textContent?.trim() ?? '',
          topBarTitle: document.querySelector('.editor .nav .t b')?.textContent?.trim() ?? '',
          stripState: document.querySelector('.editor .strip[data-recipes]')?.dataset.state ?? null,
          cardCount: cards.length,
          allLocked: cards.length > 0 && cards.every((c) => c.classList.contains('lockd')),
          predictedThumbs: cards.filter((c) => c.querySelector('.th')?.dataset.source === '电脑预测').length,
          bakeCalls: window.__camfilesBakeCalls ?? -1,
          bakesRecorded: window.__gxrBakes ? window.__gxrBakes.size : 0,
        };
      });
      before.promptOpen = promptOpen;
      before.promptText = promptText;

      // Tapping a greyed card opens the mismatch 补选 prompt again.
      await page.locator('.editor .strip[data-recipes] .rc').first().click();
      await page.waitForSelector('.ask-sheet[data-state="camfiles-mismatch"]', { timeout: 15_000 });
      before.tapReopensPrompt = true;

      // Pick matching card files -> prompt closes and real recipe renders.
      await page.locator('input.filepick:not(.calibpick)').setInputFiles(replaceFiles);
      await waitBookDat(page, 266);
      await page.waitForFunction(() => !document.querySelector('.ask-sheet[data-state="camfiles-mismatch"]'), null, { timeout: 30_000 });
      await page.waitForFunction(
        () => document.querySelector('.editor .recipe-head .rname')?.textContent?.trim() === 'K-KINO',
        null,
        { timeout: 60_000 },
      );
      const after = await page.evaluate(() => ({
        promptOpen: Boolean(document.querySelector('.ask-sheet[data-state="camfiles-mismatch"]')),
        heading: document.querySelector('.editor .recipe-head .rname')?.textContent?.trim() ?? '',
        generation: window.__gxrBook?.dat?.generation ?? null,
      }));
      return { before, after };
    }

    // Same-library non-mismatch (g262 serving g266 KUBIK photo): no mismatch prompt, adopts K-KINO.
    await page.waitForFunction(
      () => document.querySelector('.editor .recipe-head .rname')?.textContent?.trim() === 'K-KINO',
      null,
      { timeout: 60_000 },
    );
    await page.waitForTimeout(300);
    return await page.evaluate(() => ({
      promptOpen: Boolean(document.querySelector('.ask-sheet[data-state="camfiles-mismatch"]')),
      heading: document.querySelector('.editor .recipe-head .rname')?.textContent?.trim() ?? '',
      errorTile: Boolean(document.querySelector('.editor .panel.recipe [data-camera-recipe-error]')),
    }));
  } finally {
    await ctx.close();
  }
}

async function runRawJpgDefaultAndRawOnly() {
  const { ctx, page } = await freshContext('raw-jpg-default');
  try {
    // 1. RAW+JPG without camera files -> opens to camera JPG with NO blocking sheet.
    await page.locator('input.filepick:not(.calibpick)').setInputFiles(manifest.realPair);
    await waitGridLoaded(page);
    await openPhoto(page, manifest.realStem);
    await page.waitForTimeout(400);
    const rawJpgOpen = await page.evaluate(() => ({
      label: document.querySelector('.editor .lbl')?.textContent?.trim() ?? '',
      sheetCount: document.querySelectorAll('.ask-sheet').length,
      stripState: document.querySelector('.editor .strip[data-recipes]')?.dataset.state ?? null,
    }));

    // 2. Tapping a recipe card opens the ask sheet (and no 「每张卡选一次就行」).
    await page.locator('.editor .strip[data-recipes] .rc').first().click();
    await page.waitForSelector('.ask-sheet[data-state="no-recipe-file"]', { timeout: 15_000 });
    const askSheetText = await page.locator('.ask-sheet').innerText();

    // 3. Picking g266 files while open upgrades stage to RAW + recipes!
    await page.locator('input.filepick:not(.calibpick)').setInputFiles(manifest.g266);
    await waitBookDat(page, 266);
    await page.waitForFunction(
      () => !document.querySelector('.ask-sheet') && document.querySelector('.editor .lbl')?.textContent?.trim() === '电脑预测',
      null,
      { timeout: 60_000 },
    );
    const afterDatPicked = await page.evaluate(() => ({
      label: document.querySelector('.editor .lbl')?.textContent?.trim() ?? '',
      stripState: document.querySelector('.editor .strip[data-recipes]')?.dataset.state ?? null,
    }));

    out.steps.rawJpgDefault = { rawJpgOpen, askSheetText, afterDatPicked };
  } finally {
    await ctx.close();
  }

  // 4. RAW-only without camera files STILL opens to 中性转换 with no blocking sheet.
  const rawOnly = await freshContext('raw-only-default');
  try {
    await rawOnly.page.locator('input.filepick:not(.calibpick)').setInputFiles([manifest.realDngOnly]);
    await waitGridLoaded(rawOnly.page);
    await openPhoto(rawOnly.page, manifest.realStem);
    await rawOnly.page.waitForTimeout(400);
    out.steps.rawOnlyDefault = await rawOnly.page.evaluate(() => ({
      label: document.querySelector('.editor .lbl')?.textContent?.trim() ?? '',
      sheetCount: document.querySelectorAll('.ask-sheet').length,
    }));
  } finally {
    await rawOnly.ctx.close();
  }
}

async function runUncalibratedSplit() {
  const { ctx, page } = await freshContext('uncalibrated-split');
  try {
    // Load ISO 1250 photo + g266 DAT+BIN; default calibration is either on or off per manifest.calibMode.
    await page.locator('input.filepick:not(.calibpick)').setInputFiles([...manifest.isoOutPair, ...manifest.g266]);
    await waitBookDat(page, 266);
    await page.waitForFunction(() => window.__gxrBook && !window.__gxrBook.loadingDefault, null, { timeout: 60_000 });
    await waitGridLoaded(page);
    await openPhoto(page, manifest.isoOutStem);
    await page.waitForSelector('.editor .panel.recipe [data-act="recipe-status"]', { timeout: 60_000 });
    const tile = page.locator('.editor .panel.recipe [data-act="recipe-status"]').first();
    const info = {
      state: await tile.getAttribute('data-state'),
      className: await tile.getAttribute('class'),
      label: (await tile.innerText()).trim(),
    };
    await tile.click();
    await page.waitForSelector('.recipe-info-sheet', { timeout: 15_000 });
    info.sheetText = (await page.locator('.recipe-info-sheet').innerText()).trim();
    info.sheetState = await page.locator('.recipe-info-sheet [data-state]').first().getAttribute('data-state');
    out.steps.uncalibratedSplit = info;
  } finally {
    await ctx.close();
  }
}

try {
  if (manifest.mode === 'main') {
    await runPersistReloadStart();
    await runForgetAndReplace();
    if (manifest.crossGenDat) {
      out.steps.mismatchCrossGen = await runMismatchCase(
        'mismatch-cross-gen',
        [manifest.crossGenDat],
        manifest.crossGenNum,
        manifest.kubikPair,
        manifest.kubikStem,
        true,
        manifest.g266,
      );
    }
    if (manifest.nonKubikDat) {
      out.steps.mismatchSameGenDiffLib = await runMismatchCase(
        'mismatch-diff-lib',
        manifest.nonKubikDat,
        manifest.nonKubikGen,
        manifest.kubikPair,
        manifest.kubikStem,
        true,
        manifest.g266,
      );
    }
    if (manifest.g262) {
      out.steps.sameLibNoMismatch = await runMismatchCase(
        'same-lib',
        manifest.g262,
        262,
        manifest.kubikPair,
        manifest.kubikStem,
        false,
        manifest.g266,
      );
    }
    await runRawJpgDefaultAndRawOnly();
  } else if (manifest.mode === 'uncalibrated') {
    await runUncalibratedSplit();
  }
} catch (err) {
  out.error = line(err);
} finally {
  writeFileSync(manifest.out, JSON.stringify(out));
  await browser.close();
}
console.log('done');
