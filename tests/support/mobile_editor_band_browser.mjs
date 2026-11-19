// 手机单带编辑器（U2 phone-editor-band）浏览器验收测试：
// 1. 照片矩形测量（390×844 与 440×956，card250 实拍竖图与横图，含主屏安全区与默认视口）：
//    竖图展开 ≥ 56%、收起 ≥ 69%；横图满宽；拖滑块前/中/后与切工具/边框照片矩形不变；黑底无遮挡。
// 2. 可达性驱动：每项既有功能走可见控件并记录点击路径。
// 3. 行为：点照片收起且跨下一张记住；下一张回到配方；双指捏合缩放与放大后单指拖移不切图、双击复原；长按看原图；曲线拖动盒子不挪位。
// 4. 可选截图输出（仅当 manifest.screenshotsDir 非空时，隐藏照片名与日期后截各工具与收起态）。
// stdout 只有一行 JSON，不含照片名、拍摄时间或 EXIF。

import { mkdirSync, readFileSync } from 'node:fs';
import { join } from 'node:path';
import { pathToFileURL } from 'node:url';

const [runtime, base, manifestPath] = process.argv.slice(2);
const manifest = JSON.parse(readFileSync(manifestPath, 'utf8'));
const { launchPinnedChromium, verifyPinnedRuntime } = await import(pathToFileURL(runtime).href);
verifyPinnedRuntime();

const VIEWPORTS = [
  { label: '390x844', width: 390, height: 844, safeTop: 47, safeBottom: 34 },
  { label: '440x956', width: 440, height: 956, safeTop: 62, safeBottom: 34 },
];
const RECT_SLOP = 1.0;
const CURVE_BOX_SLOP = 1.0;

const browser = await launchPinnedChromium({
  args: ['--enable-unsafe-webgpu', '--enable-unsafe-swiftshader', '--use-angle=swiftshader', '--use-webgpu-adapter=swiftshader'],
});

const failures = [];
const pageErrors = [];
const paths = {};
const photoRects = {};
const behaviour = {};
const screenshots = [];

const STEM_ALIAS = new Map([
  [manifest.portrait1Stem, 'portrait1'],
  [manifest.landscapeStem, 'landscape'],
  [manifest.portrait2Stem, 'portrait2'],
]);

function redact(text) {
  let out = String(text).replace(/\/(?:Volumes|Users|private|var\/folders|tmp)\/\S+/g, '[path]');
  for (const [stem, alias] of STEM_ALIAS) {
    if (stem) out = out.replaceAll(stem, alias);
  }
  return out.slice(0, 260);
}

function check(ok, msg) {
  if (!ok) failures.push(redact(msg));
  return Boolean(ok);
}

function r1(n) {
  return Math.round(n * 10) / 10;
}

async function frame(page, n = 2) {
  await page.evaluate(
    (count) =>
      new Promise((done) => {
        const step = (left) => (left <= 0 ? done() : requestAnimationFrame(() => step(left - 1)));
        step(count);
      }),
    n,
  );
}

async function setSafeArea(page, topPx, bottomPx) {
  await page.evaluate(
    ({ top, bottom }) => {
      document.documentElement.style.setProperty('--safe-top', `${top}px`);
      document.documentElement.style.setProperty('--safe-bottom', `${bottom}px`);
    },
    { top: topPx, bottom: bottomPx },
  );
  await frame(page, 3);
}

async function waitSettled(page, stem) {
  await page.waitForFunction(
    (want) => {
      const app = document.querySelector('.app');
      const ed = document.querySelector('.screen.editor');
      return Boolean(app && ed && ed.getAttribute('data-photo') === want && app.getAttribute('data-settled') === want);
    },
    stem,
    { timeout: 60_000 },
  );
  await frame(page, 4);
}

async function measurePhoto(page, aspect = null) {
  return page.evaluate((imgAspect) => {
    const stage = document.querySelector('.editor .stage');
    const pv = document.querySelector('.editor .stage .pv');
    const media = pv?.querySelector('canvas.gpu:not([hidden]), canvas.still:not([hidden]), img:not([hidden])');
    if (!stage || !pv || !media) return null;
    const sRect = stage.getBoundingClientRect();
    const mRect = media.getBoundingClientRect();
    let x = mRect.left;
    let y = mRect.top;
    let w = mRect.width;
    let h = mRect.height;
    const nw = imgAspect ? imgAspect : media instanceof HTMLCanvasElement ? media.width : media instanceof HTMLImageElement ? media.naturalWidth : 0;
    const nh = imgAspect ? 1 : media instanceof HTMLCanvasElement ? media.height : media instanceof HTMLImageElement ? media.naturalHeight : 0;
    if (nw > 0 && nh > 0 && w > 0 && h > 0) {
      const scale = Math.min(w / nw, h / nh);
      const dw = nw * scale;
      const dh = nh * scale;
      x += (w - dw) / 2;
      y += (h - dh) / 2;
      w = dw;
      h = dh;
    }
    const vw = window.innerWidth;
    const vh = window.innerHeight;
    const pct = vw > 0 && vh > 0 ? ((w * h) / (vw * vh)) * 100 : 0;
    const band = document.querySelector('.editor .band');
    const bandRect = band ? band.getBoundingClientRect() : null;
    const toolbar = document.querySelector('.editor .toolbar');
    const barRect = toolbar ? toolbar.getBoundingClientRect() : null;
    const stageLbl = stage.querySelector('.lbl');
    const stageLblRect = stageLbl ? stageLbl.getBoundingClientRect() : null;
    const stageBg = getComputedStyle(stage).backgroundColor;
    return {
      x: Math.round(x * 10) / 10,
      y: Math.round(y * 10) / 10,
      w: Math.round(w * 10) / 10,
      h: Math.round(h * 10) / 10,
      pct: Math.round(pct * 10) / 10,
      stageW: Math.round(sRect.width * 10) / 10,
      stageH: Math.round(sRect.height * 10) / 10,
      stageBg,
      stageHasLbl: Boolean(stageLbl && stageLblRect && stageLblRect.height > 0 && stageLbl.textContent.trim()),
      bandH: bandRect ? Math.round(bandRect.height * 10) / 10 : null,
      barH: barRect ? Math.round(barRect.height * 10) / 10 : null,
      bandCollapsed: band ? band.getAttribute('data-collapsed') : null,
      bandTool: band ? band.getAttribute('data-tool') : null,
    };
  }, aspect);
}

function rectDiff(a, b) {
  if (!a || !b) return 9999;
  return Math.max(Math.abs(a.x - b.x), Math.abs(a.y - b.y), Math.abs(a.w - b.w), Math.abs(a.h - b.h));
}

async function clickVisible(page, selector, label, timeout = 3000) {
  const loc = typeof selector === 'string' ? page.locator(selector).first() : selector.first();
  try {
    await loc.waitFor({ state: 'visible', timeout });
    await loc.scrollIntoViewIfNeeded().catch(() => {});
    const box = await loc.boundingBox();
    if (!box || box.width < 1 || box.height < 1) {
      check(false, `${label}: zero bounding box`);
      return false;
    }
    await loc.click({ timeout });
    await frame(page, 2);
    return true;
  } catch (err) {
    check(false, `${label}: not clickable (${err.message.split('\n')[0]})`);
    return false;
  }
}

async function tapStageCenter(page, session) {
  const pt = await page.evaluate(() => {
    const s = document.querySelector('.editor .stage');
    if (!s) return null;
    const r = s.getBoundingClientRect();
    return { x: r.left + r.width / 2, y: r.top + r.height / 2 };
  });
  if (!pt) return false;
  await session.send('Input.dispatchTouchEvent', { type: 'touchStart', touchPoints: [{ x: pt.x, y: pt.y, id: 1 }] });
  await frame(page, 1);
  await session.send('Input.dispatchTouchEvent', { type: 'touchEnd', touchPoints: [] });
  await frame(page, 3);
  return true;
}

async function openEditorPhoto(page, stem) {
  const inEditor = await page.locator('.screen.editor').isVisible().catch(() => false);
  if (inEditor) {
    const cur = await page.locator('.screen.editor').getAttribute('data-photo').catch(() => '');
    if (cur === stem) {
      await waitSettled(page, stem);
      return;
    }
    const back = page.locator('.editor .nav .nb').first();
    if (await back.isVisible().catch(() => false)) {
      await back.click();
      await page.waitForSelector('[data-screen="grid"]', { timeout: 10_000 });
    }
  }
  const cell = page.locator(`.cell[data-id="${stem.toLowerCase()}"]`).first();
  await cell.waitFor({ state: 'visible', timeout: 15_000 });
  await cell.click();
  await waitSettled(page, stem);
}

async function snap(page, dir, name) {
  if (!dir) return;
  await page.evaluate(() => {
    const title = document.querySelector('.editor .nav .t b');
    const sub = document.querySelector('.editor .nav .t small');
    if (title) title.textContent = 'PHOTO';
    if (sub) sub.textContent = '2026-01-01';
  });
  await frame(page, 2);
  const out = join(dir, `${name}.png`);
  await page.screenshot({ path: out });
  screenshots.push(`${name}.png`);
}

async function runPhotoRectChecks(page, session, vp) {
  await page.setViewportSize({ width: vp.width, height: vp.height });
  await setSafeArea(page, vp.safeTop, vp.safeBottom);
  await openEditorPhoto(page, manifest.portrait1Stem);

  const pAspect = manifest.portraitAspect || 2 / 3;
  const lAspect = manifest.landscapeAspect || 3 / 2;

  // 确保展开态开始
  const init = await measurePhoto(page, pAspect);
  if (init?.bandCollapsed === '1') {
    await tapStageCenter(page, session);
  }
  await frame(page, 3);

  const openPwa = await measurePhoto(page, pAspect);
  check(Boolean(openPwa), `${vp.label}: missing photo media`);
  if (!openPwa) return;
  check(openPwa.stageBg === 'rgb(0, 0, 0)', `${vp.label}: stage background is ${openPwa.stageBg}, want rgb(0, 0, 0)`);
  check(!openPwa.stageHasLbl, `${vp.label}: stage .lbl still drawn over photo`);
  check(openPwa.bandH !== null && Math.abs(openPwa.bandH - 128) <= 1, `${vp.label}: band height is ${openPwa.bandH}, want 128`);
  check(openPwa.pct >= 56, `${vp.label} portrait open (PWA safe): ${openPwa.pct}% < 56%`);

  // 切四个工具 + 边框，测照片矩形不变
  const toolRects = { recipe: openPwa };
  const switchTool = async (key, fn) => {
    const ok = await fn();
    if (!ok) return;
    await frame(page, 3);
    const m = await measurePhoto(page, pAspect);
    toolRects[key] = m;
    const diff = rectDiff(openPwa, m);
    check(diff <= RECT_SLOP, `${vp.label}: photo rect shifted ${r1(diff)}px when switching to ${key}`);
  };

  await switchTool('adjust', () => clickVisible(page, '.editor .toolbar [data-tool="adjust"]', `${vp.label} tool adjust`));

  // 拖滑块前/中/后测照片矩形不变
  const beforeDrag = await measurePhoto(page, pAspect);
  const sliderLoc = page.locator('.editor .band .panel.adjust input[type="range"]').first();
  let duringDrag = null;
  let afterDrag = null;
  if (await sliderLoc.isVisible().catch(() => false)) {
    const sb = await sliderLoc.boundingBox();
    if (sb && sb.width > 20) {
      const y = sb.y + sb.height / 2;
      const x0 = sb.x + sb.width * 0.5;
      const x1 = sb.x + sb.width * 0.72;
      await session.send('Input.dispatchTouchEvent', { type: 'touchStart', touchPoints: [{ x: x0, y, id: 1 }] });
      await frame(page, 2);
      await session.send('Input.dispatchTouchEvent', { type: 'touchMove', touchPoints: [{ x: x1, y, id: 1 }] });
      await frame(page, 2);
      duringDrag = await measurePhoto(page, pAspect);
      await session.send('Input.dispatchTouchEvent', { type: 'touchEnd', touchPoints: [] });
      await frame(page, 2);
      afterDrag = await measurePhoto(page, pAspect);
      check(rectDiff(beforeDrag, duringDrag) <= RECT_SLOP, `${vp.label}: photo rect moved ${r1(rectDiff(beforeDrag, duringDrag))}px during slider drag`);
      check(rectDiff(beforeDrag, afterDrag) <= RECT_SLOP, `${vp.label}: photo rect moved ${r1(rectDiff(beforeDrag, afterDrag))}px after slider drag`);
    }
  } else {
    check(false, `${vp.label}: adjust slider not visible in .band`);
  }

  await switchTool('crop', () => clickVisible(page, '.editor .toolbar [data-tool="crop"]', `${vp.label} tool crop`));

  // 更多 -> 边框
  await switchTool('frame', async () => {
    const okMore = await clickVisible(page, '.editor .toolbar [data-tool="more"]', `${vp.label} tool more`);
    if (!okMore) return false;
    return clickVisible(page, '.more-sheet [data-more="frame"]', `${vp.label} more->frame`);
  });

  // 回到配方
  await switchTool('recipeBack', () => clickVisible(page, '.editor .toolbar [data-tool="recipe"]', `${vp.label} back to recipe`));

  // 点照片收起带子，测竖图 ≥ 69%
  await tapStageCenter(page, session);
  const collapsedPwa = await measurePhoto(page, pAspect);
  check(collapsedPwa?.bandCollapsed === '1', `${vp.label}: tap photo did not collapse band`);
  check((collapsedPwa?.pct ?? 0) >= 69, `${vp.label} portrait collapsed (PWA safe): ${collapsedPwa?.pct}% < 69%`);

  // 也在默认 0 safe-area 下测一次
  await setSafeArea(page, 0, 0);
  const collapsedZero = await measurePhoto(page, pAspect);
  await tapStageCenter(page, session);
  const openZero = await measurePhoto(page, pAspect);
  check((openZero?.pct ?? 0) >= 56, `${vp.label} portrait open (0 safe): ${openZero?.pct}% < 56%`);
  check((collapsedZero?.pct ?? 0) >= 69, `${vp.label} portrait collapsed (0 safe): ${collapsedZero?.pct}% < 69%`);

  // 恢复 PWA safe-area，测横图满宽
  await setSafeArea(page, vp.safeTop, vp.safeBottom);
  await openEditorPhoto(page, manifest.landscapeStem);
  const landOpen = await measurePhoto(page, lAspect);
  check(Math.abs((landOpen?.w ?? 0) - vp.width) <= 1.5, `${vp.label} landscape open width=${landOpen?.w}, want ${vp.width}`);
  await tapStageCenter(page, session);
  const landCollapsed = await measurePhoto(page, lAspect);
  check(Math.abs((landCollapsed?.w ?? 0) - vp.width) <= 1.5, `${vp.label} landscape collapsed width=${landCollapsed?.w}, want ${vp.width}`);
  // 展开回正常状态
  if (landCollapsed?.bandCollapsed === '1') await tapStageCenter(page, session);

  photoRects[vp.label] = {
    pwaSafe: { top: vp.safeTop, bottom: vp.safeBottom },
    portraitOpen: openPwa,
    portraitCollapsed: collapsedPwa,
    portraitOpenZeroSafe: openZero,
    portraitCollapsedZeroSafe: collapsedZero,
    landscapeOpen: landOpen,
    landscapeCollapsed: landCollapsed,
    dragDelta: {
      during: r1(rectDiff(beforeDrag, duringDrag)),
      after: r1(rectDiff(beforeDrag, afterDrag)),
    },
    toolDeltas: {
      adjust: r1(rectDiff(openPwa, toolRects.adjust)),
      crop: r1(rectDiff(openPwa, toolRects.crop)),
      frame: r1(rectDiff(openPwa, toolRects.frame)),
    },
  };
}

async function runReachability(page, session) {
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
    const hasSpace = await page.locator('.editor .band [data-act="lut-space"]').isVisible().catch(() => false);
    const hasImport = await page.locator('.editor .band [data-act="lut-import"]').isVisible().catch(() => false);
    check(hasSpace && hasImport, `LUT head controls missing: space=${hasSpace} import=${hasImport}`);
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

    // 直方图开关：默认关闭，点图标打开，再点关闭
    const histVisible0 = await page.locator('.editor .stage .hist-float').isVisible().catch(() => false);
    check(!histVisible0, 'floating histogram must be off by default');
    if (await clickVisible(page, '.editor .band .panel.adjust [data-act="hist-toggle"]', 'hist toggle on')) {
      const histVisible1 = await page.locator('.editor .stage .hist-float').isVisible().catch(() => false);
      check(histVisible1, 'floating histogram did not appear when toggled on');
      const hasClips = await page.locator('.editor .stage .hist-float .clip').count();
      check(hasClips === 2, `floating histogram clip indicators=${hasClips}, want 2`);
      await clickVisible(page, '.editor .band .panel.adjust [data-act="hist-toggle"]', 'hist toggle off');
      paths.histogram = ['底栏点「调整」', '点直方图图标开/关浮窗直方图'];
    }

    // 曲线 ›：打开更高抽屉并验证曲线拖动盒子不动
    if (await clickVisible(page, page.locator('.editor .band .panel.adjust button', { hasText: '曲线' }), 'open curves sheet')) {
      await page.waitForSelector('.editor svg.curve', { timeout: 5000 }).catch(() => {});
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
      await clickVisible(page, '.curves-sheet [data-act="close"], .curves-sheet .nb', 'close curves sheet');
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

  // 4. 裁剪：比例、拉直、横竖对调、参考线
  if (await clickVisible(page, '.editor .toolbar [data-tool="crop"]', 'reach crop')) {
    await clickVisible(page, page.locator('.editor .band .panel.crop-panel .chips .chip', { hasText: '3:2' }), 'crop ratio 3:2');
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

  // 6. 更多 -> 时间戳
  if (await clickVisible(page, '.editor .toolbar [data-tool="more"]', 'open more for stamp')) {
    if (await clickVisible(page, '.more-sheet [data-more="stamp"]', 'more -> stamp')) {
      check(await page.locator('.editor .band .switch').isVisible().catch(() => false), 'stamp band switch missing');
      if (await clickVisible(page, '.editor .band [data-act="stamp-sheet"]', 'open stamp detail sheet')) {
        check(await page.locator('.ms.stamp-sheet').isVisible().catch(() => false), 'stamp_sheet did not open');
        await closeSheet('.ms.stamp-sheet');
      }
      paths.stamp = ['底栏点「更多」', '点「时间戳」', '在带内开关或点「详细设置」开时间戳抽屉'];
    }
  }

  // 7. 复制调整
  if (await clickVisible(page, '.editor .toolbar [data-tool="more"]', 'open more for copy')) {
    const copyItem = page.locator('.more-sheet [data-act="copy"]');
    if (await copyItem.isVisible().catch(() => false)) {
      await copyItem.click();
      await frame(page, 2);
      check(await page.locator('.ms.paste-sheet[data-state="copy-options"]').isVisible().catch(() => false), 'copy sheet did not open');
      await closeSheet('.ms.paste-sheet[data-state="copy-options"]');
      paths.copy_paste = ['底栏点「更多」', '点「复制调整」（U1 顶栏 ⋯ 合并前通过「更多」直达）'];
    } else {
      await closeSheet('.more-sheet');
      paths.copy_paste = ['跳过：等待 U1 顶栏 ⋯ 合并'];
    }
  }

  // 8. 导出：底栏「存到相册」与「更多 → 导出选项」
  if (await clickVisible(page, '.editor .toolbar [data-act="export"]', 'toolbar 存到相册')) {
    check(await page.locator('.ms.export-sheet, .ms').first().isVisible().catch(() => false), '存到相册 did not open export sheet');
    await closeSheet('.ms');
  }
  if (await clickVisible(page, '.editor .toolbar [data-tool="more"]', 'open more for export')) {
    if (await clickVisible(page, '.more-sheet [data-more="export"]', 'more -> 导出选项')) {
      check(await page.locator('.ms.export-sheet, .ms').first().isVisible().catch(() => false), '更多 → 导出选项 did not open export sheet');
      await closeSheet('.ms');
      paths.export = ['底栏点「存到相册」', '或「更多 → 导出选项」'];
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

async function swipeStage(page, session, stageBox, preferNext = true) {
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

async function runBehaviourChecks(page, session) {
  await page.setViewportSize({ width: 390, height: 844 });
  await setSafeArea(page, 47, 34);
  await openEditorPhoto(page, manifest.portrait1Stem);
  if ((await measurePhoto(page))?.bandCollapsed === '1') await tapStageCenter(page, session);

  // 1. 切到「调整」，横滑到下一张：必须回到「配方」
  await clickVisible(page, '.editor .toolbar [data-tool="adjust"]', 'behaviour switch adjust');
  const stageBox = await page.locator('.editor .stage').boundingBox();
  if (stageBox) {
    await swipeStage(page, session, stageBox, true);
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
    await swipeStage(page, session, sb2, true);
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

  // 4. 长按显示原图
  const sb4 = await page.locator('.editor .stage').boundingBox();
  if (sb4) {
    const cx = sb4.x + sb4.width / 2;
    const cy = sb4.y + sb4.height / 2;
    await session.send('Input.dispatchTouchEvent', { type: 'touchStart', touchPoints: [{ x: cx, y: cy, id: 1 }] });
    await page.waitForTimeout(550);
    await frame(page, 2);
    const lblText = await page.locator('.editor .lbl').first().innerText().catch(() => '');
    await session.send('Input.dispatchTouchEvent', { type: 'touchEnd', touchPoints: [] });
    await frame(page, 2);
    behaviour.longPressShowsOriginal = lblText.includes('原图');
    check(lblText.includes('原图'), `long-press label was "${lblText}", expected 原图`);
  }
}

async function runScreenshots(page, session, dir) {
  mkdirSync(dir, { recursive: true });
  for (const vp of VIEWPORTS) {
    await page.setViewportSize({ width: vp.width, height: vp.height });
    await setSafeArea(page, vp.safeTop, vp.safeBottom);
    await openEditorPhoto(page, manifest.portrait1Stem);
    if ((await measurePhoto(page))?.bandCollapsed === '1') await tapStageCenter(page, session);
    await snap(page, dir, `${vp.label}-01-recipe`);
    await clickVisible(page, '.editor .band .panel.recipe .strip .rc.on', 'snap recipe finetune');
    await snap(page, dir, `${vp.label}-02-recipe-finetune`);
    await clickVisible(page, '.editor .band .panel.recipe [data-act="recipe-back"]', 'snap recipe back');
    await clickVisible(page, '.editor .band .panel.recipe [data-mode="lut"]', 'snap lut');
    await snap(page, dir, `${vp.label}-03-lut`);
    await clickVisible(page, '.editor .band .panel.lut [data-mode="recipe"]', 'snap lut back');
    await clickVisible(page, '.editor .toolbar [data-tool="adjust"]', 'snap adjust');
    await snap(page, dir, `${vp.label}-04-adjust`);
    await clickVisible(page, '.editor .band .panel.adjust [data-act="hist-toggle"]', 'snap adjust hist');
    await snap(page, dir, `${vp.label}-04b-adjust-hist`);
    await clickVisible(page, '.editor .band .panel.adjust [data-act="hist-toggle"]', 'snap adjust hist off');
    await clickVisible(page, page.locator('.editor .band .panel.adjust button', { hasText: '曲线' }), 'snap curves');
    await snap(page, dir, `${vp.label}-05-curves`);
    await clickVisible(page, '.curves-sheet [data-act="close"], .curves-sheet .nb', 'snap curves close');
    await clickVisible(page, '.editor .toolbar [data-tool="crop"]', 'snap crop');
    await snap(page, dir, `${vp.label}-06-crop`);
    await clickVisible(page, '.editor .toolbar [data-tool="more"]', 'snap more');
    await snap(page, dir, `${vp.label}-07-more`);
    await clickVisible(page, '.more-sheet [data-more="frame"]', 'snap frame');
    await snap(page, dir, `${vp.label}-08-frame`);
    await clickVisible(page, '.editor .toolbar [data-tool="more"]', 'snap more for stamp');
    await clickVisible(page, '.more-sheet [data-more="stamp"]', 'snap stamp');
    await snap(page, dir, `${vp.label}-08b-stamp`);
    await clickVisible(page, '.editor .toolbar [data-tool="recipe"]', 'snap back recipe');
    await tapStageCenter(page, session);
    await snap(page, dir, `${vp.label}-09-collapsed-portrait`);
    await tapStageCenter(page, session);
    await openEditorPhoto(page, manifest.landscapeStem);
    if ((await measurePhoto(page))?.bandCollapsed === '1') await tapStageCenter(page, session);
    await snap(page, dir, `${vp.label}-10-landscape`);
  }
}

try {
  const context = await browser.newContext({ viewport: { width: 390, height: 844 }, deviceScaleFactor: 2, hasTouch: true, isMobile: true });
  const page = await context.newPage();
  const session = await context.newCDPSession(page);
  page.on('pageerror', (err) => pageErrors.push(redact(err.message || String(err))));
  await page.goto(base, { waitUntil: 'domcontentloaded' });
  await page.waitForSelector('.app[data-core="1"]', { timeout: 60_000 });
  if (manifest.pack) {
    await page.locator('input.calibpick').setInputFiles(manifest.pack);
    await page.waitForSelector('.calib[data-calib="ready"]', { timeout: 60_000 });
  }
  await page.locator('input.filepick:not(.calibpick):not(.datpick)').first().setInputFiles(manifest.files);
  await page.waitForFunction(
    () => {
      const sub = document.querySelector('.nav small');
      return Boolean(sub && !sub.textContent.includes('正在读取') && document.querySelectorAll('.cell').length >= 3);
    },
    null,
    { timeout: 120_000 },
  );
  await frame(page, 3);
  const runStep = async (name, fn) => {
    try {
      await fn();
    } catch (err) {
      check(false, `${name}: ${redact(err instanceof Error ? err.message : String(err))}`);
    }
  };
  for (const vp of VIEWPORTS) await runStep(`rect:${vp.label}`, () => runPhotoRectChecks(page, session, vp));
  if (manifest.screenshotsDir) await runStep('screenshots', () => runScreenshots(page, session, manifest.screenshotsDir));
  await runStep('reachability', () => runReachability(page, session));
  await runStep('behaviour', () => runBehaviourChecks(page, session));
  check(pageErrors.length === 0, `pageErrors: ${pageErrors.join(' | ')}`);
  console.log(JSON.stringify({ ok: failures.length === 0, failures, pageErrors, photoRects, paths, behaviour, screenshots }));
  process.exitCode = failures.length === 0 ? 0 : 1;
} finally {
  await browser.close();
}
