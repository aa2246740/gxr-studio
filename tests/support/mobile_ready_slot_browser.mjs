// 手机导出条固定顶槽（U5 phone-ready-slot）真实浏览器验收脚本：
// - 双视口（390×844 / 440×956）实拍照片（card250 + g250）；
// - 全部 6 种活动导出条态（运行中 / 运行+已做好 / 单张就绪 / 两张就绪 / 部分失败 / 全部失败）+ 详情打开态下：
//   照片 0px 不移位、.pdock <= 44pt、工具控制层 84pt 全可达且 elementFromPoint 绝不被 .pdock 遮挡、命中高度 >= 44pt；
// - P2-1：活动导出态下 LUT「导入 LUT」「色彩空间」「状态提示」与配方状态瓷砖全可达；
// - P2-2：详情工作表在运行中连续重画保焦，Escape / 遮罩 / 收起关闭后还焦给重建后的 .pdock [data-act="toggle-details"]；
// - P3-1：拖动及键盘操作调参/拉直后 28pt 数值胶囊按时淡出；
// - P3-4：已保存部分成片后仅剩失败时，详情汇总为「1 张没做成」；
// - VoiceOver 区域/DOM 顺序/polite 播报 / 存进相册 vs 存到相册 aria-label 区分 / 250ms 防误触 / 收起贴底 / 网格贴底；
// - 可选脱敏实拍截图（GXR_READY_SLOT_SCREENSHOTS）。
// stdout 只有一行脱敏 JSON。

import { mkdirSync, readFileSync } from 'node:fs';
import { join } from 'node:path';
import { pathToFileURL } from 'node:url';
import {
  auditDeckHitTests,
  auditRecipeStatusInState,
  verifyDetailsFocusLifecycle,
  verifyKeyboardScrubFadeout,
  verifyLutDeckActions,
  verifySavedFailureSummary,
} from './mobile_ready_slot_checks.mjs';

const [runtime, base, manifestPath] = process.argv.slice(2);
const manifest = JSON.parse(readFileSync(manifestPath, 'utf8'));
const { launchPinnedChromium, verifyPinnedRuntime } = await import(pathToFileURL(runtime).href);
verifyPinnedRuntime();

const VIEWPORTS = [
  { label: '390x844', width: 390, height: 844, safeTop: 47, safeBottom: 34, maxCollapsedOverlap: 2.5 },
  { label: '440x956', width: 440, height: 956, safeTop: 62, safeBottom: 34, maxCollapsedOverlap: 0.5 },
];
const RECT_SLOP = 0.6;

const browser = await launchPinnedChromium({
  args: ['--enable-unsafe-webgpu', '--enable-unsafe-swiftshader', '--use-angle=swiftshader', '--use-webgpu-adapter=swiftshader'],
});

const failures = [];
const pageErrors = [];
const metrics = {};
const screenshots = [];

const STEM_ALIAS = new Map([
  [manifest.portrait1Stem, 'portrait1'],
  [manifest.landscapeStem, 'landscape'],
  [manifest.portrait2Stem, 'portrait2'],
  [manifest.jpgOnlyStem, 'jpgOnly'],
]);

function redact(text) {
  let out = String(text).replace(/\/(?:Volumes|Users|private|var\/folders|tmp)\/\S+/g, '[path]');
  for (const [stem, alias] of STEM_ALIAS) {
    if (stem) out = out.replaceAll(stem, alias);
  }
  return out.slice(0, 280);
}

function check(ok, msg) {
  if (!ok) failures.push(redact(msg));
  return Boolean(ok);
}

const r1 = (n) => Math.round(n * 10) / 10;

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
  await frame(page, 3);
}

async function openEditorPhoto(page, stem) {
  const inEditor = await page.locator('.screen.editor').count();
  if (inEditor) {
    const cur = await page.locator('.screen.editor').getAttribute('data-photo');
    if (cur === stem) {
      await waitSettled(page, stem);
      return;
    }
    await page.locator('.editor .nav .nb').first().click();
    await page.waitForSelector('[data-screen="grid"]', { timeout: 15_000 });
  }
  const cell = page.locator(`.cell[data-id="${stem.toLowerCase()}"]`);
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
        calls.push({
          count: files.length,
          files,
          userActive: navigator.userActivation ? navigator.userActivation.isActive : true,
        });
      },
    });
  });
}

async function installRunGate(page) {
  await page.evaluate(() => {
    const core = window.__devExport?.core;
    if (!core || core.__readySlotWrapped) return;
    core.__readySlotWrapped = true;
    const origRun = core.run.bind(core);
    window.__runGate = { hold: false, release: null };
    core.run = async (...args) => {
      if (window.__runGate.hold) {
        await new Promise((res) => {
          window.__runGate.release = res;
        });
      }
      return origRun(...args);
    };
  });
}

async function setHold(page, hold) {
  await page.evaluate((on) => {
    if (!window.__runGate) return;
    window.__runGate.hold = on;
    if (!on && window.__runGate.release) {
      const fn = window.__runGate.release;
      window.__runGate.release = null;
      fn();
    }
  }, hold);
}

async function installAnnouncerLog(page) {
  await page.evaluate(() => {
    window.__annLog = [];
    const obs = new MutationObserver((records) => {
      const touched = records.some((r) => {
        const el = r.target.nodeType === 3 ? r.target.parentElement : r.target;
        return el instanceof Element && el.matches('[data-role="export-announcer"]');
      });
      if (!touched) return;
      const el = document.querySelector('[data-role="export-announcer"]');
      const txt = el?.textContent?.trim() ?? '';
      if (txt) window.__annLog.push(txt);
    });
    obs.observe(document.body, { childList: true, subtree: true, characterData: true });
    window.__annObs = obs;
  });
}

async function measurePhoto(page, aspect) {
  return page.evaluate((imgAspect) => {
    const pv = document.querySelector('.editor .stage .pv');
    const media = pv?.querySelector('canvas.gpu:not([hidden]), canvas.still:not([hidden]), img:not([hidden])');
    if (!pv || !media) return null;
    const mRect = media.getBoundingClientRect();
    let x = mRect.left;
    let y = mRect.top;
    let w = mRect.width;
    let h = mRect.height;
    const nw = imgAspect || (media instanceof HTMLCanvasElement ? media.width : media.naturalWidth);
    const nh = imgAspect ? 1 : (media instanceof HTMLCanvasElement ? media.height : media.naturalHeight);
    if (nw > 0 && nh > 0 && w > 0 && h > 0) {
      const scale = Math.min(w / nw, h / nh);
      const dw = nw * scale;
      const dh = nh * scale;
      x += (w - dw) / 2;
      y += (h - dh) / 2;
      w = dw;
      h = dh;
    }
    const band = document.querySelector('.editor .band');
    const top = document.querySelector('.editor .band-top');
    const body = document.querySelector('.editor .band-body');
    const dock = document.querySelector('.pdock');
    const bar = document.querySelector('.editor .toolbar');
    const br = band?.getBoundingClientRect();
    const tr = top?.getBoundingClientRect();
    const byr = body?.getBoundingClientRect();
    const dr = dock?.getBoundingClientRect();
    const tbr = bar?.getBoundingClientRect();
    return {
      x: Math.round(x * 10) / 10,
      y: Math.round(y * 10) / 10,
      w: Math.round(w * 10) / 10,
      h: Math.round(h * 10) / 10,
      bottom: Math.round((y + h) * 10) / 10,
      bandH: br ? Math.round(br.height * 10) / 10 : null,
      bandTop: br ? Math.round(br.top * 10) / 10 : null,
      slotH: tr ? Math.round(tr.height * 10) / 10 : null,
      deckH: byr ? Math.round(byr.height * 10) / 10 : null,
      dockH: dr ? Math.round(dr.height * 10) / 10 : null,
      dockTop: dr ? Math.round(dr.top * 10) / 10 : null,
      dockBottom: dr ? Math.round(dr.bottom * 10) / 10 : null,
      dockMode: dock?.dataset.dockMode ?? null,
      visualState: dock?.dataset.visualState ?? null,
      toolbarTop: tbr ? Math.round(tbr.top * 10) / 10 : null,
      collapsed: band?.getAttribute('data-collapsed') ?? null,
    };
  }, aspect);
}

const rectDiff = (a, b) =>
  !a || !b ? 9999 : Math.max(Math.abs(a.x - b.x), Math.abs(a.y - b.y), Math.abs(a.w - b.w), Math.abs(a.h - b.h));

async function snap(page, dir, name) {
  if (!dir) return;
  const stems = [...STEM_ALIAS.keys()].filter(Boolean);
  await page.evaluate((stemList) => {
    const root = document.querySelector('.app');
    if (!root) return;
    window.__snapEdits = [];
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    const dateRe = /\b\d{4}[-/.:]\d{2}[-/.:]\d{2}(?:\s+\d{2}:\d{2}(?::\d{2})?)?\b/g;
    const cnDateRe = /\d{1,2}月\d{1,2}日/g;
    const timeRe = /\b\d{2}:\d{2}(?::\d{2})?\b/g;
    let node;
    while ((node = walker.nextNode())) {
      const orig = node.nodeValue ?? '';
      let next = orig;
      for (const stem of stemList) {
        if (next.toLowerCase().includes(stem.toLowerCase())) next = next.replace(new RegExp(stem, 'gi'), 'PHOTO');
      }
      next = next.replace(dateRe, '2026-01-01').replace(cnDateRe, '1月1日').replace(timeRe, '12:00');
      if (next !== orig) {
        window.__snapEdits.push({ node, orig });
        node.nodeValue = next;
      }
    }
  }, stems);
  const path = join(dir, `${name}.png`);
  await page.screenshot({ path, animations: 'disabled' });
  screenshots.push(name);
  await page.evaluate(() => {
    for (const { node, orig } of window.__snapEdits ?? []) node.nodeValue = orig;
    window.__snapEdits = [];
  });
}

async function tapStageCenter(page, session) {
  const box = await page.locator('.editor .stage').boundingBox();
  if (!box) return;
  const x = box.x + box.width / 2;
  const y = box.y + box.height / 2;
  await session.send('Input.dispatchTouchEvent', { type: 'touchStart', touchPoints: [{ x, y, id: 1 }] });
  await frame(page, 1);
  await session.send('Input.dispatchTouchEvent', { type: 'touchEnd', touchPoints: [] });
  await page.waitForTimeout(340);
  await frame(page, 3);
}

async function runViewport(page, session, vp, shotDir) {
  await page.setViewportSize({ width: vp.width, height: vp.height });
  await setSafeArea(page, vp.safeTop, vp.safeBottom);
  await openEditorPhoto(page, manifest.portrait1Stem);
  if ((await measurePhoto(page, manifest.portraitAspect))?.collapsed === '1') {
    await tapStageCenter(page, session);
  }
  await page.locator('.editor .toolbar [data-tool="adjust"]').click();
  await frame(page, 3);

  const idle = await measurePhoto(page, manifest.portraitAspect);
  check(Math.abs((idle?.bandH ?? 0) - 128) <= 0.6, `${vp.label} idle bandH=${idle?.bandH}, want 128`);
  check(Math.abs((idle?.slotH ?? 0) - 44) <= 0.6, `${vp.label} idle slotH=${idle?.slotH}, want 44`);
  check(Math.abs((idle?.deckH ?? 0) - 84) <= 0.6, `${vp.label} idle deckH=${idle?.deckH}, want 84`);

  const vpMetrics = { idle, states: {} };

  // Verify pinned recipe filter tile stays visible when strip scrolls horizontally
  await page.locator('.editor .toolbar [data-tool="recipe"]').click();
  await frame(page, 2);
  const pinCheck = await page.evaluate(() => {
    const strip = document.querySelector('.editor .panel.recipe .strip');
    const pin = document.querySelector('.editor .panel.recipe .recipe-filter-pin [data-act="recipe-filter"]');
    if (!strip || !pin) return null;
    const before = pin.getBoundingClientRect();
    strip.scrollLeft = 220;
    const after = pin.getBoundingClientRect();
    strip.scrollLeft = 0;
    return {
      w: Math.round(before.width * 10) / 10,
      h: Math.round(before.height * 10) / 10,
      moved: Math.abs(before.left - after.left),
    };
  });
  check(Boolean(pinCheck && pinCheck.moved <= 0.5 && pinCheck.h >= 44), `${vp.label} recipe filter tile not pinned: ${JSON.stringify(pinCheck)}`);

  // Verify Frame strip + fcontrols heights
  await page.locator('.editor .toolbar [data-tool="more"]').click();
  await frame(page, 2);
  await page.locator('.more-sheet [data-more="frame"]').click();
  await frame(page, 2);
  const frameStyle = page.locator('.editor .panel.frame .strip .rc:not([data-style="none"])').first();
  await frameStyle.click();
  await frame(page, 2);
  await page.locator('.editor .panel.frame .strip .rc.on').click();
  await frame(page, 2);
  const frameCtrlH = await page.evaluate(() => {
    const btns = [...document.querySelectorAll('.editor .panel.frame .fcontrols button')];
    return btns.length ? Math.min(...btns.map((b) => b.getBoundingClientRect().height)) : 0;
  });
  check(frameCtrlH >= 43.5, `${vp.label} frame fcontrols min height ${r1(frameCtrlH)} < 44`);
  await page.locator('.editor .panel.frame [data-act="frame-back"]').click();
  await frame(page, 1);
  await page.locator('.editor .panel.frame .strip .rc[data-style="none"]').click();
  await frame(page, 2);

  // Back to adjust
  await page.locator('.editor .toolbar [data-tool="adjust"]').click();
  await frame(page, 2);

  // Clear announcer log before export transitions
  await page.evaluate(() => {
    window.__annLog = [];
  });

  // === STATE 1: Running (0 finished) ===
  await setHold(page, true);
  await page.locator('.editor .toolbar [data-act="export"]').click();
  await page.waitForFunction(() => document.querySelector('.pdock')?.dataset.visualState === 'running', null, { timeout: 30_000 });
  await frame(page, 3);
  const mRun = await measurePhoto(page, manifest.portraitAspect);
  check(rectDiff(idle, mRun) <= RECT_SLOP, `${vp.label} running photo shift ${r1(rectDiff(idle, mRun))}px`);
  check((mRun?.dockH ?? 99) <= 44.5, `${vp.label} running pdock height ${mRun?.dockH} > 44`);
  check(mRun?.dockMode === 'slot', `${vp.label} running dockMode=${mRun?.dockMode}, want slot`);

  // VoiceOver region + DOM order + zero announcements while running
  const voRunning = await page.evaluate(() => {
    const dock = document.querySelector('.pdock');
    const body = document.querySelector('.editor .band-body');
    const expBtn = document.querySelector('.editor .toolbar [data-act="export"]');
    const ann = document.querySelector('[data-role="export-announcer"]');
    return {
      role: dock?.getAttribute('role'),
      label: dock?.getAttribute('aria-label'),
      beforeDeck: Boolean(dock && body && (dock.compareDocumentPosition(body) & Node.DOCUMENT_POSITION_FOLLOWING)),
      toolbarAria: expBtn?.getAttribute('aria-label'),
      liveMode: ann?.getAttribute('aria-live'),
      liveAtomic: ann?.getAttribute('aria-atomic'),
      annCountWhileRunning: (window.__annLog ?? []).length,
    };
  });
  check(voRunning.role === 'region' && voRunning.label === '导出状态', `${vp.label} VO region mismatch: ${JSON.stringify(voRunning)}`);
  check(voRunning.beforeDeck, `${vp.label} .pdock does not precede .band-body in DOM`);
  check(voRunning.toolbarAria === '导出当前照片并存到相册', `${vp.label} toolbar aria-label mismatch: ${voRunning.toolbarAria}`);
  check(voRunning.liveMode === 'polite' && voRunning.liveAtomic === 'true', `${vp.label} announcer attrs wrong: ${JSON.stringify(voRunning)}`);
  check(voRunning.annCountWhileRunning === 0, `${vp.label} announced during running: count=${voRunning.annCountWhileRunning}`);

  await snap(page, shotDir, `${vp.label}-01-adjust-running`);
  vpMetrics.states.running = { photoShift: r1(rectDiff(idle, mRun)), dockH: mRun?.dockH, hit: await auditDeckHitTests(page, `${vp.label}:running`, check, frame, true) };
  await verifyLutDeckActions(page, manifest.lutPath, `${vp.label}:running`, check, frame);
  await page.locator('.editor .toolbar [data-tool="recipe"]').click();
  await frame(page, 2);
  await page.locator('.editor .panel.recipe [data-mode="lut"]').click();
  await frame(page, 2);
  await snap(page, shotDir, `${vp.label}-12-lut-running`);
  await page.locator('.editor .panel.lut [data-mode="recipe"]').click();
  await frame(page, 2);
  await verifyDetailsFocusLifecycle(page, `${vp.label}:running`, check, frame);
  await verifyKeyboardScrubFadeout(page, `${vp.label}:running`, check, frame);
  await auditRecipeStatusInState(
    page,
    manifest.jpgOnlyStem,
    manifest.portrait1Stem,
    `${vp.label}:running`,
    openEditorPhoto,
    check,
    frame,
    () => snap(page, shotDir, `${vp.label}-13-recipe-status-running`),
  );
  await page.locator('.editor .toolbar [data-tool="adjust"]').click();
  await frame(page, 2);

  // === Live 28pt Scrub Pill during active export (Adjust + Crop + Recipe-fine) ===
  const sliderBox = await page.locator('.editor .panel.adjust input[type="range"]').boundingBox();
  if (sliderBox) {
    const sy = sliderBox.y + sliderBox.height / 2;
    await page.mouse.move(sliderBox.x + sliderBox.width * 0.5, sy);
    await page.mouse.down();
    await page.mouse.move(sliderBox.x + sliderBox.width * 0.72, sy, { steps: 6 });
    await frame(page, 2);
    const pillDuring = await page.evaluate(() => {
      const pill = document.querySelector('.editor .scrub-pill');
      const band = document.querySelector('.editor .band');
      if (!pill || !band) return null;
      const pr = pill.getBoundingClientRect();
      const br = band.getBoundingClientRect();
      return {
        visible: pill.dataset.visible,
        text: pill.textContent?.trim() ?? '',
        h: Math.round(pr.height * 10) / 10,
        gapAboveBand: Math.round((br.top - pr.bottom) * 10) / 10,
      };
    });
    check(
      Boolean(pillDuring && pillDuring.visible === '1' && pillDuring.h >= 24 && pillDuring.h <= 32 && Math.abs(pillDuring.gapAboveBand - 12) <= 2 && pillDuring.text.includes('曝光')),
      `${vp.label} adjust scrub pill mismatch: ${JSON.stringify(pillDuring)}`,
    );
    await snap(page, shotDir, `${vp.label}-04-adjust-scrub-pill`);
    await page.mouse.up();
    await page.waitForTimeout(550);
    const pillAfter = await page.evaluate(() => document.querySelector('.editor .scrub-pill')?.dataset.visible ?? '');
    check(pillAfter === '0', `${vp.label} scrub pill did not fade after 400ms (got ${pillAfter})`);
    vpMetrics.scrubPill = pillDuring;
    await page.locator('.editor .panel.adjust [data-act="reset-all"], .editor .panel.adjust button', { hasText: '全部重置' }).first().click().catch(() => {});
  }

  // Crop dial live scrub pill check
  await page.locator('.editor .toolbar [data-tool="crop"]').click();
  await frame(page, 2);
  const rulerBox = await page.locator('.editor .panel.crop-panel .ruler').boundingBox();
  if (rulerBox) {
    const ry = rulerBox.y + rulerBox.height / 2;
    await page.mouse.move(rulerBox.x + rulerBox.width * 0.5, ry);
    await page.mouse.down();
    await page.mouse.move(rulerBox.x + rulerBox.width * 0.65, ry, { steps: 4 });
    await frame(page, 2);
    const cropPill = await page.evaluate(() => ({
      visible: document.querySelector('.editor .scrub-pill')?.dataset.visible ?? '',
      text: document.querySelector('.editor .scrub-pill')?.textContent?.trim() ?? '',
    }));
    check(cropPill.visible === '1' && cropPill.text.includes('°'), `${vp.label} crop scrub pill mismatch: ${JSON.stringify(cropPill)}`);
    await page.mouse.up();
    await page.locator('.editor .panel.crop-panel .chips .chip', { hasText: '原始' }).click().catch(() => {});
  }
  await page.locator('.editor .toolbar [data-tool="adjust"]').click();
  await frame(page, 2);

  // Release gate -> real export finishes -> STATE 3: Ready (1 finished)
  await setHold(page, false);
  await page.waitForFunction(() => document.querySelector('.pdock')?.dataset.visualState === 'ready', null, { timeout: 180_000 });
  await frame(page, 3);
  const mReady1 = await measurePhoto(page, manifest.portraitAspect);
  check(rectDiff(idle, mReady1) <= RECT_SLOP, `${vp.label} ready1 photo shift ${r1(rectDiff(idle, mReady1))}px`);
  check((mReady1?.dockH ?? 99) <= 44.5, `${vp.label} ready1 pdock height ${mReady1?.dockH} > 44`);

  const voReady1 = await page.evaluate(() => {
    const shareBtn = document.querySelector('.pdock [data-act="share"]');
    return {
      shareAria: shareBtn?.getAttribute('aria-label') ?? '',
      annLog: [...(window.__annLog ?? [])],
    };
  });
  check(
    voReady1.shareAria.includes('存进相册') && voReady1.shareAria.includes('打开系统分享'),
    `${vp.label} ready share aria-label mismatch: ${voReady1.shareAria}`,
  );
  check(voReady1.annLog.length === 1 && voReady1.annLog[0].includes('做好了'), `${vp.label} ready announcer wrong: ${JSON.stringify(voReady1.annLog)}`);

  await snap(page, shotDir, `${vp.label}-02-adjust-ready`);
  vpMetrics.states.ready1 = { photoShift: r1(rectDiff(idle, mReady1)), dockH: mReady1?.dockH, hit: await auditDeckHitTests(page, `${vp.label}:ready1`, check, frame) };
  await auditRecipeStatusInState(page, manifest.jpgOnlyStem, manifest.portrait1Stem, `${vp.label}:ready1`, openEditorPhoto, check, frame);

  // Crop & Stamp ready screenshots at 390/440
  await page.locator('.editor .toolbar [data-tool="crop"]').click();
  await frame(page, 2);
  await snap(page, shotDir, `${vp.label}-05-crop-ready`);
  await page.locator('.editor .toolbar [data-tool="more"]').click();
  await frame(page, 2);
  await page.locator('.more-sheet [data-more="stamp"]').click();
  await frame(page, 2);
  await snap(page, shotDir, `${vp.label}-06-stamp-ready`);

  // === 250ms drag-end accidental tap guard ===
  await page.locator('.editor .toolbar [data-tool="adjust"]').click();
  await frame(page, 2);
  const shareBeforeGuard = await page.evaluate(() => window.__shareLog.calls.length);
  const sb2 = await page.locator('.editor .panel.adjust input[type="range"]').boundingBox();
  if (sb2) {
    const sy = sb2.y + sb2.height / 2;
    await page.mouse.move(sb2.x + sb2.width * 0.5, sy);
    await page.mouse.down();
    await page.mouse.move(sb2.x + sb2.width * 0.6, sy, { steps: 3 });
    await page.mouse.up();
    // Click immediately (< 250ms, bypassing Playwright multi-frame actionability wait) -> MUST be guarded!
    await page.evaluate(() => {
      document.querySelector('.pdock [data-act="share"]')?.click();
    });
    await page.waitForTimeout(80);
    const shareGuarded = await page.evaluate(() => window.__shareLog.calls.length);
    check(shareGuarded === shareBeforeGuard, `${vp.label} 250ms drag guard failed: share fired immediately`);
  }
  // Wait > 250ms and verify export bar responds normally again
  await page.waitForTimeout(350);
  await page.locator('.pdock [data-act="toggle-details"]').click();
  await page.waitForSelector('.export-sheet', { timeout: 5_000 });
  await page.locator('.export-sheet [data-act="close-details"]').click();
  await frame(page, 2);
  await page.locator('.editor .panel.adjust button', { hasText: '全部重置' }).first().click().catch(() => {});
  await frame(page, 2);

  // === STATE 2: Running + some finished (switch to portrait2, export with hold) ===
  await openEditorPhoto(page, manifest.portrait2Stem);
  await page.locator('.editor .toolbar [data-tool="recipe"]').click();
  await frame(page, 2);
  await setHold(page, true);
  await page.locator('.editor .toolbar [data-act="export"]').click();
  await page.waitForFunction(() => document.querySelector('.pdock')?.dataset.visualState === 'running-ready', null, { timeout: 30_000 });
  await frame(page, 3);
  const mRunReady = await measurePhoto(page, manifest.portraitAspect);
  check((mRunReady?.dockH ?? 99) <= 44.5, `${vp.label} running-ready pdock height ${mRunReady?.dockH} > 44`);
  check(
    (await page.locator('.pdock [data-act="share-ready"]').count()) === 1,
    `${vp.label} running-ready missing direct share-ready button`,
  );
  await snap(page, shotDir, `${vp.label}-03-recipe-running-ready`);
  vpMetrics.states.runningReady = { dockH: mRunReady?.dockH, hit: await auditDeckHitTests(page, `${vp.label}:running-ready`, check, frame) };
  await auditRecipeStatusInState(page, manifest.jpgOnlyStem, manifest.portrait2Stem, `${vp.label}:running-ready`, openEditorPhoto, check, frame);

  // Release -> STATE 4: Two ready
  await setHold(page, false);
  await page.waitForFunction(
    () => document.querySelector('.pdock')?.dataset.visualState === 'ready' && (document.querySelector('.pdock')?.textContent ?? '').includes('2 张做好了'),
    null,
    { timeout: 180_000 },
  );
  await frame(page, 3);
  const mReady2 = await measurePhoto(page, manifest.portraitAspect);
  check((mReady2?.dockH ?? 99) <= 44.5, `${vp.label} ready2 pdock height ${mReady2?.dockH} > 44`);
  vpMetrics.states.ready2 = { dockH: mReady2?.dockH, hit: await auditDeckHitTests(page, `${vp.label}:ready2`, check, frame) };
  await auditRecipeStatusInState(page, manifest.jpgOnlyStem, manifest.portrait2Stem, `${vp.label}:ready2`, openEditorPhoto, check, frame);

  // === Details Modal Sheet (role=dialog, <= 240pt, 0px shift, focus trap, Escape, scrim, direct sync share) ===
  const beforeSheet = await measurePhoto(page, manifest.portraitAspect);
  await page.locator('.pdock [data-act="toggle-details"]').click();
  await page.waitForSelector('.export-sheet');
  await frame(page, 3);
  const sheetInfo = await page.evaluate(() => {
    const sheet = document.querySelector('.export-sheet');
    const bar = document.querySelector('.editor .toolbar');
    const sr = sheet?.getBoundingClientRect();
    const tbr = bar?.getBoundingClientRect();
    return {
      role: sheet?.getAttribute('role'),
      modal: sheet?.getAttribute('aria-modal'),
      h: sr ? Math.round(sr.height * 10) / 10 : 999,
      bottom: sr ? Math.round(sr.bottom * 10) / 10 : 999,
      toolbarTop: tbr ? Math.round(tbr.top * 10) / 10 : 0,
      hasDirectShare: Boolean(sheet?.querySelector('[data-act="share"]')),
      focusInside: Boolean(sheet && sheet.contains(document.activeElement)),
    };
  });
  const photoWithSheet = await measurePhoto(page, manifest.portraitAspect);
  check(sheetInfo.role === 'dialog' && sheetInfo.modal === 'true', `${vp.label} sheet dialog attrs wrong: ${JSON.stringify(sheetInfo)}`);
  check(sheetInfo.h <= 240.5, `${vp.label} sheet height ${sheetInfo.h} > 240`);
  check(sheetInfo.bottom <= sheetInfo.toolbarTop + 0.5, `${vp.label} sheet covers toolbar: ${JSON.stringify(sheetInfo)}`);
  check(rectDiff(beforeSheet, photoWithSheet) <= RECT_SLOP, `${vp.label} sheet shifted photo ${r1(rectDiff(beforeSheet, photoWithSheet))}px`);
  check(sheetInfo.hasDirectShare && sheetInfo.focusInside, `${vp.label} sheet CTA/focus wrong: ${JSON.stringify(sheetInfo)}`);
  await snap(page, shotDir, `${vp.label}-09-details-sheet`);

  // Tab focus trap stays inside .export-sheet
  await page.keyboard.press('Tab');
  await page.keyboard.press('Tab');
  await page.keyboard.press('Shift+Tab');
  const stillInside = await page.evaluate(() => Boolean(document.querySelector('.export-sheet')?.contains(document.activeElement)));
  check(stillInside, `${vp.label} focus escaped .export-sheet on Tab`);

  // Escape closes sheet
  await page.keyboard.press('Escape');
  await frame(page, 2);
  check((await page.locator('.export-sheet').count()) === 0, `${vp.label} Escape did not close .export-sheet`);

  // Reopen and verify scrim click closes sheet
  await page.locator('.pdock [data-act="toggle-details"]').click();
  await page.waitForSelector('.export-details-scrim');
  await page.locator('.export-details-scrim').click({ position: { x: 20, y: 80 } });
  await frame(page, 2);
  check((await page.locator('.export-sheet').count()) === 0, `${vp.label} scrim click did not close .export-sheet`);

  // === Collapsed Band + Portrait/Landscape Overlap + Grid Flush ===
  await openEditorPhoto(page, manifest.portrait1Stem);
  await tapStageCenter(page, session);
  const mColPortrait = await measurePhoto(page, manifest.portraitAspect);
  const colOverlap = mColPortrait && mColPortrait.dockTop != null ? Math.max(0, r1(mColPortrait.bottom - mColPortrait.dockTop)) : 999;
  check(mColPortrait?.collapsed === '1' && mColPortrait?.dockMode === 'flush', `${vp.label} collapsed flush mode wrong: ${JSON.stringify(mColPortrait)}`);
  check((mColPortrait?.dockH ?? 99) <= 44.5, `${vp.label} collapsed dockH=${mColPortrait?.dockH} > 44`);
  check(Math.abs((mColPortrait?.dockBottom ?? 0) - (mColPortrait?.toolbarTop ?? 0)) <= 1.5, `${vp.label} collapsed gap above toolbar != 0`);
  check(colOverlap <= vp.maxCollapsedOverlap, `${vp.label} collapsed portrait overlap ${colOverlap}px > ${vp.maxCollapsedOverlap}px`);
  await snap(page, shotDir, `${vp.label}-10-collapsed-ready`);

  // Landscape photo collapsed -> 0pt overlap
  await openEditorPhoto(page, manifest.landscapeStem);
  if ((await measurePhoto(page, manifest.landscapeAspect))?.collapsed !== '1') await tapStageCenter(page, session);
  const mColLand = await measurePhoto(page, manifest.landscapeAspect);
  const landOverlap = mColLand && mColLand.dockTop != null ? Math.max(0, r1(mColLand.bottom - mColLand.dockTop)) : 999;
  check(landOverlap <= 0.5, `${vp.label} collapsed landscape overlap ${landOverlap}px > 0`);

  // Grid screen flush bar
  await page.locator('.editor .nav .nb').first().click();
  await page.waitForSelector('[data-screen="grid"]', { timeout: 15_000 });
  await frame(page, 3);
  const gridDock = await page.evaluate(() => {
    const dock = document.querySelector('.pdock');
    const r = dock?.getBoundingClientRect();
    return dock && r ? { mode: dock.dataset.dockMode, h: Math.round(r.height * 10) / 10, bottom: Math.round(r.bottom * 10) / 10, vh: window.innerHeight } : null;
  });
  check(Boolean(gridDock && gridDock.mode === 'flush' && gridDock.h <= 44.5), `${vp.label} grid flush dock wrong: ${JSON.stringify(gridDock)}`);
  await snap(page, shotDir, `${vp.label}-11-grid-flush-ready`);

  // Return to portrait1 and expand band
  await openEditorPhoto(page, manifest.portrait1Stem);
  if ((await measurePhoto(page, manifest.portraitAspect))?.collapsed === '1') await tapStageCenter(page, session);

  // === STATE 5: Partial Failure (2 ready carryover + 1 trapped landscape photo) ===
  await openEditorPhoto(page, manifest.landscapeStem);
  if ((await measurePhoto(page, manifest.landscapeAspect))?.collapsed === '1') await tapStageCenter(page, session);
  await page.evaluate(() => {
    window.__devExport.trapAt = 0;
  });
  await page.locator('.editor .toolbar [data-act="export"]').click();
  await page.waitForFunction(() => document.querySelector('.pdock')?.dataset.visualState === 'partial-fail', null, { timeout: 60_000 });
  await page.evaluate(() => {
    window.__devExport.trapAt = undefined;
  });
  await frame(page, 3);
  const mPartial = await measurePhoto(page, manifest.landscapeAspect);
  check((mPartial?.dockH ?? 99) <= 44.5, `${vp.label} partial-fail dockH=${mPartial?.dockH} > 44`);
  await snap(page, shotDir, `${vp.label}-07-partial-fail`);
  vpMetrics.states.partialFail = { dockH: mPartial?.dockH, hit: await auditDeckHitTests(page, `${vp.label}:partial-fail`, check, frame) };
  await auditRecipeStatusInState(page, manifest.jpgOnlyStem, manifest.landscapeStem, `${vp.label}:partial-fail`, openEditorPhoto, check, frame);

  // Share the 2 ready files via details sheet bottom CTA (sync user activation!) -> drops to STATE 6: Total Failure (0 ready, 1 fail)
  await page.locator('.pdock [data-act="toggle-details"]').click();
  await page.waitForSelector('.export-sheet [data-act="share"]');
  const shareBeforeSheet = await page.evaluate(() => window.__shareLog.calls.length);
  await page.locator('.export-sheet [data-act="share"]').click();
  await page.waitForFunction((k) => window.__shareLog.calls.length > k, shareBeforeSheet, { timeout: 15_000 });
  const sheetShareCall = await page.evaluate((k) => window.__shareLog.calls[k], shareBeforeSheet);
  check(Boolean(sheetShareCall && sheetShareCall.count === 2 && sheetShareCall.userActive), `${vp.label} sheet CTA share wrong: ${JSON.stringify(sheetShareCall)}`);

  // === STATE 6: Total Failure (0 ready + 1 fail) ===
  await page.waitForFunction(() => document.querySelector('.pdock')?.dataset.visualState === 'total-fail', null, { timeout: 15_000 });
  await frame(page, 3);
  const mTotal = await measurePhoto(page, manifest.landscapeAspect);
  check((mTotal?.dockH ?? 99) <= 44.5, `${vp.label} total-fail dockH=${mTotal?.dockH} > 44`);
  await snap(page, shotDir, `${vp.label}-08-total-fail`);
  vpMetrics.states.totalFail = { dockH: mTotal?.dockH, hit: await auditDeckHitTests(page, `${vp.label}:total-fail`, check, frame) };
  await auditRecipeStatusInState(page, manifest.jpgOnlyStem, manifest.landscapeStem, `${vp.label}:total-fail`, openEditorPhoto, check, frame);
  await verifySavedFailureSummary(page, `${vp.label}:total-fail`, check, frame);

  // Dismiss total failure cleanly via × button
  await page.locator('.pdock [data-act="dismiss"]').click();
  await page.waitForFunction(() => !document.querySelector('.pdock'), null, { timeout: 10_000 });

  vpMetrics.collapsed = { portraitOverlap: colOverlap, landscapeOverlap: landOverlap, gridDock };
  metrics[vp.label] = vpMetrics;
}

try {
  const context = await browser.newContext({ viewport: { width: 390, height: 844 }, deviceScaleFactor: 2, hasTouch: true, isMobile: true });
  const page = await context.newPage();
  const session = await context.newCDPSession(page);
  page.on('pageerror', (err) => pageErrors.push(redact(err.message)));
  await installShareMock(page);
  await page.goto(base, { waitUntil: 'domcontentloaded' });
  await page.waitForSelector('input.filepick:not(.calibpick)', { timeout: 30_000 });
  await page.locator('input.calibpick').setInputFiles(manifest.pack);
  // 起始页没有校准卡了：等导入的包真的用上（不是内置的默认校准）。
  await page.waitForFunction(() => window.__gxrBook?.pack?.kind === 'user' && !window.__gxrBook.packBusy, null, { timeout: 60_000 });
  await page.locator('input.filepick:not(.calibpick)').setInputFiles(manifest.files);
  await page.waitForFunction(
    () => {
      const sub = document.querySelector('.nav small')?.textContent ?? '';
      return (
        !sub.includes('正在读取') &&
        document.querySelectorAll('.cell').length >= 4 &&
        window.__gxrBook?.dat?.read?.entries?.length === 13248 &&
        !window.__gxrBook.datBusy
      );
    },
    null,
    { timeout: 120_000 },
  );
  await frame(page, 3);
  try {
    // Pre-import one synthetic sRGB LUT and apply it on portrait1 so [data-act="lut-space"] is enabled during active export
    await openEditorPhoto(page, manifest.portrait1Stem);
    await page.locator('.editor .toolbar [data-tool="recipe"]').click();
    await frame(page, 2);
    await page.locator('.editor .panel.recipe [data-mode="lut"]').click();
    await frame(page, 2);
    await page.locator('input.lutpick').setInputFiles([manifest.lutPath]);
    await page.waitForFunction(() => (window.__gxrLutBook?.list?.()?.length ?? 0) >= 1, null, { timeout: 30_000 });
    await page.locator('.editor .panel.lut .rc[data-lut]:not([data-lut="none"])').first().click();
    await frame(page, 3);
    await page.locator('.editor .panel.lut [data-mode="recipe"]').click();
    await frame(page, 2);
    await page.locator('.editor .toolbar [data-tool="adjust"]').click();
    await frame(page, 2);

    await installRunGate(page);
    await installAnnouncerLog(page);

    const shotDir = manifest.screenshotsDir || '';
    if (shotDir) mkdirSync(shotDir, { recursive: true });

    for (const vp of VIEWPORTS) {
      await runViewport(page, session, vp, shotDir);
    }
  } catch (err) {
    const diag = await page
      .evaluate(() => ({
        screen: document.querySelector('.app')?.getAttribute('data-screen'),
        settled: document.querySelector('.app')?.getAttribute('data-settled'),
        photo: document.querySelector('.screen.editor')?.getAttribute('data-photo'),
        cellCount: document.querySelectorAll('.cell').length,
        dockPhase: document.querySelector('.pdock')?.dataset.phase ?? null,
        dockVisual: document.querySelector('.pdock')?.dataset.visualState ?? null,
        dockMode: document.querySelector('.pdock')?.dataset.dockMode ?? null,
        dockText: document.querySelector('.pdock')?.textContent?.trim() ?? null,
        shareCalls: window.__shareLog?.calls?.length ?? null,
        exportPhase: window.__devExport?.flow?.current()?.phase ?? null,
      }))
      .catch(() => null);
    failures.push(`threw: ${redact(err?.message ?? err)} diag=${redact(JSON.stringify(diag))} pageErrors=${redact(JSON.stringify(pageErrors))}`);
  }
  check(pageErrors.length === 0, `pageErrors: ${JSON.stringify(pageErrors)}`);
  await context.close();
} finally {
  await browser.close();
}

const out = { ok: failures.length === 0, failures, pageErrors, screenshots, metrics };
console.log(JSON.stringify(out));
process.exit(out.ok ? 0 : 1);
