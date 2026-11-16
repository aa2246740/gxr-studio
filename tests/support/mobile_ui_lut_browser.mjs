// UI-M4-LUT 第 7 项：在无头 Chrome 里完整驱动 LUT 导入、色彩空间选择、3 槽短命 Worker 预览、
// 四对 RAW+JPG（A/B/C/D）+ JPG-only 的 SDR 与 HDR 导出（原始尺寸/高 与 小/低），
// 以及重命名和删除使用中 LUT 后的错误语义。
// 照片数据与 EXIF 绝不打到 stdout；文件写到 manifest.outDir，结构化结果写进 manifest.out。

import { pathToFileURL } from 'node:url';
import { readFileSync, writeFileSync } from 'node:fs';
import { batchFacts, cellOf, exportFromGrid, shareAll, sharedFiles, waitPhase } from './mobile_ui_export_parity.mjs';

const [runtime, url, manifestPath] = process.argv.slice(2);
const manifest = JSON.parse(readFileSync(manifestPath, 'utf8'));
const { launchPinnedChromium, verifyPinnedRuntime } = await import(pathToFileURL(runtime).href);
verifyPinnedRuntime();

const browser = await launchPinnedChromium({
  args: ['--enable-unsafe-webgpu', '--enable-unsafe-swiftshader', '--use-angle=swiftshader', '--use-webgpu-adapter=swiftshader'],
});

const out = { pageErrors: [], steps: {} };

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

async function openPhoto(page, stem) {
  await page.locator(cellOf(stem)).evaluate((node) => node.click());
  await page.waitForFunction((want) => document.querySelector('.editor')?.dataset.photo === want, stem, { timeout: 120_000 });
}

async function backToGrid(page) {
  await page.getByRole('button', { name: '照片', exact: true }).click();
  await page.waitForSelector('[data-screen="grid"]');
}

async function waitLutReady(page, cubeId) {
  await page.waitForFunction(
    (id) => {
      const gpu = window.__gxrLutGpu;
      const book = window.__gxrLutBook;
      if (!gpu || !book) return false;
      const stats = gpu.stats();
      if (stats.alive !== 0 || stats.pending !== 0) return false;
      const token = book.renderToken(id);
      return document.querySelector('.app')?.dataset.lutToken === token;
    },
    cubeId,
    { timeout: 120_000 },
  );
  return page.evaluate(() => window.__gxrLutGpu.stats());
}

async function longPress(page, selector) {
  await page.locator(selector).scrollIntoViewIfNeeded();
  await page.locator(selector).evaluate((node) => {
    const rect = node.getBoundingClientRect();
    node.dispatchEvent(
      new PointerEvent('pointerdown', {
        bubbles: true,
        button: 0,
        clientX: rect.left + rect.width / 2,
        clientY: rect.top + rect.height / 2,
      }),
    );
  });
  await page.waitForSelector('[data-state="lut-actions"]', { timeout: 10_000 });
  await page.locator(selector).evaluate((node) => {
    node.dispatchEvent(new PointerEvent('pointerup', { bubbles: true, button: 0 }));
  });
}

try {
  const page = await browser.newPage({ viewport: { width: 390, height: 844 } });
  page.on('pageerror', (error) => out.pageErrors.push(error.message));
  await page.addInitScript(fakeShare);
  await page.goto(url);
  await settle(page);

  await page.evaluate(({ binding }) => {
    window.__devExport.binding = binding;
    window.__devExport.chunkFiles = 20;
    localStorage.removeItem('gxr.mobile.export_hdr');
  }, manifest);

  // 1. 导入校准数据、样本照片与 GXRCE.DAT
  await page.locator('input.calibpick').setInputFiles(manifest.pack);
  await page.waitForSelector('.calib[data-calib="ready"]', { timeout: 60_000 });
  await page.locator('input.filepick:not(.calibpick)').setInputFiles(manifest.files);
  await page.waitForFunction(
    (n) => {
      const sub = document.querySelector('.nav small');
      return sub && !sub.textContent.includes('正在读取') && document.querySelectorAll('.cell').length >= n && window.__gxrBook?.dat;
    },
    manifest.count,
    { timeout: 120_000 },
  );

  // 2. 打开 Pair A → 切到 LUT 页签 → 导入 4 个合成 LUT + 1 个非 .cube 忽略文件 → 重复导入检查
  const { a, b, c, d, jpgOnly } = manifest.stems;
  const ids = {
    disp33: manifest.luts.disp33.cubeId,
    shaper: manifest.luts.shaper.cubeId,
    undeclared: manifest.luts.undeclared.cubeId,
    applelog: manifest.luts.applelog.cubeId,
  };

  await openPhoto(page, a);
  await page.locator('.tabs button[data-tab="lut"]').click();
  await page.waitForSelector('.panel.lut .lut-foot');
  const footnote = await page.locator('.panel.lut .lut-foot').innerText();

  await page.locator('input.lutpick').setInputFiles([
    manifest.luts.disp33.path,
    manifest.luts.shaper.path,
    manifest.luts.undeclared.path,
    manifest.luts.applelog.path,
    manifest.ignoreFile,
  ]);
  await page.waitForFunction(
    (wantIds) => wantIds.every((id) => Boolean(window.__gxrLutBook?.get(id))),
    Object.values(ids),
    { timeout: 120_000 },
  );
  const importStatus = await page.locator('.panel.lut [data-state="lut-status"]').innerText();

  await page.locator('input.lutpick').setInputFiles([manifest.luts.disp33.path]);
  await page.waitForFunction(
    () => document.querySelector('.panel.lut [data-state="lut-status"]')?.textContent === '已经导入过',
    null,
    { timeout: 30_000 },
  );
  const dupStatus = await page.locator('.panel.lut [data-state="lut-status"]').innerText();

  // 3. 未声明的 LUT 显示「请选择色彩空间」，点开空间选择器核对分组/12个空间后手选 sRGB
  const undeclaredCard = page.locator(`.panel.lut .rc[data-lut="${ids.undeclared}"]`);
  const undeclaredLocked = await undeclaredCard.evaluate((node) => node.classList.contains('lockd'));
  const undeclaredSub = await undeclaredCard.locator('.sub').innerText();
  await undeclaredCard.click();
  await page.waitForSelector('.lut-space-sheet[data-state="lut-space"]', { timeout: 15_000 });
  const spaceSheet = await page.evaluate(() => {
    const sheet = document.querySelector('.lut-space-sheet');
    return {
      groups: [...sheet.querySelectorAll('.lut-space-group')].map((n) => n.textContent),
      spaces: [...sheet.querySelectorAll('.lut-space-opt')].map((n) => n.getAttribute('data-space')),
      badge: sheet.querySelector('.tag')?.textContent ?? '',
      note: sheet.querySelector('.lut-sheet-note')?.textContent ?? '',
    };
  });
  await page.locator('.lut-space-sheet [data-space="srgb"]').click();
  await page.waitForFunction(
    (id) => !document.querySelector('.lut-space-sheet') && window.__gxrLutBook?.get(id)?.input_space === 'srgb',
    ids.undeclared,
    { timeout: 15_000 },
  );
  const spaceRowAfterPick = await page.locator('.panel.lut [data-act="lut-space"] small').innerText();

  // 4. 连续切换 3 张 Display LUT，核对短命 table Worker 每次销毁、引擎槽 <= 3，最后停在 disp33
  const switchStats = [];
  for (const id of [ids.disp33, ids.shaper, ids.undeclared, ids.disp33]) {
    const card = page.locator(`.panel.lut .rc[data-lut="${id}"]`);
    await card.scrollIntoViewIfNeeded();
    await card.click();
    switchStats.push({ id, stats: await waitLutReady(page, id) });
  }
  const pairAPill = await page.locator('.stage .lbl').innerText();
  const pairADot = await page.locator('.tabs button[data-tab="lut"].dot').count();
  await backToGrid(page);

  out.steps.importAndSpaces = {
    footnote,
    importStatus,
    dupStatus,
    undeclaredLocked,
    undeclaredSub,
    spaceSheet,
    spaceRowAfterPick,
    switchStats,
    pairAPill,
    pairADot,
  };

  // 5. Pair B: 配方 builtin:2 + Display 1D+3D LUT (shaper)
  await openPhoto(page, b);
  await page.waitForFunction(() => document.querySelector('.editor .strip[data-recipes]')?.dataset.state === 'calibrated', null, { timeout: 120_000 });
  const bCard = page.locator('.editor .strip[data-recipes] .rc[data-card="builtin:2"]');
  await bCard.scrollIntoViewIfNeeded();
  await bCard.click();
  await page.waitForFunction(
    () => {
      const app = document.querySelector('.app');
      const card = document.querySelector('.editor .strip[data-recipes] .rc.on')?.dataset.card;
      return card === 'builtin:2' && app?.dataset.recipeKey && app.dataset.recipeKey !== 'neutral';
    },
    null,
    { timeout: 60_000 },
  );
  await page.locator('.tabs button[data-tab="lut"]').click();
  await page.locator(`.panel.lut .rc[data-lut="${ids.shaper}"]`).click();
  await waitLutReady(page, ids.shaper);
  const pairBPill = await page.locator('.stage .lbl').innerText();
  await backToGrid(page);

  // 6. Pair C: Apple Log LUT
  await openPhoto(page, c);
  await page.locator('.tabs button[data-tab="lut"]').click();
  await page.locator(`.panel.lut .rc[data-lut="${ids.applelog}"]`).click();
  await waitLutReady(page, ids.applelog);
  const pairCPill = await page.locator('.stage .lbl').innerText();
  await backToGrid(page);

  // 7. JPG-only: 先点 Log LUT（拒绝并给桌面提示），再点 Display LUT undeclared->sRGB（允许）
  await openPhoto(page, jpgOnly);
  await page.locator('.tabs button[data-tab="lut"]').click();
  const logCard = page.locator(`.panel.lut .rc[data-lut="${ids.applelog}"]`);
  const jpgLogLocked = await logCard.evaluate((node) => node.classList.contains('lockd'));
  const jpgLogSub = await logCard.locator('.sub').innerText();
  await logCard.click();
  await page.waitForSelector('.panel.lut [data-state="lut-status"]', { timeout: 10_000 });
  const jpgLogRefused = await page.locator('.panel.lut [data-state="lut-status"]').innerText();
  const jpgSelectedAfterLog = await page.evaluate((stem) => window.__gxrEdits().find((r) => r.stem === stem)?.lut ?? null, jpgOnly);

  await page.locator(`.panel.lut .rc[data-lut="${ids.undeclared}"]`).click();
  await waitLutReady(page, ids.undeclared);
  const jpgDispDot = await page.locator('.tabs button[data-tab="lut"].dot').count();
  await backToGrid(page);

  // 8. Pair D: Apple Log LUT（之后开 HDR 导出）
  await openPhoto(page, d);
  await page.locator('.tabs button[data-tab="lut"]').click();
  await page.locator(`.panel.lut .rc[data-lut="${ids.applelog}"]`).click();
  await waitLutReady(page, ids.applelog);
  await backToGrid(page);

  const previewMem = await page.evaluate(() => window.__gxrLutGpu.stats());
  out.steps.preview = {
    pairBPill,
    pairCPill,
    jpgLogLocked,
    jpgLogSub,
    jpgLogRefused,
    jpgSelectedAfterLog,
    jpgDispDot,
    previewMem,
  };

  // 9. 导出批次：SDR [A, B, C, jpgOnly]（原始尺寸/高 与 小/低）+ HDR [D]（原始尺寸/高 与 小/低）
  const sdrOrder = [a, b, c, jpgOnly];
  const hdrOrder = [d];

  await exportFromGrid(page, sdrOrder, '原始尺寸', '高');
  const sdrFullReady = await waitPhase(page, ['ready']);
  const sdrFullFacts = await batchFacts(page);
  await shareAll(page);
  const sdrFullFiles = await sharedFiles(page, manifest.outDir, 'full-');
  await page.locator('.pdock [data-act="done"]').click();
  await page.waitForFunction(() => !document.querySelector('.pdock'), null, { timeout: 10_000 });

  await page.evaluate(() => window.__devExport.core.stop());
  let callIndex = await page.evaluate(() => window.__shareLog.calls.length);
  await exportFromGrid(page, sdrOrder, '小', '低');
  const sdrSmallReady = await waitPhase(page, ['ready']);
  const sdrSmallFacts = await batchFacts(page);
  await shareAll(page);
  const sdrSmallFiles = await sharedFiles(page, manifest.outDir, 'small-', callIndex);
  await page.locator('.pdock [data-act="done"]').click();
  await page.waitForFunction(() => !document.querySelector('.pdock'), null, { timeout: 10_000 });

  await page.evaluate(() => window.__devExport.core.stop());
  callIndex = await page.evaluate(() => window.__shareLog.calls.length);
  await exportFromGrid(page, hdrOrder, '原始尺寸', '高', async () => {
    await page.locator('.ms.export input.switch').click();
  });
  const hdrFullReady = await waitPhase(page, ['ready']);
  const hdrFullFacts = await batchFacts(page);
  await shareAll(page);
  const hdrFullFiles = await sharedFiles(page, manifest.outDir, 'hdr-full-', callIndex);
  await page.locator('.pdock [data-act="done"]').click();
  await page.waitForFunction(() => !document.querySelector('.pdock'), null, { timeout: 10_000 });

  await page.evaluate(() => window.__devExport.core.stop());
  callIndex = await page.evaluate(() => window.__shareLog.calls.length);
  await exportFromGrid(page, hdrOrder, '小', '低');
  const hdrSmallReady = await waitPhase(page, ['ready']);
  const hdrSmallFacts = await batchFacts(page);
  await shareAll(page);
  const hdrSmallFiles = await sharedFiles(page, manifest.outDir, 'hdr-small-', callIndex);
  await page.locator('.pdock [data-act="done"]').click();
  await page.waitForFunction(() => !document.querySelector('.pdock'), null, { timeout: 10_000 });

  out.steps.exports = {
    sdrFull: { ready: sdrFullReady, facts: sdrFullFacts, files: sdrFullFiles },
    sdrSmall: { ready: sdrSmallReady, facts: sdrSmallFacts, files: sdrSmallFiles },
    hdrFull: { ready: hdrFullReady, facts: hdrFullFacts, files: hdrFullFiles },
    hdrSmall: { ready: hdrSmallReady, facts: hdrSmallFacts, files: hdrSmallFiles },
  };

  // 10. 长按重命名与删除使用中的 LUT → 「LUT 已删除」卡片 + 导出按桌面文案拒绝
  await openPhoto(page, a);
  await page.locator('.tabs button[data-tab="lut"]').click();

  await longPress(page, `.panel.lut .rc[data-lut="${ids.shaper}"]`);
  await page.locator('[data-state="lut-actions"] [data-act="lut-rename"]').click();
  await page.waitForSelector('[data-state="lut-rename"]');
  await page.locator('[data-state="lut-rename"] .lut-name-input').fill('胶片颗粒 LUT');
  await page.locator('[data-state="lut-rename"] [data-act="lut-rename-save"]').click();
  await page.waitForFunction(
    (id) => !document.querySelector('[data-state="lut-rename"]') && window.__gxrLutBook?.get(id)?.name === '胶片颗粒 LUT',
    ids.shaper,
    { timeout: 15_000 },
  );
  const renamedName = await page.locator(`.panel.lut .rc[data-lut="${ids.shaper}"] .nm`).innerText();

  await longPress(page, `.panel.lut .rc[data-lut="${ids.disp33}"]`);
  await page.locator('[data-state="lut-actions"] [data-act="lut-delete"]').click();
  await page.waitForSelector('[data-state="lut-delete"]');
  await page.locator('[data-state="lut-delete"] [data-act="lut-delete-confirm"]').click();
  await page.waitForFunction(
    (id) => !document.querySelector('[data-state="lut-delete"]') && !window.__gxrLutBook?.get(id),
    ids.disp33,
    { timeout: 15_000 },
  );
  const deletedCardText = await page.locator('.panel.lut .rc[data-lut="deleted"]').innerText();
  const deletedBannerText = await page.locator('.panel.lut [data-state="lut-deleted"]').innerText();

  await page.locator('.actbar [data-act="export"]').click();
  await page.waitForSelector('.ms.export');
  await page.locator('.ms.export .go').click();
  const deletedExportReady = await waitPhase(page, ['ready']);
  const deletedFailures = await page.evaluate(() => {
    const b = window.__devExport.flow.current();
    return {
      staged: b.staged.length,
      failures: [...b.failures.values()].map((f) => ({ kind: f.kind, message: f.message })),
    };
  });

  out.steps.manageAndDelete = {
    renamedName,
    deletedCardText,
    deletedBannerText,
    deletedExportReady,
    deletedFailures,
  };

  // 11. post-r1 回归：P3-01 非 UTF-8 .cube 中文提示、P1-01 保留 output_space、P2-03 删除事务中止恢复、P2-04 清空销毁 GPU LUT
  // 关闭导出 dock 并仍在 Pair A LUT 页签继续
  const doneBtn = page.locator('.pdock [data-act="done"]');
  if (await doneBtn.count()) {
    await doneBtn.click();
    await page.waitForFunction(() => !document.querySelector('.pdock'), null, { timeout: 10_000 });
  }

  // P3-01：非 UTF-8 .cube 报桌面 LUT_ERRORS.cube_unreadable（不透出底层 UTF-8/Rust 文案）
  await page.locator('input.lutpick').setInputFiles([manifest.badUtf8File]);
  await page.waitForFunction(
    () => document.querySelector('.panel.lut [data-state="lut-status"]')?.textContent?.includes('无法读取 LUT。请改用 .cube 文件。'),
    null,
    { timeout: 15_000 },
  );
  const badUtf8Status = await page.locator('.panel.lut [data-state="lut-status"]').innerText();

  // P1-01：导入 # Input: sRGB + # Output: Rec.709，改输入范围等参数后 output_space 仍保留 rec709；UI 也显示输出色彩空间行
  const rec709Id = manifest.rec709Out.cubeId;
  await page.locator('input.lutpick').setInputFiles([manifest.rec709Out.path]);
  await page.waitForFunction((id) => Boolean(window.__gxrLutBook?.get(id)), rec709Id, { timeout: 15_000 });
  const recCard = page.locator(`.panel.lut .rc[data-lut="${rec709Id}"]`);
  await recCard.scrollIntoViewIfNeeded();
  await recCard.click();
  await waitLutReady(page, rec709Id);
  await page.locator('.panel.lut [data-act="lut-space"]').click();
  await page.waitForSelector('.lut-space-sheet[data-state="lut-space"]', { timeout: 10_000 });
  const outputSpaceChoices = await page.evaluate(() =>
    [...document.querySelectorAll('.lut-adv-row[data-field="output_space"] button')].map((b) => ({
      value: b.getAttribute('data-value'),
      on: b.classList.contains('on'),
      text: b.textContent,
    })),
  );
  await page.locator('.lut-adv-row[data-field="input_range"] [data-value="legal"]').click();
  await page.waitForFunction(
    (id) => window.__gxrLutBook?.get(id)?.input_range === 'legal',
    rec709Id,
    { timeout: 10_000 },
  );
  const outputSpaceAfterEdit = await page.evaluate((id) => window.__gxrLutBook?.get(id)?.output_space ?? null, rec709Id);
  await page.locator('.lut-space-sheet .hd .nb').click();
  await page.waitForFunction(() => !document.querySelector('.lut-space-sheet'), null, { timeout: 10_000 });

  // 确保已有多个 LUT 上到 GPU 纹理（为 P2-04 做准备）
  for (const id of [ids.shaper, ids.undeclared, rec709Id]) {
    const cBtn = page.locator(`.panel.lut .rc[data-lut="${id}"]`);
    await cBtn.scrollIntoViewIfNeeded();
    await cBtn.click();
    await waitLutReady(page, id);
  }
  const gpuBeforeClear = await page.evaluate(() => ({
    engine: window.__gxrEngine?.stats() ?? null,
    lutGpu: window.__gxrLutGpu?.stats() ?? null,
  }));

  // P2-03：真实 IndexedDB tx.abort() 触发删除失败时，按钮恢复可点、显示单行错误且 LUT 仍在本地库
  await longPress(page, `.panel.lut .rc[data-lut="${ids.shaper}"]`);
  await page.locator('[data-state="lut-actions"] [data-act="lut-delete"]').click();
  await page.waitForSelector('[data-state="lut-delete"]', { timeout: 10_000 });
  await page.evaluate(() => {
    const book = window.__gxrLutBook;
    book.__origRemove = book.io.remove;
    book.io.remove = () =>
      new Promise((_resolve, reject) => {
        const req = indexedDB.open('gxr-mobile');
        req.onsuccess = () => {
          const db = req.result;
          const tx = db.transaction(['lut_records'], 'readwrite');
          tx.onabort = () => {
            db.close();
            reject(tx.error ?? new DOMException('The transaction was aborted.', 'AbortError'));
          };
          tx.abort();
        };
        req.onerror = () => reject(req.error ?? new DOMException('AbortError', 'AbortError'));
      });
  });
  await page.locator('[data-state="lut-delete"] [data-act="lut-delete-confirm"]').click();
  await page.waitForFunction(
    () => {
      const err = document.querySelector('[data-state="lut-delete"] .bad');
      const go = document.querySelector('[data-state="lut-delete"] [data-act="lut-delete-confirm"]');
      return err && !err.hidden && err.textContent && go && !go.disabled;
    },
    null,
    { timeout: 15_000 },
  );
  const abortRecovery = await page.evaluate((id) => {
    const dlg = document.querySelector('[data-state="lut-delete"]');
    const errs = [...dlg.querySelectorAll('.bad')].filter((n) => !n.hidden).map((n) => n.textContent);
    const go = dlg.querySelector('[data-act="lut-delete-confirm"]');
    const stillPresent = Boolean(window.__gxrLutBook?.get(id));
    window.__gxrLutBook.io.remove = window.__gxrLutBook.__origRemove;
    delete window.__gxrLutBook.__origRemove;
    return { errs, goDisabled: Boolean(go?.disabled), stillPresent };
  }, ids.shaper);
  await page.locator('[data-state="lut-delete"] .acts button', { hasText: '取消' }).click();
  await page.waitForFunction(() => !document.querySelector('[data-state="lut-delete"]'), null, { timeout: 10_000 });

  // P2-04：回到网格点击「清空照片，重新选」→ Engine LUT 纹理和 LutGpu 队列与统计全清零
  await backToGrid(page);
  await page.getByRole('button', { name: '更多' }).click();
  await page.waitForSelector('.ms');
  await page.getByText('清空照片，重新选').click();
  await page.waitForSelector('[data-screen="start"]', { timeout: 15_000 });
  const gpuAfterClear = await page.evaluate(() => ({
    engine: window.__gxrEngine?.stats() ?? null,
    lutGpu: window.__gxrLutGpu?.stats() ?? null,
  }));

  out.steps.postR1Regressions = {
    badUtf8Status,
    outputSpaceChoices,
    outputSpaceAfterEdit,
    gpuBeforeClear,
    abortRecovery,
    gpuAfterClear,
  };

  await page.close();
} catch (err) {
  out.error = err instanceof Error ? `${err.message}\n${err.stack ?? ''}` : String(err);
} finally {
  writeFileSync(manifest.out, JSON.stringify(out));
  await browser.close();
}
console.log('done');
if (out.error) process.exit(1);
