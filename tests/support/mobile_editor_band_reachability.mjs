// 手机单带编辑器（U2 phone-editor-band）可达性验收步骤：从 mobile_editor_band_browser.mjs 拆出的纯搬迁模块。

export async function runReachability(page, session, ctx) {
  const { manifest, check, r1, frame, setSafeArea, measurePhoto, clickVisible, tapStageCenter, openEditorPhoto, paths, behaviour, CURVE_BOX_SLOP } = ctx;
  await page.setViewportSize({ width: 390, height: 844 });
  await setSafeArea(page, 47, 34);
  await openEditorPhoto(page, manifest.portrait1Stem);
  const m0 = await measurePhoto(page);
  if (m0?.bandCollapsed === '1') await tapStageCenter(page, session);

  // 1. 配方选择、微调、筛选
  const bandTool0 = await page.locator('.editor .band').getAttribute('data-tool', { timeout: 2000 }).catch(() => null);
  check(bandTool0 === 'recipe', `initial tool is ${bandTool0}, want recipe`);
  const cards = page.locator('.editor .band .panel.recipe .strip .rc');
  const cardCount = await cards.count();
  check(cardCount >= 2, `recipe cards count=${cardCount}, want >= 2`);
  if (cardCount >= 2) {
    const secondCard = cards.nth(1);
    const okPick = await clickVisible(page, secondCard, 'recipe pick');
    paths.recipe_pick = ['打开照片（默认在「配方」）', '点第二张配方卡'];
    if (okPick) {
      const okFine = await clickVisible(page, '.editor .band .panel.recipe .strip .rc.on', 'recipe fine-tune');
      const hasKnobSlider = await page.locator('.editor .band .panel.recipe .knobs input[type="range"]').first().isVisible().catch(() => false);
      check(okFine && hasKnobSlider, 'recipe fine-tune slider not visible after second tap on selected card');
      paths.recipe_finetune = ['点已选配方卡进入微调', '在 128pt 带内调整滑块', '点「‹ 配方」返回'];
      await clickVisible(page, '.editor .band .panel.recipe [data-act="recipe-back"]', 'recipe fine-tune back');
    }
  }

  // 配方筛选
  if (await clickVisible(page, '.editor .band [data-act="recipe-filter"]', 'recipe filter button')) {
    const okBuiltin = await clickVisible(page, '.filter-sheet [data-filter="builtin"], .band-pop [data-filter="builtin"]', 'recipe filter builtin');
    if (okBuiltin) {
      await clickVisible(page, '.editor .band [data-act="recipe-filter"]', 'recipe filter reopen');
      await clickVisible(page, '.filter-sheet [data-filter="all"], .band-pop [data-filter="all"]', 'recipe filter all');
    }
    paths.recipe_filter = ['配方带顶栏点筛选按钮', '在弹层选「内置」/「全部」'];
  }

  // 2. LUT（从配方带头部开关进入）
  if (await clickVisible(page, '.editor .band .panel.recipe [data-mode="lut"]', 'switch to LUT mode')) {
    const hasMenu = await page.locator('.editor .band [data-act="lut-menu"]').isVisible().catch(() => false);
    let hasSpace = false;
    let hasImport = false;
    if (hasMenu) {
      await page.locator('.editor .band [data-act="lut-menu"]').click();
      await page.waitForSelector('.lut-menu-pop:popover-open', { timeout: 5_000 }).catch(() => {});
      hasSpace = await page.locator('.lut-menu-pop:popover-open [data-act="lut-menu-space"]').isVisible().catch(() => false);
      hasImport = await page.locator('.lut-menu-pop:popover-open [data-act="lut-menu-import"]').isVisible().catch(() => false);
      await page.keyboard.press('Escape');
      await frame(page, 2);
    }
    check(hasMenu && hasSpace && hasImport, `LUT menu controls missing: menu=${hasMenu} space=${hasSpace} import=${hasImport}`);
    if (manifest.lut && hasImport) {
      const lutInput = page.locator('input.lutpick');
      await lutInput.setInputFiles(manifest.lut);
      await frame(page, 4);
      const lutCard = page.locator('.editor .band .panel.lut .strip .rc[data-lut]:not([data-lut="none"]):not([data-lut="deleted"])').first();
      await clickVisible(page, lutCard, 'pick imported LUT');
      if (await page.locator('.ms.lut-space-sheet').isVisible().catch(() => false)) {
        const srgbBtn = page.locator('.ms.lut-space-sheet [data-space="srgb"], .ms.lut-space-sheet .lut-space-list button').first();
        if (await srgbBtn.isVisible().catch(() => false)) await srgbBtn.click();
        await frame(page, 4);
        if (await page.locator('.ms.lut-space-sheet').isVisible().catch(() => false)) {
          await page.locator('.ms.lut-space-sheet .nb').first().click().catch(() => {});
          await frame(page, 2);
        }
      }
      await clickVisible(page, '.editor .band .panel.lut .strip .rc[data-lut="none"]', 'clear LUT');
    }
    paths.lut = ['配方带首点「LUT」', '顶栏「导入 LUT」/「色彩空间」与横条选卡'];
    await clickVisible(page, '.editor .band .panel.lut [data-mode="recipe"]', 'switch back to recipe mode');
  }

  // 3. 调整：10 个参数 + 全部重置 + 浮窗直方图 + 曲线 ›
  if (await clickVisible(page, '.editor .toolbar [data-tool="adjust"]', 'reach adjust')) {
    const paramNames = ['曝光', '对比度', '高光', '阴影', '黑点', '色温', '色调', '饱和度', '自然饱和度', '暗角'];
    const reachedParams = [];
    for (const name of paramNames) {
      const chip = page.locator('.editor .band .panel.adjust .chips .chip', { hasText: new RegExp(`^${name}$`) });
      if (await clickVisible(page, chip, `adjust param ${name}`)) reachedParams.push(name);
    }
    check(reachedParams.length === 10, `reached ${reachedParams.length}/10 adjust params`);
    const slider = page.locator('.editor .band .panel.adjust input[type="range"]').first();
    if (await slider.isVisible().catch(() => false)) {
      await slider.fill('1');
      await slider.dispatchEvent('input');
      await slider.dispatchEvent('change');
      await frame(page, 2);
    }
    await clickVisible(page, page.locator('.editor .band .panel.adjust button', { hasText: '全部重置' }), 'adjust 全部重置');
    paths.adjust_10_and_reset = ['底栏点「调整」', '滑片横条选 10 项参数 + 大滑块', '横条末点「全部重置」'];

    // P2-2：直方图关闭时 20 步拖滑块必须 0 次 engine.histogram() 读回；打开后拖滑块有读回
    const histVisible0 = await page.locator('.editor .stage .hist-float').isVisible().catch(() => false);
    check(!histVisible0, 'floating histogram must be off by default');
    await page.evaluate(() => {
      const engine = window.__gxrEngine;
      if (!engine || engine.__wrappedForHistCount) return;
      engine.__wrappedForHistCount = true;
      window.__histReadbacks = 0;
      const orig = engine.histogram.bind(engine);
      engine.histogram = (...args) => {
        window.__histReadbacks += 1;
        return orig(...args);
      };
    });
    await page.waitForTimeout(150);
    await page.evaluate(() => {
      window.__histReadbacks = 0;
    });
    const expChip = page.locator('.editor .band .panel.adjust .chips .chip', { hasText: /^曝光$/ });
    await clickVisible(page, expChip, 'select 曝光 for hist readback check');
    const expSlider = page.locator('.editor .band .panel.adjust input[type="range"]').first();
    const expBox = await expSlider.boundingBox();
    if (expBox && expBox.width > 20) {
      const sy = expBox.y + expBox.height / 2;
      await page.mouse.move(expBox.x + expBox.width * 0.5, sy);
      await page.mouse.down();
      for (let step = 1; step <= 20; step += 1) {
        await page.mouse.move(expBox.x + expBox.width * (0.5 + 0.35 * (step / 20)), sy);
        await frame(page, 1);
      }
      await page.mouse.up();
      await page.waitForTimeout(200);
    }
    const closedHistReadbacks = await page.evaluate(() => window.__histReadbacks ?? -1);
    behaviour.histClosedDragReadbacks = closedHistReadbacks;
    check(closedHistReadbacks === 0, `histogram closed 20-step drag triggered ${closedHistReadbacks} engine.histogram() readbacks, want 0`);

    if (await clickVisible(page, '.editor .band .panel.adjust [data-act="hist-toggle"]', 'hist toggle on')) {
      const histVisible1 = await page.locator('.editor .stage .hist-float').isVisible().catch(() => false);
      check(histVisible1, 'floating histogram did not appear when toggled on');
      const hasClips = await page.locator('.editor .stage .hist-float .clip').count();
      check(hasClips === 2, `floating histogram clip indicators=${hasClips}, want 2`);
      await page.waitForTimeout(200);
      await page.evaluate(() => {
        window.__histReadbacks = 0;
      });
      if (expBox && expBox.width > 20) {
        const sy = expBox.y + expBox.height / 2;
        await page.mouse.move(expBox.x + expBox.width * 0.85, sy);
        await page.mouse.down();
        for (let step = 1; step <= 10; step += 1) {
          await page.mouse.move(expBox.x + expBox.width * (0.85 - 0.35 * (step / 10)), sy);
          await frame(page, 1);
        }
        await page.mouse.up();
        await page.waitForTimeout(250);
      }
      const openHistReadbacks = await page.evaluate(() => window.__histReadbacks ?? 0);
      behaviour.histOpenDragReadbacks = openHistReadbacks;
      check(openHistReadbacks > 0, `histogram open drag triggered ${openHistReadbacks} engine.histogram() readbacks, want > 0`);
      await clickVisible(page, '.editor .band .panel.adjust [data-act="hist-toggle"]', 'hist toggle off');
      await clickVisible(page, page.locator('.editor .band .panel.adjust button', { hasText: '全部重置' }), 'adjust reset after hist check');
      paths.histogram = ['底栏点「调整」', '点直方图图标开/关浮窗直方图'];
    }

    // 曲线 ›：打开更高抽屉，验证遮罩不压暗照片（P2-3）、点遮罩可关且拖动盒子不动
    if (await clickVisible(page, page.locator('.editor .band .panel.adjust button', { hasText: '曲线' }), 'open curves sheet')) {
      await page.waitForSelector('.editor svg.curve', { timeout: 5000 }).catch(() => {});
      const scrimBg = await page.evaluate(() => {
        const s = document.querySelector('.curves-scrim');
        return s ? getComputedStyle(s).backgroundColor : null;
      });
      const scrimAlpha = (() => {
        if (!scrimBg) return -1;
        if (scrimBg === 'transparent') return 0;
        const m = scrimBg.match(/rgba?\(\s*[\d.]+\s*,\s*[\d.]+\s*,\s*[\d.]+(?:\s*,\s*([\d.]+))?\s*\)/);
        if (!m) return -1;
        return m[1] === undefined ? 1 : Number(m[1]);
      })();
      behaviour.curvesScrimBg = scrimBg;
      check(scrimAlpha === 0, `curves scrim dims photo: background=${scrimBg}, want transparent (alpha=0)`);
      const box0 = await page.locator('.editor svg.curve').boundingBox();
      check(Boolean(box0 && box0.width > 100 && box0.height > 100), 'curves sheet svg.curve missing or too small');
      if (box0) {
        const cx = box0.x + box0.width * 0.5;
        const cy = box0.y + box0.height * 0.5;
        await session.send('Input.dispatchTouchEvent', { type: 'touchStart', touchPoints: [{ x: cx, y: cy, id: 1 }] });
        await frame(page, 2);
        let maxShift = 0;
        for (let i = 1; i <= 5; i += 1) {
          await session.send('Input.dispatchTouchEvent', {
            type: 'touchMove',
            touchPoints: [{ x: cx, y: cy - i * 10, id: 1 }],
          });
          await frame(page, 2);
          const b = await page.locator('.editor svg.curve').boundingBox();
          if (b) {
            maxShift = Math.max(
              maxShift,
              Math.abs(b.x - box0.x),
              Math.abs(b.y - box0.y),
              Math.abs(b.width - box0.width),
              Math.abs(b.height - box0.height),
            );
          }
        }
        await session.send('Input.dispatchTouchEvent', { type: 'touchEnd', touchPoints: [] });
        await frame(page, 2);
        behaviour.curveDragBoxShiftPx = r1(maxShift);
        check(maxShift <= CURVE_BOX_SLOP, `curve box shifted ${r1(maxShift)}px during drag`);
      }
      const resetCurve = page.getByRole('button', { name: '重置全部曲线', exact: true });
      if (await resetCurve.isEnabled().catch(() => false)) await resetCurve.click();
      // 点击遮罩空白处（舞台上半部）关闭曲线工作表
      const stageTop = await page.locator('.editor .stage').boundingBox();
      if (stageTop) {
        await page.mouse.click(stageTop.x + stageTop.width * 0.5, stageTop.y + 40);
        await frame(page, 2);
      }
      const curvesClosedByScrim = !(await page.locator('.curves-sheet').isVisible().catch(() => false));
      behaviour.curvesClosedByScrim = curvesClosedByScrim;
      check(curvesClosedByScrim, 'tapping transparent .curves-scrim outside sheet did not close curves sheet');
      if (!curvesClosedByScrim) {
        await clickVisible(page, '.curves-sheet [data-act="close"], .curves-sheet .nb', 'close curves sheet');
      }
      paths.curves = ['底栏点「调整」', '横条末点「曲线 ›」（或「更多 → 曲线」）'];
    }
  }

  const closeSheet = async (sheetSel = '.ms') => {
    const sheet = page.locator(sheetSel).last();
    const btn = sheet.locator('.nb, [data-act="close"]').first();
    if (await btn.isVisible().catch(() => false)) {
      await btn.click({ timeout: 3000 }).catch(() => {});
    } else {
      await page.locator('.scrim').last().click({ position: { x: 10, y: 10 }, timeout: 3000 }).catch(() => {});
    }
    await frame(page, 2);
  };

  // 4. 裁剪：比例、拉直、横竖对调、参考线与成品尺寸文案（P3-4）
  if (await clickVisible(page, '.editor .toolbar [data-tool="crop"]', 'reach crop')) {
    await clickVisible(page, page.locator('.editor .band .panel.crop-panel .chips .chip', { hasText: '3:2' }), 'crop ratio 3:2');
    await frame(page, 3);
    const cropSizeLine = (await page.locator('.editor .band-crop-hd .crop-size-line').innerText().catch(() => '')).trim();
    behaviour.cropSizeLine = cropSizeLine;
    check(/^成品\s+\d+\s*×\s*\d+\s*像素\s*·\s*.+$/.test(cropSizeLine), `crop size line "${cropSizeLine}" missing 成品 W × H 像素 · <ratio>`);
    await clickVisible(page, '.editor .band .panel.crop-panel [data-act="crop-flip"]', 'crop 横竖对调');
    const ruler = page.locator('.editor .band .panel.crop-panel .ruler');
    check(await ruler.isVisible().catch(() => false), 'crop straighten ruler not visible');
    if (await clickVisible(page, '.editor .band .panel.crop-panel .guide-btn', 'crop guide sheet')) {
      await clickVisible(page, '.guide-sheet .guide-item', 'pick crop guide');
    }
    await clickVisible(page, page.locator('.editor .band .panel.crop-panel .chips .chip', { hasText: '原始' }), 'crop reset original');
    await page.waitForSelector('.editor .band .panel.crop-panel .chips .chip[data-preset="original"].on', { timeout: 5000 }).catch(() => {});
    paths.crop = ['底栏点「裁剪」', '选比例 / 点「横竖对调」/ 拖拉直标尺 / 点「参考线」'];
  }

  // 5. 更多 -> 边框及其全部子抽屉
  if (await clickVisible(page, '.editor .toolbar [data-tool="more"]', 'open more for frame')) {
    if (await clickVisible(page, '.more-sheet [data-more="frame"]', 'more -> frame')) {
      const styleBtn = page.locator('.editor .band .panel.frame .strip .rc:not([data-style="none"])').first();
      if (await clickVisible(page, styleBtn, 'pick frame style')) {
        await clickVisible(page, '.editor .band .band-frame-hd [data-act="frame-opts"], .editor .band .panel.frame .strip .rc.on', 'open frame options');
        const reachedControls = [];
        if (await clickVisible(page, '.editor .band .panel.frame [data-act="frame-logo"]', 'frame sheet 标志')) {
          if (await page.locator('.ms.logo-sheet, .ms.frame-sheet').first().isVisible().catch(() => false)) reachedControls.push('标志抽屉');
          await closeSheet('.ms.logo-sheet, .ms.frame-sheet');
        }
        if (await clickVisible(page, '.editor .band .panel.frame [data-act="frame-params"]', 'frame toggle 参数')) reachedControls.push('参数');
        if (await clickVisible(page, '.editor .band .panel.frame [data-act="frame-date"]', 'frame toggle 日期')) reachedControls.push('日期');
        if (await clickVisible(page, '.editor .band .panel.frame [data-act="frame-more"]', 'frame sheet 更多')) {
          if (await page.locator('.ms.frame-sheet').first().isVisible().catch(() => false)) reachedControls.push('更多抽屉');
          await closeSheet('.ms.frame-sheet');
        }
        check(reachedControls.length === 4, `frame options reached=${reachedControls.join(',')}, want 4`);
        await clickVisible(page, '.editor .band .panel.frame [data-act="frame-back"]', 'frame back to styles');
        await clickVisible(page, '.editor .band .panel.frame .strip .rc[data-style="none"]', 'frame reset none');
        paths.frame = ['底栏点「更多」', '点「边框」', '选款式后再点卡片/设置进入配色、线条与标志/参数/日期/更多子抽屉'];
      }
    }
  }

  // 6. 更多 -> 时间戳（含 P3-2 >= 44pt 触控热区检查）
  if (await clickVisible(page, '.editor .toolbar [data-tool="more"]', 'open more for stamp')) {
    if (await clickVisible(page, '.more-sheet [data-more="stamp"]', 'more -> stamp')) {
      check(await page.locator('.editor .band .switch').isVisible().catch(() => false), 'stamp band switch missing');
      const stampHit = await page.evaluate(() => {
        const sw = document.querySelector('.editor .band .stamp-band input.switch');
        const label = sw?.closest('label');
        const target = label ?? sw;
        if (!target) return null;
        const r = target.getBoundingClientRect();
        return { tag: target.tagName.toLowerCase(), w: Math.round(r.width * 10) / 10, h: Math.round(r.height * 10) / 10 };
      });
      behaviour.stampSwitchHit = stampHit;
      check(Boolean(stampHit && stampHit.w >= 44 && stampHit.h >= 44), `stamp switch hit area ${JSON.stringify(stampHit)}, want >= 44x44`);
      if (await clickVisible(page, '.editor .band [data-act="stamp-sheet"]', 'open stamp detail sheet')) {
        check(await page.locator('.ms.stamp-sheet').isVisible().catch(() => false), 'stamp_sheet did not open');
        await closeSheet('.ms.stamp-sheet');
      }
      paths.stamp = ['底栏点「更多」', '点「时间戳」', '在带内开关或点「详细设置」开时间戳抽屉'];
    }
  }

  // 7. 两套独立菜单：底栏「更多」严格 5 项（不含复制调整）；顶栏「⋯」进入「复制调整」
  if (await clickVisible(page, '.editor .toolbar [data-tool="more"]', 'open more menu check')) {
    const moreLabels = await page.locator('.more-sheet .more-row b').allInnerTexts();
    behaviour.bottomMoreEntries = moreLabels;
    const wantMore = ['边框', '时间戳', '曲线', '导出选项', '校准与配方文件'];
    check(
      JSON.stringify(moreLabels) === JSON.stringify(wantMore),
      `bottom 更多 items=${JSON.stringify(moreLabels)}, want ${JSON.stringify(wantMore)}`,
    );
    await closeSheet('.more-sheet');
  }
  if (await clickVisible(page, '.editor .nav [data-act="editor-more"]', 'open top ⋯ for copy')) {
    const topMoreBefore = await page.locator('.ms[data-state="editor-more"] .lrow b').allInnerTexts();
    behaviour.topMoreEntriesBeforeCopy = topMoreBefore;
    check(
      JSON.stringify(topMoreBefore) === JSON.stringify(['复制调整', '导出选项', '做成关机图', '照片信息']),
      `top ⋯ items before copy=${JSON.stringify(topMoreBefore)}, want ['复制调整','导出选项','做成关机图','照片信息']`,
    );
    if (await clickVisible(page, '.ms[data-state="editor-more"] [data-act="more-copy"]', 'top ⋯ -> 复制调整')) {
      check(await page.locator('.ms.paste-sheet[data-state="copy-options"]').isVisible().catch(() => false), 'copy sheet did not open from top ⋯');
      await closeSheet('.ms.paste-sheet[data-state="copy-options"]');
      paths.copy_paste = ['顶栏点「⋯」', '点「复制调整」'];
    }
  }

  // 8. 导出：底栏「存到相册」（一键导出 + 就绪条）与「更多 → 导出选项」
  if (await clickVisible(page, '.editor .toolbar [data-act="export"]', 'toolbar 存到相册')) {
    await page.waitForTimeout(150);
    check(!(await page.locator('.ms.export').isVisible().catch(() => false)), '存到相册 opened export options sheet instead of quick export');
    await page.waitForFunction(() => document.querySelector('.pdock')?.dataset.phase === 'ready', null, { timeout: 300_000 });
    await clickVisible(page, '.pdock [data-act="share"]', 'share quick export in reachability');
    await page.waitForFunction(() => !document.querySelector('.pdock'), null, { timeout: 15_000 });
  }
  if (await clickVisible(page, '.editor .toolbar [data-tool="more"]', 'open more for export')) {
    if (await clickVisible(page, '.more-sheet [data-more="export"]', 'more -> 导出选项')) {
      check(await page.locator('.ms.export').first().isVisible().catch(() => false), '更多 → 导出选项 did not open export sheet');
      const title = (await page.locator('.ms.export h2').innerText().catch(() => '')).trim();
      check(title === '导出选项', `更多 → 导出选项 title=\"${title}\", want 导出选项`);
      await closeSheet('.ms.export');
      paths.export = ['底栏点「存到相册」（一键导出）', '或「更多 → 导出选项」/ 顶栏「⋯ → 导出选项」'];
    }
  }

  // 9. 校准与配方文件
  if (await clickVisible(page, '.editor .toolbar [data-tool="more"]', 'open more for calib')) {
    if (await clickVisible(page, '.more-sheet [data-more="calib"]', 'more -> 校准与配方文件')) {
      check(await page.locator('.ms.calib-sheet').isVisible().catch(() => false), 'calib sheet did not open');
      await closeSheet('.ms.calib-sheet');
      paths.calibration = ['底栏点「更多」', '点「校准与配方文件」'];
    }
  }
}
