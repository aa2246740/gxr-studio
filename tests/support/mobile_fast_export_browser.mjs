// U1 phone-fast-export 真实照片浏览器验收驱动（390×844 与 430×932，真 card250 + g250）。
// 隐私约束：绝不向 stdout 或报告输出任何照片文件名、拍摄时间或 EXIF；仅输出 redacted 布尔与计数。

import { pathToFileURL } from 'node:url';
import { readFileSync, writeFileSync } from 'node:fs';
import { phoneTouch } from './phone_touch.mjs';

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

async function visibleInactiveRecipeCard(page) {
  await page.waitForFunction(
    () => {
      const strip = document.querySelector('.editor .strip[data-recipes]');
      const on = strip?.querySelector('.rc.on');
      if (!strip || !on) return false;
      const sr = strip.getBoundingClientRect();
      const cr = on.getBoundingClientRect();
      return cr.width >= 10 && cr.left >= sr.left - 1 && cr.right <= sr.right + 1;
    },
    null,
    { timeout: 10_000 },
  ).catch(() => {});
  await page.evaluate(() => new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r))));
  const cardId = await page.evaluate(() => {
    const strip = document.querySelector('.editor .strip[data-recipes]');
    if (!strip) return null;
    const sr = strip.getBoundingClientRect();
    const cards = strip.querySelectorAll('.rc:not(.on):not(.lockd)');
    for (const card of cards) {
      const r = card.getBoundingClientRect();
      if (r.width >= 10 && r.height >= 10 && r.left >= Math.max(0, sr.left) && r.right <= Math.min(window.innerWidth, sr.right)) {
        return card.getAttribute('data-card');
      }
    }
    return null;
  });
  if (!cardId) return page.locator('.editor .strip[data-recipes] .rc:not(.on):not(.lockd)').first();
  return page.locator(`.editor .strip[data-recipes] .rc[data-card="${cardId}"]`);
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
  await ctx.addInitScript(phoneTouch);
  const page = await ctx.newPage();
  page.on('pageerror', (err) => out.pageErrors.push(`${width}x${height}: ${err.message}`));
  await installShareMock(page);
  await page.goto(url);
  await settle(page);
  await page.evaluate(() => localStorage.clear());

  // Import calibration pack. The start screen has no calibration card any more (default calibration; 更多 → 校准数据).
  await page.locator('input.calibpick').setInputFiles(manifest.pack);
  await page.waitForFunction(() => window.__gxrBook?.pack?.kind === 'user' && !window.__gxrBook.packBusy, null, { timeout: 60_000 });
  const startCalibBox = (await page.locator('.start .calib').count()) ? await page.locator('.start .calib').boundingBox() : null;
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

  const exportBtn = page.locator('.editor .toolbar [data-act="export"]');
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
  const nextCard = await visibleInactiveRecipeCard(page);
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
  await ctx.addInitScript(phoneTouch);
  const page = await ctx.newPage();
  page.on('pageerror', (err) => out.pageErrors.push(`detail: ${err.message}`));
  await installShareMock(page);
  await page.goto(url);
  await settle(page);
  await page.evaluate(() => localStorage.clear());

  // 1) Start screen has no calibration step any more; the full panel is in 更多 → 校准数据 (opened from the grid ⋯ below).
  await page.locator('input.calibpick').setInputFiles(manifest.pack);
  await page.waitForFunction(() => window.__gxrBook?.pack?.kind === 'user' && !window.__gxrBook.packBusy, null, { timeout: 60_000 });
  const startCards = await page.locator('.start .calib').count();
  const startCollapsed = {
    cards: startCards,
    height: startCards ? Math.round((await page.locator('.start .calib').boundingBox())?.height ?? 0) : 0,
    unitsVisible: await page.locator('.start .calib .units').isVisible().catch(() => false),
    actsVisible: await page.locator('.start .calib .acts').isVisible().catch(() => false),
  };

  // 2) Grid calm-down checks (with 1-photo solo group included)
  await page.locator('input.filepick:not(.calibpick)').setInputFiles(manifest.gridFiles);
  await waitGridReady(page, manifest.gridCount);
  await visibleTap(page, page.locator('.nav button[aria-label="更多"]'), 'grid more');
  await visibleTap(page, page.locator('.ms .lrow', { hasText: '校准数据' }), 'grid more calibration');
  await page.waitForSelector('.calib-sheet .calib[data-calib="user"]', { timeout: 10_000 });
  startCollapsed.sheetOpens = await page.locator('.calib-sheet .calib .acts').isVisible();
  await visibleTap(page, page.locator('.calib-sheet .nb', { hasText: '完成' }), 'close calib sheet');
  const soloRowInfo = await page.evaluate((soloId) => {
    const cell = document.querySelector(`.cell[data-id="${soloId}"]`);
    const row = cell?.closest('.cells');
    const prev = row?.previousElementSibling;
    const cellRect = cell?.getBoundingClientRect();
    const prevRect = prev?.getBoundingClientRect();
    return {
      hasSoloSep: Boolean(row?.classList.contains('solo-sep')),
      gapPx: cellRect && prevRect ? Math.round(cellRect.top - prevRect.bottom) : 0,
    };
  }, manifest.soloStem.toLowerCase());
  const gridBeforeCopy = {
    applyGroupButtons: await page.locator('.gh .go').count(),
    topSortButtons: await page.locator('.order [data-order]').count(),
    singleGroupHeadCount: await page.locator(`.gh[data-row="group:${manifest.soloStem.toLowerCase()}"]`).count(),
    groupHeadCount: await page.locator('.gh[data-row]').count(),
    soloRowInfo,
  };

  // Open grid ⋯ menu: sort is inside, 胶片柜 is gone, and 「完成」 sits on the same line as 「更多」
  await visibleTap(page, page.locator('.nav .nb.plain'), 'grid more');
  await page.waitForSelector('.ms');
  const gridMenuHeaderSameLine = await page.evaluate(() => {
    const h2 = document.querySelector('.ms > .hd h2')?.getBoundingClientRect();
    const done = document.querySelector('.ms > .hd .nb')?.getBoundingClientRect();
    return Boolean(h2 && done && Math.abs(h2.y - done.y) < 16);
  });
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
    gridMenuHeaderSameLine,
    hasSortInMenu,
    hasFilmCabinet,
    sortReversed: firstIdBeforeSort !== firstIdAfterSort,
  };

  // 3) Ready bar queue + running-bar save-ready observation: open photo 3, export, swipe to photo 4, export -> both listed, both save
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

  const readReadyGeom = async () =>
    page.evaluate(() => {
      const dock = document.querySelector('.pdock')?.getBoundingClientRect();
      const bar = document.querySelector('.editor .toolbar')?.getBoundingClientRect();
      const btn = document.querySelector('.editor .toolbar [data-act="export"]')?.getBoundingClientRect();
      if (!dock || !bar || !btn) return { height: 999, clearsToolbar: false };
      return {
        height: Math.round(dock.height * 10) / 10,
        clearsToolbar: dock.bottom <= bar.top + 0.5 && dock.bottom <= btn.top + 0.5,
      };
    });

  // Start export on photo 3
  await visibleTap(page, page.locator('.editor .toolbar [data-act="export"]'), 'export photo 3');
  await page.waitForFunction(() => document.querySelector('.pdock')?.dataset.phase === 'ready', null, { timeout: 300_000 });
  const readyAfterPhoto3 = (await page.locator('.pdock').innerText()).trim();
  const readyGeom3 = await readReadyGeom();

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

  // Observe whether the running bar offers direct 「存进相册」 for finished photo 3 while photo 4 exports
  await page.evaluate(() => {
    window.__runningShareSeen = { seen: false, text: '' };
    const check = () => {
      const dock = document.querySelector('.pdock[data-phase="running"]');
      const btn = dock?.querySelector('[data-act="share-ready"]');
      if (btn) {
        window.__runningShareSeen.seen = true;
        window.__runningShareSeen.text = btn.textContent?.trim() ?? '';
      }
    };
    const obs = new MutationObserver(check);
    obs.observe(document.querySelector('.app'), { childList: true, subtree: true, attributes: true });
    window.__runningShareObs = obs;
  });

  // Start export on photo 4 -> must NOT discard photo 3!
  await visibleTap(page, page.locator('.editor .toolbar [data-act="export"]'), 'export photo 4');
  await page.waitForFunction(() => document.querySelector('.pdock')?.dataset.phase === 'ready', null, { timeout: 300_000 });
  const runningShareSeen = await page.evaluate(() => {
    window.__runningShareObs?.disconnect();
    return window.__runningShareSeen;
  });
  const readyGeomPair = await readReadyGeom();
  const readyAfterPhoto4Collapsed = (await page.locator('.pdock').innerText()).trim();
  if (await page.locator('.pdock [data-act="toggle-details"]').count()) {
    await visibleTap(page, page.locator('.pdock [data-act="toggle-details"]'), 'expand pair ready details');
  }
  const readyAfterPhoto4Expanded = (await page.locator('.export-sheet').innerText()).trim();
  const readyGeomPairExpanded = await readReadyGeom();
  const shareBeforePair = await page.evaluate(() => window.__shareLog.calls.length);
  await visibleTap(page, page.locator('.export-sheet [data-act="share"]'), 'share photo 3 + 4');
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
    readyBarPhoto3Height: readyGeom3.height,
    readyBarPhoto3ClearsToolbar: readyGeom3.clearsToolbar,
    runningShareSeen,
    readyHasPairCount: readyAfterPhoto4Collapsed.includes('2 张做好了 · 存进相册'),
    readyBarPairCollapsedHeight: readyGeomPair.height,
    readyBarPairClearsToolbar: readyGeomPair.clearsToolbar && readyGeomPairExpanded.clearsToolbar,
    readyNamesBoth: readyAfterPhoto4Expanded.includes('第 3 张') && readyAfterPhoto4Expanded.includes('第 4 张'),
    sharedTotal: pairShare.totalFiles,
    hasPhoto3: pairShare.names.some((n) => n.includes(pos3Id)),
    hasPhoto4: pairShare.names.some((n) => n.includes(pos4Id)),
  };

  // 3b) Double tap on 「存到相册」 -> 1 export, 1 share call with 1 file (no duplicate!)
  const exportBtnPhoto4 = page.locator('.editor .toolbar [data-act="export"]');
  const shareBeforeDouble = await page.evaluate(() => window.__shareLog.calls.length);
  await visibleTap(page, exportBtnPhoto4, 'double tap 1');
  await exportBtnPhoto4.click();
  await page.waitForFunction(() => document.querySelector('.pdock')?.dataset.phase === 'ready', null, { timeout: 300_000 });
  // Tapping again while ready-and-unsaved must also do nothing new
  await exportBtnPhoto4.click();
  await page.waitForTimeout(150);
  const doubleTapReadyText = (await page.locator('.pdock [data-act="share"]').innerText()).trim();
  await visibleTap(page, page.locator('.pdock [data-act="share"]'), 'share after double tap');
  await page.waitForFunction((k) => window.__shareLog.calls.length > k, shareBeforeDouble, { timeout: 30_000 });
  await page.waitForFunction(() => !document.querySelector('.pdock'), null, { timeout: 10_000 });
  const doubleTapShare = await page.evaluate((from) => {
    const calls = window.__shareLog.calls.slice(from);
    return {
      callCount: calls.length,
      totalFiles: calls.reduce((sum, c) => sum + c.count, 0),
    };
  }, shareBeforeDouble);
  out.steps.doubleTap = {
    readyText: doubleTapReadyText,
    callCount: doubleTapShare.callCount,
    totalFiles: doubleTapShare.totalFiles,
  };

  // 4) Working ⋯: before copying hides 粘贴 & 套用到这一组; after copying shows all 5 entries
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
  const moreEntriesBeforeCopy = await page.locator('.ms[data-state="editor-more"] .lrow b').allInnerTexts();
  // 4a) 照片信息
  await visibleTap(page, page.locator('.ms[data-state="editor-more"] [data-act="more-photo-info"]'), 'more 照片信息');
  await page.waitForSelector('.ms[data-state="photo-info"]');
  const infoText = await page.locator('.ms[data-state="photo-info"]').innerText();
  const photoInfoHasStem = infoText.toLowerCase().includes(manifest.rawStems[0].toLowerCase());
  await visibleTap(page, page.locator('.ms[data-state="photo-info"] .nb', { hasText: '完成' }), 'close photo info');

  // 4b) 导出选项 (verify manager-approved footnote; change size to medium + quality to low to verify persistence)
  await openEditorMore();
  await visibleTap(page, page.locator('.ms[data-state="editor-more"] [data-act="more-export-options"]'), 'more 导出选项');
  await page.waitForSelector('.ms.export[data-state="export-sheet"]');
  const exportOptionsTitle = (await page.locator('.ms.export h2').innerText()).trim();
  const exportOptionsFoot = (await page.locator('.ms.export .foot').innerText()).trim();
  await visibleTap(page, page.locator('.ms.export .frow').nth(0).locator('.seg button', { hasText: '中' }), 'size medium');
  await visibleTap(page, page.locator('.ms.export .frow').nth(1).locator('.seg button', { hasText: '低' }), 'quality low');
  await visibleTap(page, page.locator('.ms.export .row .nb'), 'close export options');

  // 4c) 复制调整 -> copy an edit so 粘贴 & 套用到这一组 appear and open confirm sheet
  const cardToCopy = await visibleInactiveRecipeCard(page);
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

  // 4d) 粘贴 (after copying, all 5 entries are present)
  await openEditorMore();
  const moreEntriesAfterCopy = await page.locator('.ms[data-state="editor-more"] .lrow b').allInnerTexts();
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
    moreEntriesBeforeCopy,
    moreEntriesAfterCopy,
    photoInfoHasStem,
    exportOptionsTitle,
    exportOptionsFoot,
    copySheetOpened,
    applyGroupAfterCopy,
    pasteConfirmTitle,
    groupConfirmTitle,
  };

  // 5) HDR top bar: first-run on (filled accent) -> turn off -> reload -> still off; JPG-only & P0-diptych disabled with reason
  const hdrBtn = page.locator('.editor .nav [data-act="hdr-toggle"]');
  const hdrInitial = await hdrBtn.getAttribute('aria-checked');
  const hdrStyleOn = await hdrBtn.evaluate((el) => {
    const s = getComputedStyle(el);
    return { bg: s.backgroundColor, color: s.color };
  });
  await visibleTap(page, hdrBtn, 'toggle HDR off');
  const hdrAfterOff = await hdrBtn.getAttribute('aria-checked');
  const hdrStyleOff = await hdrBtn.evaluate((el) => {
    const s = getComputedStyle(el);
    return { bg: s.backgroundColor, color: s.color };
  });

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
    hdrStyleOn,
    hdrAfterOff,
    hdrStyleOff,
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
