// 手机单带编辑器（U2 phone-editor-band）交互行为验收步骤：从 mobile_editor_band_browser.mjs 拆出的纯搬迁模块。

async function swipeStage(page, session, frame, stageBox, preferNext = true) {
  const sub = await page.locator('.editor .nav small').innerText().catch(() => '');
  const m = sub.match(/(\d+)\s*\/\s*(\d+)/);
  const idx = m ? Number(m[1]) : 1;
  const total = m ? Number(m[2]) : 2;
  const dir = preferNext ? (idx < total ? 1 : -1) : (idx > 1 ? -1 : 1);
  const y = stageBox.y + stageBox.height / 2;
  const x0 = dir > 0 ? stageBox.x + stageBox.width * 0.8 : stageBox.x + stageBox.width * 0.2;
  const x1 = dir > 0 ? stageBox.x + stageBox.width * 0.2 : stageBox.x + stageBox.width * 0.8;
  await session.send('Input.dispatchTouchEvent', { type: 'touchStart', touchPoints: [{ x: x0, y, id: 1 }] });
  for (let i = 1; i <= 5; i += 1) {
    const x = x0 + ((x1 - x0) * i) / 5;
    await session.send('Input.dispatchTouchEvent', { type: 'touchMove', touchPoints: [{ x, y, id: 1 }] });
    await frame(page, 1);
  }
  await session.send('Input.dispatchTouchEvent', { type: 'touchEnd', touchPoints: [] });
  await frame(page, 4);
}

export async function runBehaviourChecks(page, session, ctx) {
  const { manifest, check, r1, frame, setSafeArea, measurePhoto, clickVisible, tapStageCenter, openEditorPhoto, behaviour } = ctx;
  await page.setViewportSize({ width: 390, height: 844 });
  await setSafeArea(page, 47, 34);
  await openEditorPhoto(page, manifest.portrait1Stem);
  if ((await measurePhoto(page))?.bandCollapsed === '1') await tapStageCenter(page, session);

  // 1. 切到「调整」，横滑到下一张：必须回到「配方」
  await clickVisible(page, '.editor .toolbar [data-tool="adjust"]', 'behaviour switch adjust');
  const stageBox = await page.locator('.editor .stage').boundingBox();
  if (stageBox) {
    await swipeStage(page, session, frame, stageBox, true);
    await page.waitForFunction(
      (prev) => {
        const ed = document.querySelector('.screen.editor');
        const app = document.querySelector('.app');
        const cur = ed?.getAttribute('data-photo') ?? '';
        return Boolean(cur && cur !== prev && app?.getAttribute('data-settled') === cur);
      },
      manifest.portrait1Stem,
      { timeout: 30_000 },
    );
    const toolNext = await page.locator('.editor .band').getAttribute('data-tool', { timeout: 2000 }).catch(() => null);
    behaviour.nextPhotoTool = toolNext;
    check(toolNext === 'recipe', `next photo opened on ${toolNext}, want recipe`);
  }

  // 2. 点照片收起带子，再横滑到下一张：收起态必须记住，且再展开时工具是「配方」
  await openEditorPhoto(page, manifest.portrait1Stem);
  if ((await measurePhoto(page))?.bandCollapsed === '1') await tapStageCenter(page, session);
  await tapStageCenter(page, session);
  const c1 = await page.locator('.editor .band').getAttribute('data-collapsed', { timeout: 2000 }).catch(() => null);
  check(c1 === '1', `tap photo collapse state=${c1}, want 1`);
  const sb2 = await page.locator('.editor .stage').boundingBox();
  if (sb2) {
    await swipeStage(page, session, frame, sb2, true);
    await page.waitForFunction(
      (prev) => {
        const ed = document.querySelector('.screen.editor');
        const app = document.querySelector('.app');
        const cur = ed?.getAttribute('data-photo') ?? '';
        return Boolean(cur && cur !== prev && app?.getAttribute('data-settled') === cur);
      },
      manifest.portrait1Stem,
      { timeout: 30_000 },
    );
    await frame(page, 3);
    const cNext = await page.locator('.editor .band').getAttribute('data-collapsed', { timeout: 2000 }).catch(() => null);
    behaviour.collapsedRememberedOnNext = cNext === '1';
    check(cNext === '1', `collapsed state not remembered on next photo (${cNext})`);
    await tapStageCenter(page, session);
  }

  // 3. 双指捏合放大 + 放大后单指拖移不切图 + 双击复原
  await openEditorPhoto(page, manifest.portrait1Stem);
  if ((await measurePhoto(page))?.bandCollapsed === '1') await tapStageCenter(page, session);
  const sb3 = await page.locator('.editor .stage').boundingBox();
  if (sb3) {
    const cx = sb3.x + sb3.width / 2;
    const cy = sb3.y + sb3.height / 2;
    await session.send('Input.dispatchTouchEvent', {
      type: 'touchStart',
      touchPoints: [
        { x: cx - 30, y: cy, id: 1 },
        { x: cx + 30, y: cy, id: 2 },
      ],
    });
    for (let i = 1; i <= 5; i += 1) {
      await session.send('Input.dispatchTouchEvent', {
        type: 'touchMove',
        touchPoints: [
          { x: cx - 30 - i * 16, y: cy, id: 1 },
          { x: cx + 30 + i * 16, y: cy, id: 2 },
        ],
      });
      await frame(page, 1);
    }
    await session.send('Input.dispatchTouchEvent', { type: 'touchEnd', touchPoints: [] });
    await frame(page, 3);
    const scaleAfterPinch = await page.evaluate(() => Number(document.querySelector('.editor .stage')?.getAttribute('data-zoom') || '1'));
    behaviour.pinchScale = r1(scaleAfterPinch);
    check(scaleAfterPinch > 1.25, `pinch zoom scale=${scaleAfterPinch}, want > 1.25`);

    // 放大状态下大幅单指横滑：应该平移而不是切照片
    const photoBeforePan = await page.locator('.screen.editor').getAttribute('data-photo');
    await session.send('Input.dispatchTouchEvent', { type: 'touchStart', touchPoints: [{ x: cx + 80, y: cy, id: 1 }] });
    await session.send('Input.dispatchTouchEvent', { type: 'touchMove', touchPoints: [{ x: cx - 80, y: cy, id: 1 }] });
    await session.send('Input.dispatchTouchEvent', { type: 'touchEnd', touchPoints: [] });
    await frame(page, 4);
    const photoAfterPan = await page.locator('.screen.editor').getAttribute('data-photo');
    behaviour.zoomPanStayedOnPhoto = photoBeforePan === photoAfterPan;
    check(photoBeforePan === photoAfterPan, 'one-finger swipe while zoomed changed photo instead of panning');

    // 双击复原
    await session.send('Input.dispatchTouchEvent', { type: 'touchStart', touchPoints: [{ x: cx, y: cy, id: 1 }] });
    await session.send('Input.dispatchTouchEvent', { type: 'touchEnd', touchPoints: [] });
    await page.waitForTimeout(80);
    await session.send('Input.dispatchTouchEvent', { type: 'touchStart', touchPoints: [{ x: cx, y: cy, id: 1 }] });
    await session.send('Input.dispatchTouchEvent', { type: 'touchEnd', touchPoints: [] });
    await frame(page, 4);
    const scaleAfterDbl = await page.evaluate(() => Number(document.querySelector('.editor .stage')?.getAttribute('data-zoom') || '1'));
    behaviour.doubleTapResetScale = r1(scaleAfterDbl);
    check(Math.abs(scaleAfterDbl - 1) <= 0.05, `double-tap zoom reset scale=${scaleAfterDbl}, want 1`);
  }

  // 4. 长按显示原图（Decision 4：顶栏第二行切为「原图」、放手回到 i / N；收起单带时顶栏 HDR 仍可见且长按同样显示「原图」）
  const sb4 = await page.locator('.editor .stage').boundingBox();
  if (sb4) {
    const cx = sb4.x + sb4.width / 2;
    const cy = sb4.y + sb4.height / 2;
    const navSubBefore = (await page.locator('.editor .nav .t small').innerText().catch(() => '')).trim();
    await session.send('Input.dispatchTouchEvent', { type: 'touchStart', touchPoints: [{ x: cx, y: cy, id: 1 }] });
    await page.waitForTimeout(550);
    await frame(page, 2);
    const lblText = (await page.locator('.editor .lbl').first().innerText().catch(() => '')).trim();
    const navSubHoldOpen = (await page.locator('.editor .nav .t small').innerText().catch(() => '')).trim();
    await session.send('Input.dispatchTouchEvent', { type: 'touchEnd', touchPoints: [] });
    await frame(page, 2);
    const navSubAfterOpen = (await page.locator('.editor .nav .t small').innerText().catch(() => '')).trim();
    behaviour.longPressShowsOriginal = lblText.includes('原图') && navSubHoldOpen === '原图';
    behaviour.longPressNavSubOpen = { before: navSubBefore, hold: navSubHoldOpen, after: navSubAfterOpen };
    check(lblText.includes('原图'), `long-press label was "${lblText}", expected 原图`);
    check(navSubHoldOpen === '原图', `long-press top-bar subtitle (expanded) was "${navSubHoldOpen}", expected 原图`);
    check(navSubAfterOpen === navSubBefore && /^\d+\s*\/\s*\d+$/.test(navSubAfterOpen), `top-bar subtitle did not return to i / N after release ("${navSubAfterOpen}")`);

    // 收起单带：顶栏 HDR 仍可见，.band-hd 隐藏，长按时顶栏第二行同样切为「原图」且松手回到 i / N
    await tapStageCenter(page, session);
    const collapsedForHold = (await page.locator('.editor .band').getAttribute('data-collapsed')) === '1';
    const hdrVisibleCollapsed = await page.locator('.editor .nav [data-act="hdr-toggle"]').isVisible().catch(() => false);
    const sbCollapsed = await page.locator('.editor .stage').boundingBox();
    let navSubHoldCollapsed = '';
    let navSubAfterCollapsed = '';
    if (sbCollapsed) {
      const ccx = sbCollapsed.x + sbCollapsed.width / 2;
      const ccy = sbCollapsed.y + sbCollapsed.height / 2;
      await session.send('Input.dispatchTouchEvent', { type: 'touchStart', touchPoints: [{ x: ccx, y: ccy, id: 1 }] });
      await page.waitForTimeout(550);
      await frame(page, 2);
      navSubHoldCollapsed = (await page.locator('.editor .nav .t small').innerText().catch(() => '')).trim();
      await session.send('Input.dispatchTouchEvent', { type: 'touchEnd', touchPoints: [] });
      await frame(page, 2);
      navSubAfterCollapsed = (await page.locator('.editor .nav .t small').innerText().catch(() => '')).trim();
    }
    behaviour.longPressCollapsed = {
      collapsed: collapsedForHold,
      hdrVisibleCollapsed,
      hold: navSubHoldCollapsed,
      after: navSubAfterCollapsed,
    };
    check(collapsedForHold, 'band failed to collapse before collapsed long-press check');
    check(hdrVisibleCollapsed, 'top-bar HDR switch not visible while band is collapsed');
    check(navSubHoldCollapsed === '原图', `long-press top-bar subtitle (collapsed) was "${navSubHoldCollapsed}", expected 原图`);
    check(navSubAfterCollapsed === navSubBefore, `collapsed long-press top-bar subtitle did not return to "${navSubBefore}" ("${navSubAfterCollapsed}")`);
    await tapStageCenter(page, session);
  }

  // 5. P3-1：未放大时双击照片不会闪动收起/再展开单带
  if (sb4) {
    if ((await measurePhoto(page))?.bandCollapsed === '1') await tapStageCenter(page, session);
    const cx = sb4.x + sb4.width / 2;
    const cy = sb4.y + sb4.height / 2;
    await page.evaluate(() => {
      const band = document.querySelector('.editor .band');
      window.__collapseChanges = [];
      window.__collapseObserver?.disconnect();
      if (!band) return;
      window.__collapseObserver = new MutationObserver(() => {
        window.__collapseChanges.push(band.getAttribute('data-collapsed'));
      });
      window.__collapseObserver.observe(band, { attributes: true, attributeFilter: ['data-collapsed'] });
    });
    await session.send('Input.dispatchTouchEvent', { type: 'touchStart', touchPoints: [{ x: cx, y: cy, id: 1 }] });
    await session.send('Input.dispatchTouchEvent', { type: 'touchEnd', touchPoints: [] });
    await page.waitForTimeout(80);
    await session.send('Input.dispatchTouchEvent', { type: 'touchStart', touchPoints: [{ x: cx, y: cy, id: 1 }] });
    await session.send('Input.dispatchTouchEvent', { type: 'touchEnd', touchPoints: [] });
    await page.waitForTimeout(420);
    const collapseChanges = await page.evaluate(() => {
      window.__collapseObserver?.disconnect();
      return window.__collapseChanges ?? [];
    });
    behaviour.unzoomedDoubleTapCollapseChanges = collapseChanges;
    check(collapseChanges.length === 0, `unzoomed double-tap mutated data-collapsed ${collapseChanges.length} times (${collapseChanges.join(',')}), want 0`);
  }

  // 6. P3-6：左边缘 clientX < 24 起手右滑不拦截（保留 iOS 边缘返回手势；先停在 idx >= 2 确保右滑会触发上一张）
  await openEditorPhoto(page, manifest.portrait1Stem);
  if ((await measurePhoto(page))?.bandCollapsed === '1') await tapStageCenter(page, session);
  const subEdge0 = await page.locator('.editor .nav small').innerText().catch(() => '');
  if (/^\s*1\s*\//.test(subEdge0)) {
    const sbAdv = await page.locator('.editor .stage').boundingBox();
    if (sbAdv) {
      await swipeStage(page, session, frame, sbAdv, true);
      await page.waitForFunction(
        (prev) => {
          const ed = document.querySelector('.screen.editor');
          const app = document.querySelector('.app');
          const cur = ed?.getAttribute('data-photo') ?? '';
          return Boolean(cur && cur !== prev && app?.getAttribute('data-settled') === cur);
        },
        manifest.portrait1Stem,
        { timeout: 30_000 },
      );
      await frame(page, 3);
    }
  }
  const sbEdge = await page.locator('.editor .stage').boundingBox();
  if (sbEdge) {
    const beforeEdgePhoto = await page.locator('.screen.editor').getAttribute('data-photo');
    const y = sbEdge.y + sbEdge.height / 2;
    await session.send('Input.dispatchTouchEvent', { type: 'touchStart', touchPoints: [{ x: 10, y, id: 1 }] });
    for (let i = 1; i <= 5; i += 1) {
      await session.send('Input.dispatchTouchEvent', { type: 'touchMove', touchPoints: [{ x: 10 + i * 35, y, id: 1 }] });
      await frame(page, 1);
    }
    await session.send('Input.dispatchTouchEvent', { type: 'touchEnd', touchPoints: [] });
    await page.waitForTimeout(360);
    const afterEdgePhoto = await page.locator('.screen.editor').getAttribute('data-photo');
    const afterEdgeCollapsed = await page.locator('.editor .band').getAttribute('data-collapsed');
    behaviour.leftEdgeSwipeIgnored = beforeEdgePhoto === afterEdgePhoto && afterEdgeCollapsed === '0';
    check(beforeEdgePhoto === afterEdgePhoto, `left-edge swipe (x=10) switched photo instead of yielding to iOS back gesture`);
    check(afterEdgeCollapsed === '0', `left-edge swipe (x=10) toggled band collapsed=${afterEdgeCollapsed}`);
  }

  // 7. P2-4：选中的远端配方卡在打开照片与横滑换片后自动滚入 .strip 可见区域
  const readActiveCardVisibility = () =>
    page.evaluate(() => {
      const strip = document.querySelector('.editor .band .panel.recipe .strip');
      const card = strip?.querySelector('.rc.on');
      if (!strip || !card) return null;
      const sr = strip.getBoundingClientRect();
      const cr = card.getBoundingClientRect();
      return {
        cardId: card.getAttribute('data-card'),
        scrollLeft: Math.round(strip.scrollLeft),
        stripLeft: Math.round(sr.left),
        stripRight: Math.round(sr.right),
        cardLeft: Math.round(cr.left),
        cardRight: Math.round(cr.right),
        visible: cr.left >= sr.left - 1 && cr.right <= sr.right + 1,
      };
    });

  // 给第一张（portrait1）与第二张（下一张）分别选远端配方卡（超出首屏）
  await openEditorPhoto(page, manifest.portrait1Stem);
  if ((await measurePhoto(page))?.bandCollapsed === '1') await tapStageCenter(page, session);
  const p1Cards = page.locator('.editor .band .panel.recipe .strip .rc');
  const p1Count = await p1Cards.count();
  if (p1Count >= 6) {
    await clickVisible(page, p1Cards.nth(p1Count - 1), 'select far-right recipe on photo 1');
    const sbSwipe = await page.locator('.editor .stage').boundingBox();
    if (sbSwipe) {
      await swipeStage(page, session, frame, sbSwipe, true);
      await page.waitForFunction(
        (prev) => {
          const ed = document.querySelector('.screen.editor');
          const app = document.querySelector('.app');
          const cur = ed?.getAttribute('data-photo') ?? '';
          return Boolean(cur && cur !== prev && app?.getAttribute('data-settled') === cur);
        },
        manifest.portrait1Stem,
        { timeout: 30_000 },
      );
      await frame(page, 3);
      const p2Cards = page.locator('.editor .band .panel.recipe .strip .rc');
      const p2Count = await p2Cards.count();
      if (p2Count >= 6) {
        await clickVisible(page, p2Cards.nth(p2Count - 2), 'select far-right recipe on photo 2');
      }
      // 回到网格再重新打开 photo 1：选中的远端配方卡应可见
      await page.locator('.editor .nav .nb').first().click();
      await page.waitForSelector('[data-screen="grid"]', { timeout: 10_000 });
      await openEditorPhoto(page, manifest.portrait1Stem);
      await frame(page, 4);
      const onOpenVis = await readActiveCardVisibility();
      behaviour.recipeStripScrollOnOpen = onOpenVis;
      check(Boolean(onOpenVis && onOpenVis.visible && onOpenVis.scrollLeft > 0), `selected recipe card not scrolled into view on open: ${JSON.stringify(onOpenVis)}`);

      // 横滑到下一张：其选中的远端配方卡也应自动滚入可见区域
      const sbSwipe2 = await page.locator('.editor .stage').boundingBox();
      if (sbSwipe2) {
        await swipeStage(page, session, frame, sbSwipe2, true);
        await page.waitForFunction(
          (prev) => {
            const ed = document.querySelector('.screen.editor');
            const app = document.querySelector('.app');
            const cur = ed?.getAttribute('data-photo') ?? '';
            return Boolean(cur && cur !== prev && app?.getAttribute('data-settled') === cur);
          },
          manifest.portrait1Stem,
          { timeout: 30_000 },
        );
        await frame(page, 4);
        const onSwipeVis = await readActiveCardVisibility();
        behaviour.recipeStripScrollOnSwipe = onSwipeVis;
        check(Boolean(onSwipeVis && onSwipeVis.visible && onSwipeVis.scrollLeft > 0), `selected recipe card not scrolled into view after swipe: ${JSON.stringify(onSwipeVis)}`);
      }
    }
  }
}
