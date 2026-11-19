// 手机端拍摄配方采用 + 「全部重置」清曲线的真实浏览器测试（card250 实拍 + g250 卡包 + 异代 DAT）。
// 只在 stdout 打印 "done"；绝不打印照片文件名、拍摄时间、EXIF 或原始标签。

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
    const reloaded = await page
      .waitForEvent('framenavigated', { timeout: 4_000 })
      .then(() => true)
      .catch(() => false);
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

async function editStateOf(page, stem) {
  return page.evaluate((s) => {
    const row = window.__gxrEdits?.().find((item) => item.stem === s) ?? null;
    if (!row) return null;
    return {
      rev: row.rev,
      choice: row.choice,
      levels: row.levels,
      knobs: row.knobs,
      curve: row.curve,
    };
  }, stem);
}

async function waitRecipeName(page, expectedName) {
  await page.waitForFunction(
    (want) => document.querySelector('.editor .recipe-head .rname')?.textContent?.trim() === want,
    expectedName,
    { timeout: 60_000 },
  );
}

async function waitStageRecipeReady(page) {
  await page.waitForFunction(
    () => (document.querySelector('.app')?.getAttribute('data-recipe-key') || '').startsWith('bake:'),
    null,
    { timeout: 60_000 },
  );
  return page.evaluate(() => document.querySelector('.app')?.getAttribute('data-recipe-key') || '');
}

async function runMain(page) {
  await page.addInitScript(fakeShare);
  await page.goto(url);
  await settle(page);
  await page.evaluate((binding) => {
    window.__devExport ??= {};
    window.__devExport.binding = binding;
  }, manifest.binding);

  await pickAll(page, { pack: manifest.pack, count: 3 }, manifest.files);
  await page.waitForFunction(
    () => window.__gxrBook?.dat?.read?.entries?.length === 13248 && !window.__gxrBook.datBusy,
    null,
    { timeout: 120_000 },
  );

  // 1. Untouched grid badges before opening any photo.
  const badgeBefore = await page.locator(`${cellOf(manifest.file1)} .edt`).count();

  // 2. Open card250 #1 -> 配方 tab should adopt Teal Orange, render it on stage, without storing photo.choice.
  await openPhoto(page, manifest.file1);
  await page.locator('.editor .toolbar [data-tool="recipe"]').click();
  await page.waitForSelector('.editor .recipe-head .rname');
  await waitStageRecipeReady(page);
  const adoptedHeading = (await page.locator('.editor .recipe-head .rname').textContent())?.trim() ?? '';
  const adoptedTag = (await page.locator('.editor .recipe-head .neutral-tag').textContent())?.trim() ?? '';
  const { adoptedOnName, adoptedOnCard } = await page.evaluate(() => {
    const on = document.querySelector('.strip[data-recipes] .rc.on');
    return {
      adoptedOnName: on?.querySelector('.nm')?.textContent?.trim() ?? '',
      adoptedOnCard: on?.getAttribute('data-card') ?? null,
    };
  });
  const adoptedStageKey = await page.evaluate(() => document.querySelector('.app')?.getAttribute('data-recipe-key') || '');
  const adoptedState = await editStateOf(page, manifest.file1);
  const adoptedSide = await page.evaluate(async (s) => (await window.__gxrRequest(s))?.side ?? null, manifest.file1);

  // Check copy sheet on untouched adopted photo: 'recipe' should NOT be marked as "这张没有设置".
  await page.locator('.editor .toolbar [data-tool="more"]').click();
  await page.locator('.more-sheet [data-more="copy"]').click();
  await page.waitForSelector('.paste-sheet[data-state="copy-options"]');
  const recipeCopyRowText = (await page.locator('.paste-sheet .optrow[data-group="recipe"]').innerText()).trim();
  await page.locator('.paste-sheet .hd .nb').click();
  await page.waitForSelector('.paste-sheet', { state: 'detached' });

  out.steps.adopted = {
    badgeBefore,
    heading: adoptedHeading,
    tag: adoptedTag,
    stripOnName: adoptedOnName,
    stripOnCard: adoptedOnCard,
    stageKey: adoptedStageKey,
    choice: adoptedState?.choice ?? null,
    sideHasRecipe: Boolean(adoptedSide?.recipe),
    recipeCopyRowHasNotSet: recipeCopyRowText.includes('这张没有设置'),
  };

  // Back to grid: untouched adopted photo still has no pencil badge.
  await backToGrid(page);
  const badgeAfterOpen = await page.locator(`${cellOf(manifest.file1)} .edt`).count();
  out.steps.adopted.badgeAfterOpen = badgeAfterOpen;
  if (adoptedHeading !== 'Teal Orange' || !out.steps.adopted.sideHasRecipe) {
    return false;
  }

  // 3. Export auto on untouched adopted file1.
  await exportFromGrid(page, [manifest.file1], '原始尺寸', '高');
  await waitPhase(page, ['running', 'ready']);
  await waitPhase(page, ['ready']);
  const autoFacts = await batchFacts(page);
  await shareAll(page);
  const autoFiles = await sharedFiles(page, manifest.outDir, 'auto-');
  if (await page.locator('.pdock [data-act="done"]').count()) {
    await page.locator('.pdock [data-act="done"]').first().click();
  }

  // 4. Open file1, switch away and back to Teal Orange so Teal Orange is explicitly chosen (photo.choice !== null),
  // then export again -> must match auto byte-for-byte.
  await openPhoto(page, manifest.file1);
  await page.locator('.editor .toolbar [data-tool="recipe"]').click();
  await page.waitForSelector('.strip[data-recipes] .rc');
  const stdFirst = page.locator('.strip[data-recipes] .rc[data-card="builtin:4"]');
  await stdFirst.scrollIntoViewIfNeeded();
  await stdFirst.click();
  await waitRecipeName(page, '原厂标准');
  await page.waitForFunction(
    (prev) => {
      const key = document.querySelector('.app')?.getAttribute('data-recipe-key') || '';
      return key.startsWith('bake:') && key !== prev;
    },
    adoptedStageKey,
    { timeout: 60_000 },
  );
  const tealCard = page.locator('.strip[data-recipes] .rc', { has: page.locator('.nm', { hasText: /^Teal Orange$/ }) }).first();
  await tealCard.scrollIntoViewIfNeeded();
  await tealCard.click();
  await waitRecipeName(page, 'Teal Orange');
  await page.waitForFunction(
    (want) => document.querySelector('.app')?.getAttribute('data-recipe-key') === want,
    adoptedStageKey,
    { timeout: 60_000 },
  );
  const explicitTealStageKey = await waitStageRecipeReady(page);
  const explicitTealState = await editStateOf(page, manifest.file1);
  await backToGrid(page);

  const shareFrom = await page.evaluate(() => window.__shareLog.calls.length);
  await exportFromGrid(page, [manifest.file1], '原始尺寸', '高');
  await waitPhase(page, ['running', 'ready']);
  await waitPhase(page, ['ready']);
  const explicitFacts = await batchFacts(page);
  await shareAll(page);
  const explicitFiles = await sharedFiles(page, manifest.outDir, 'explicit-', shareFrom);
  if (await page.locator('.pdock [data-act="done"]').count()) {
    await page.locator('.pdock [data-act="done"]').first().click();
  }
  const autoFileCount = autoFiles.reduce((n, row) => n + row.files.length, 0);
  const explicitFileCount = explicitFiles.reduce((n, row) => n + row.files.length, 0);
  out.steps.exportParity = {
    autoCount: autoFileCount,
    explicitCount: explicitFileCount,
    explicitChoice: explicitTealState?.choice ?? null,
    explicitStageKey: explicitTealStageKey,
    autoStageEqualsExplicit: adoptedStageKey === explicitTealStageKey,
    autoOk: autoFacts.done,
    explicitOk: explicitFacts.done,
  };

  // 5. Choose 原厂标准 (builtin:4) -> persists and wins across back + reopen.
  await openPhoto(page, manifest.file1);
  await page.locator('.editor .toolbar [data-tool="recipe"]').click();
  const stdCard = page.locator('.strip[data-recipes] .rc[data-card="builtin:4"]');
  await stdCard.scrollIntoViewIfNeeded();
  await stdCard.click();
  await waitRecipeName(page, '原厂标准');
  await page.waitForFunction(
    (prev) => {
      const key = document.querySelector('.app')?.getAttribute('data-recipe-key') || '';
      return key.startsWith('bake:') && key !== prev;
    },
    adoptedStageKey,
    { timeout: 60_000 },
  );
  const stdStageKey = await waitStageRecipeReady(page);
  await backToGrid(page);
  const badgeAfterExplicit = await page.locator(`${cellOf(manifest.file1)} .edt`).count();
  await openPhoto(page, manifest.file1);
  await page.locator('.editor .toolbar [data-tool="recipe"]').click();
  await page.waitForTimeout(300);
  const persistedHeading = (await page.locator('.editor .recipe-head .rname').textContent())?.trim() ?? '';
  const persistedState = await editStateOf(page, manifest.file1);
  await backToGrid(page);
  out.steps.explicitWins = {
    stdStageKey,
    stageDiffersFromAdopted: stdStageKey !== adoptedStageKey,
    badgeAfterExplicit,
    persistedHeading,
    persistedChoice: persistedState?.choice ?? null,
  };

  // 6. Diptych (file2) and JPG-only photo do NOT adopt.
  await openPhoto(page, manifest.diptych);
  await page.locator('.editor .toolbar [data-tool="recipe"]').click();
  await page.locator('.editor .recipe-head [data-act="recipe-status"]').click();
  await page.waitForSelector('.recipe-info-sheet');
  const diptychPanelText = (await page.locator('.recipe-info-sheet').innerText()).trim();
  await page.locator('.recipe-info-sheet [data-act="close"]').click();
  const diptychState = await editStateOf(page, manifest.diptych);
  const diptychSide = await page.evaluate(async (s) => (await window.__gxrRequest(s))?.side ?? null, manifest.diptych);
  await backToGrid(page);

  await openPhoto(page, manifest.jpgOnly);
  await page.locator('.editor .toolbar [data-tool="recipe"]').click();
  await page.locator('.editor .recipe-head [data-act="recipe-status"]').click();
  await page.waitForSelector('.recipe-info-sheet');
  const jpgPanelText = (await page.locator('.recipe-info-sheet').innerText()).trim();
  await page.locator('.recipe-info-sheet [data-act="close"]').click();
  const jpgState = await editStateOf(page, manifest.jpgOnly);
  const jpgSide = await page.evaluate(async (s) => (await window.__gxrRequest(s))?.side ?? null, manifest.jpgOnly);
  out.steps.noAdopt = {
    diptychHasMessage: diptychPanelText.includes('这张是半格双联'),
    diptychChoice: diptychState?.choice ?? null,
    diptychSideHasRecipe: Boolean(diptychSide?.recipe),
    jpgHasMessage: jpgPanelText.includes('配方需要 RAW 文件'),
    jpgChoice: jpgState?.choice ?? null,
    jpgSideHasRecipe: Boolean(jpgSide?.recipe),
  };

  // 7. Acceptance #3 (Reset in browser) on jpgOnly (starts clean, no badge).
  // Set exposure + curve point -> badge appears -> press 全部重置 -> both cleared, rev += 1, badge clears -> press again -> no-op.
  await page.locator('.editor .toolbar [data-tool="adjust"]').click();
  await page.locator('.panel.adjust input[type="range"]').evaluate((input) => {
    input.value = '1.2';
    input.dispatchEvent(new Event('input', { bubbles: true }));
    input.dispatchEvent(new Event('change', { bubbles: true }));
  });
  await page.locator('.editor .toolbar [data-tool="more"]').click();
  await page.locator('.more-sheet [data-more="curves"]').click();
  await page.waitForSelector('.curves-sheet svg.curve');
  const box = await page.locator('.curves-sheet svg.curve').boundingBox();
  if (!box) throw new Error('curve svg has no bounding box');
  await page.mouse.move(box.x + box.width * 0.5, box.y + box.height * 0.3);
  await page.mouse.down();
  await page.mouse.move(box.x + box.width * 0.5, box.y + box.height * 0.25);
  await page.mouse.up();
  await page.waitForTimeout(200);
  await page.locator('.curves-sheet [data-act="close"]').click();
  const beforeReset = await editStateOf(page, manifest.jpgOnly);
  await backToGrid(page);
  const badgeBeforeReset = await page.locator(`${cellOf(manifest.jpgOnly)} .edt`).count();

  await openPhoto(page, manifest.jpgOnly);
  await page.locator('.editor .toolbar [data-tool="adjust"]').click();
  await page.getByRole('button', { name: '全部重置', exact: true }).click();
  await page.waitForTimeout(150);
  const afterFirstReset = await editStateOf(page, manifest.jpgOnly);
  await page.getByRole('button', { name: '全部重置', exact: true }).click();
  await page.waitForTimeout(150);
  const afterSecondReset = await editStateOf(page, manifest.jpgOnly);
  await backToGrid(page);
  const badgeAfterReset = await page.locator(`${cellOf(manifest.jpgOnly)} .edt`).count();

  out.steps.reset = {
    beforeExposure: beforeReset?.knobs?.exposure_ev ?? null,
    beforeHasCurve: beforeReset?.curve !== null,
    beforeRev: beforeReset?.rev ?? null,
    badgeBeforeReset,
    afterFirstExposure: afterFirstReset?.knobs?.exposure_ev ?? null,
    afterFirstCurve: afterFirstReset?.curve ?? null,
    afterFirstRev: afterFirstReset?.rev ?? null,
    afterSecondRev: afterSecondReset?.rev ?? null,
    badgeAfterReset,
  };
  return true;
}

async function runWrongGen(page) {
  await page.addInitScript(fakeShare);
  await page.goto(url);
  await settle(page);
  await page.evaluate((binding) => {
    window.__devExport ??= {};
    window.__devExport.binding = binding;
  }, manifest.binding);
  await pickAll(page, { pack: manifest.pack, count: 1 }, manifest.wrongGenFiles);
  await page.waitForFunction(
    () => window.__gxrBook?.dat?.read?.entries?.length === 13248 && !window.__gxrBook.datBusy,
    null,
    { timeout: 120_000 },
  );
  await openPhoto(page, manifest.file1);
  await page.locator('.editor .toolbar [data-tool="recipe"]').click();
  await page.waitForSelector('.editor .recipe-head .rname');
  await page.waitForTimeout(400);
  const heading = (await page.locator('.editor .recipe-head .rname').textContent())?.trim() ?? '';
  const errorCode = await page.locator('.editor .recipe-head [data-camera-recipe-error]').getAttribute('data-camera-recipe-error').catch(() => null);
  await page.locator('.editor .recipe-head [data-camera-recipe-error]').click();
  await page.waitForSelector('.recipe-info-sheet');
  const panelText = (await page.locator('.recipe-info-sheet').innerText()).trim();
  const errorText = (await page.locator('.recipe-info-sheet [data-camera-recipe-error] .tx').textContent().catch(() => ''))?.trim() ?? '';
  await page.locator('.recipe-info-sheet [data-act="close"]').click();
  const state = await editStateOf(page, manifest.file1);
  await backToGrid(page);
  await exportFromGrid(page, [manifest.file1], '原始尺寸', '高');
  await waitPhase(page, ['running', 'ready']);
  await waitPhase(page, ['ready']);
  const exportCounts = () =>
    page.evaluate(() => {
      const b = window.__devExport.flow.current();
      return { done: b ? b.staged.length : 0, failed: b ? b.failures.size : 0 };
    });
  const counts = await exportCounts();
  const exportNoteText = (await page.locator('.pdock [data-state="camera-recipe-notes"]').innerText().catch(() => ''))?.trim() ?? '';
  await shareAll(page);
  const files = await sharedFiles(page, manifest.outDir, 'wrong-gen-');
  const fileCount = files.reduce((n, row) => n + row.files.length, 0);
  out.steps.wrongGen = {
    heading,
    hasMissingCatalogMessage: panelText.includes('请先导入拍摄时使用的相机配方文件。'),
    hasFullEditorMessage: errorText.includes('请先导入拍摄时使用的相机配方文件。 照片调整仍可保存，暂不生成拍摄配方预测。'),
    errorCode,
    choice: state?.choice ?? null,
    exportDone: counts.done,
    exportFailed: counts.failed,
    fileCount,
    exportHasNote: exportNoteText.includes('请先导入拍摄时使用的相机配方文件。'),
  };
}

async function runMissingDat(page) {
  await page.addInitScript(fakeShare);
  await page.goto(url);
  await settle(page);
  await page.evaluate((binding) => {
    window.__devExport ??= {};
    window.__devExport.binding = binding;
  }, manifest.binding);
  await pickAll(page, { count: 1 }, manifest.missingDatFiles);
  await exportFromGrid(page, [manifest.file1], '原始尺寸', '高');
  await waitPhase(page, ['running', 'ready']);
  await waitPhase(page, ['ready']);
  const counts = await page.evaluate(() => {
    const b = window.__devExport.flow.current();
    return { done: b ? b.staged.length : 0, failed: b ? b.failures.size : 0 };
  });
  const exportNoteText = (await page.locator('.pdock [data-state="camera-recipe-notes"]').innerText().catch(() => ''))?.trim() ?? '';
  await shareAll(page);
  const files = await sharedFiles(page, manifest.outDir, 'missing-dat-');
  const fileCount = files.reduce((n, row) => n + row.files.length, 0);
  out.steps.missingDat = {
    exportDone: counts.done,
    exportFailed: counts.failed,
    fileCount,
    exportHasNote: exportNoteText.includes('请先导入拍摄时使用的相机配方文件。'),
  };
}

async function runDatNoCalib(page) {
  await page.addInitScript(fakeShare);
  await page.goto(url);
  await settle(page);
  await page.evaluate((binding) => {
    window.__devExport ??= {};
    window.__devExport.binding = binding;
  }, manifest.binding);
  await pickAll(page, { count: 1 }, [...manifest.missingDatFiles, ...manifest.datFiles]);
  await page.waitForFunction(
    () => window.__gxrBook?.dat?.read?.entries?.length === 13248 && !window.__gxrBook.datBusy,
    null,
    { timeout: 120_000 },
  );
  const meta = await page.evaluate(async (s) => {
    const req = await window.__gxrRequest(s);
    return {
      hasPack: Boolean(window.__gxrBook?.pack),
      sideRecipe: req?.side?.recipe ? `${req.side.recipe.kind}:${req.side.recipe.slot}` : null,
    };
  }, manifest.file1);
  await exportFromGrid(page, [manifest.file1], '原始尺寸', '高');
  await waitPhase(page, ['running', 'ready']);
  await waitPhase(page, ['ready']);
  const result = await page.evaluate(() => {
    const b = window.__devExport.flow.current();
    return {
      done: b ? b.staged.length : 0,
      failed: b ? b.failures.size : 0,
      failures: b ? [...b.failures.values()].map((f) => ({ kind: f.kind, message: f.message })) : [],
    };
  });
  let fileCount = 0;
  if (result.done > 0) {
    await shareAll(page);
    const files = await sharedFiles(page, manifest.outDir, 'dat-nocalib-');
    fileCount = files.reduce((n, row) => n + row.files.length, 0);
  }
  out.steps.datNoCalib = {
    hasPack: meta.hasPack,
    sideRecipe: meta.sideRecipe,
    exportDone: result.done,
    exportFailed: result.failed,
    failures: result.failures,
    fileCount,
  };
}

async function runDatNoCalibRawOnly(page) {
  await page.addInitScript(fakeShare);
  await page.goto(url);
  await settle(page);
  await page.evaluate((binding) => {
    window.__devExport ??= {};
    window.__devExport.binding = binding;
  }, manifest.binding);
  await pickAll(page, { count: 1 }, [manifest.rawOnlyDng, ...manifest.datFiles]);
  await page.waitForFunction(
    () => window.__gxrBook?.dat?.read?.entries?.length === 13248 && !window.__gxrBook.datBusy,
    null,
    { timeout: 120_000 },
  );
  const meta = await page.evaluate(async (s) => {
    const req = await window.__gxrRequest(s);
    return {
      hasPack: Boolean(window.__gxrBook?.pack),
      sideRecipe: req?.side?.recipe ? `${req.side.recipe.kind}:${req.side.recipe.slot}` : null,
    };
  }, manifest.file1);
  await exportFromGrid(page, [manifest.file1], '原始尺寸', '高');
  await waitPhase(page, ['running', 'ready']);
  await waitPhase(page, ['ready']);
  const result = await page.evaluate(() => {
    const b = window.__devExport.flow.current();
    return {
      done: b ? b.staged.length : 0,
      failed: b ? b.failures.size : 0,
      failures: b ? [...b.failures.values()].map((f) => ({ kind: f.kind, message: f.message })) : [],
    };
  });
  out.steps.datNoCalibRawOnly = {
    hasPack: meta.hasPack,
    sideRecipe: meta.sideRecipe,
    exportDone: result.done,
    exportFailed: result.failed,
    failures: result.failures,
  };
}

async function runDatPartialCalib(page) {
  await page.addInitScript(fakeShare);
  await page.goto(url);
  await settle(page);
  await page.evaluate((binding) => {
    window.__devExport ??= {};
    window.__devExport.binding = binding;
  }, manifest.binding);
  await pickAll(page, { pack: manifest.partialPack, count: 1 }, [...manifest.missingDatFiles, ...manifest.datFiles]);
  await page.waitForFunction(
    () => window.__gxrBook?.dat?.read?.entries?.length === 13248 && !window.__gxrBook.datBusy,
    null,
    { timeout: 120_000 },
  );
  const meta = await page.evaluate(async (s) => {
    const req = await window.__gxrRequest(s);
    return {
      hasPack: Boolean(window.__gxrBook?.pack),
      sideRecipe: req?.side?.recipe ? `${req.side.recipe.kind}:${req.side.recipe.slot}` : null,
    };
  }, manifest.file1);
  await exportFromGrid(page, [manifest.file1], '原始尺寸', '高');
  await waitPhase(page, ['running', 'ready']);
  await waitPhase(page, ['ready']);
  const result = await page.evaluate(() => {
    const b = window.__devExport.flow.current();
    return {
      done: b ? b.staged.length : 0,
      failed: b ? b.failures.size : 0,
      failures: b ? [...b.failures.values()].map((f) => ({ kind: f.kind, message: f.message })) : [],
    };
  });
  let fileCount = 0;
  if (result.done > 0) {
    await shareAll(page);
    const files = await sharedFiles(page, manifest.outDir, 'dat-partial-calib-');
    fileCount = files.reduce((n, row) => n + row.files.length, 0);
  }
  out.steps.datPartialCalib = {
    hasPack: meta.hasPack,
    sideRecipe: meta.sideRecipe,
    exportDone: result.done,
    exportFailed: result.failed,
    failures: result.failures,
    fileCount,
  };
}

async function runRawOnlyOpenBeforeDat(page) {
  await page.addInitScript(fakeShare);
  await page.goto(url);
  await settle(page);
  await page.evaluate((binding) => {
    window.__devExport ??= {};
    window.__devExport.binding = binding;
  }, manifest.binding);
  await pickAll(page, { pack: manifest.pack, count: 1 }, [manifest.rawOnlyDng]);
  // Open editor BEFORE importing camera data so describeDng runs first.
  await openPhoto(page, manifest.file1);
  await backToGrid(page);
  // Import g250 camera data on the grid, then reopen the RAW-only photo.
  await page.locator('input.filepick:not(.calibpick)').setInputFiles(manifest.datFiles);
  await page.waitForFunction(
    () => window.__gxrBook?.dat?.read?.entries?.length === 13248 && !window.__gxrBook.datBusy,
    null,
    { timeout: 120_000 },
  );
  await openPhoto(page, manifest.file1);
  await page.locator('.editor .toolbar [data-tool="recipe"]').click();
  await page.waitForSelector('.editor .recipe-head .rname');
  await waitStageRecipeReady(page);
  const heading = (await page.locator('.editor .recipe-head .rname').textContent())?.trim() ?? '';
  const sideRecipe = await page.evaluate(async (s) => {
    const req = await window.__gxrRequest(s);
    return req?.side?.recipe ? `${req.side.recipe.kind}:${req.side.recipe.slot}` : null;
  }, manifest.file1);
  out.steps.rawOnlyBeforeDat = { heading, sideRecipe };
}

async function withFreshPage(fn) {
  const context = await browser.newContext({ viewport: { width: 390, height: 844 } });
  const page = await context.newPage();
  page.on('pageerror', (error) => out.pageErrors.push(error.message));
  try {
    return await fn(page);
  } finally {
    await context.close();
  }
}

try {
  const adoptedOk = await withFreshPage(runMain);
  if (adoptedOk) {
    await withFreshPage(runWrongGen);
    await withFreshPage(runMissingDat);
    await withFreshPage(runDatNoCalib);
    await withFreshPage(runDatNoCalibRawOnly);
    await withFreshPage(runDatPartialCalib);
    await withFreshPage(runRawOnlyOpenBeforeDat);
  }
} catch (err) {
  out.error = line(err);
} finally {
  writeFileSync(manifest.out, JSON.stringify(out));
  await browser.close();
}
console.log('done');
