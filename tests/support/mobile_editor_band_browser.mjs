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
import { runBehaviourChecks } from './mobile_editor_band_behaviour.mjs';
import { runReachability } from './mobile_editor_band_reachability.mjs';

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
    // 起始页没有校准卡了：等导入的包真的用上（不是内置的默认校准）。
    await page.waitForFunction(() => window.__gxrBook?.pack?.kind === 'user' && !window.__gxrBook.packBusy, null, { timeout: 60_000 });
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
  const stepCtx = { manifest, check, r1, frame, setSafeArea, measurePhoto, clickVisible, tapStageCenter, openEditorPhoto, paths, behaviour, CURVE_BOX_SLOP };
  for (const vp of VIEWPORTS) await runStep(`rect:${vp.label}`, () => runPhotoRectChecks(page, session, vp));
  if (manifest.screenshotsDir) await runStep('screenshots', () => runScreenshots(page, session, manifest.screenshotsDir));
  await runStep('reachability', () => runReachability(page, session, stepCtx));
  await runStep('behaviour', () => runBehaviourChecks(page, session, stepCtx));
  check(pageErrors.length === 0, `pageErrors: ${pageErrors.join(' | ')}`);
  console.log(JSON.stringify({ ok: failures.length === 0, failures, pageErrors, photoRects, paths, behaviour, screenshots }));
  process.exitCode = failures.length === 0 ? 0 : 1;
} finally {
  await browser.close();
}
