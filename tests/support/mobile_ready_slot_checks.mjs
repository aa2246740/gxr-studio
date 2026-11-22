// U5 phone-ready-slot 浏览器验收检查模块：
// - 全工具（调整 / 配方 / LUT / 裁剪 / 时间戳）elementFromPoint 命中与 >=44pt 触控区；
// - P2-1：活动导出态下 LUT「导入 LUT」「色彩空间」「状态提示」与配方状态瓷砖全可达且可打开对应弹层；
// - P2-2：「详情」工作表在连续进度重画中保焦，Escape / 遮罩 / 收起关闭后还焦给重建后的 .pdock [data-act="toggle-details"]；
// - P3-1：键盘操作曝光滑块与裁剪拉直尺后数值胶囊按时淡出隐藏；
// - P3-4：已保存部分成片后仅剩失败时，详情汇总显示「1 张没做成」而非「1 张都没做成」。

export async function probeVisible(page, selector, containerSelector) {
  return page.evaluate(
    ({ sel, contSel }) => {
      const cont = document.querySelector(contSel);
      const cr = cont ? cont.getBoundingClientRect() : { left: 0, right: window.innerWidth, top: 0, bottom: window.innerHeight };
      const nodes = [...document.querySelectorAll(sel)];
      let tested = 0;
      const bad = [];
      let minH = 999;
      let minW = 999;
      for (const el of nodes) {
        const r = el.getBoundingClientRect();
        if (r.width <= 0 || r.height <= 0) continue;
        const cx = r.left + r.width / 2;
        const cy = r.top + r.height / 2;
        if (cx < cr.left + 2 || cx > cr.right - 2 || cy < 0 || cy > window.innerHeight) continue;
        tested += 1;
        minH = Math.min(minH, r.height);
        minW = Math.min(minW, r.width);
        const hit = document.elementFromPoint(cx, cy);
        const ok = Boolean(hit && (hit === el || el.contains(hit) || hit.contains(el)) && !hit.closest('.pdock'));
        if (!ok) bad.push({ tag: el.tagName, cls: el.className, hitCls: hit?.className ?? 'null' });
      }
      return {
        tested,
        bad,
        minH: tested ? Math.round(minH * 10) / 10 : 0,
        minW: tested ? Math.round(minW * 10) / 10 : 0,
      };
    },
    { sel: selector, contSel: containerSelector },
  );
}

/** 在当前活动导出态下逐工具做 elementFromPoint 命中测试（调整 / 配方 / LUT / 裁剪 / 时间戳）。 */
export async function auditDeckHitTests(page, tag, check, frame, checkHeights = false) {
  const result = {};

  // 1) Adjust
  await page.locator('.editor .toolbar [data-tool="adjust"]').click();
  await frame(page, 2);
  const adjSlider = await probeVisible(page, '.editor .panel.adjust input[type="range"]', '.editor .band-body');
  const adjChips = await probeVisible(page, '.editor .panel.adjust .chips .chip', '.editor .panel.adjust .chips');
  check(adjSlider.tested >= 1 && adjSlider.bad.length === 0, `${tag} adjust slider hit blocked: ${JSON.stringify(adjSlider)}`);
  check(adjChips.tested >= 4 && adjChips.bad.length === 0, `${tag} adjust chips hit blocked: ${JSON.stringify(adjChips)}`);
  if (checkHeights) {
    check(adjSlider.minH >= 43.5, `${tag} adjust slider hit height ${adjSlider.minH} < 44`);
    check(adjChips.minH >= 43.5, `${tag} adjust chip hit height ${adjChips.minH} < 44`);
  }
  result.adjust = { slider: adjSlider, chips: adjChips };

  // 2) Recipe (cards + pinned filter tile)
  await page.locator('.editor .toolbar [data-tool="recipe"]').click();
  await frame(page, 2);
  const recFilter = await probeVisible(page, '.editor .panel.recipe .recipe-filter-pin [data-act="recipe-filter"]', '.editor .band-body');
  const recCards = await probeVisible(page, '.editor .panel.recipe .strip .rc', '.editor .panel.recipe .strip');
  check(recFilter.tested === 1 && recFilter.bad.length === 0, `${tag} recipe filter tile blocked: ${JSON.stringify(recFilter)}`);
  check(recCards.tested >= 3 && recCards.bad.length === 0, `${tag} recipe cards blocked: ${JSON.stringify(recCards)}`);
  if (checkHeights) {
    check(recFilter.minH >= 43.5, `${tag} recipe filter tile height ${recFilter.minH} < 44`);
    check(recCards.minH >= 43.5, `${tag} recipe card height ${recCards.minH} < 44`);
  }
  result.recipe = { filter: recFilter, cards: recCards };

  // 2b) LUT deck homes (P2-1 / polish-2): compact 44pt menu tile + popover exposing 导入 LUT & 色彩空间 (>= 44pt) in every active export state
  await page.locator('.editor .panel.recipe [data-mode="lut"]').click();
  await frame(page, 2);
  const lutMenu = await probeVisible(page, '.editor .band [data-act="lut-menu"]', '.editor .band-body');
  check(lutMenu.tested === 1 && lutMenu.bad.length === 0, `${tag} lut-menu missing/blocked: ${JSON.stringify(lutMenu)}`);
  check(lutMenu.minH >= 43.5 && lutMenu.minW >= 43.5, `${tag} lut-menu size < 44pt: ${JSON.stringify(lutMenu)}`);
  await page.locator('.editor .band [data-act="lut-menu"]').click();
  await page.waitForSelector('.lut-menu-pop:popover-open', { timeout: 5_000 });
  await frame(page, 2);
  const lutImport = await probeVisible(page, '.lut-menu-pop:popover-open [data-act="lut-menu-import"]', '.lut-menu-pop:popover-open');
  const lutSpace = await probeVisible(page, '.lut-menu-pop:popover-open [data-act="lut-menu-space"]', '.lut-menu-pop:popover-open');
  check(lutImport.tested === 1 && lutImport.bad.length === 0, `${tag} lut-import missing/blocked: ${JSON.stringify(lutImport)}`);
  check(lutSpace.tested === 1 && lutSpace.bad.length === 0, `${tag} lut-space missing/blocked: ${JSON.stringify(lutSpace)}`);
  check(lutImport.minH >= 43.5 && lutImport.minW >= 43.5, `${tag} lut-import size < 44pt: ${JSON.stringify(lutImport)}`);
  check(lutSpace.minH >= 43.5 && lutSpace.minW >= 43.5, `${tag} lut-space size < 44pt: ${JSON.stringify(lutSpace)}`);
  await page.keyboard.press('Escape');
  await page.waitForFunction(() => !document.querySelector('.lut-menu-pop:popover-open'), null, { timeout: 5_000 });
  await frame(page, 1);
  result.lut = { menu: lutMenu, import: lutImport, space: lutSpace };
  await page.locator('.editor .panel.lut [data-mode="recipe"]').click();
  await frame(page, 2);

  // 3) Crop (dial + ratio chips)
  await page.locator('.editor .toolbar [data-tool="crop"]').click();
  await frame(page, 2);
  const cropRuler = await probeVisible(page, '.editor .panel.crop-panel .ruler', '.editor .band-body');
  const cropChips = await probeVisible(page, '.editor .panel.crop-panel .chips .chip', '.editor .panel.crop-panel .chips');
  check(cropRuler.tested === 1 && cropRuler.bad.length === 0, `${tag} crop ruler blocked: ${JSON.stringify(cropRuler)}`);
  check(cropChips.tested >= 3 && cropChips.bad.length === 0, `${tag} crop ratio chips blocked: ${JSON.stringify(cropChips)}`);
  if (checkHeights) {
    check(cropRuler.minH >= 43.5, `${tag} crop ruler height ${cropRuler.minH} < 44`);
    check(cropChips.minH >= 43.5, `${tag} crop chip height ${cropChips.minH} < 44`);
  }
  result.crop = { ruler: cropRuler, chips: cropChips };

  // 4) Stamp (switch + detail button)
  await page.locator('.editor .toolbar [data-tool="more"]').click();
  await frame(page, 2);
  await page.locator('.more-sheet [data-more="stamp"]').click();
  await frame(page, 2);
  const stampBtns = await probeVisible(page, '.editor .stamp-band .stamp-toggle-hit, .editor .stamp-band [data-act="stamp-sheet"]', '.editor .band-body');
  check(stampBtns.tested >= 2 && stampBtns.bad.length === 0, `${tag} stamp controls blocked: ${JSON.stringify(stampBtns)}`);
  if (checkHeights) check(stampBtns.minH >= 43.5, `${tag} stamp control height ${stampBtns.minH} < 44`);
  result.stamp = stampBtns;

  // Back to adjust for clean baseline
  await page.locator('.editor .toolbar [data-tool="adjust"]').click();
  await frame(page, 2);
  return result;
}

/** P2-1: 活动导出态下验证 LUT「色彩空间」可开表、「导入 LUT」可点且状态提示在 84pt 控制区内可见不遮挡。 */
export async function verifyLutDeckActions(page, lutPath, tag, check, frame) {
  await page.locator('.editor .toolbar [data-tool="recipe"]').click();
  await frame(page, 2);
  await page.locator('.editor .panel.recipe [data-mode="lut"]').click();
  await frame(page, 2);

  // Open compact LUT menu -> tap 色彩空间 -> opens lut-space-sheet during active export
  await page.locator('.editor .panel.lut [data-act="lut-menu"]').click();
  await page.waitForSelector('.lut-menu-pop:popover-open', { timeout: 5_000 });
  await page.locator('.lut-menu-pop:popover-open [data-act="lut-menu-space"]').click();
  await page.waitForSelector('.lut-space-sheet[data-state="lut-space"]', { timeout: 10_000 });
  await page.locator('.lut-space-sheet .hd .nb').click();
  await page.waitForFunction(() => !document.querySelector('.lut-space-sheet'), null, { timeout: 10_000 });

  // Re-import duplicate LUT -> status notice must appear visibly inside .panel.lut during active export
  await page.locator('input.lutpick').setInputFiles([lutPath]);
  await page.waitForFunction(
    () => document.querySelector('.editor .panel.lut [data-state="lut-status"]')?.textContent?.includes('已经导入过'),
    null,
    { timeout: 15_000 },
  );
  const statusBox = await page.evaluate(() => {
    const el = document.querySelector('.editor .panel.lut [data-state="lut-status"]');
    if (!el) return null;
    const r = el.getBoundingClientRect();
    return { text: el.textContent?.trim() ?? '', h: Math.round(r.height * 10) / 10, pe: getComputedStyle(el).pointerEvents };
  });
  check(Boolean(statusBox && statusBox.h > 0 && statusBox.pe === 'none'), `${tag} lut-status invisible or blocking: ${JSON.stringify(statusBox)}`);

  // Ensure compact menu tile & its lut-import / lut-space items remain hit-testable even while lut-status banner is shown
  const menuHit = await probeVisible(page, '.editor .band [data-act="lut-menu"]', '.editor .band-body');
  check(menuHit.tested === 1 && menuHit.bad.length === 0, `${tag} lut-menu blocked by status: ${JSON.stringify(menuHit)}`);
  await page.locator('.editor .band [data-act="lut-menu"]').click();
  await page.waitForSelector('.lut-menu-pop:popover-open', { timeout: 5_000 });
  await frame(page, 2);
  const impHit = await probeVisible(page, '.lut-menu-pop:popover-open [data-act="lut-menu-import"]', '.lut-menu-pop:popover-open');
  const spcHit = await probeVisible(page, '.lut-menu-pop:popover-open [data-act="lut-menu-space"]', '.lut-menu-pop:popover-open');
  check(impHit.tested === 1 && impHit.bad.length === 0, `${tag} lut-import blocked by status: ${JSON.stringify(impHit)}`);
  check(spcHit.tested === 1 && spcHit.bad.length === 0, `${tag} lut-space blocked by status: ${JSON.stringify(spcHit)}`);
  await page.keyboard.press('Escape');
  await page.waitForFunction(() => !document.querySelector('.lut-menu-pop:popover-open'), null, { timeout: 5_000 });

  await page.locator('.editor .panel.lut [data-mode="recipe"]').click();
  await frame(page, 2);
}

/** P2-1: 活动导出态下切到 JPG-only 照片，验证配方状态瓷砖可见、>=44pt、无遮挡且可点开状态说明表。 */
export async function auditRecipeStatusInState(page, jpgOnlyStem, returnStem, tag, openEditorPhoto, check, frame, onReady = null) {
  await openEditorPhoto(page, jpgOnlyStem);
  await page.locator('.editor .toolbar [data-tool="recipe"]').click();
  await frame(page, 2);
  const statusHit = await probeVisible(page, '.editor .band [data-act="recipe-status"]', '.editor .band-body');
  check(statusHit.tested === 1 && statusHit.bad.length === 0, `${tag} recipe-status missing/blocked: ${JSON.stringify(statusHit)}`);
  check(statusHit.minH >= 43.5 && statusHit.minW >= 43.5, `${tag} recipe-status size < 44pt: ${JSON.stringify(statusHit)}`);
  if (onReady) await onReady();
  await page.locator('.editor .band [data-act="recipe-status"]').click();
  await page.waitForSelector('.recipe-info-sheet', { timeout: 10_000 });
  const sheetText = (await page.locator('.recipe-info-sheet').innerText()).trim();
  check(sheetText.includes('配方需要 RAW 文件'), `${tag} recipe-status sheet text wrong`);
  await page.locator('.recipe-info-sheet [data-act="close"]').click();
  await page.waitForSelector('.recipe-info-sheet', { state: 'detached', timeout: 10_000 });
  await openEditorPhoto(page, returnStem);
  await frame(page, 2);
}

/** P2-2: 详情工作表在运行中连续重画 >=2 次时保持焦点在工作表内同一按钮，关闭（Escape/遮罩/收起）还焦给 .pdock [data-act="toggle-details"]。 */
export async function verifyDetailsFocusLifecycle(page, tag, check, frame) {
  await page.locator('.pdock [data-act="toggle-details"]').click();
  await page.waitForSelector('.export-sheet [data-act="stop"]', { timeout: 10_000 });
  await page.locator('.export-sheet [data-act="stop"]').focus();
  await frame(page, 1);

  for (let tick = 1; tick <= 2; tick += 1) {
    const afterTick = await page.evaluate(() => {
      const flow = window.__devExport?.flow;
      if (!flow || typeof flow.paint !== 'function') return { hasPaint: false };
      flow.paint();
      const sheet = document.querySelector('.export-sheet');
      const ae = document.activeElement;
      return {
        hasPaint: true,
        inside: Boolean(sheet && ae && sheet.contains(ae)),
        act: ae instanceof HTMLElement ? (ae.dataset.act ?? null) : null,
      };
    });
    check(
      Boolean(afterTick.hasPaint && afterTick.inside && afterTick.act === 'stop'),
      `${tag} details focus lost on repaint #${tick}: ${JSON.stringify(afterTick)}`,
    );
  }

  const checkOpenerFocused = async (how) => {
    const info = await page.evaluate(() => {
      const opener = document.querySelector('.pdock [data-act="toggle-details"]');
      const ae = document.activeElement;
      return {
        sheetGone: !document.querySelector('.export-sheet'),
        returnedToOpener: Boolean(opener && ae === opener),
        activeTag: ae?.tagName ?? 'null',
        activeAct: ae instanceof HTMLElement ? (ae.dataset.act ?? null) : null,
      };
    });
    check(info.sheetGone && info.returnedToOpener, `${tag} focus did not return to toggle-details after ${how}: ${JSON.stringify(info)}`);
  };

  // Close 1: Escape
  await page.keyboard.press('Escape');
  await frame(page, 2);
  await checkOpenerFocused('Escape');

  // Close 2: Scrim click
  await page.locator('.pdock [data-act="toggle-details"]').click();
  await page.waitForSelector('.export-details-scrim', { timeout: 10_000 });
  await page.locator('.export-details-scrim').click({ position: { x: 20, y: 80 } });
  await frame(page, 2);
  await checkOpenerFocused('scrim');

  // Close 3: 收起 button
  await page.locator('.pdock [data-act="toggle-details"]').click();
  await page.waitForSelector('.export-sheet [data-act="close-details"]', { timeout: 10_000 });
  await page.locator('.export-sheet [data-act="close-details"]').click();
  await frame(page, 2);
  await checkOpenerFocused('close-details');
}

/** P3-1: 键盘操作曝光滑块和裁剪拉直尺后，实时数值胶囊显示并按时淡出隐藏（绝不滞留）。 */
export async function verifyKeyboardScrubFadeout(page, tag, check, frame) {
  await page.locator('.editor .toolbar [data-tool="adjust"]').click();
  await frame(page, 2);
  const slider = page.locator('.editor .panel.adjust input[type="range"]');
  await slider.focus();
  await page.keyboard.press('ArrowRight');
  await frame(page, 2);
  const shown = await page.evaluate(() => {
    const pill = document.querySelector('.editor .scrub-pill');
    return { visible: pill?.dataset.visible ?? '', text: pill?.textContent?.trim() ?? '' };
  });
  check(shown.visible === '1' && shown.text.includes('曝光'), `${tag} keyboard adjust scrub pill did not show: ${JSON.stringify(shown)}`);
  await page.waitForTimeout(700);
  const hidden = await page.evaluate(() => {
    const pill = document.querySelector('.editor .scrub-pill');
    return { visible: pill?.dataset.visible ?? '', hidden: Boolean(pill?.hidden) };
  });
  check(hidden.visible === '0' && hidden.hidden, `${tag} keyboard adjust scrub pill stuck: ${JSON.stringify(hidden)}`);
  await page.locator('.editor .panel.adjust button', { hasText: '全部重置' }).first().click().catch(() => {});

  // Crop ruler keyboard ArrowRight also fades cleanly
  await page.locator('.editor .toolbar [data-tool="crop"]').click();
  await frame(page, 2);
  await page.locator('.editor .panel.crop-panel .ruler').focus();
  await page.keyboard.press('ArrowRight');
  await frame(page, 2);
  const cropShown = await page.evaluate(() => ({
    visible: document.querySelector('.editor .scrub-pill')?.dataset.visible ?? '',
    text: document.querySelector('.editor .scrub-pill')?.textContent?.trim() ?? '',
  }));
  check(cropShown.visible === '1' && cropShown.text.includes('°'), `${tag} keyboard crop scrub pill did not show: ${JSON.stringify(cropShown)}`);
  await page.waitForTimeout(700);
  const cropHidden = await page.evaluate(() => ({
    visible: document.querySelector('.editor .scrub-pill')?.dataset.visible ?? '',
    hidden: Boolean(document.querySelector('.editor .scrub-pill')?.hidden),
  }));
  check(cropHidden.visible === '0' && cropHidden.hidden, `${tag} keyboard crop scrub pill stuck: ${JSON.stringify(cropHidden)}`);
  await page.locator('.editor .panel.crop-panel .chips .chip', { hasText: '原始' }).click().catch(() => {});
  await page.locator('.editor .toolbar [data-tool="adjust"]').click();
  await frame(page, 2);
}

/** P3-4: 已有成片保存后仅剩 1 张失败时，详情汇总必须是「1 张没做成」而不是「1 张都没做成」。 */
export async function verifySavedFailureSummary(page, tag, check, frame) {
  await page.locator('.pdock [data-act="toggle-details"]').click();
  await page.waitForSelector('.export-sheet .es-summary', { timeout: 10_000 });
  const summary = (await page.locator('.export-sheet .es-summary').innerText()).trim();
  check(summary === '1 张没做成', `${tag} post-save failure summary wrong: got "${summary}", want "1 张没做成"`);
  await page.keyboard.press('Escape');
  await frame(page, 2);
  const returned = await page.evaluate(() => document.activeElement === document.querySelector('.pdock [data-act="toggle-details"]'));
  check(returned, `${tag} focus did not return to toggle-details after Escape in total-fail`);
}
