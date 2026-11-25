// 手机版导览 r2 三项视觉缺陷回归驱动（Playwright WebKit，hasTouch: true, isMobile: true）：
// 1) 状态瓷砖「校准范围外（推算）」在 320/375/390/440 竖屏与横屏下最多两行，且「（推算）」不断行；点击仍可打开说明表。
// 2) 「配方 | LUT」切换键拥有独立排版空间，不与配方/LUT 横条或任何卡片重叠（320/375/390/440 竖屏 + 844×390/956×440 横屏）。
// 3) 导出就绪条显示蓝色「存进相册」时，底栏「存到相册」退为次级非蓝样式（当前照片已就绪时带 aria-disabled="true"，切到另一张可继续排队导出）；
//    用户点击就绪条触发同步 navigator.share 后恢复主蓝按钮。
// stdout 仅打印单行脱敏 JSON，绝不含照片文件名、拍摄时间或 EXIF。

import { existsSync, mkdirSync, readdirSync, readFileSync } from 'node:fs';
import path from 'node:path';
import { pathToFileURL } from 'node:url';

const [runtime, base, manifestPath] = process.argv.slice(2);
const manifest = JSON.parse(readFileSync(manifestPath, 'utf8'));
const { pinnedRuntime, verifyPinnedRuntime } = await import(pathToFileURL(runtime).href);
verifyPinnedRuntime();

const { webkit } = await import(pathToFileURL(pinnedRuntime.modulePath).href);

function resolveWebKitExecutable() {
  const direct = webkit.executablePath();
  if (direct && existsSync(direct)) return direct;
  const baseName = direct ? path.basename(direct) : 'pw_run.sh';
  if (existsSync(pinnedRuntime.browsersPath)) {
    for (const entry of readdirSync(pinnedRuntime.browsersPath)) {
      if (!entry.startsWith('webkit-')) continue;
      const candidate = path.join(pinnedRuntime.browsersPath, entry, baseName);
      if (existsSync(candidate)) return candidate;
    }
  }
  return null;
}

const executablePath = resolveWebKitExecutable();
if (!executablePath) {
  process.stdout.write(`${JSON.stringify({ skipped: 'Playwright WebKit 未安装（需运行 playwright install webkit）' })}\n`);
  process.exit(0);
}

const IPHONE_UA =
  'Mozilla/5.0 (iPhone; CPU iPhone OS 18_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.5 Mobile/15E148 Safari/604.1';

const PORTRAIT_VPS = [
  { label: '320x568', width: 320, height: 568, safe: { top: 20, bottom: 0, left: 0, right: 0 } },
  { label: '375x667', width: 375, height: 667, safe: { top: 20, bottom: 0, left: 0, right: 0 } },
  { label: '390x844', width: 390, height: 844, safe: { top: 47, bottom: 34, left: 0, right: 0 } },
  { label: '440x956', width: 440, height: 956, safe: { top: 62, bottom: 34, left: 0, right: 0 } },
];

const LANDSCAPE_VPS = [
  { label: '844x390', width: 844, height: 390, safe: { top: 0, bottom: 21, left: 47, right: 47 } },
  { label: '956x440', width: 956, height: 440, safe: { top: 0, bottom: 21, left: 59, right: 59 } },
];

const STEMS = [manifest.isoOutStem, manifest.secondStem].filter(Boolean);
const failures = [];
const pageErrors = [];
const summary = { fix1: {}, fix2: {}, fix3: {}, shots: [] };

function redact(text) {
  let out = String(text).replace(/\/(?:Volumes|Users|private|var\/folders|tmp)\/\S+/g, '[path]');
  for (let i = 0; i < STEMS.length; i += 1) {
    if (STEMS[i]) out = out.replace(new RegExp(STEMS[i], 'gi'), `photo#${i + 1}`);
  }
  return out.slice(0, 320);
}

function check(ok, msg) {
  if (!ok) failures.push(redact(msg));
  return Boolean(ok);
}

async function frame(page, n = 3) {
  await page.evaluate(
    (count) =>
      new Promise((done) => {
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

function isBlueAccent(rgb) {
  const m = /rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)/.exec(rgb || '');
  if (!m) return false;
  const r = Number(m[1]);
  const b = Number(m[3]);
  return b >= 150 && b - r >= 60;
}

async function measureFix1AndFix2(page) {
  return page.evaluate(() => {
    const r1 = (n) => Math.round(n * 10) / 10;
    const rect = (el) => {
      if (!el) return null;
      const b = el.getBoundingClientRect();
      return { x: r1(b.left), y: r1(b.top), w: r1(b.width), h: r1(b.height), right: r1(b.right), bottom: r1(b.bottom) };
    };
    const overlap = (a, b) => {
      if (!a || !b) return 0;
      const ow = Math.max(0, Math.min(a.right, b.right) - Math.max(a.x, b.x));
      const oh = Math.max(0, Math.min(a.bottom, b.bottom) - Math.max(a.y, b.y));
      return r1(ow * oh);
    };
    const countLines = (rects) => {
      const tops = [];
      for (const r of rects) {
        if (r.width < 0.5 || r.height < 0.5) continue;
        if (!tops.some((t) => Math.abs(t - r.top) <= 2.5)) tops.push(r.top);
      }
      return tops.length;
    };

    const layout = document.querySelector('.app')?.getAttribute('data-layout') ?? '';
    const tile = document.querySelector('.editor .panel.recipe [data-act="recipe-status"]');
    const tileBoxEl = tile?.querySelector('.filter-th');
    const lab = tile?.querySelector('.status-lab');
    let fix1 = null;
    if (tile && lab) {
      const walker = document.createTreeWalker(lab, NodeFilter.SHOW_TEXT);
      const nodes = [];
      let cur;
      while ((cur = walker.nextNode())) {
        if ((cur.nodeValue || '').length) nodes.push(cur);
      }
      const allRects = [];
      let fullText = '';
      const spans = [];
      for (const node of nodes) {
        const start = fullText.length;
        fullText += node.nodeValue || '';
        spans.push({ node, start, end: fullText.length });
        const rg = document.createRange();
        rg.selectNodeContents(node);
        allRects.push(...rg.getClientRects());
      }
      const lineCount = countLines(allRects);
      let parenLines = 0;
      const parenMatch = /（[^）]+）/.exec(fullText);
      if (parenMatch) {
        const pStart = parenMatch.index;
        const pEnd = pStart + parenMatch[0].length;
        const pRects = [];
        for (const sp of spans) {
          const a = Math.max(pStart, sp.start);
          const b = Math.min(pEnd, sp.end);
          if (b <= a) continue;
          const rg = document.createRange();
          rg.setStart(sp.node, a - sp.start);
          rg.setEnd(sp.node, b - sp.start);
          pRects.push(...rg.getClientRects());
        }
        parenLines = countLines(pRects);
      }
      const tr = rect(tile);
      const boxR = rect(tileBoxEl);
      const lr = rect(lab);
      const insideBox = Boolean(
        lr &&
          boxR &&
          lr.x >= boxR.x - 0.5 &&
          lr.right <= boxR.right + 0.5 &&
          lr.y >= boxR.y - 0.5 &&
          lr.bottom <= boxR.bottom + 0.5,
      );
      fix1 = {
        state: tile.getAttribute('data-state'),
        label: (tile.innerText || '').trim(),
        tileRect: tr,
        labRect: lr,
        lineCount,
        parenLines,
        scrollOverflowX: r1(lab.scrollWidth - lab.clientWidth),
        scrollOverflowY: r1(lab.scrollHeight - lab.clientHeight),
        insideBox,
      };
    }

    const sw = document.querySelector('.editor .panel.recipe > .mode-switch');
    const wrap = document.querySelector('.editor .panel.recipe .recipe-strip-wrap');
    const strip = document.querySelector('.editor .panel.recipe .strip');
    const swRect = rect(sw);
    const wrapRect = rect(wrap);
    const stripRect = rect(strip);
    const swPos = sw ? getComputedStyle(sw).position : null;
    const cards = [...document.querySelectorAll('.editor .panel.recipe .strip .rc')];
    const cardOverlaps = cards
      .map((card, i) => ({
        index: i,
        name: card.querySelector('.nm')?.textContent?.trim() ?? '',
        rect: rect(card),
        overlapWithSwitch: overlap(swRect, rect(card)),
      }))
      .filter((c) => c.overlapWithSwitch > 0.5);
    const firstCards = cards.slice(0, 4).map((card, i) => ({
      index: i,
      rect: rect(card),
      overlapWithSwitch: overlap(swRect, rect(card)),
    }));

    const swBtns = [...document.querySelectorAll('.editor .panel.recipe > .mode-switch .mode-btn')].map((btn) => {
      const r = btn.getBoundingClientRect();
      const hit = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
      return {
        mode: btn.dataset.mode,
        w: r1(r.width),
        h: r1(r.height),
        hitOk: Boolean(hit && (hit === btn || btn.contains(hit))),
      };
    });

    let scrolledStripOverlap = 0;
    if (strip && strip.scrollWidth > strip.clientWidth) {
      const prevScroll = strip.scrollLeft;
      strip.scrollLeft = Math.min(120, strip.scrollWidth - strip.clientWidth);
      scrolledStripOverlap = overlap(rect(sw), rect(strip));
      strip.scrollLeft = prevScroll;
    }

    const visibleCardHitMisses = [];
    if (stripRect) {
      for (const card of cards) {
        const cr = card.getBoundingClientRect();
        const cx = cr.left + cr.width / 2;
        const cy = cr.top + cr.height / 2;
        if (cx < stripRect.x + 2 || cx > stripRect.right - 2 || cy < 0 || cy > window.innerHeight) continue;
        const hit = document.elementFromPoint(cx, cy);
        if (!hit || (!card.contains(hit) && hit !== card) || hit.closest('.mode-switch')) {
          visibleCardHitMisses.push({ hitCls: hit?.className ?? 'null' });
        }
      }
    }

    const fix2 = {
      layout,
      swPos,
      swRect,
      wrapRect,
      stripRect,
      wrapOverlapSwitch: overlap(swRect, wrapRect),
      stripOverlapSwitch: overlap(swRect, stripRect),
      scrolledStripOverlap,
      cardOverlapsCount: cardOverlaps.length,
      cardOverlaps,
      firstCards,
      swBtns,
      visibleCardHitMisses,
    };

    return { layout, fix1, fix2 };
  });
}

async function measureExportButtons(page) {
  return page.evaluate(() => {
    const save = document.querySelector('.editor .toolbar [data-act="export"]');
    const dock = document.querySelector('.pdock');
    const sharePill = dock?.querySelector('[data-act="share"] .es-pill, [data-act="share-ready"] .es-pill');
    const shareBtn = dock?.querySelector('[data-act="share"], [data-act="share-ready"]');
    const saveCs = save ? getComputedStyle(save) : null;
    const shareCs = sharePill ? getComputedStyle(sharePill) : null;
    return {
      layout: document.querySelector('.app')?.getAttribute('data-layout') ?? '',
      dockPhase: dock?.dataset.phase ?? null,
      dockVisual: dock?.dataset.visualState ?? null,
      shareText: shareBtn?.textContent?.trim() ?? null,
      shareBg: shareCs?.backgroundColor ?? null,
      saveText: save?.textContent?.trim() ?? null,
      saveClasses: save ? [...save.classList] : [],
      saveBg: saveCs?.backgroundColor ?? null,
      saveColor: saveCs?.color ?? null,
      saveAriaDisabled: save?.getAttribute('aria-disabled') ?? null,
      saveDisabledProp: save ? Boolean(save.disabled) : null,
      saveExportState: save?.getAttribute('data-export-state') ?? null,
    };
  });
}

async function saveBandCrop(page, name) {
  if (!manifest.shotsDir) return;
  mkdirSync(manifest.shotsDir, { recursive: true });
  const clip = await page.evaluate(() => {
    const band = document.querySelector('.editor .band');
    const bar = document.querySelector('.editor .toolbar');
    if (!band || !bar) return null;
    const br = band.getBoundingClientRect();
    const tr = bar.getBoundingClientRect();
    const y = Math.max(0, Math.floor(br.top) - 10);
    const bottom = Math.min(window.innerHeight, Math.ceil(tr.bottom));
    return { x: 0, y, width: window.innerWidth, height: Math.max(1, bottom - y) };
  });
  if (!clip) return;
  const file = path.join(manifest.shotsDir, `${name}.png`);
  await page.screenshot({ path: file, clip, animations: 'disabled' });
  summary.shots.push(name);
}

const browser = await webkit.launch({ headless: true, executablePath });
const ctx = await browser.newContext({
  viewport: { width: 440, height: 956 },
  deviceScaleFactor: 3,
  isMobile: true,
  hasTouch: true,
  userAgent: IPHONE_UA,
  locale: 'zh-CN',
});

await ctx.addInitScript(() => {
  const calls = [];
  window.__shareLog = { calls };
  Object.defineProperty(navigator, 'canShare', {
    configurable: true,
    value: (data) => Boolean(data && Array.isArray(data.files) && data.files.length > 0),
  });
  Object.defineProperty(navigator, 'share', {
    configurable: true,
    value: async (data) => {
      const files = (data?.files ?? []).map((f) => ({ size: f.size, type: f.type }));
      calls.push({
        count: files.length,
        userActive: navigator.userActivation ? navigator.userActivation.isActive : true,
      });
    },
  });
});

const page = await ctx.newPage();
page.on('pageerror', (err) => pageErrors.push(redact(err.message)));

try {
  await page.goto(`${base}/`, { waitUntil: 'domcontentloaded' });
  await page.waitForSelector('.app[data-core="1"]', { timeout: 60_000 });
  await page.locator('input.calibpick').setInputFiles(manifest.pack);
  await page.waitForFunction(() => window.__gxrBook?.pack?.kind === 'user' && !window.__gxrBook.packBusy, null, { timeout: 60_000 });
  await page.locator('input.filepick:not(.calibpick)').first().setInputFiles(manifest.files);
  await page.waitForFunction(
    () => {
      const sub = document.querySelector('.nav small')?.textContent ?? '';
      return !sub.includes('正在读取') && document.querySelectorAll('.cell').length >= 2 && Boolean(window.__gxrBook?.dat) && !window.__gxrBook.datBusy;
    },
    null,
    { timeout: 120_000 },
  );
  await frame(page, 3);

  // 打开 ISO 1250 照片（触发「校准范围外（推算）」状态瓷砖）
  await page.locator(`.cell[data-id="${manifest.isoOutStem.toLowerCase()}"]`).tap();
  await page.waitForSelector('.editor .panel.recipe [data-act="recipe-status"]', { timeout: 60_000 });
  await frame(page, 4);

  // 1 & 2) 手机竖屏四个宽度（320 / 375 / 390 / 440）
  for (const vp of PORTRAIT_VPS) {
    await page.setViewportSize({ width: vp.width, height: vp.height });
    await setSafeArea(page, vp.safe);
    await page.waitForFunction(() => document.querySelector('.app')?.getAttribute('data-layout') === 'phone-portrait', null, { timeout: 10_000 });
    await frame(page, 4);
    const m = await measureFix1AndFix2(page);
    summary.fix1[vp.label] = m.fix1;
    summary.fix2[vp.label] = m.fix2;

    check(Boolean(m.fix1 && m.fix1.state === 'uncalibrated'), `fix1 ${vp.label}: missing uncalibrated status tile`);
    if (m.fix1) {
      check(m.fix1.lineCount >= 1 && m.fix1.lineCount <= 2, `fix1 ${vp.label}: status tile wrapped into ${m.fix1.lineCount} lines (want <= 2)`);
      check(m.fix1.parenLines <= 1, `fix1 ${vp.label}: bracketed text split across ${m.fix1.parenLines} lines (want <= 1)`);
      check(
        m.fix1.scrollOverflowX <= 1 && m.fix1.scrollOverflowY <= 1 && m.fix1.insideBox,
        `fix1 ${vp.label}: status label clipped/overflows (dx=${m.fix1.scrollOverflowX}, dy=${m.fix1.scrollOverflowY}, inside=${m.fix1.insideBox})`,
      );
    }

    const f2 = m.fix2;
    check(f2.wrapOverlapSwitch <= 0.5, `fix2 ${vp.label}: recipe-strip-wrap overlaps mode-switch by ${f2.wrapOverlapSwitch}px2`);
    check(f2.stripOverlapSwitch <= 0.5, `fix2 ${vp.label}: strip overlaps mode-switch by ${f2.stripOverlapSwitch}px2`);
    check(f2.scrolledStripOverlap <= 0.5, `fix2 ${vp.label}: scrolled strip overlaps mode-switch by ${f2.scrolledStripOverlap}px2`);
    check(f2.cardOverlapsCount === 0, `fix2 ${vp.label}: ${f2.cardOverlapsCount} recipe card(s) render underneath mode-switch`);
    check(
      f2.swBtns.length === 2 && f2.swBtns.every((b) => b.w >= 43.5 && b.h >= 43.5 && b.hitOk),
      `fix2 ${vp.label}: mode-switch buttons invalid: ${JSON.stringify(f2.swBtns)}`,
    );
    check(f2.visibleCardHitMisses.length === 0, `fix2 ${vp.label}: visible card hit misses: ${JSON.stringify(f2.visibleCardHitMisses)}`);

    if (vp.label === '440x956') {
      const prefix = manifest.shotsPrefix || 'shot';
      await saveBandCrop(page, `${prefix}-fix1-status-tile-440`);
      await saveBandCrop(page, `${prefix}-fix2-recipe-strip-440`);
      // 点状态瓷砖仍能解释原因
      await page.locator('.editor .panel.recipe [data-act="recipe-status"]').tap();
      await page.waitForSelector('.recipe-info-sheet', { timeout: 10_000 });
      const sheetText = (await page.locator('.recipe-info-sheet').innerText()).trim();
      summary.fix1.tapSheetHasReason = sheetText.includes('校准范围外（推算）') && sheetText.includes('ISO 1250');
      check(summary.fix1.tapSheetHasReason, `fix1 440x956: status sheet missing explanation`);
      await page.locator('.recipe-info-sheet [data-act="close"]').tap();
      await page.waitForFunction(() => !document.querySelector('.recipe-info-sheet'), null, { timeout: 10_000 });
      await frame(page, 2);

      // 切到 LUT 再查一次切换键同 x 且无重叠
      await page.locator('.editor .panel.recipe > .mode-switch [data-mode="lut"]').tap();
      await page.waitForFunction(() => !document.querySelector('.editor .panel.lut')?.hidden, null, { timeout: 10_000 });
      await frame(page, 2);
      const lutCheck = await page.evaluate(() => {
        const r1 = (n) => Math.round(n * 10) / 10;
        const rect = (el) => {
          if (!el) return null;
          const b = el.getBoundingClientRect();
          return { x: r1(b.left), y: r1(b.top), right: r1(b.right), bottom: r1(b.bottom) };
        };
        const overlap = (a, b) => {
          if (!a || !b) return 0;
          return r1(Math.max(0, Math.min(a.right, b.right) - Math.max(a.x, b.x)) * Math.max(0, Math.min(a.bottom, b.bottom) - Math.max(a.y, b.y)));
        };
        const sw = rect(document.querySelector('.editor .panel.lut > .mode-switch'));
        const wrap = rect(document.querySelector('.editor .panel.lut .lut-strip-wrap'));
        const strip = rect(document.querySelector('.editor .panel.lut .strip'));
        return { swX: sw?.x ?? null, wrapOverlap: overlap(sw, wrap), stripOverlap: overlap(sw, strip) };
      });
      summary.fix2.lut440 = lutCheck;
      check(
        lutCheck.swX !== null && f2.swRect && Math.abs(lutCheck.swX - f2.swRect.x) <= 0.5,
        `fix2 440x956: LUT switch x=${lutCheck.swX} differs from recipe switch x=${f2.swRect?.x}`,
      );
      check(lutCheck.wrapOverlap <= 0.5 && lutCheck.stripOverlap <= 0.5, `fix2 440x956: LUT strip overlaps switch: ${JSON.stringify(lutCheck)}`);
      await page.locator('.editor .panel.lut > .mode-switch [data-mode="recipe"]').tap();
      await page.waitForFunction(() => !document.querySelector('.editor .panel.recipe')?.hidden, null, { timeout: 10_000 });
      await frame(page, 2);
    }
  }

  // 1 & 2) 手机横屏（844×390 / 956×440）
  for (const vp of LANDSCAPE_VPS) {
    await page.setViewportSize({ width: vp.width, height: vp.height });
    await setSafeArea(page, vp.safe);
    await page.waitForFunction(() => document.querySelector('.app')?.getAttribute('data-layout') === 'phone-landscape', null, { timeout: 10_000 });
    await frame(page, 4);
    const m = await measureFix1AndFix2(page);
    summary.fix1[vp.label] = m.fix1;
    summary.fix2[vp.label] = m.fix2;

    if (m.fix1) {
      check(m.fix1.lineCount >= 1 && m.fix1.lineCount <= 2, `fix1 ${vp.label}: status tile wrapped into ${m.fix1.lineCount} lines`);
      check(m.fix1.parenLines <= 1, `fix1 ${vp.label}: bracketed text split across ${m.fix1.parenLines} lines`);
    }
    const f2 = m.fix2;
    check(f2.wrapOverlapSwitch <= 0.5 && f2.stripOverlapSwitch <= 0.5, `fix2 ${vp.label}: landscape strip overlaps mode-switch`);
    check(f2.cardOverlapsCount === 0, `fix2 ${vp.label}: landscape cards overlap mode-switch`);
    check(
      f2.firstCards.every((c) => !c.rect || !f2.swRect || f2.swRect.bottom <= c.rect.y + 0.5),
      `fix2 ${vp.label}: landscape mode-switch not above cards`,
    );
  }

  // 3) 导出就绪态按钮区分（440×956 竖屏 + 956×440 横屏 + 切到第二张排队导出 + 分享后恢复）
  await page.setViewportSize({ width: 440, height: 956 });
  await setSafeArea(page, PORTRAIT_VPS[3].safe);
  await page.waitForFunction(() => document.querySelector('.app')?.getAttribute('data-layout') === 'phone-portrait', null, { timeout: 10_000 });
  await frame(page, 3);

  const idleBtn = await measureExportButtons(page);
  summary.fix3.idle = idleBtn;
  check(
    isBlueAccent(idleBtn.saveBg) && idleBtn.saveClasses.includes('primary') && idleBtn.saveAriaDisabled === null,
    `fix3 idle: toolbar export button should be primary blue, got ${JSON.stringify(idleBtn)}`,
  );

  // 点「存到相册」开始导出第 1 张，等到就绪条出现
  await page.locator('.editor .toolbar [data-act="export"]').tap();
  await page.waitForFunction(() => document.querySelector('.pdock')?.dataset.visualState === 'ready', null, { timeout: 120_000 });
  await frame(page, 3);

  const readyCurrent = await measureExportButtons(page);
  summary.fix3.readyCurrent = readyCurrent;
  const prefix = manifest.shotsPrefix || 'shot';
  await saveBandCrop(page, `${prefix}-fix3-export-ready-440`);

  check(isBlueAccent(readyCurrent.shareBg), `fix3 ready: ready bar share button must be blue (got ${readyCurrent.shareBg})`);
  check(
    !isBlueAccent(readyCurrent.saveBg) && !readyCurrent.saveClasses.includes('primary'),
    `fix3 ready: toolbar button must not stay primary blue alongside ready bar (bg=${readyCurrent.saveBg}, cls=${readyCurrent.saveClasses.join('.')})`,
  );
  check(
    readyCurrent.saveAriaDisabled === 'true' && readyCurrent.saveDisabledProp === false,
    `fix3 ready-current: toolbar button should mark aria-disabled="true" without HTML disabled (got aria=${readyCurrent.saveAriaDisabled}, disabled=${readyCurrent.saveDisabledProp})`,
  );

  // 横屏下就绪态也只保留一个蓝色主按钮
  await page.setViewportSize({ width: 956, height: 440 });
  await setSafeArea(page, LANDSCAPE_VPS[1].safe);
  await page.waitForFunction(() => document.querySelector('.app')?.getAttribute('data-layout') === 'phone-landscape', null, { timeout: 10_000 });
  await frame(page, 3);
  const readyLandscape = await measureExportButtons(page);
  summary.fix3.readyLandscape = readyLandscape;
  check(
    isBlueAccent(readyLandscape.shareBg) && !isBlueAccent(readyLandscape.saveBg),
    `fix3 landscape: expected single blue CTA while ready (share=${readyLandscape.shareBg}, save=${readyLandscape.saveBg})`,
  );

  // 回到 440×956，切到第 2 张照片：底栏按钮仍是次级非蓝，但可点（无 aria-disabled），且可把第 2 张加入导出
  await page.setViewportSize({ width: 440, height: 956 });
  await setSafeArea(page, PORTRAIT_VPS[3].safe);
  await page.waitForFunction(() => document.querySelector('.app')?.getAttribute('data-layout') === 'phone-portrait', null, { timeout: 10_000 });
  await frame(page, 3);
  await page.locator('.editor .nav .nb').first().tap();
  await page.waitForSelector('[data-screen="grid"]', { timeout: 15_000 });
  await page.locator(`.cell[data-id="${manifest.secondStem.toLowerCase()}"]`).tap();
  await page.waitForSelector('.editor .toolbar [data-act="export"]', { timeout: 30_000 });
  await frame(page, 3);

  const readyOther = await measureExportButtons(page);
  summary.fix3.readyOther = readyOther;
  check(
    isBlueAccent(readyOther.shareBg) && !isBlueAccent(readyOther.saveBg) && readyOther.saveAriaDisabled === null,
    `fix3 ready-other: toolbar button on another photo should be non-blue secondary and enabled (${JSON.stringify(readyOther)})`,
  );

  await page.locator('.editor .toolbar [data-act="export"]').tap();
  await page.waitForFunction(
    () =>
      document.querySelector('.pdock')?.dataset.visualState === 'ready' &&
      (document.querySelector('.pdock [data-act="share"]')?.textContent || '').includes('2 张做好了'),
    null,
    { timeout: 120_000 },
  );
  await frame(page, 2);

  // 用户点击就绪条「存进相册」触发同步 navigator.share（保持 iOS 用户手势规则），完成后底栏恢复蓝色主按钮
  const shareBefore = await page.evaluate(() => window.__shareLog.calls.length);
  await page.locator('.pdock [data-act="share"]').tap();
  await page.waitForFunction((k) => window.__shareLog.calls.length > k, shareBefore, { timeout: 15_000 });
  await page.waitForFunction(() => !document.querySelector('.pdock'), null, { timeout: 10_000 });
  await frame(page, 3);

  const shareCall = await page.evaluate(() => window.__shareLog.calls.at(-1) ?? null);
  const afterShare = await measureExportButtons(page);
  summary.fix3.shareCall = shareCall;
  summary.fix3.afterShare = afterShare;
  check(
    Boolean(shareCall && shareCall.count === 2 && shareCall.userActive === true),
    `fix3 share: expected 2 files shared from user tap, got ${JSON.stringify(shareCall)}`,
  );
  check(
    isBlueAccent(afterShare.saveBg) && afterShare.saveClasses.includes('primary') && afterShare.saveAriaDisabled === null,
    `fix3 after-share: toolbar button did not restore primary blue (${JSON.stringify(afterShare)})`,
  );
} catch (err) {
  failures.push(`threw: ${redact(err?.stack || err?.message || err)}`);
} finally {
  await ctx.close();
  await browser.close();
}

check(pageErrors.length === 0, `pageErrors: ${pageErrors.join(' | ')}`);
process.stdout.write(`${JSON.stringify({ ok: failures.length === 0, failures, pageErrors, summary })}\n`);
if (failures.length > 0) process.exit(1);
