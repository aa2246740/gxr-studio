// U1 phone-fast-export 真实照片浏览器验收驱动（390×844 与 430×932，真 card250 + g250）。
// 隐私约束：绝不向 stdout 或报告输出任何照片文件名、拍摄时间或 EXIF；仅输出 redacted 布尔与计数。

import { pathToFileURL } from 'node:url';
import { readFileSync, writeFileSync } from 'node:fs';

const [runtime, url, manifestPath] = process.argv.slice(2);
const manifest = JSON.parse(readFileSync(manifestPath, 'utf8'));
const { launchPinnedChromium, verifyPinnedRuntime } = await import(pathToFileURL(runtime).href);
verifyPinnedRuntime();

const browser = await launchPinnedChromium({
  args: ['--enable-unsafe-webgpu', '--enable-unsafe-swiftshader', '--use-angle=swiftshader', '--use-webgpu-adapter=swiftshader'],
});

const out = { pageErrors: [], steps: {}, error: null };

async function installShareMock(page) {
  await page.addInitScript(() => {
    const calls = [];
    window.__shareLog = { calls };
    Object.defineProperty(navigator, 'canShare', {
      configurable: true,
      value: (data) => Boolean(data && Array.isArray(data.files) && data.files.length > 0),
    });
    Object.defineProperty(navigator, 'share', {
      configurable: true,
      value: async (data) => {
        const files = (data?.files ?? []).map((f) => ({ size: f.size, type: f.type, name: f.name }));
        calls.push({ active: navigator.userActivation?.isActive ?? true, count: files.length, files });
      },
    });
  });
}

async function settle(page) {
  for (let attempt = 0; attempt < 5; attempt += 1) {
    await page.waitForSelector('.app[data-core="1"]', { timeout: 30_000 });
    const reloaded = await page.waitForEvent('framenavigated', { timeout: 3_000 }).then(() => true).catch(() => false);
    if (!reloaded) return;
  }
  throw new Error('page kept reloading');
}

async function visibleTap(page, locator, label) {
  await locator.waitFor({ state: 'visible', timeout: 30_000 });
  const box = await locator.boundingBox();
  const vp = page.viewportSize();
  if (!box || box.width < 10 || box.height < 10) throw new Error(`control not visible/sized: ${label}`);
  if (vp && (box.x + box.width <= 0 || box.y + box.height <= 0 || box.x >= vp.width || box.y >= vp.height)) {
    throw new Error(`control outside viewport: ${label} (${JSON.stringify(box)})`);
  }
  await locator.click();
}

async function waitGridReady(page, count) {
  await page.waitForFunction(
    (n) => {
      const sub = document.querySelector('.nav small');
      return sub && !sub.textContent.includes('正在读取') && document.querySelectorAll('.cell').length >= n && window.__gxrBook?.dat;
    },
    count,
    { timeout: 120_000 },
  );
}

async function waitEditorReady(page) {
  await page.waitForFunction(
    () => {
      const strip = document.querySelector('.editor .strip[data-recipes]');
      return document.querySelector('.app')?.dataset.frame === '1' && strip?.dataset.state === 'calibrated';
    },
    null,
    { timeout: 120_000 },
  );
}

async function runTapCountViewport(width, height) {
  const ctx = await browser.newContext({ viewport: { width, height } });
  const page = await ctx.newPage();
  page.on('pageerror', (err) => out.pageErrors.push(`${width}x${height}: ${err.message}`));
  await installShareMock(page);
  await page.goto(url);
  await settle(page);
  await page.evaluate(() => localStorage.clear());

  // Import calibration pack and verify collapsed 1-line start card
  await page.locator('input.calibpick').setInputFiles(manifest.pack);
  await page.waitForSelector('.start .calib[data-calib="ready"]', { timeout: 60_000 });
  const startCalibBox = await page.locator('.start .calib[data-calib="ready"]').boundingBox();
  const startUnitsVisible = await page.locator('.start .calib .units').isVisible().catch(() => false);
  const startActsVisible = await page.locator('.start .calib .acts').isVisible().catch(() => false);

  // Import real card250 photos + g250 DAT/FX
  await page.locator('input.filepick:not(.calibpick)').setInputFiles(manifest.files);
  await waitGridReady(page, manifest.count);

  // Flow A: Keep shooting recipe -> HDR to Photos (target: 4 taps including system sheet 「存储图像」)
  let keepTaps = 0;
  const firstRawCell = page.locator(`.cell[data-id="${manifest.rawStems[0].toLowerCase()}"]`);
  await visibleTap(page, firstRawCell, 'grid photo (keep)');
  keepTaps += 1;
  await waitEditorReady(page);

  const exportBtn = page.locator('.editor .actbar [data-act="export"]');
  const exportBtnTextBefore = (await exportBtn.innerText()).trim();
  const hdrLoc = page.locator('.editor .nav [data-act="hdr-toggle"]');
  const hdrOnFirstRun = (await hdrLoc.count()) ? await hdrLoc.getAttribute('aria-checked') : null;

  let sheetAppearedKeep = false;
  await visibleTap(page, exportBtn, '存到相册 (keep)');
  keepTaps += 1;
  await page.waitForTimeout(200);
  if (await page.locator('.ms.export').count()) {
    sheetAppearedKeep = true;
    await visibleTap(page, page.locator('.ms.export .go'), 'base export sheet go (keep)');
    keepTaps += 1;
  }

  await page.waitForFunction(() => document.querySelector('.pdock')?.dataset.phase === 'ready', null, { timeout: 300_000 });
  const exportBtnTextWhileReady = (await exportBtn.innerText()).trim();
  const readyShareBtn = page.locator('.pdock [data-act="share"]');
  const readyTextKeep = (await readyShareBtn.innerText()).trim();
  const shareBeforeKeep = await page.evaluate(() => window.__shareLog.calls.length);
  await visibleTap(page, readyShareBtn, 'ready bar share (keep)');
  keepTaps += 1;
  await page.waitForFunction((k) => window.__shareLog.calls.length > k, shareBeforeKeep, { timeout: 30_000 });
  // +1 tap for 「存储图像」 in system share sheet
  keepTaps += 1;
  await page.waitForFunction(
    () => !document.querySelector('.pdock') || !!document.querySelector('.pdock [data-act="done"]'),
    null,
    { timeout: 10_000 },
  );
  const hasTrailingOkKeep = (await page.locator('.pdock [data-act="done"]').count()) > 0;
  if (hasTrailingOkKeep) {
    await visibleTap(page, page.locator('.pdock [data-act="done"]'), 'trailing done (keep)');
    await page.waitForFunction(() => !document.querySelector('.pdock'), null, { timeout: 10_000 });
  }
  const keepHdrResult = await page.evaluate(() => {
    const call = window.__shareLog.calls.at(-1);
    return { count: call?.count ?? 0, active: call?.active ?? false };
  });

  // Return to grid for Flow B
  await visibleTap(page, page.getByRole('button', { name: '照片', exact: true }), 'back to grid');
  await page.waitForSelector('[data-screen="grid"]');

  // Flow B: Change recipe -> HDR to Photos (target: 5 taps including system sheet 「存储图像」)
  let changeTaps = 0;
  await visibleTap(page, firstRawCell, 'grid photo (change)');
  changeTaps += 1;
  await waitEditorReady(page);

  // Pick a visible recipe card that is not currently active
  const nextCard = page.locator('.editor .strip[data-recipes] .rc:not(.on):not(.lockd)').first();
  const nextCardName = (await nextCard.locator('.nm').innerText()).trim();
  await visibleTap(page, nextCard, 'recipe card');
  changeTaps += 1;
  await page.waitForTimeout(300);

  let sheetAppearedChange = false;
  await visibleTap(page, exportBtn, '存到相册 (change)');
  changeTaps += 1;
  await page.waitForTimeout(200);
  if (await page.locator('.ms.export').count()) {
    sheetAppearedChange = true;
    await visibleTap(page, page.locator('.ms.export .go'), 'base export sheet go (change)');
    changeTaps += 1;
  }

  await page.waitForFunction(() => document.querySelector('.pdock')?.dataset.phase === 'ready', null, { timeout: 300_000 });
  const shareBeforeChange = await page.evaluate(() => window.__shareLog.calls.length);
  await visibleTap(page, page.locator('.pdock [data-act="share"]'), 'ready bar share (change)');
  changeTaps += 1;
  await page.waitForFunction((k) => window.__shareLog.calls.length > k, shareBeforeChange, { timeout: 30_000 });
  // +1 tap for 「存储图像」 in system share sheet
  changeTaps += 1;
  await page.waitForFunction(
    () => !document.querySelector('.pdock') || !!document.querySelector('.pdock [data-act="done"]'),
    null,
    { timeout: 10_000 },
  );
  const hasTrailingOkChange = (await page.locator('.pdock [data-act="done"]').count()) > 0;
  if (hasTrailingOkChange) {
    await visibleTap(page, page.locator('.pdock [data-act="done"]'), 'trailing done (change)');
    await page.waitForFunction(() => !document.querySelector('.pdock'), null, { timeout: 10_000 });
  }
  const navMatchesPickedRecipe = (await page.locator('.editor .nav .t b').innerText()).trim() === nextCardName;

  await ctx.close();
  return {
    viewport: `${width}x${height}`,
    startCalibHeight: Math.round(startCalibBox?.height ?? 0),
    startUnitsVisible,
    startActsVisible,
    exportBtnTextBefore,
    exportBtnTextWhileReady,
    hdrOnFirstRun,
    keepTaps,
    sheetAppearedKeep,
    readyTextKeep,
    hasTrailingOkKeep,
    keepHdrResult,
    changeTaps,
    sheetAppearedChange,
    hasTrailingOkChange,
    navMatchesPickedRecipe,
  };
}

async function runDetailedAcceptances() {
  const ctx = await browser.newContext({ viewport: { width: 390, height: 844 } });
  const page = await ctx.newPage();
  page.on('pageerror', (err) => out.pageErrors.push(`detail: ${err.message}`));
  await installShareMock(page);
  await page.goto(url);
  await settle(page);
  await page.evaluate(() => localStorage.clear());

  // 1) Start screen calibration collapse + tap opens full sheet
  await page.locator('input.calibpick').setInputFiles(manifest.pack);
  await page.waitForSelector('.start .calib[data-calib="ready"]', { timeout: 60_000 });
  const startCollapsed = {
    height: Math.round((await page.locator('.start .calib[data-calib="ready"]').boundingBox())?.height ?? 0),
    unitsVisible: await page.locator('.start .calib .units').isVisible().catch(() => false),
    actsVisible: await page.locator('.start .calib .acts').isVisible().catch(() => false),
  };
  await visibleTap(page, page.locator('.start .calib[data-calib="ready"]'), 'collapsed calib card');
  await page.waitForSelector('.calib-sheet .calib[data-calib="ready"]', { timeout: 10_000 });
  startCollapsed.sheetOpens = await page.locator('.calib-sheet .calib .acts').isVisible();
  await visibleTap(page, page.locator('.calib-sheet .nb', { hasText: '完成' }), 'close calib sheet');

  // 2) Grid calm-down checks (with 1-photo solo group included)
  await page.locator('input.filepick:not(.calibpick)').setInputFiles(manifest.gridFiles);
  await waitGridReady(page, manifest.gridCount);
  const gridBeforeCopy = {
    applyGroupButtons: await page.locator('.gh .go').count(),
    topSortButtons: await page.locator('.order [data-order]').count(),
    singleGroupHeadCount: await page.locator(`.gh[data-row="group:${manifest.soloStem.toLowerCase()}"]`).count(),
    groupHeadCount: await page.locator('.gh[data-row]').count(),
  };

  // Open grid ⋯ menu: sort is inside, 胶片柜 is gone
  await visibleTap(page, page.locator('.nav .nb.plain'), 'grid more');
  await page.waitForSelector('.ms');
  const gridMenuText = await page.locator('.ms').innerText();
  const hasSortInMenu = gridMenuText.includes('从早到晚') && gridMenuText.includes('从晚到早');
  const hasFilmCabinet = gridMenuText.includes('胶片柜');
  const firstIdBeforeSort = await page.locator('.cell').first().getAttribute('data-id');
  await visibleTap(page, page.locator('.ms [data-order="newest"]'), 'sort newest in menu');
  await page.waitForFunction(() => !document.querySelector('.ms'), null, { timeout: 10_000 });
  const firstIdAfterSort = await page.locator('.cell').first().getAttribute('data-id');
  // Switch back to oldest so positions match 1..N
  await visibleTap(page, page.locator('.nav .nb.plain'), 'grid more again');
  await visibleTap(page, page.locator('.ms [data-order="oldest"]'), 'sort oldest in menu');
  await page.waitForFunction(() => !document.querySelector('.ms'), null, { timeout: 10_000 });

  out.steps.grid = {
    startCollapsed,
    gridBeforeCopy,
    hasSortInMenu,
    hasFilmCabinet,
    sortReversed: firstIdBeforeSort !== firstIdAfterSort,
  };

  // 3) Ready bar queue: open photo 3 (1-based index 3), export, swipe to photo 4, export -> both listed, both save
  const pos3Cell = page.locator('.cell').nth(2);
  const pos3Id = await pos3Cell.getAttribute('data-id');
  const pos4Id = await page.locator('.cell').nth(3).getAttribute('data-id');
  await visibleTap(page, pos3Cell, 'open photo 3');
  await page.waitForSelector('.editor');
  await page.waitForFunction(() => document.querySelector('.app')?.dataset.frame === '1', null, { timeout: 120_000 });

  // Top bar check: no filename in .nav .t, shows recipe name + "3 / N"
  const navTitle = (await page.locator('.editor .nav .t b').innerText()).trim();
  const navSub = (await page.locator('.editor .nav .t small').innerText()).trim();
  const navHasFilename = navTitle.toLowerCase().includes(pos3Id) || navSub.toLowerCase().includes(pos3Id);
  const navSubMatchesPos = navSub === `3 / ${manifest.gridCount}`;

  // Start export on photo 3
  await visibleTap(page, page.locator('.editor .actbar [data-act="export"]'), 'export photo 3');
  await page.waitForFunction(() => document.querySelector('.pdock')?.dataset.phase === 'ready', null, { timeout: 300_000 });
  const readyAfterPhoto3 = (await page.locator('.pdock').innerText()).trim();

  // Swipe left to photo 4 while photo 3 is STILL unsaved in ready bar
  const stageBox = await page.locator('.stage').boundingBox();
  const sx = stageBox.x + stageBox.width / 2;
  const sy = stageBox.y + stageBox.height / 2;
  await page.mouse.move(sx, sy);
  await page.mouse.down();
  await page.mouse.move(sx - 140, sy, { steps: 8 });
  await page.mouse.up();
  await page.waitForFunction((want) => document.querySelector('.editor')?.dataset.photo?.toLowerCase() === want, pos4Id, { timeout: 60_000 });
  await page.waitForFunction(() => document.querySelector('.app')?.dataset.frame === '1', null, { timeout: 120_000 });

  // Start export on photo 4 -> must NOT discard photo 3!
  await visibleTap(page, page.locator('.editor .actbar [data-act="export"]'), 'export photo 4');
  await page.waitForFunction(() => document.querySelector('.pdock')?.dataset.phase === 'ready', null, { timeout: 300_000 });
  const readyAfterPhoto4 = (await page.locator('.pdock').innerText()).trim();
  const shareBeforePair = await page.evaluate(() => window.__shareLog.calls.length);
  await visibleTap(page, page.locator('.pdock [data-act="share"]'), 'share photo 3 + 4');
  await page.waitForFunction((k) => window.__shareLog.calls.length > k, shareBeforePair, { timeout: 30_000 });
  await page.waitForFunction(() => !document.querySelector('.pdock'), null, { timeout: 10_000 });
  const pairShare = await page.evaluate((from) => {
    const calls = window.__shareLog.calls.slice(from);
    const names = calls.flatMap((c) => c.files.map((f) => f.name.toLowerCase()));
    return { totalFiles: names.length, names };
  }, shareBeforePair);

  out.steps.readyQueue = {
    navHasFilename,
    navSubMatchesPos,
    readyHasPos3: readyAfterPhoto3.includes('第 3 张做好了 · 存进相册'),
    readyHasPairCount: readyAfterPhoto4.includes('2 张做好了 · 存进相册'),
    readyNamesBoth: readyAfterPhoto4.includes('第 3 张') && readyAfterPhoto4.includes('第 4 张'),
    sharedTotal: pairShare.totalFiles,
    hasPhoto3: pairShare.names.some((n) => n.includes(pos3Id)),
    hasPhoto4: pairShare.names.some((n) => n.includes(pos4Id)),
  };

  // 4) Working ⋯: check all 5 entries open their sheets
  // Open photo 1 (RAW) for ⋯ menu
  await visibleTap(page, page.getByRole('button', { name: '照片', exact: true }), 'back to grid');
  await page.waitForSelector('[data-screen="grid"]');
  await visibleTap(page, page.locator(`.cell[data-id="${manifest.rawStems[0].toLowerCase()}"]`), 'open raw 1');
  await waitEditorReady(page);

  const openEditorMore = async () => {
    await visibleTap(page, page.locator('.editor .nav [data-act="editor-more"]'), 'editor ⋯');
    await page.waitForSelector('.ms[data-state="editor-more"]');
  };

  await openEditorMore();
  const moreEntries = await page.locator('.ms[data-state="editor-more"] .lrow b').allInnerTexts();
  // 4a) 照片信息
  await visibleTap(page, page.locator('.ms[data-state="editor-more"] [data-act="more-photo-info"]'), 'more 照片信息');
  await page.waitForSelector('.ms[data-state="photo-info"]');
  const infoText = await page.locator('.ms[data-state="photo-info"]').innerText();
  const photoInfoHasStem = infoText.toLowerCase().includes(manifest.rawStems[0].toLowerCase());
  await visibleTap(page, page.locator('.ms[data-state="photo-info"] .nb', { hasText: '完成' }), 'close photo info');

  // 4b) 导出选项 (change size to medium + quality to low to verify persistence across reload later)
  await openEditorMore();
  await visibleTap(page, page.locator('.ms[data-state="editor-more"] [data-act="more-export-options"]'), 'more 导出选项');
  await page.waitForSelector('.ms.export[data-state="export-sheet"]');
  const exportOptionsTitle = (await page.locator('.ms.export h2').innerText()).trim();
  await visibleTap(page, page.locator('.ms.export .frow').nth(0).locator('.seg button', { hasText: '中' }), 'size medium');
  await visibleTap(page, page.locator('.ms.export .frow').nth(1).locator('.seg button', { hasText: '低' }), 'quality low');
  await visibleTap(page, page.locator('.ms.export .row .nb'), 'close export options');

  // 4c) 复制调整 -> copy an edit so 粘贴 & 套用到这一组 both open confirm sheet
  const cardToCopy = page.locator('.editor .strip[data-recipes] .rc:not(.on):not(.lockd)').first();
  await visibleTap(page, cardToCopy, 'pick recipe before copy');
  await openEditorMore();
  await visibleTap(page, page.locator('.ms[data-state="editor-more"] [data-act="more-copy"]'), 'more 复制调整');
  await page.waitForSelector('[data-state="copy-options"]');
  const copySheetOpened = true;
  await visibleTap(page, page.locator('[data-state="copy-options"] .btn.primary'), 'confirm copy');
  await page.waitForSelector('[data-state="copied-where"]');
  await visibleTap(page, page.locator('[data-state="copied-where"] .btn.ghost'), '先不套用');

  // Navigate to another RAW photo to test 粘贴 & 套用到这一组
  await visibleTap(page, page.getByRole('button', { name: '照片', exact: true }), 'back to grid after copy');
  await page.waitForSelector('[data-screen="grid"]');
  const applyGroupAfterCopy = await page.locator('.gh .go').count();
  await visibleTap(page, page.locator(`.cell[data-id="${manifest.rawStems[1].toLowerCase()}"]`), 'open raw 2');
  await waitEditorReady(page);

  // 4d) 粘贴
  await openEditorMore();
  await visibleTap(page, page.locator('.ms[data-state="editor-more"] [data-act="more-paste"]'), 'more 粘贴');
  await page.waitForSelector('[data-state="confirm"]');
  const pasteConfirmTitle = (await page.locator('[data-state="confirm"] h2').innerText()).trim();
  await visibleTap(page, page.locator('[data-state="confirm"] .btn', { hasText: '取消' }), 'cancel paste confirm');

  // 4e) 套用到这一组
  await openEditorMore();
  await visibleTap(page, page.locator('.ms[data-state="editor-more"] [data-act="more-apply-group"]'), 'more 套用到这一组');
  await page.waitForSelector('[data-state="confirm"]');
  const groupConfirmTitle = (await page.locator('[data-state="confirm"] h2').innerText()).trim();
  await visibleTap(page, page.locator('[data-state="confirm"] .btn', { hasText: '取消' }), 'cancel group confirm');

  out.steps.more = {
    moreEntries,
    photoInfoHasStem,
    exportOptionsTitle,
    copySheetOpened,
    applyGroupAfterCopy,
    pasteConfirmTitle,
    groupConfirmTitle,
  };

  // 5) HDR top bar: first-run on -> turn off -> reload -> still off; JPG-only & P0-diptych disabled with reason
  const hdrBtn = page.locator('.editor .nav [data-act="hdr-toggle"]');
  const hdrInitial = await hdrBtn.getAttribute('aria-checked');
  await visibleTap(page, hdrBtn, 'toggle HDR off');
  const hdrAfterOff = await hdrBtn.getAttribute('aria-checked');

  // Reload page to verify saved OFF + saved size/quality survive reload
  await page.reload();
  await settle(page);
  await page.locator('input.filepick:not(.calibpick)').setInputFiles(manifest.gridFiles);
  await waitGridReady(page, manifest.gridCount);
  await visibleTap(page, page.locator(`.cell[data-id="${manifest.rawStems[0].toLowerCase()}"]`), 'reopen raw after reload');
  await waitEditorReady(page);
  const hdrAfterReload = await page.locator('.editor .nav [data-act="hdr-toggle"]').getAttribute('aria-checked');
  const persistedPrefs = await page.evaluate(() => ({
    size: localStorage.getItem('gxr.mobile.export_size'),
    quality: localStorage.getItem('gxr.mobile.export_quality'),
    hdr: localStorage.getItem('gxr.mobile.export_hdr'),
  }));

  // Open JPG-only photo: HDR toggle disabled with reason
  await visibleTap(page, page.getByRole('button', { name: '照片', exact: true }), 'back to grid for jpg-only');
  await page.waitForSelector('[data-screen="grid"]');
  await visibleTap(page, page.locator(`.cell[data-id="${manifest.jpgOnlyStem.toLowerCase()}"]`), 'open jpg-only');
  await page.waitForSelector('.editor');
  const jpgHdrBtn = page.locator('.editor .nav [data-act="hdr-toggle"]');
  const jpgHdrDisabled = await jpgHdrBtn.isDisabled();
  const jpgHdrReason = await jpgHdrBtn.getAttribute('data-reason');
  await visibleTap(page, page.locator('.editor .nav .hdr-wrap'), 'tap disabled HDR wrapper');
  await page.waitForSelector('.ftoast', { timeout: 5_000 });
  const jpgHdrToast = (await page.locator('.ftoast').innerText()).trim();

  // Open P0-diptych photo (card250 #2): HDR toggle also disabled with reason
  await visibleTap(page, page.getByRole('button', { name: '照片', exact: true }), 'back to grid for diptych');
  await page.waitForSelector('[data-screen="grid"]');
  await visibleTap(page, page.locator(`.cell[data-id="${manifest.diptychStem.toLowerCase()}"]`), 'open diptych');
  await page.waitForSelector('.editor');
  const diptychHdrBtn = page.locator('.editor .nav [data-act="hdr-toggle"]');
  const diptychHdrDisabled = await diptychHdrBtn.isDisabled();
  const diptychHdrReason = await diptychHdrBtn.getAttribute('data-reason');

  out.steps.hdr = {
    hdrInitial,
    hdrAfterOff,
    hdrAfterReload,
    persistedPrefs,
    jpgHdrDisabled,
    jpgHdrReason,
    jpgHdrToast,
    diptychHdrDisabled,
    diptychHdrReason,
  };

  await ctx.close();
}

try {
  out.steps.tap390 = await runTapCountViewport(390, 844);
  out.steps.tap430 = await runTapCountViewport(430, 932);
  await runDetailedAcceptances();
} catch (err) {
  out.error = String(err?.stack ?? err);
} finally {
  await browser.close();
}

writeFileSync(manifest.reportPath, JSON.stringify(out, null, 2));
console.log('done');
if (out.error) process.exit(1);
