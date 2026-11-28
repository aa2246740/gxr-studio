// Browser driver for phone-halfframe-export:
// - mode="modes": ordinary photo (no 半格 row), diptych 半格 row + PAIR_EXPORT_NOTE,
//   两张单格 (2 files), reload persistence + one-tap 存到相册 (ori=8), 都要 (3 files), mixed batch.
// - mode="labels": hf258 pair (with and without photo 1) — top bar「半格」and 照片信息「第 1 格 / 第 2 格」.
// - mode="parity": card250 diptych singles & both-mode exports (unedited, edited+crop/border/stamp, small/low, ori=6)
//   written to outDir for Python byte-for-byte comparison with desktop export_pair_finish.
// Photo filenames and EXIF are never printed to stdout.

import { readFileSync, writeFileSync } from 'node:fs';
import { pathToFileURL } from 'node:url';
import { cellOf, pickAll, shareAll, sharedFiles, waitPhase } from './mobile_ui_export_parity.mjs';
import { phoneTouch } from './phone_touch.mjs';

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

async function openPhoto(page, stem) {
  await page.locator(cellOf(stem)).evaluate((node) => node.click());
  await page.waitForFunction((want) => document.querySelector('.editor')?.dataset.photo === want, stem, { timeout: 120_000 });
  await page.waitForFunction((want) => document.querySelector('.app')?.dataset.settled === want, stem, { timeout: 120_000 });
}

async function backToGrid(page) {
  await page.getByRole('button', { name: '照片', exact: true }).click();
  await page.waitForSelector('[data-screen="grid"]');
}

async function openEditorMore(page) {
  await page.locator('.editor .nav [data-act="editor-more"]').click();
  await page.waitForSelector('.paste-sheet[data-state="editor-more"]', { timeout: 10_000 });
}

async function openExportOptionsFromEditor(page) {
  await openEditorMore(page);
  await page.locator('.paste-sheet[data-state="editor-more"] .lrow', { hasText: '导出选项' }).click();
  await page.waitForSelector('.ms.export', { timeout: 10_000 });
}

async function openExportFromGrid(page, stems) {
  const selectBtn = page.locator('.nav .nb.bold');
  await selectBtn.click();
  for (const stem of stems) await page.locator(cellOf(stem)).evaluate((node) => node.click());
  await page.locator('.dock [data-act="export"]').click();
  await page.waitForSelector('.ms.export', { timeout: 10_000 });
}

async function readExportSheetState(page) {
  const sheet = page.locator('.ms.export');
  const hfRow = sheet.locator('[data-role="halfframe-row"]');
  const hasHfRow = (await hfRow.count()) > 0;
  const hfActive = hasHfRow ? await hfRow.locator('.seg button.on').getAttribute('data-value') : null;
  const hfLabels = hasHfRow ? await hfRow.locator('.seg button').allInnerTexts() : [];
  const hfNote = hasHfRow ? await hfRow.locator('.hf-note').innerText().catch(() => '') : '';
  const sizeLine = await sheet.locator('.frow').nth(0).locator('.lab .size').innerText();
  const hdrDisabled = await sheet.locator('input.switch').isDisabled();
  return { hasHfRow, hfActive, hfLabels, hfNote, sizeLine, hdrDisabled };
}

async function setKnob(page, key, value) {
  if (!(await page.locator('.editor .panel.adjust:not([hidden])').count())) {
    await page.locator('.editor .toolbar [data-tool="adjust"]').click();
    await page.waitForSelector('.editor .panel.adjust:not([hidden])');
  }
  await page.locator(`.editor .panel.adjust .chips .chip[data-key="${key}"]`).click();
  return page.locator('.editor .panel.adjust input[type="range"]').evaluate((input, v) => {
    input.value = String(v);
    input.dispatchEvent(new Event('input', { bubbles: true }));
    return Number(input.value);
  }, value);
}

try {
  const page = await browser.newPage({ viewport: { width: 390, height: 844 } });
  await page.addInitScript(phoneTouch);
  page.on('pageerror', (error) => out.pageErrors.push(error.message));
  await page.addInitScript(fakeShare);

  if (manifest.mode === 'modes') {
    await page.goto(url);
    await settle(page);
    await pickAll(page, { count: manifest.count }, manifest.files);

    // 1. Ordinary photo: NO 半格 row in 导出选项
    await openPhoto(page, manifest.ordinaryStem);
    await openExportOptionsFromEditor(page);
    await page.waitForFunction(
      () => {
        const el = document.querySelector('.ms.export .frow .lab .size');
        return el && !el.textContent.includes('正在计算');
      },
      null,
      { timeout: 30_000 },
    );
    out.ordinarySheet = await readExportSheetState(page);
    await page.locator('.ms.export .nb', { hasText: '取消' }).click();
    await page.waitForSelector('.ms.export', { state: 'detached' });
    await backToGrid(page);

    // 2. Diptych ori=1: 半格 row shown, default diptych, switch to singles -> 2 files
    await openPhoto(page, manifest.diptychOri1Stem);
    await openExportOptionsFromEditor(page);
    await page.waitForFunction(
      () => {
        const el = document.querySelector('.ms.export .frow .lab .size');
        return el && !el.textContent.includes('正在计算');
      },
      null,
      { timeout: 30_000 },
    );
    out.diptychDefaultSheet = await readExportSheetState(page);
    await page.locator('.ms.export [data-role="halfframe-row"] .seg button[data-value="singles"]').click();
    out.diptychSinglesSheet = await readExportSheetState(page);
    const beforeSingles = await page.evaluate(() => window.__shareLog.calls.length);
    await page.locator('.ms.export .go').click();
    const singlesReady = await waitPhase(page, ['ready']);
    const singlesShared = await shareAll(page);
    const singlesFiles = await sharedFiles(page, manifest.outDir, 'singles-', beforeSingles);
    out.singles = { ready: singlesReady, shared: singlesShared, files: singlesFiles };

    // 3. Reload page: pref remembered ('singles'), one-tap 存到相册 on ori=8 diptych uses it!
    await page.reload();
    await settle(page);
    await pickAll(page, { count: manifest.count }, manifest.files);
    await openPhoto(page, manifest.diptychOri8Stem);
    await openExportOptionsFromEditor(page);
    await page.waitForFunction(
      () => {
        const el = document.querySelector('.ms.export .frow .lab .size');
        return el && !el.textContent.includes('正在计算');
      },
      null,
      { timeout: 30_000 },
    );
    out.afterReloadSheet = await readExportSheetState(page);
    await page.locator('.ms.export .nb', { hasText: '取消' }).click();
    await page.waitForSelector('.ms.export', { state: 'detached' });

    const beforeQuick = await page.evaluate(() => window.__shareLog.calls.length);
    await page.locator('.editor .toolbar [data-act="export"]').click();
    const quickReady = await waitPhase(page, ['ready']);
    const quickShared = await shareAll(page);
    const quickFiles = await sharedFiles(page, manifest.outDir, 'quick-ori8-', beforeQuick);
    out.quickOri8 = { ready: quickReady, shared: quickShared, files: quickFiles };

    // 4. Switch to 都要 ('both') on ori=1 diptych -> 3 files
    await backToGrid(page);
    await openPhoto(page, manifest.diptychOri1Stem);
    await openExportOptionsFromEditor(page);
    await page.waitForFunction(
      () => {
        const el = document.querySelector('.ms.export .frow .lab .size');
        return el && !el.textContent.includes('正在算');
      },
      null,
      { timeout: 30_000 },
    );
    await page.locator('.ms.export [data-role="halfframe-row"] .seg button[data-value="both"]').click();
    out.diptychBothSheet = await readExportSheetState(page);
    const beforeBoth = await page.evaluate(() => window.__shareLog.calls.length);
    await page.locator('.ms.export .go').click();
    const bothReady = await waitPhase(page, ['ready']);
    const bothShared = await shareAll(page);
    const bothFiles = await sharedFiles(page, manifest.outDir, 'both-', beforeBoth);
    out.both = { ready: bothReady, shared: bothShared, files: bothFiles };

    // 5. Mixed batch from grid: ordinary + ori=1 diptych in singles mode -> 3 files
    await backToGrid(page);
    await openExportFromGrid(page, [manifest.ordinaryStem, manifest.diptychOri1Stem]);
    out.mixedSheet = await readExportSheetState(page);
    await page.locator('.ms.export [data-role="halfframe-row"] .seg button[data-value="singles"]').click();
    const beforeMixed = await page.evaluate(() => window.__shareLog.calls.length);
    await page.locator('.ms.export .go').click();
    const mixedReady = await waitPhase(page, ['ready']);
    const mixedShared = await shareAll(page);
    const mixedFiles = await sharedFiles(page, manifest.outDir, 'mixed-', beforeMixed);
    out.mixed = { ready: mixedReady, shared: mixedShared, files: mixedFiles };
  } else if (manifest.mode === 'labels') {
    const readPhotoInfo = async () => {
      await openEditorMore(page);
      await page.locator('.paste-sheet[data-state="editor-more"] .lrow', { hasText: '照片信息' }).click();
      await page.waitForSelector('.paste-sheet[data-state="photo-info"]', { timeout: 10_000 });
      await page
        .waitForFunction(
          () => {
            const row = document.querySelector('.paste-sheet[data-state="photo-info"] [data-role="halfframe-recipes"]');
            return Boolean(row && row.dataset.firstChecked);
          },
          null,
          { timeout: 15_000 },
        )
        .catch(() => undefined);
      const info = await page.locator('.paste-sheet[data-state="photo-info"]').evaluate((sheet) => {
        const rows = Array.from(sheet.querySelectorAll('.lrow')).map((r) => ({
          role: r.dataset.role || '',
          firstChecked: r.dataset.firstChecked || '',
          label: r.querySelector('b')?.textContent || '',
          values: Array.from(r.querySelectorAll('small')).map((s) => ({
            half: s.dataset.half || '',
            text: s.textContent || '',
          })),
        }));
        return { rows };
      });
      await page.locator('.paste-sheet[data-state="photo-info"] .nb').click().catch(() => {});
      await page.waitForSelector('.paste-sheet[data-state="photo-info"]', { state: 'detached', timeout: 10_000 }).catch(() => {});
      return info;
    };

    const exportSinglesFromOpenEditor = async (prefix) => {
      await openExportOptionsFromEditor(page);
      const sheet = page.locator('.ms.export');
      await sheet.locator('[data-role="halfframe-row"] .seg button[data-value="singles"]').click();
      const before = await page.evaluate(() => window.__shareLog.calls.length);
      await sheet.locator('.go').click();
      const ready = await waitPhase(page, ['ready']);
      const shared = await shareAll(page);
      const files = await sharedFiles(page, manifest.outDir, prefix, before);
      return { ready, shared, files };
    };

    // 1. With matching ori=1 photo 1 present
    await page.goto(url);
    await settle(page);
    await pickAll(page, { count: manifest.withFirstCount }, manifest.withFirstFiles);
    await openPhoto(page, manifest.diptychStem);
    const topLabelWith = await page.locator('.editor .nav .t b').innerText();
    const statusLabelWith = await page.locator('.editor .panel.recipe [data-act="recipe-status"]').innerText().catch(() => '');
    const infoWith = await readPhotoInfo();
    out.withFirst = { topLabel: topLabelWith, statusLabel: statusLabelWith, info: infoWith };

    // 2. Without photo 1 (only diptych imported) — omit 第 1 格 + export singles strips Copyright from 第 1 格
    await page.reload();
    await settle(page);
    await pickAll(page, { count: 1 }, manifest.onlySecondFiles);
    await openPhoto(page, manifest.diptychStem);
    const topLabelAlone = await page.locator('.editor .nav .t b').innerText();
    const infoAlone = await readPhotoInfo();
    const aloneExport = await exportSinglesFromOpenEditor('alone-');
    out.onlySecond = { topLabel: topLabelAlone, info: infoAlone, export: aloneExport };

    // 3. Orientation-mismatched neighbour (photo 1 patched to ori=6, diptych ori=1) — must reject in BOTH 照片信息 and export!
    await page.reload();
    await settle(page);
    await pickAll(page, { count: manifest.mismatchCount }, manifest.mismatchFiles);
    await openPhoto(page, manifest.diptychStem);
    const infoMismatch = await readPhotoInfo();
    const mismatchExport = await exportSinglesFromOpenEditor('mismatch-');
    out.oriMismatch = { info: infoMismatch, export: mismatchExport };
  } else if (manifest.mode === 'parity') {
    await page.goto(url);
    await settle(page);
    await pickAll(page, { count: manifest.count }, manifest.files);

    const exportOne = async (stem, prefix, { size = '原始尺寸', quality = '高', halfframe = 'singles' }) => {
      await openPhoto(page, stem);
      await openExportOptionsFromEditor(page);
      const sheet = page.locator('.ms.export');
      await sheet.locator('.frow').nth(0).locator('.seg button', { hasText: size }).click();
      await sheet.locator('.frow').nth(1).locator('.seg button', { hasText: quality }).click();
      await sheet.locator('[data-role="halfframe-row"] .seg button', { hasText: halfframe }).click();
      const before = await page.evaluate(() => window.__shareLog.calls.length);
      await sheet.locator('.go').click();
      const ready = await waitPhase(page, ['ready']);
      const reqSides = await page.evaluate(() => {
        const b = window.__devExport?.flow?.current?.();
        return b ? b.items.map((it) => ({ id: it.id, stem: it.stem, side: it.req.side })) : [];
      });
      const shared = await shareAll(page);
      const files = await sharedFiles(page, manifest.outDir, prefix, before);
      await backToGrid(page);
      return { ready, shared, files, reqSides };
    };

    // 1. Mixed-recipe hf258 pair (p1 + p2): unedited singles
    out.hf258Singles = await exportOne(manifest.hf258Stem, 'hf258-singles-', { halfframe: '两张单格' });

    // 2. card250 ori=1, ori=6, ori=8 pairs + derived ori=3 pair: unedited singles
    out.uneditedOri1 = await exportOne(manifest.ori1Stem, 'unedited-ori1-', { halfframe: '两张单格' });
    out.uneditedOri6 = await exportOne(manifest.ori6Stem, 'unedited-ori6-', { halfframe: '两张单格' });
    out.uneditedOri8 = await exportOne(manifest.ori8Stem, 'unedited-ori8-', { halfframe: '两张单格' });
    out.uneditedOri3 = await exportOne(manifest.ori3Stem, 'unedited-ori3-', { halfframe: '两张单格' });

    // 3. Edit card250 ori=1 diptych: adjust (exposure + contrast + vignette) + curve + gallery border
    await openPhoto(page, manifest.ori1Stem);
    const expVal = await setKnob(page, 'exposure_ev', 0.3);
    const conVal = await setKnob(page, 'contrast', 12);
    const vigVal = await setKnob(page, 'vignette', 35);
    // Drag master curve midpoint upward via real curves sheet UI
    await page.locator('.editor .band .panel.adjust button', { hasText: '曲线' }).click();
    await page.waitForSelector('.editor svg.curve', { timeout: 10_000 });
    const box = await page.locator('.editor svg.curve').boundingBox();
    if (!box) throw new Error('curve svg boundingBox missing');
    const cx = box.x + box.width * 0.5;
    const cy = box.y + box.height * 0.5;
    await page.mouse.move(cx, cy);
    await page.mouse.down();
    await page.mouse.move(cx, cy - box.height * 0.14, { steps: 6 });
    await page.mouse.up();
    const stageTop = await page.locator('.editor .stage').boundingBox();
    if (stageTop) {
      await page.mouse.click(stageTop.x + stageTop.width * 0.5, stageTop.y + 40);
    }
    await page.waitForSelector('.curves-sheet', { state: 'detached', timeout: 10_000 }).catch(() => {});

    // Enable gallery border via 更多 -> 边框
    await page.locator('.editor .toolbar [data-tool="more"]').click();
    await page.waitForSelector('.more-sheet', { timeout: 10_000 });
    await page.locator('.more-sheet [data-more="frame"]').click();
    await page.waitForSelector('.editor .band .panel.frame', { timeout: 10_000 });
    await page.locator('.editor .band .panel.frame .strip .rc[data-style="gallery"]').click();
    await backToGrid(page);

    out.editedKnobs = { exposure_ev: expVal, contrast: conVal, vignette: vigVal };
    out.editedBoth = await exportOne(manifest.ori1Stem, 'edited-both-', { halfframe: '都要' });
    out.editedSmallSingles = await exportOne(manifest.ori1Stem, 'edited-small-', {
      size: '小',
      quality: '低',
      halfframe: '两张单格',
    });

    // 4. Display-space .cube LUT on hf258 diptych -> singles
    await openPhoto(page, manifest.hf258Stem);
    await page.locator('.editor .toolbar [data-tool="recipe"]').click();
    await page.locator('.editor .band .panel.recipe [data-mode="lut"]').click();
    await page.waitForSelector('.editor .band .panel.lut:not([hidden])', { timeout: 10_000 });
    await page.locator('input.lutpick').setInputFiles([manifest.lutPath]);
    await page.waitForSelector(`.editor .band .panel.lut .strip .rc[data-lut="${manifest.lutCubeId}"]`, { timeout: 20_000 });
    await page.locator(`.editor .band .panel.lut .strip .rc[data-lut="${manifest.lutCubeId}"]`).click();
    await page.waitForFunction((want) => document.querySelector('.app')?.dataset.settled === want, manifest.hf258Stem, { timeout: 60_000 });
    await backToGrid(page);
    out.lutSingles = await exportOne(manifest.hf258Stem, 'lut-singles-', { halfframe: '两张单格' });
  } else {
    throw new Error(`unknown mode: ${manifest.mode}`);
  }
} catch (err) {
  out.error = line(err);
} finally {
  await browser.close();
  writeFileSync(manifest.out, JSON.stringify(out, null, 2));
}
if (out.error) process.exit(1);
