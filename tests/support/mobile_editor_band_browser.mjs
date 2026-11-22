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
  await page.waitForTimeout(340);
  await frame(page, 2);
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
        calls.push({ count: files.length, files });
      },
    });
  });
}

async function readReadyBar(page) {
  return page.evaluate(() => {
    const dock = document.querySelector('.pdock');
    const bar = document.querySelector('.editor .toolbar');
    const btn = document.querySelector('.editor .toolbar [data-act="export"]');
    const details = document.querySelector('.export-sheet') ?? document.querySelector('.pdock .ready-details');
    if (!dock || !bar || !btn) return null;
    const dr = dock.getBoundingClientRect();
    const br = bar.getBoundingClientRect();
    const er = btn.getBoundingClientRect();
    const detailsRect = details && !details.hidden ? details.getBoundingClientRect() : null;
    const detailsClears = !detailsRect || (detailsRect.bottom <= br.top + 0.5 && detailsRect.bottom <= er.top + 0.5);
    return {
      phase: dock.dataset.phase ?? '',
      compact: dock.classList.contains('ready-compact'),
      height: Math.round(dr.height * 10) / 10,
      top: Math.round(dr.top * 10) / 10,
      bottom: Math.round(dr.bottom * 10) / 10,
      toolbarTop: Math.round(br.top * 10) / 10,
      clearsToolbar: dr.bottom <= br.top + 0.5 && dr.bottom <= er.top + 0.5 && detailsClears,
      detailsVisible: Boolean(detailsRect && detailsRect.height > 0),
    };
  });
}

async function snap(page, dir, name) {
  if (!dir) return;
  const stems = [...STEM_ALIAS.keys()].filter(Boolean);
  const restored = await page.evaluate((stemList) => {
    const edits = [];
    const root = document.querySelector('.app');
    if (!root) return edits;
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    const dateRe = /\b\d{4}[-/.:]\d{2}[-/.:]\d{2}(?:\s+\d{2}:\d{2}(?::\d{2})?)?\b/g;
    const timeRe = /\b\d{2}:\d{2}(?::\d{2})?\b/g;
    let node;
    while ((node = walker.nextNode())) {
      const orig = node.nodeValue ?? '';
      let next = orig;
      for (const stem of stemList) {
        if (next.includes(stem)) next = next.replaceAll(stem, 'PHOTO');
        if (next.toLowerCase().includes(stem.toLowerCase())) next = next.replace(new RegExp(stem, 'gi'), 'PHOTO');
      }
      next = next.replace(dateRe, '2026-01-01').replace(timeRe, '12:00');
      if (next !== orig) {
        edits.push({ node, orig });
        node.nodeValue = next;
      }
    }
    window.__snapRestore = () => {
      for (const { node: n, orig } of edits) n.nodeValue = orig;
    };
    return edits.length;
  }, stems);
  await frame(page, 2);
  const out = join(dir, `${name}.png`);
  await page.screenshot({ path: out });
  if (restored > 0) {
    await page.evaluate(() => {
      window.__snapRestore?.();
      delete window.__snapRestore;
    });
  }
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

  // U1+U2 合并验收：就绪条浮在底栏上方（折叠 <= 52pt、单行、不遮挡底栏与「存到相册」、折叠与展开均 0px 不改照片矩形）
  const beforeReady = await measurePhoto(page, pAspect);
  await clickVisible(page, '.editor .toolbar [data-act="export"]', `${vp.label} quick export`);
  await page.waitForTimeout(150);
  check(!(await page.locator('.ms.export').isVisible().catch(() => false)), `${vp.label}: 存到相册 opened .ms.export sheet instead of quick export`);
  await page.waitForFunction(() => document.querySelector('.pdock')?.dataset.phase === 'ready', null, { timeout: 300_000 });
  await frame(page, 3);
  const readyCollapsed = await readReadyBar(page);
  const photoWithReadyCollapsed = await measurePhoto(page, pAspect);
  const readyCollapsedShift = r1(rectDiff(beforeReady, photoWithReadyCollapsed));
  check(Boolean(readyCollapsed && readyCollapsed.height <= 52), `${vp.label}: collapsed ready bar height=${readyCollapsed?.height}, want <= 52`);
  check(Boolean(readyCollapsed?.clearsToolbar), `${vp.label}: collapsed ready bar covers bottom bar (${JSON.stringify(readyCollapsed)})`);
  check(readyCollapsedShift <= RECT_SLOP, `${vp.label}: photo rect shifted ${readyCollapsedShift}px with ready bar collapsed`);

  let readyExpanded = null;
  let readyExpandedShift = 0;
  if (await page.locator('.pdock [data-act="toggle-details"]').count()) {
    await clickVisible(page, '.pdock [data-act="toggle-details"]', `${vp.label} ready details expand`);
    await frame(page, 3);
    readyExpanded = await readReadyBar(page);
    const photoWithReadyExpanded = await measurePhoto(page, pAspect);
    readyExpandedShift = r1(rectDiff(beforeReady, photoWithReadyExpanded));
    check(Boolean(readyExpanded?.detailsVisible), `${vp.label}: ready details drawer did not open`);
    check(Boolean(readyExpanded?.clearsToolbar), `${vp.label}: expanded ready bar covers bottom bar (${JSON.stringify(readyExpanded)})`);
    check(readyExpandedShift <= RECT_SLOP, `${vp.label}: photo rect shifted ${readyExpandedShift}px with ready bar expanded`);
  } else {
    check(false, `${vp.label}: ready bar missing toggle-details button for RAW+HDR export`);
  }
  await clickVisible(page, '.export-sheet [data-act="share"]', `${vp.label} share ready bar`);
  await page.waitForFunction(() => !document.querySelector('.pdock'), null, { timeout: 15_000 });

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
    readyBar: {
      collapsed: readyCollapsed,
      collapsedPhotoShift: readyCollapsedShift,
      expanded: readyExpanded,
      expandedPhotoShift: readyExpandedShift,
    },
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
      await swipeStage(page, session, sbAdv, true);
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
      await swipeStage(page, session, sbSwipe, true);
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
        await swipeStage(page, session, sbSwipe2, true);
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

async function runScreenshots(page, session, dir) {
  mkdirSync(dir, { recursive: true });
  for (const vp of VIEWPORTS) {
    await page.setViewportSize({ width: vp.width, height: vp.height });
    await setSafeArea(page, vp.safeTop, vp.safeBottom);
    await openEditorPhoto(page, manifest.portrait1Stem);
    if ((await measurePhoto(page))?.bandCollapsed === '1') await tapStageCenter(page, session);
    await clickVisible(page, '.editor .toolbar [data-tool="recipe"]', 'snap ensure recipe');

    // 1 & 2. 配方 + 就绪条折叠 / 展开
    await clickVisible(page, '.editor .toolbar [data-act="export"]', 'snap ready bar export');
    await page.waitForFunction(() => document.querySelector('.pdock')?.dataset.phase === 'ready', null, { timeout: 300_000 });
    await frame(page, 3);
    await snap(page, dir, `${vp.label}-01-recipe-ready-collapsed`);
    if (await page.locator('.pdock [data-act="toggle-details"]').count()) {
      await clickVisible(page, '.pdock [data-act="toggle-details"]', 'snap ready bar expand');
      await frame(page, 3);
    }
    await snap(page, dir, `${vp.label}-02-recipe-ready-expanded`);
    await clickVisible(page, '.export-sheet [data-act="share"], .pdock [data-act="share"]', 'snap ready bar share dismiss');
    await page.waitForFunction(() => !document.querySelector('.pdock'), null, { timeout: 15_000 });

    // 3. 调整
    await clickVisible(page, '.editor .toolbar [data-tool="adjust"]', 'snap adjust');
    await snap(page, dir, `${vp.label}-03-adjust`);

    // 4. 收起单带 + 长按原图（顶栏第二行显示「原图」，HDR 常驻可见）
    await clickVisible(page, '.editor .toolbar [data-tool="recipe"]', 'snap back recipe for collapsed');
    await tapStageCenter(page, session);
    const sb = await page.locator('.editor .stage').boundingBox();
    if (sb) {
      const cx = sb.x + sb.width / 2;
      const cy = sb.y + sb.height / 2;
      await session.send('Input.dispatchTouchEvent', { type: 'touchStart', touchPoints: [{ x: cx, y: cy, id: 1 }] });
      await page.waitForTimeout(550);
      await frame(page, 2);
      await snap(page, dir, `${vp.label}-04-collapsed-longpress-orig`);
      await session.send('Input.dispatchTouchEvent', { type: 'touchEnd', touchPoints: [] });
      await frame(page, 2);
    }
    await tapStageCenter(page, session);

    // 5a. 顶栏 ⋯
    await clickVisible(page, '.editor .nav [data-act="editor-more"]', 'snap top ⋯');
    await snap(page, dir, `${vp.label}-05a-top-more`);
    await clickVisible(page, '.ms[data-state="editor-more"] .nb', 'snap close top ⋯');

    // 5b. 底栏 更多
    await clickVisible(page, '.editor .toolbar [data-tool="more"]', 'snap bottom 更多');
    await snap(page, dir, `${vp.label}-05b-bottom-more`);
    await clickVisible(page, '.more-sheet [data-act="close"], .more-sheet .nb', 'snap close bottom 更多');
  }
}

try {
  const context = await browser.newContext({ viewport: { width: 390, height: 844 }, deviceScaleFactor: 2, hasTouch: true, isMobile: true });
  const page = await context.newPage();
  await installShareMock(page);
  const session = await context.newCDPSession(page);
  page.on('pageerror', (err) => pageErrors.push(redact(err.message || String(err))));
  await page.goto(base, { waitUntil: 'domcontentloaded' });
  await page.waitForSelector('.app[data-core="1"]', { timeout: 60_000 });
  await page.evaluate(() => localStorage.clear());
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
