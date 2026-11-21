// iPad 方案 A + 手机横屏（unit ipad-a）真实浏览器验收驱动：
// 覆盖 5 个新视口（1366×1024、1024×1366、1180×820、844×390、956×440）+ 手机竖屏基线（390×844、440×956）：
// 1. 照片矩形在目标 ±2% 以内，且切工具、拖滑块、导出中/就绪 0px 挪位；
// 2. iPad 双底条常驻（含调整与裁剪），手机横屏无底条；
// 3. 13 条编辑器路径 + ⋯ 在所有版式均可达且点击热区 ≥ 44pt；
// 4. iPad 专有行为：直方图默认开且记住关、微调旋钮直接显示、10 项调整无横滚、更多内联列表（边框首位）、
//    导出选项右上角弹出框含批准文案与「好」、点照片收起面板与双底条并记住；
// 5. 可选脱敏实拍截图输出（manifest.screenshotsDir）。stdout 只打印单行 JSON，绝不含照片名/日期/EXIF。

import { mkdirSync, readFileSync } from 'node:fs';
import { join } from 'node:path';
import { pathToFileURL } from 'node:url';

const [runtime, base, manifestPath] = process.argv.slice(2);
const manifest = JSON.parse(readFileSync(manifestPath, 'utf8'));
const { launchPinnedChromium, verifyPinnedRuntime } = await import(pathToFileURL(runtime).href);
verifyPinnedRuntime();

const RECT_SLOP = 1.0;
const HIT_MIN = 43.5;

// 主屏幕打开时的安全区：iPad 顶 24（状态栏）底 20（横条）；手机横过来左右各让出刘海 / 灵动岛，底 21。
// 照片目标照样稿 alt-a-desktop.html 实测：13" 958×636、11" 757×503、竖屏 1008×670、844×390 553×367；
// 956×440 样稿没画，按「照片铺满整个高度」算（高 = 440 − 21 − 2，宽按照片比例）。
const TARGETS = [
  { label: '1366x1024', width: 1366, height: 1024, layout: 'ipad-landscape', ipad: true, wantW: 958, wantH: 636, safe: { top: 24, bottom: 20, left: 0, right: 0 }, panelW: 392 },
  { label: '1024x1366', width: 1024, height: 1366, layout: 'ipad-portrait', ipad: true, wantW: 1008, wantH: 670, safe: { top: 24, bottom: 20, left: 0, right: 0 } },
  { label: '1180x820', width: 1180, height: 820, layout: 'ipad-landscape', ipad: true, wantW: 757, wantH: 503, safe: { top: 24, bottom: 20, left: 0, right: 0 }, panelW: 360 },
  { label: '844x390', width: 844, height: 390, layout: 'phone-landscape', ipad: false, wantW: 553, wantH: 367, safe: { top: 0, bottom: 21, left: 47, right: 47 } },
  { label: '956x440', width: 956, height: 440, layout: 'phone-landscape', ipad: false, wantW: 'fullHeight', wantH: 417, safe: { top: 0, bottom: 21, left: 62, right: 62 } },
];

// 手机竖屏：照片矩形必须和 main（6e95983）逐项相同。数值用本驱动的 phoneOnly 模式在 main 上量得。
const PHONE_PORTRAIT = [
  { label: '390x844', width: 390, height: 844, safe: { top: 47, bottom: 34, left: 0, right: 0 } },
  { label: '440x956', width: 440, height: 956, safe: { top: 62, bottom: 34, left: 0, right: 0 } },
  { label: '390x844-nosafe', width: 390, height: 844, safe: { top: 0, bottom: 0, left: 0, right: 0 } },
];
const MAIN_PHONE_RECTS = manifest.mainPhoneRects ?? null;

const browser = await launchPinnedChromium({
  args: ['--enable-unsafe-webgpu', '--enable-unsafe-swiftshader', '--use-angle=swiftshader', '--use-webgpu-adapter=swiftshader'],
});

const failures = [];
const pageErrors = [];
const rects = {};
const phonePortraitRects = {};
const reachability = {};
const behaviour = {};
const screenshots = [];

const STEM_ALIAS = new Map([
  [manifest.landscapeStem, 'landscape'],
  [manifest.portrait1Stem, 'portrait1'],
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

function withinPct(actual, target, pct = 0.02) {
  return Math.abs(actual - target) / target <= pct;
}

function rectDiff(a, b) {
  if (!a || !b) return 9999;
  return Math.max(Math.abs(a.x - b.x), Math.abs(a.y - b.y), Math.abs(a.w - b.w), Math.abs(a.h - b.h));
}

async function frame(page, n = 2) {
  await page.evaluate(
    (count) => new Promise((done) => {
      const step = (left) => (left <= 0 ? done() : requestAnimationFrame(() => step(left - 1)));
      step(count);
    }),
    n,
  );
}

async function setSafeArea(page, safe) {
  await page.evaluate((sa) => {
    for (const side of ['top', 'bottom', 'left', 'right']) {
      document.documentElement.style.setProperty(`--safe-${side}`, `${sa[side] ?? 0}px`);
    }
  }, safe);
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

async function measurePhoto(page, aspect) {
  return page.evaluate((imgAspect) => {
    const app = document.querySelector('.app');
    const stage = document.querySelector('.editor .stage');
    const pv = document.querySelector('.editor .stage .pv');
    const media = pv?.querySelector('canvas.gpu:not([hidden]), canvas.still:not([hidden]), img:not([hidden])');
    if (!stage || !pv || !media) return null;
    const mRect = media.getBoundingClientRect();
    let x = mRect.left;
    let y = mRect.top;
    let w = mRect.width;
    let h = mRect.height;
    const nw = imgAspect || (media instanceof HTMLCanvasElement ? media.width : 0);
    const nh = imgAspect ? 1 : (media instanceof HTMLCanvasElement ? media.height : 0);
    if (nw > 0 && nh > 0 && w > 0 && h > 0) {
      const scale = Math.min(w / nw, h / nh);
      const dw = nw * scale;
      const dh = nh * scale;
      x += (w - dw) / 2;
      y += (h - dh) / 2;
      w = dw;
      h = dh;
    }
    const dock = document.querySelector('.editor .ipad-dock');
    const rstrip = document.querySelector('.editor .ipad-recipe-strip');
    const film = document.querySelector('.editor .ipad-filmstrip');
    const vis = (el) => {
      if (!el) return false;
      const cs = getComputedStyle(el);
      if (cs.display === 'none' || cs.visibility === 'hidden') return false;
      const r = el.getBoundingClientRect();
      return r.width > 1 && r.height > 1;
    };
    const rr = (el) => {
      if (!el || !vis(el)) return null;
      const b = el.getBoundingClientRect();
      return { x: Math.round(b.left * 10) / 10, y: Math.round(b.top * 10) / 10, w: Math.round(b.width * 10) / 10, h: Math.round(b.height * 10) / 10 };
    };
    return {
      dockRect: rr(dock),
      bandRect: rr(document.querySelector('.editor > .band')),
      barRect: rr(document.querySelector('.editor > .toolbar')),
      navRect: rr(document.querySelector('.editor > .nav')),
      layout: app?.getAttribute('data-layout') ?? null,
      x: Math.round(x * 10) / 10,
      y: Math.round(y * 10) / 10,
      w: Math.round(w * 10) / 10,
      h: Math.round(h * 10) / 10,
      dockVis: vis(dock),
      rstripVis: vis(rstrip),
      filmVis: vis(film),
      collapsed: document.querySelector('.screen.editor')?.getAttribute('data-collapsed') ?? null,
    };
  }, aspect);
}

async function checkHit(page, selector, label, { click = false, timeout = 3000 } = {}) {
  const loc = typeof selector === 'string' ? page.locator(selector).filter({ visible: true }).first() : selector.first();
  try {
    await loc.waitFor({ state: 'visible', timeout });
    await loc.scrollIntoViewIfNeeded().catch(() => {});
    const box = await loc.boundingBox();
    if (!box) {
      check(false, `${label}: no boundingBox`);
      return null;
    }
    check(box.width >= HIT_MIN && box.height >= HIT_MIN, `${label}: hit ${box.width.toFixed(1)}x${box.height.toFixed(1)} < 44pt`);
    const onTop = await loc.evaluate((node) => {
      const r = node.getBoundingClientRect();
      const hit = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
      return Boolean(hit && (hit === node || node.contains(hit)));
    });
    check(onTop, `${label}: covered at its centre`);
    if (click) {
      await loc.click({ timeout });
      await frame(page, 2);
    }
    return { w: Math.round(box.width * 10) / 10, h: Math.round(box.height * 10) / 10 };
  } catch (err) {
    check(false, `${label}: unreachable (${err.message.split('\n')[0]})`);
    return null;
  }
}

// 视觉比 44 小、靠伪元素扩命中区的控件（顶栏 HDR 胶囊）：在以它为中心的 44×44 方框四边各点一下，都要落在它身上。
async function checkHitArea(page, selector, label) {
  const res = await page.evaluate((sel) => {
    const node = [...document.querySelectorAll(sel)].find((n) => n.getBoundingClientRect().width > 0);
    if (!node) return null;
    const r = node.getBoundingClientRect();
    const cx = r.left + r.width / 2;
    const cy = r.top + r.height / 2;
    const w = Math.max(44, r.width);
    const probes = [[cx, cy - 21.5], [cx, cy + 21.5], [cx - w / 2 + 1, cy], [cx + w / 2 - 1, cy]];
    const ok = probes.every(([x, y]) => {
      const hit = document.elementFromPoint(x, y);
      return Boolean(hit && (hit === node || node.contains(hit)));
    });
    return { ok, w: Math.round(w * 10) / 10, h: 44 };
  }, selector);
  check(Boolean(res?.ok), `${label}: 44pt hit area not covered by the control (${JSON.stringify(res)})`);
  return res ? { w: res.w, h: res.h } : null;
}

async function closeTopSheet(page) {
  for (let i = 0; i < 3; i += 1) {
    const closeBtn = page.locator('.ms [data-act="close"], .ms [data-act="done"], .ms .nb').filter({ visible: true }).first();
    if (await closeBtn.isVisible().catch(() => false)) {
      await closeBtn.click();
      await frame(page, 2);
      continue;
    }
    const scrim = page.locator('.scrim').filter({ visible: true }).last();
    if (await scrim.isVisible().catch(() => false)) {
      await scrim.click({ position: { x: 8, y: 8 }, force: true });
      await frame(page, 2);
      continue;
    }
    break;
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
  await frame(page, 3);
  return true;
}

async function snap(page, dir, name) {
  if (!dir) return;
  const stems = [...STEM_ALIAS.keys()].filter(Boolean);
  const count = await page.evaluate((stemList) => {
    const edits = [];
    const root = document.querySelector('.app');
    if (!root) return 0;
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    const isoDate = /\b\d{4}[-/.:]\d{2}[-/.:]\d{2}(?:\s+\d{2}:\d{2}(?::\d{2})?)?\b/g;
    const zhDate = /\d{1,2}月\d{1,2}日/g;
    const timeRe = /\b\d{2}:\d{2}(?::\d{2})?\b/g;
    let node;
    while ((node = walker.nextNode())) {
      const orig = node.nodeValue ?? '';
      let next = orig;
      for (const stem of stemList) {
        if (next.toLowerCase().includes(stem.toLowerCase())) next = next.replace(new RegExp(stem, 'gi'), 'PHOTO');
      }
      next = next.replace(isoDate, '2026-01-01').replace(zhDate, '1月1日').replace(timeRe, '12:00');
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
  await page.screenshot({ path: join(dir, `${name}.png`) });
  if (count > 0) {
    await page.evaluate(() => {
      window.__snapRestore?.();
      delete window.__snapRestore;
    });
  }
  screenshots.push(`${name}.png`);
}

async function installShareMock(page) {
  await page.addInitScript(() => {
    window.__shareLog = [];
    Object.defineProperty(navigator, 'canShare', {
      configurable: true,
      value: (data) => Boolean(data && Array.isArray(data.files) && data.files.length > 0),
    });
    Object.defineProperty(navigator, 'share', {
      configurable: true,
      value: async (data) => {
        window.__shareLog.push((data?.files ?? []).length);
      },
    });
  });
}

async function auditLayout(page, session, spec, lAspect, shotDir) {
  await page.setViewportSize({ width: spec.width, height: spec.height });
  await setSafeArea(page, spec.safe);
  await openEditorPhoto(page, manifest.landscapeStem);
  await closeTopSheet(page);

  // 确保展开态
  const initM = await measurePhoto(page, lAspect);
  if (initM?.collapsed === '1') {
    await checkHit(page, '.toolbar [data-tool="recipe"]', `${spec.label} uncollapse`, { click: true });
    await frame(page, 3);
  }
  const baseM = await measurePhoto(page, lAspect);
  check(Boolean(baseM), `${spec.label}: photo missing`);
  if (!baseM) return;

  check(baseM.layout === spec.layout, `${spec.label}: layout=${baseM.layout}, want ${spec.layout}`);
  const wantH = spec.wantH;
  const wantW = spec.wantW === 'fullHeight' ? Math.round(wantH * lAspect * 10) / 10 : spec.wantW;
  check(withinPct(baseM.w, wantW), `${spec.label}: width ${baseM.w} not within 2% of ${wantW}`);
  check(withinPct(baseM.h, wantH), `${spec.label}: height ${baseM.h} not within 2% of ${wantH}`);
  if (spec.layout === 'ipad-landscape') {
    // 样稿骨架：面板在照片右边、工具在面板最下面，配方卡和胶片条横跨整屏宽度、在两者下面。
    const d = baseM.dockRect;
    const b = baseM.bandRect;
    const t = baseM.barRect;
    check(Boolean(d && Math.abs(d.x) <= 1 && Math.abs(d.w - spec.width) <= 1), `${spec.label}: strips not full width (${JSON.stringify(d)})`);
    check(Boolean(b && Math.abs(b.w - spec.panelW) <= 1.5), `${spec.label}: panel width ${b?.w} != ${spec.panelW}`);
    check(Boolean(t && d && Math.abs(t.y + t.h - d.y) <= 1 && t.x >= spec.width - spec.panelW - 1), `${spec.label}: tools not at the panel bottom above the strips`);
    check(Boolean(d && Math.abs(d.y + d.h - spec.height) <= 1), `${spec.label}: strips do not reach the screen bottom`);
  } else if (spec.layout === 'phone-landscape') {
    // 照片避开左边的刘海 / 灵动岛；右边一列让出右安全区和底下横条。
    check(baseM.x >= spec.safe.left - 0.5, `${spec.label}: photo starts at x=${baseM.x}, under the left safe area ${spec.safe.left}`);
    const t = baseM.barRect;
    check(Boolean(t && t.x >= baseM.x + baseM.w - 1), `${spec.label}: tools not in the right column`);
  }

  if (spec.ipad) {
    check(baseM.rstripVis && baseM.filmVis, `${spec.label}: bottom strips missing on iPad`);
  } else {
    check(!baseM.dockVis && !baseM.rstripVis && !baseM.filmVis, `${spec.label}: bottom strips must be absent on rotated phone`);
  }

  const hits = {};
  const maxShift = { tools: 0, drag: 0, export: 0 };
  const trackShift = async (bucket, tag) => {
    await frame(page, 3);
    const m = await measurePhoto(page, lAspect);
    const d = rectDiff(baseM, m);
    if (d > maxShift[bucket]) maxShift[bucket] = Math.round(d * 10) / 10;
    check(
      d <= RECT_SLOP,
      `${spec.label} shift on ${tag}: ${d.toFixed(1)}px (${baseM.x},${baseM.y} ${baseM.w}x${baseM.h} -> ${m?.x},${m?.y} ${m?.w}x${m?.h})`,
    );
    return m;
  };

  // Screenshot: default Recipe view on this viewport (wait for selected recipe card thumb to paint)
  await page.waitForSelector('[data-recipes="1"] .rc.on .th canvas', { timeout: 25_000 }).catch(() => {});
  await page.waitForTimeout(1500);
  await frame(page, 2);
  if (spec.label === '1366x1024') await snap(page, shotDir, 'ipad-13-landscape-recipe');
  else if (spec.label === '1024x1366') await snap(page, shotDir, 'ipad-13-portrait');
  else if (spec.label === '1180x820') await snap(page, shotDir, 'ipad-11-landscape');
  else if (spec.label === '844x390') await snap(page, shotDir, 'phone-844x390-landscape');
  else if (spec.label === '956x440') await snap(page, shotDir, 'phone-956x440-landscape');

  // 1. recipe_pick + 2. recipe_finetune + 3. recipe_filter
  const cardBtn = page.locator('[data-recipes="1"] .rc[data-card]:not(.on)').first();
  hits.recipe_pick = await checkHit(page, cardBtn, `${spec.label} recipe_pick`, { click: true });
  if (spec.ipad) {
    const visibleKnobs = await page.locator('.panel.recipe .krow').filter({ visible: true }).count();
    check(visibleKnobs >= 1, `${spec.label}: fine-tune knobs not shown directly in Recipe panel (${visibleKnobs})`);
    hits.recipe_finetune = await checkHit(page, '.panel.recipe .krow input[type="range"]', `${spec.label} recipe_finetune`);
  } else {
    const selCard = page.locator('[data-recipes="1"] .rc[data-card].on').first();
    await checkHit(page, selCard, `${spec.label} recipe_finetune enter`, { click: true });
    hits.recipe_finetune = await checkHit(page, '.panel.recipe .krow input[type="range"]', `${spec.label} recipe_finetune`);
    await checkHit(page, '[data-act="recipe-back"]', `${spec.label} recipe_back`, { click: true });
  }
  hits.recipe_filter = await checkHit(page, '[data-act="recipe-filter"]', `${spec.label} recipe_filter`, { click: true });
  await checkHit(page, '.filter-sheet [data-filter="builtin"]', `${spec.label} filter builtin`, { click: true });
  await checkHit(page, '[data-act="recipe-filter"]', `${spec.label} recipe_filter reopen`, { click: true });
  await checkHit(page, '.filter-sheet [data-filter="all"]', `${spec.label} filter all`, { click: true });

  // 顶栏：返回、HDR、⋯
  hits.nav_back = await checkHit(page, '.editor .nav .nav-back', `${spec.label} nav_back`);
  hits.nav_hdr = await checkHitArea(page, '.editor .nav [data-act="hdr-toggle"]', `${spec.label} nav_hdr`);

  // 4. lut
  hits.lut = await checkHit(page, '[data-mode="lut"]', `${spec.label} lut_tab`, { click: true });
  if (spec.label === '1366x1024') {
    await page.waitForTimeout(800);
    await snap(page, shotDir, 'ipad-13-landscape-lut');
  }
  await checkHit(page, '[data-act="lut-import"]', `${spec.label} lut_import_hit`);
  const lutCard = page.locator('[data-luts="1"] .rc[data-lut]:not([data-lut="none"])').first();
  if (await lutCard.isVisible().catch(() => false)) {
    await checkHit(page, lutCard, `${spec.label} lut_card`, { click: true });
    if (await page.locator('.lut-space-sheet').isVisible().catch(() => false)) {
      await checkHit(page, '.lut-space-sheet [data-space="srgb"]', `${spec.label} lut_space_pick`, { click: true });
      await closeTopSheet(page);
    }
    await checkHit(page, '[data-act="lut-space"], [data-act="lut-space-panel"]', `${spec.label} lut_space_hit`);
    await checkHit(page, '[data-luts="1"] .rc[data-lut="none"]', `${spec.label} lut_none`, { click: true });
  }
  await checkHit(page, '[data-mode="recipe"]', `${spec.label} back_to_recipe_sub`, { click: true });

  // 5. adjust_10_and_reset + 6. histogram + 7. curves
  await checkHit(page, '.toolbar [data-tool="adjust"]', `${spec.label} tool_adjust`, { click: true });
  const mAdj = await trackShift('tools', 'adjust');
  if (spec.ipad) {
    check(mAdj.rstripVis && mAdj.filmVis, `${spec.label}: bottom recipe strip missing in 调整`);
    const adjOverflow = await page.evaluate(() => {
      const chips = document.querySelector('.panel.adjust .adjust-row .chips');
      const visible = chips
        ? [...chips.querySelectorAll('.chip')].filter((c) => getComputedStyle(c).display !== 'none').length
        : 0;
      return { count: visible, scrollW: chips?.scrollWidth ?? 0, clientW: chips?.clientWidth ?? 0 };
    });
    check(adjOverflow.count === 11, `${spec.label}: expected 11 visible adjust chips (10 + 曲线), got ${adjOverflow.count}`);
    check(adjOverflow.scrollW <= adjOverflow.clientW + 2, `${spec.label}: adjust chips scroll horizontally (${adjOverflow.scrollW} > ${adjOverflow.clientW})`);
    if (spec.label === '1366x1024') await snap(page, shotDir, 'ipad-13-landscape-adjust');
  }
  if (spec.label === '1180x820') await snap(page, shotDir, 'ipad-11-landscape-adjust');
  else if (spec.label === '1024x1366') await snap(page, shotDir, 'ipad-13-portrait-adjust');
  else if (!spec.ipad) await snap(page, shotDir, `phone-${spec.label}-adjust`);
  hits.adjust_10 = await checkHit(page, '.panel.adjust [data-key="exposure_ev"]', `${spec.label} adjust_exposure`, { click: true });
  const sliderLoc = page.locator('.panel.adjust .big input[type="range"]').first();
  const sBox = await sliderLoc.boundingBox();
  if (sBox) {
    await page.mouse.move(sBox.x + sBox.width * 0.5, sBox.y + sBox.height / 2);
    await page.mouse.down();
    await page.mouse.move(sBox.x + sBox.width * 0.72, sBox.y + sBox.height / 2, { steps: 4 });
    await trackShift('drag', 'slider-drag-mid');
    await page.mouse.up();
    await trackShift('drag', 'slider-drag-end');
  }
  hits.adjust_reset = await checkHit(page, '.panel.adjust [data-act="adjust-reset"], .panel.adjust [data-act="adjust-reset-all"]', `${spec.label} adjust_reset`, { click: true });

  hits.histogram = await checkHit(page, '[data-act="hist-toggle"]', `${spec.label} hist_toggle`, { click: true });
  // Restore histogram state
  await checkHit(page, '[data-act="hist-toggle"]', `${spec.label} hist_toggle_restore`, { click: true });

  hits.curves = await checkHit(page, '.panel.adjust [data-act="open-curves"]', `${spec.label} open_curves`, { click: true });
  await page.locator('.curves-sheet svg.curve').waitFor({ state: 'visible', timeout: 5000 });
  await closeTopSheet(page);

  // 8. crop
  hits.crop = await checkHit(page, '.toolbar [data-tool="crop"]', `${spec.label} tool_crop`, { click: true });
  const mCrop = await trackShift('tools', 'crop');
  if (spec.ipad) {
    check(mCrop.rstripVis && mCrop.filmVis, `${spec.label}: bottom recipe strip missing in 裁剪`);
  }
  await checkHit(page, '.crop-panel [data-preset="4:3"]', `${spec.label} crop_preset`, { click: true });
  await checkHit(page, '.crop-panel [data-act="crop-flip"]', `${spec.label} crop_flip`, { click: true });
  await checkHit(page, '.crop-panel [data-act="guides"]', `${spec.label} crop_guides`);
  await checkHit(page, '.crop-panel [data-preset="original"]', `${spec.label} crop_original`, { click: true });
  await page.waitForSelector('.crop-panel [data-preset="original"].on', { timeout: 5000 }).catch(() => {});
  await frame(page, 2);

  // 9. frame + 10. stamp + 13. calibration via 更多
  await checkHit(page, '.toolbar [data-tool="more"]', `${spec.label} tool_more`, { click: true });
  if (spec.ipad) {
    await trackShift('tools', 'more-inline');
    const popupOpen = await page.locator('.ms.more-sheet').isVisible().catch(() => false);
    check(!popupOpen, `${spec.label}: iPad 更多 must be inline, not a popup sheet`);
    const firstKey = await page.locator('.more-panel .more-row').first().getAttribute('data-more');
    check(firstKey === 'frame', `${spec.label}: iPad 更多 first item is ${firstKey}, want frame`);
    if (spec.label === '1366x1024') await snap(page, shotDir, 'ipad-13-landscape-more');
    hits.frame = await checkHit(page, '.more-panel [data-more="frame"]', `${spec.label} more_frame`, { click: true });
    await trackShift('tools', 'frame');
    if (spec.label === '1366x1024') {
      await page.waitForTimeout(800);
      await snap(page, shotDir, 'ipad-13-landscape-frame');
    }
    await checkHit(page, '.panel.frame [data-act="more-back"]', `${spec.label} frame_back`, { click: true });
    hits.stamp = await checkHit(page, '.more-panel [data-more="stamp"]', `${spec.label} more_stamp`, { click: true });
    await trackShift('tools', 'stamp');
    await checkHit(page, '.stamp-band [data-act="more-back"]', `${spec.label} stamp_back`, { click: true });
    hits.calibration = await checkHit(page, '.more-panel [data-more="calib"]', `${spec.label} more_calib`, { click: true });
    await page.locator('.calib-sheet').waitFor({ state: 'visible', timeout: 5000 });
    await closeTopSheet(page);
  } else {
    hits.frame = await checkHit(page, '.ms [data-more="frame"]', `${spec.label} more_frame`, { click: true });
    await trackShift('tools', 'frame');
    await checkHit(page, '.toolbar [data-tool="more"]', `${spec.label} tool_more_2`, { click: true });
    hits.stamp = await checkHit(page, '.ms [data-more="stamp"]', `${spec.label} more_stamp`, { click: true });
    await trackShift('tools', 'stamp');
    await checkHit(page, '.toolbar [data-tool="more"]', `${spec.label} tool_more_3`, { click: true });
    hits.calibration = await checkHit(page, '.ms [data-more="calib"]', `${spec.label} more_calib`, { click: true });
    await page.locator('.calib-sheet').waitFor({ state: 'visible', timeout: 5000 });
    await closeTopSheet(page);
  }

  // 11. copy_paste + 12. export via ⋯ (editor-more)
  hits.editor_more = await checkHit(page, '.editor .nav [data-act="editor-more"]', `${spec.label} editor_more`, { click: true });
  hits.copy_paste = await checkHit(page, '.ms [data-act="more-copy"]', `${spec.label} copy_edits`, { click: true });
  await closeTopSheet(page);
  await checkHit(page, '.editor .nav [data-act="editor-more"]', `${spec.label} editor_more_2`, { click: true });
  hits.export_opts = await checkHit(page, '.ms [data-act="more-export-options"]', `${spec.label} export_options`, { click: true });
  if (spec.ipad) {
    const popInfo = await page.evaluate(() => {
      const pop = document.querySelector('.ms.export.popover');
      if (!pop) return null;
      const r = pop.getBoundingClientRect();
      const lead = pop.querySelector('.lead')?.textContent?.trim() ?? '';
      const done = pop.querySelector('[data-act="close"]')?.textContent?.trim() ?? '';
      return {
        isTopRight: r.right > window.innerWidth - 40 && r.top < 120,
        lead,
        done,
      };
    });
    check(Boolean(popInfo?.isTopRight), `${spec.label}: export options not a top-right popover`);
    check(popInfo?.lead === '「存到相册」每次都按这里的设置做。', `${spec.label}: export popover copy mismatch: ${popInfo?.lead}`);
    check(popInfo?.done === '好', `${spec.label}: export popover close button is ${popInfo?.done}, want 好`);
    if (spec.label === '1366x1024') await snap(page, shotDir, 'ipad-13-landscape-export-popover');
  }
  await closeTopSheet(page);

  // Quick export button & 0px shift during export
  await checkHit(page, '.toolbar [data-tool="recipe"]', `${spec.label} back_recipe`, { click: true });
  hits.export = await checkHit(page, '.toolbar [data-act="export"]', `${spec.label} quick_export`, { click: true });
  await page.waitForFunction(
    () => {
      const dock = document.querySelector('.pdock');
      return dock && (dock.dataset.phase === 'ready' || dock.dataset.phase === 'running');
    },
    null,
    { timeout: 60_000 },
  );
  await trackShift('export', 'export-running-or-ready');
  await page.waitForFunction(() => document.querySelector('.pdock')?.dataset.phase === 'ready', null, { timeout: 90_000 });
  await trackShift('export', 'export-ready');
  // 导出条占着面板的槽时，顶栏、4 个工具、存到相册、直方图开关和「存进相册」都还点得到
  await checkHit(page, '.pdock [data-act="share"]', `${spec.label} ready share`);
  await checkHit(page, '.editor .nav [data-act="editor-more"]', `${spec.label} ready editor_more`);
  for (const tool of ['recipe', 'adjust', 'crop', 'more']) {
    await checkHit(page, `.toolbar [data-tool="${tool}"]`, `${spec.label} ready tool_${tool}`);
  }
  await checkHit(page, '.toolbar [data-act="export"]', `${spec.label} ready save`);
  if (spec.ipad) await checkHit(page, '.ipad-hist-area [data-act="hist-toggle"]', `${spec.label} ready hist_toggle`);
  await page.waitForTimeout(600);
  await snap(page, shotDir, `${spec.ipad ? 'ipad' : 'phone'}-${spec.label}-export-ready`);
  const dismiss = page.locator('.pdock [data-act="dismiss"]').filter({ visible: true }).first();
  if (await dismiss.isVisible().catch(() => false)) {
    await dismiss.click();
    await frame(page, 2);
  }

  rects[spec.label] = {
    x: baseM.x,
    y: baseM.y,
    w: baseM.w,
    h: baseM.h,
    wantW,
    wantH,
    safe: spec.safe,
    dock: baseM.dockRect,
    band: baseM.bandRect,
    bar: baseM.barRect,
    layout: baseM.layout,
    strips: { dock: baseM.dockVis, recipe: baseM.rstripVis, film: baseM.filmVis },
    maxShift,
  };
  reachability[spec.label] = hits;
}

const context = await browser.newContext({ viewport: { width: 1366, height: 1024 }, deviceScaleFactor: 2, hasTouch: true });
const page = await context.newPage();
const session = await context.newCDPSession(page);
page.on('pageerror', (err) => pageErrors.push(redact(err.message)));
await installShareMock(page);

try {
  if (manifest.screenshotsDir) mkdirSync(manifest.screenshotsDir, { recursive: true });
  await page.goto(`${base}/`, { waitUntil: 'domcontentloaded' });
  await page.waitForSelector('input.filepick:not(.calibpick)', { state: 'attached', timeout: 30_000 });
  await page.locator('input.calibpick').setInputFiles(manifest.pack);
  await page.waitForSelector('.calib[data-calib="ready"]', { timeout: 60_000 });
  await page.locator('input.filepick:not(.calibpick)').first().setInputFiles(manifest.files);
  await page.waitForFunction(
    () => {
      const sub = document.querySelector('.nav small')?.textContent ?? '';
      return (
        !sub.includes('正在读取') &&
        document.querySelectorAll('.cell').length >= 3 &&
        window.__gxrBook?.dat?.read?.entries?.length === 13248 &&
        !window.__gxrBook.datBusy
      );
    },
    null,
    { timeout: 120_000 },
  );
  await frame(page, 3);

  // 手机竖屏先量（不受后面 iPad 操作影响）：照片矩形必须和 main 逐项相同。
  for (const vp of PHONE_PORTRAIT) {
    await page.setViewportSize({ width: vp.width, height: vp.height });
    await setSafeArea(page, vp.safe);
    await openEditorPhoto(page, manifest.portrait1Stem);
    const pm = await measurePhoto(page, manifest.portraitAspect);
    await openEditorPhoto(page, manifest.landscapeStem);
    const lm = await measurePhoto(page, manifest.landscapeAspect);
    const pick = (m) => (m ? { x: m.x, y: m.y, w: m.w, h: m.h, band: m.bandRect, bar: m.barRect } : null);
    phonePortraitRects[vp.label] = { portrait: pick(pm), landscape: pick(lm) };
    if (manifest.phoneOnly) continue;
    check(pm?.layout === 'phone-portrait' && !pm?.dockVis, `${vp.label}: phone portrait layout regression`);
    check(lm?.layout === 'phone-portrait' && !lm?.dockVis, `${vp.label}: phone portrait landscape-photo regression`);
    const film = await page.locator('.ipad-filmstrip .cell').count();
    check(film === 0, `${vp.label}: phone portrait built ${film} hidden filmstrip cells`);
    // 手机竖屏的 DOM 里不多出 iPad 的块，也不多出同名的隐藏控件（旧测试按这些选择器严格定位）
    const extra = await page.evaluate(() => ({
      ipadNodes: document.querySelectorAll('.editor .ipad-dock, .editor .ipad-hist-area').length,
      shown: [...document.querySelectorAll('.editor .adj-l1, .editor .adj-hint, .editor .crop-inline-hd, .editor .ipad-sub-hd, .editor .ipad-lut-meta')]
        .filter((node) => getComputedStyle(node).display !== 'none').map((node) => node.className),
      dup: ['[data-act="adjust-reset"]', '[data-act="lut-space"]', '[data-act="recipe-filter"]', '[data-act="hist-toggle"]', '.crop-angle', '.crop-size-line']
        .map((sel) => [sel, document.querySelectorAll(`.editor ${sel}`).length])
        .filter(([, count]) => count > 1),
    }));
    check(extra.ipadNodes === 0 && extra.dup.length === 0 && extra.shown.length === 0, `${vp.label}: phone portrait DOM has iPad extras ${JSON.stringify(extra)}`);
    // 旧测试按无障碍名字找按钮（getByRole），iPad 的隐藏控件不能多出一份同名按钮
    await page.locator('.toolbar [data-tool="adjust"]').click();
    await frame(page, 2);
    const resets = await page.getByRole('button', { name: '全部重置', exact: true }).count();
    check(resets === 1, `${vp.label}: ${resets} accessible 「全部重置」 buttons in phone 调整, want 1`);
    await page.locator('.toolbar [data-tool="recipe"]').click();
    await frame(page, 2);
    const want = MAIN_PHONE_RECTS?.[vp.label];
    check(Boolean(want), `${vp.label}: no main reference rects in manifest`);
    for (const kind of ['portrait', 'landscape']) {
      const got = phonePortraitRects[vp.label][kind];
      const ref = want?.[kind];
      if (!ref) continue;
      for (const key of ['x', 'y', 'w', 'h']) {
        check(Math.abs((got?.[key] ?? -1) - ref[key]) <= 0.5, `${vp.label} ${kind} photo ${key} ${got?.[key]} != main ${ref[key]}`);
      }
      for (const part of ['band', 'bar']) {
        for (const key of ['x', 'y', 'w', 'h']) {
          check(Math.abs((got?.[part]?.[key] ?? -1) - (ref[part]?.[key] ?? -2)) <= 0.5, `${vp.label} ${kind} ${part} ${key} ${got?.[part]?.[key]} != main ${ref[part]?.[key]}`);
        }
      }
    }
  }
  if (manifest.phoneOnly) throw new Error('phoneOnly');

  // iPad：打开横图并导入测试 LUT（供全部版式复用）
  await page.setViewportSize({ width: 1366, height: 1024 });
  await setSafeArea(page, TARGETS[0].safe);
  await openEditorPhoto(page, manifest.landscapeStem);
  await checkHit(page, '[data-mode="lut"]', 'lut_init_tab', { click: true });
  await page.locator('input.lutpick').setInputFiles([manifest.lut]);
  await page.waitForFunction(() => (window.__gxrLutBook?.list?.()?.length ?? 0) >= 1, null, { timeout: 30_000 });
  await closeTopSheet(page);
  await checkHit(page, '[data-mode="recipe"]', 'lut_init_back', { click: true });

  // iPad 直方图默认开、关了记住（跨切图）
  const histDefaultOn = await page.locator('.ipad-hist-slot .hist').isVisible();
  check(histDefaultOn, 'iPad histogram must default ON');
  await checkHit(page, '.ipad-hist-area [data-act="hist-toggle"]', 'ipad_hist_off', { click: true });
  const histAfterOff = await page.locator('.ipad-hist-slot .hist').isVisible();
  check(!histAfterOff, 'iPad histogram did not hide on toggle');
  const savedOff = await page.evaluate(() => localStorage.getItem('gxr.mobile.ipad_hist'));
  check(savedOff === '0', `ipad_hist localStorage is ${savedOff}, want 0`);

  // 胶片条切照片，关闭记忆保留
  const otherThumb = page.locator('.ipad-filmstrip .cell:not(.cur)').first();
  await checkHit(page, otherThumb, 'filmstrip_switch', { click: true });
  await frame(page, 4);
  const switched = await page.locator('.screen.editor').getAttribute('data-photo');
  check(switched !== manifest.landscapeStem, 'filmstrip tap did not switch the photo');
  const histStillOff = await page.locator('.ipad-hist-slot .hist').isVisible();
  check(!histStillOff, 'iPad histogram OFF was not remembered across photo switch');
  await checkHit(page, '.ipad-hist-area [data-act="hist-toggle"]', 'ipad_hist_on', { click: true });
  const histBackOn = await page.locator('.ipad-hist-slot .hist').isVisible();
  check(histBackOn, 'iPad histogram did not turn back ON');
  behaviour.ipadHist = { defaultOn: histDefaultOn, offAfterToggle: !histAfterOff, rememberedAcrossSwitch: !histStillOff, restoredOn: histBackOn };

  // 5 个新视口
  await openEditorPhoto(page, manifest.landscapeStem);
  for (const spec of TARGETS) {
    await auditLayout(page, session, spec, manifest.landscapeAspect, manifest.screenshotsDir);
  }

  // iPad 点照片收起面板和两条，只留底栏，并记住
  await page.setViewportSize({ width: 1366, height: 1024 });
  await setSafeArea(page, TARGETS[0].safe);
  await openEditorPhoto(page, manifest.landscapeStem);
  await tapStageCenter(page, session);
  const colM = await measurePhoto(page, manifest.landscapeAspect);
  check(colM?.collapsed === '1', 'iPad tap photo did not collapse');
  check(!colM?.dockVis && !colM?.rstripVis && !colM?.filmVis, 'iPad collapse did not hide bottom strips');
  check(!colM?.bandRect, 'iPad collapse did not hide the panel');
  const barVis = await page.locator('.editor .toolbar').isVisible();
  check(barVis, 'iPad collapse must keep bottom toolbar visible');
  for (const tool of ['recipe', 'adjust', 'crop', 'more']) {
    await checkHit(page, `.toolbar [data-tool="${tool}"]`, `collapsed tool_${tool}`);
  }
  await checkHit(page, '.toolbar [data-act="export"]', 'collapsed save');
  const colStored = await page.evaluate(() => localStorage.getItem('gxr.mobile.band_collapsed'));
  check(colStored === '1', `band_collapsed localStorage is ${colStored}, want 1`);
  await snap(page, manifest.screenshotsDir, 'ipad-13-landscape-collapsed');
  await openEditorPhoto(page, manifest.portrait1Stem);
  const colNext = await measurePhoto(page, manifest.portraitAspect);
  check(colNext?.collapsed === '1', 'iPad collapsed state not remembered on the next photo');
  await checkHit(page, '.toolbar [data-tool="recipe"]', 'ipad_uncollapse_via_bar', { click: true });
  const uncolM = await measurePhoto(page, manifest.portraitAspect);
  check(uncolM?.collapsed === '0' && uncolM?.dockVis, 'iPad toolbar tap did not restore panel + strips');
  behaviour.ipadCollapse = { collapsed: colM?.collapsed === '1', stripsHidden: !colM?.dockVis, barVis, stored: colStored === '1', rememberedNext: colNext?.collapsed === '1', restored: uncolM?.collapsed === '0', collapsedPhoto: colM ? { w: colM.w, h: colM.h } : null };
} catch (err) {
  const diag = await page
    .evaluate(() => ({
      screen: document.querySelector('.app')?.getAttribute('data-screen') ?? null,
      layout: document.querySelector('.app')?.getAttribute('data-layout') ?? null,
      settled: document.querySelector('.app')?.getAttribute('data-settled') ?? null,
      photo: document.querySelector('.screen.editor')?.getAttribute('data-photo') ?? null,
      cells: document.querySelectorAll('.cell').length,
      datBusy: Boolean(window.__gxrBook?.datBusy),
    }))
    .catch(() => null);
  if (!(manifest.phoneOnly && err?.message === 'phoneOnly')) {
    failures.push(redact(`${err?.stack || err?.message || String(err)} diag=${JSON.stringify(diag)}`));
  }
} finally {
  await browser.close();
}

check(pageErrors.length === 0, `pageerror: ${pageErrors.join(' | ')}`);
console.log(JSON.stringify({ ok: failures.length === 0, failures, rects, phonePortraitRects, reachability, behaviour, screenshots }));
if (failures.length > 0) process.exit(1);
