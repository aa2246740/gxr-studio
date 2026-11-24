// iPad Safari / WebKit 无 WebGPU 回归驱动（D1–D5）：
// 用固定 Playwright WebKit（hasTouch: true, isMobile: true）验证 1032×1376、1376×1032、
// 820×1180、844×390 与 390×844；带头另查 375×667、320×568，横屏「存到相册」另查 932×430、844×300、932×330。
// 未装 WebKit 时打印 skipped JSON 并以 0 退出。
// stdout 仅打印单行脱敏 JSON，绝不含文件名、拍摄时间或 EXIF。

import { existsSync, readdirSync, readFileSync } from 'node:fs';
import path from 'node:path';
import { pathToFileURL } from 'node:url';
import { auditPhonePortraitNoGpuHeader } from './mobile_ipad_webkit_header.mjs';
import { auditPhoneLandscapeSave } from './mobile_phone_landscape_save.mjs';

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

const IPAD_SAFE = { top: 24, bottom: 20, left: 0, right: 0 };
const PHONE_LAND_SAFE = { top: 0, bottom: 21, left: 47, right: 47 };
const PHONE_PORT_SAFE = { top: 47, bottom: 34, left: 0, right: 0 };

const EDITOR_VIEWPORTS = [
  { label: '1032x1376', width: 1032, height: 1376, layout: 'ipad-portrait', ipad: true, safe: IPAD_SAFE, checkExport: true },
  { label: '1376x1032', width: 1376, height: 1032, layout: 'ipad-landscape', ipad: true, safe: IPAD_SAFE, checkExport: true },
  { label: '820x1180', width: 820, height: 1180, layout: 'ipad-portrait', ipad: true, safe: IPAD_SAFE, checkExport: false },
  { label: '844x390', width: 844, height: 390, layout: 'phone-landscape', ipad: false, safe: PHONE_LAND_SAFE, checkExport: false },
  { label: '390x844', width: 390, height: 844, layout: 'phone-portrait', ipad: false, safe: PHONE_PORT_SAFE, checkExport: false },
  { label: '440x956', width: 440, height: 956, layout: 'phone-portrait', ipad: false, safe: PHONE_PORT_SAFE, checkExport: false },
];

// 更窄的手机竖屏（iPhone SE 375×667，SE 放大显示 320×568）：只查无 WebGPU 带头（N2 「LUT」不单独一行，N5 配方槽宽度）。
const NARROW_PORTRAIT = [
  { label: '375x667', width: 375, height: 667, layout: 'phone-portrait', ipad: false, safe: { top: 20, bottom: 0, left: 0, right: 0 } },
  { label: '320x568', width: 320, height: 568, layout: 'phone-portrait', ipad: false, safe: { top: 20, bottom: 0, left: 0, right: 0 } },
];

const STEMS = (manifest.stems ?? []).filter(Boolean);
const failures = [];
const pageErrors = [];
const summary = { grid: {}, editor: {}, d5: {}, d6: {}, d7: {}, f1: {}, rotation: {}, landscapeSave: {} };

const IPAD_UA = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/26.0 Safari/605.1.15';
const IPHONE_UA = 'Mozilla/5.0 (iPhone; CPU iPhone OS 18_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.6 Mobile/15E148 Safari/604.1';

function redact(text) {
  let out = String(text).replace(/\/(?:Volumes|Users|private|var\/folders|tmp)\/\S+/g, '[path]');
  for (let i = 0; i < STEMS.length; i += 1) {
    const stem = STEMS[i];
    if (stem) out = out.replace(new RegExp(stem.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'), 'gi'), `photo#${i}`);
  }
  return out.slice(0, 300);
}

function check(ok, msg) {
  if (!ok) failures.push(redact(msg));
  return Boolean(ok);
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

async function measureStillPhoto(page) {
  return page.evaluate(() => {
    const media = document.querySelector(
      '.editor .stage .pv canvas.gpu:not([hidden]), .editor .stage .pv canvas.still:not([hidden]), .editor .stage .pv img:not([hidden])',
    );
    if (!media) return null;
    const b = media.getBoundingClientRect();
    const nw = media instanceof HTMLCanvasElement ? media.width : media.naturalWidth || 0;
    const nh = media instanceof HTMLCanvasElement ? media.height : media.naturalHeight || 0;
    let x = b.left;
    let y = b.top;
    let w = b.width;
    let h = b.height;
    if (nw > 0 && nh > 0 && w > 0 && h > 0) {
      const s = Math.min(w / nw, h / nh);
      const dw = nw * s;
      const dh = nh * s;
      x += (w - dw) / 2;
      y += (h - dh) / 2;
      w = dw;
      h = dh;
    }
    return {
      x: Math.round(x * 10) / 10,
      y: Math.round(y * 10) / 10,
      w: Math.round(w * 10) / 10,
      h: Math.round(h * 10) / 10,
      nw,
      nh,
      kind: media.tagName.toLowerCase() + (media.className ? `.${media.className}` : ''),
    };
  });
}

async function toGrid(page) {
  if (await page.locator('[data-screen="grid"]').isVisible().catch(() => false)) return;
  const back = page.locator('.editor .nav .nav-back, .editor .nav .nb').filter({ visible: true }).first();
  if (await back.isVisible().catch(() => false)) {
    await back.tap();
    await page.waitForSelector('[data-screen="grid"]', { timeout: 15_000 });
    await frame(page, 3);
  }
}

async function auditGridViewport(page, vp) {
  await toGrid(page);
  await page.setViewportSize({ width: vp.width, height: vp.height });
  await setSafeArea(page, vp.safe);
  await page.waitForFunction(
    (want) => document.querySelector('.app')?.getAttribute('data-layout') === want,
    vp.layout,
    { timeout: 10_000 },
  );
  await frame(page, 4);

  const info = await page.evaluate(() => {
    const cells = [...document.querySelectorAll('.gscroll .cell')];
    const rects = cells.map((c) => {
      const r = c.getBoundingClientRect();
      return { w: Math.round(r.width * 10) / 10, h: Math.round(r.height * 10) / 10 };
    });
    const firstRow = document.querySelector('.gscroll .cells');
    const colsCss = firstRow ? getComputedStyle(firstRow).gridTemplateColumns.split(/\s+/).filter(Boolean).length : 0;
    const dayBad = [...document.querySelectorAll('.gscroll .day, .gscroll .gh')].filter((n) => {
      const r = n.getBoundingClientRect();
      return r.height < 20 || r.right > window.innerWidth + 1;
    }).length;
    const badgeBad = cells.filter((c) => {
      const cr = c.getBoundingClientRect();
      for (const sub of c.querySelectorAll('.bdg, .edt')) {
        const sr = sub.getBoundingClientRect();
        if (sr.width > 0 && (sr.left < cr.left - 1 || sr.right > cr.right + 1 || sr.top < cr.top - 1 || sr.bottom > cr.bottom + 1)) {
          return true;
        }
      }
      return false;
    }).length;
    return {
      layout: document.querySelector('.app')?.getAttribute('data-layout') ?? null,
      count: cells.length,
      colsCss,
      minW: rects.length ? Math.min(...rects.map((r) => r.w)) : 0,
      maxW: rects.length ? Math.max(...rects.map((r) => r.w)) : 0,
      hscroll: document.documentElement.scrollWidth > window.innerWidth + 1,
      dayBad,
      badgeBad,
    };
  });
  summary.grid[vp.label] = info;
  check(info.count >= 5, `D4 ${vp.label}: expected >= 5 cells, got ${info.count}`);
  check(!info.hscroll, `D4 ${vp.label}: horizontal page scroll detected`);
  check(info.dayBad === 0, `D4 ${vp.label}: ${info.dayBad} day/group headers clipped`);
  check(info.badgeBad === 0, `D4 ${vp.label}: ${info.badgeBad} badges outside cell bounds`);
  if (vp.ipad) {
    check(
      info.minW >= 150 && info.maxW <= 200,
      `D4 ${vp.label}: iPad thumbnail width ${info.minW}..${info.maxW} pt outside 150–200 pt (cols=${info.colsCss})`,
    );
  } else {
    check(info.colsCss === 3, `D4 ${vp.label}: phone portrait grid columns=${info.colsCss}, want 3`);
  }
}

async function auditD5Labels(page, phase, { expectEdited = false, expectSelected = false } = {}) {
  const rows = await page.evaluate((stems) => {
    const timeOrDate = /\b\d{4}[-/.:]\d{2}[-/.:]\d{2}\b|\b\d{1,2}:\d{2}\b|\d{1,2}月\d{1,2}日/;
    return [...document.querySelectorAll('.gscroll .cell')].map((cell) => {
      const label = (cell.getAttribute('aria-label') || '').trim();
      const lower = label.toLowerCase();
      const leakedStem = stems.some((s) => s && lower.includes(s.toLowerCase()));
      const leakedTime = timeOrDate.test(label);
      const hasKind = /RAW\+JPG|RAW|JPG|半格双联/.test(label);
      const hasPos = /第\s*\d+\s*张/.test(label);
      return {
        nonEmpty: label.length > 0,
        hasPos,
        hasKind,
        hasEdited: label.includes('已编辑'),
        hasSelected: label.includes('已选中'),
        leakedStem,
        leakedTime,
      };
    });
  }, STEMS);

  summary.d5[phase] = {
    total: rows.length,
    nonEmpty: rows.filter((r) => r.nonEmpty).length,
    withPosAndKind: rows.filter((r) => r.hasPos && r.hasKind).length,
    editedCount: rows.filter((r) => r.hasEdited).length,
    selectedCount: rows.filter((r) => r.hasSelected).length,
    leaked: rows.filter((r) => r.leakedStem || r.leakedTime).length,
  };
  check(rows.length >= 5, `D5 ${phase}: expected >= 5 cells, got ${rows.length}`);
  check(rows.every((r) => r.nonEmpty), `D5 ${phase}: found thumbnail buttons with empty accessible name (${rows.filter((r) => !r.nonEmpty).length}/${rows.length})`);
  check(rows.every((r) => r.hasPos && r.hasKind), `D5 ${phase}: thumbnails missing position or kind in accessible name`);
  check(rows.every((r) => !r.leakedStem && !r.leakedTime), `D5 ${phase}: accessible name leaked file stem or capture time`);
  if (expectEdited) {
    check(rows.some((r) => r.hasEdited), `D5 ${phase}: edited photo accessible name missing 「已编辑」`);
  }
  if (expectSelected) {
    check(rows.some((r) => r.hasSelected), `D5 ${phase}: selected photo accessible name missing 「已选中」`);
  }
}

async function checkVisibleControlNotCovered(page, selector, label) {
  const res = await page.evaluate((sel) => {
    const nodes = [...document.querySelectorAll(sel)].filter((n) => {
      const cs = getComputedStyle(n);
      const r = n.getBoundingClientRect();
      return (
        cs.display !== 'none' &&
        cs.visibility !== 'hidden' &&
        r.width > 1 &&
        r.height > 1 &&
        r.right > 0 &&
        r.bottom > 0 &&
        r.left < window.innerWidth &&
        r.top < window.innerHeight
      );
    });
    const node = nodes.find((n) => {
      const r = n.getBoundingClientRect();
      const cx = r.left + r.width / 2;
      const cy = r.top + r.height / 2;
      return cx >= 0 && cy >= 0 && cx < window.innerWidth && cy < window.innerHeight;
    }) ?? nodes[0];
    if (!node) return { found: false, onTop: false };
    const r = node.getBoundingClientRect();
    const cx = r.left + r.width / 2;
    const cy = r.top + r.height / 2;
    if (cx < 0 || cy < 0 || cx >= window.innerWidth || cy >= window.innerHeight) {
      return { found: true, onTop: false, offscreen: true };
    }
    const hit = document.elementFromPoint(cx, cy);
    return { found: true, onTop: Boolean(hit && (hit === node || node.contains(hit))) };
  }, selector);
  check(res.found && res.onTop, `${label}: control missing or covered (${JSON.stringify(res)})`);
}

async function auditEditorViewport(page, vp, landscapeId, landscapeStem) {
  await toGrid(page);
  await page.setViewportSize({ width: vp.width, height: vp.height });
  await setSafeArea(page, vp.safe);
  await page.waitForFunction(
    (want) => document.querySelector('.app')?.getAttribute('data-layout') === want,
    vp.layout,
    { timeout: 10_000 },
  );
  await frame(page, 2);

  // 点开横图：等首帧排版完成后抓初始矩形（D3，尚未换清图或刚画完），再等 settled 后复测。
  const cell = page.locator(`.cell[data-id="${landscapeId}"]`).first();
  await cell.waitFor({ state: 'visible', timeout: 15_000 });
  await cell.tap();
  await page.waitForSelector('.screen.editor .stage .pv canvas.still:not([hidden])', { timeout: 20_000 });
  await frame(page, 1);
  const initialRect = await measureStillPhoto(page);

  await page.waitForFunction(
    (stem) => document.querySelector('.app')?.getAttribute('data-settled') === stem,
    landscapeStem,
    { timeout: 60_000 },
  );
  await frame(page, 4);

  // 若上次收起了，先展开
  const collapsed = await page.locator('.screen.editor').getAttribute('data-collapsed');
  if (collapsed === '1') {
    await page.locator('.editor .toolbar [data-tool="recipe"]').tap();
    await frame(page, 3);
  }
  const settledRect = await measureStillPhoto(page);
  const sharpenShift = rectDiff(initialRect, settledRect);
  check(
    sharpenShift <= 1.0,
    `D3 ${vp.label}: photo shifted ${sharpenShift.toFixed(1)}px on sharpen (${JSON.stringify(initialRect)} -> ${JSON.stringify(settledRect)})`,
  );

  // D1：无 WebGPU 说明可见，清楚写明预览为相机 JPG 且导出仍套用配方和 LUT，不遮挡控件，无空白直方图框。
  const d1 = await page.evaluate(() => {
    const isVis = (n) => {
      const cs = getComputedStyle(n);
      if (cs.display === 'none' || cs.visibility === 'hidden' || Number(cs.opacity) === 0) return false;
      const r = n.getBoundingClientRect();
      return r.width > 4 && r.height > 4 && r.right > 0 && r.bottom > 0 && r.left < window.innerWidth && r.top < window.innerHeight;
    };
    const leaves = [...document.querySelectorAll('.editor *')].filter((n) => n.childElementCount === 0 && isVis(n));
    // 说明里「配方和 LUT」用了词连接符和不换行空格，比对前先还原成普通文字。
    const visText = leaves.map((n) => (n.textContent || '').replace(/\u2060/g, '').replace(/\u00a0/g, ' ').trim()).filter(Boolean).join('\n');
    const noticeText = leaves.filter((n) => n.matches('.nogpu-note, .nogpu-detail')).map((n) => (n.textContent || '').replace(/\u2060/g, '').replace(/\u00a0/g, ' ')).join('\n');
    const noticeLeaves = leaves.filter((n) => /显卡预览/.test(n.textContent || ''));
    const hists = [...document.querySelectorAll('.editor .hist')].map((h) => {
      const r = h.getBoundingClientRect();
      const cs = getComputedStyle(h);
      const visible = !h.hidden && cs.display !== 'none' && cs.visibility !== 'hidden' && r.width > 4 && r.height > 4;
      const canvas = h.querySelector('canvas');
      const txt = (h.textContent || '').trim();
      return { visible, version: canvas?.dataset.version ?? null, hasDash: txt.includes('—') };
    });
    const emptyHist = hists.filter((h) => h.visible && h.version === null && !h.hasDash);
    return {
      gpu: document.querySelector('.app')?.getAttribute('data-gpu') ?? null,
      layout: document.querySelector('.app')?.getAttribute('data-layout') ?? null,
      noticeVisibleCount: noticeLeaves.length,
      hasCoreNotice: visText.includes('这个浏览器现在用不了显卡预览'),
      hasExportAndJpg: /导出/.test(visText) && /配方/.test(visText) && /LUT/.test(visText) && /JPG/.test(visText),
      noticeExact: noticeText.includes('预览只显示相机 JPG，导出仍会套用配方和 LUT'),
      emptyHistCount: emptyHist.length,
      hists,
      hscroll: document.documentElement.scrollWidth > window.innerWidth + 1,
    };
  });

  check(d1.gpu === '0', `D1 ${vp.label}: expected no-GPU WebKit (data-gpu=0), got ${d1.gpu}`);
  check(d1.layout === vp.layout, `D1 ${vp.label}: layout=${d1.layout}, want ${vp.layout}`);
  check(d1.noticeVisibleCount >= 1 && d1.hasCoreNotice, `D1 ${vp.label}: no-GPU notice 「这个浏览器现在用不了显卡预览」 is not visible`);
  check(d1.hasExportAndJpg, `D1 ${vp.label}: visible notice does not explain JPG preview + export still applying recipe and LUT`);
  check(d1.noticeExact, `D1 ${vp.label}: no-GPU detail text is not 「预览只显示相机 JPG，导出仍会套用配方和 LUT」 after normalising joiners`);
  check(d1.emptyHistCount === 0, `D1 ${vp.label}: visible empty histogram frame found (${JSON.stringify(d1.hists)})`);
  check(!d1.hscroll, `D1 ${vp.label}: horizontal page scroll detected in editor`);

  // 确认主要控件均未被说明条遮挡
  for (const [sel, name] of [
    ['.editor .nav .nav-back', 'nav-back'],
    ['.editor .nav [data-act="editor-more"]', 'editor-more'],
    ['.editor .toolbar [data-tool="recipe"]', 'tool-recipe'],
    ['.editor .toolbar [data-tool="adjust"]', 'tool-adjust'],
    ['.editor .toolbar [data-tool="crop"]', 'tool-crop'],
    ['.editor .toolbar [data-tool="more"]', 'tool-more'],
    ['.editor .toolbar [data-act="export"]', 'toolbar-export'],
  ]) {
    await checkVisibleControlNotCovered(page, sel, `D1 ${vp.label} ${name}`);
  }
  await checkVisibleControlNotCovered(page, '[data-recipes="1"] .rc[data-card]', `D1 ${vp.label} first-recipe-card`);

  // F1 + N2（手机竖屏 390×844 / 440×956 无 WebGPU）：带头防挤压与说明行两行完整展示（见 mobile_ipad_webkit_header.mjs）。
  if (vp.layout === 'phone-portrait') {
    await auditPhonePortraitNoGpuHeader(page, vp, { check, frame, summary });
  }

  // D2：切一张不同的配方卡，顶栏标题必须立刻跟上卡片名称
  const targetCard = page.locator('[data-recipes="1"] .rc[data-card]:not(.on):not(.lockd)').filter({ visible: true }).first();
  await targetCard.waitFor({ state: 'visible', timeout: 10_000 });
  const wantTitle = (await targetCard.locator('.nm').innerText()).trim();
  const titleBefore = (await page.locator('.editor .nav .t b').innerText()).trim();
  await targetCard.tap();
  await frame(page, 3);
  const titleAfter = (await page.locator('.editor .nav .t b').innerText()).trim();
  check(
    titleAfter === wantTitle,
    `D2 ${vp.label}: editor title stayed 「${titleAfter}」 after picking 「${wantTitle}」 (was 「${titleBefore}」)`,
  );

  // D3：切到「调整」与回「配方」不挪动照片；且在调整页无空白直方图框、说明仍可见
  await page.locator('.editor .toolbar [data-tool="adjust"]').tap();
  await frame(page, 3);
  const adjustRect = await measureStillPhoto(page);
  const toolShift = rectDiff(settledRect, adjustRect);
  check(toolShift <= 1.0, `D3 ${vp.label}: photo shifted ${toolShift.toFixed(1)}px on adjust tab`);
  await page.locator('.editor .toolbar [data-tool="recipe"]').tap();
  await frame(page, 3);

  // D3：导出前后照片位移 <= 1 px（在两个主 iPad 尺寸验证）
  let exportShift = 0;
  if (vp.checkExport) {
    await page.locator('.editor .toolbar [data-act="export"]').tap();
    await page.waitForFunction(() => document.querySelector('.pdock')?.dataset.phase === 'ready', null, { timeout: 120_000 });
    await frame(page, 3);
    const afterExportRect = await measureStillPhoto(page);
    exportShift = rectDiff(settledRect, afterExportRect);
    check(
      exportShift <= 1.0,
      `D3 ${vp.label}: photo shifted ${exportShift.toFixed(1)}px on export (${JSON.stringify(settledRect)} -> ${JSON.stringify(afterExportRect)})`,
    );
    const shareBtn = page.locator('.pdock [data-act="share"]').filter({ visible: true }).first();
    if (await shareBtn.isVisible().catch(() => false)) {
      await shareBtn.tap();
      await page.waitForFunction(() => !document.querySelector('.pdock'), null, { timeout: 10_000 }).catch(() => {});
      await frame(page, 2);
    }
  }

  summary.editor[vp.label] = {
    d1,
    d2: { before: titleBefore, want: wantTitle, after: titleAfter },
    d3: { initialRect, settledRect, sharpenShift: Math.round(sharpenShift * 10) / 10, toolShift: Math.round(toolShift * 10) / 10, exportShift: Math.round(exportShift * 10) / 10 },
  };
  return settledRect;
}

async function auditVisibleDeviceCopy(page, label, expectDev, mustInclude = []) {
  const other = expectDev === 'iPad' ? 'iPhone' : 'iPad';
  const info = await page.evaluate(() => {
    const isVis = (n) => {
      if (n.closest('[hidden]')) return false;
      const cs = getComputedStyle(n);
      if (cs.display === 'none' || cs.visibility === 'hidden' || Number(cs.opacity) === 0) return false;
      const r = n.getBoundingClientRect();
      return r.width > 1 && r.height > 1;
    };
    const texts = [];
    const root = document.querySelector('.app');
    if (root) {
      const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
      let node;
      while ((node = walker.nextNode())) {
        const txt = (node.nodeValue || '').trim();
        if (!txt || !node.parentElement || !isVis(node.parentElement)) continue;
        texts.push(txt);
      }
    }
    return {
      text: texts.join('\n'),
      touchPoints: navigator.maxTouchPoints,
      platform: navigator.platform,
    };
  });
  const missing = mustInclude.filter((s) => !info.text.includes(s));
  const hasExpect = info.text.includes(expectDev);
  const hasOther = info.text.includes(other);
  const otherHits = info.text
    .split('\n')
    .filter((line) => line.includes(other))
    .slice(0, 6);
  summary.d6[label] = {
    expectDev,
    touchPoints: info.touchPoints,
    platform: info.platform,
    hasExpect,
    hasOther,
    missingCount: missing.length,
    otherHitCount: otherHits.length,
  };
  check(hasExpect, `D6 ${label}: visible text missing 「${expectDev}」`);
  check(!hasOther, `D6 ${label}: visible text still contains 「${other}」 (${otherHits.join(' | ')})`);
  for (const phrase of missing) {
    check(false, `D6 ${label}: visible text missing expected phrase 「${phrase}」`);
  }
}

async function auditStartLayout(page, label, vp) {
  await page.setViewportSize({ width: vp.width, height: vp.height });
  await setSafeArea(page, vp.safe);
  await page.waitForFunction(
    (want) => document.querySelector('.app')?.getAttribute('data-layout') === want,
    vp.layout,
    { timeout: 10_000 },
  );
  await frame(page, 3);
  const info = await page.evaluate(() => {
    const pickRect = (sel) => {
      const el = document.querySelector(sel);
      if (!el) return null;
      const r = el.getBoundingClientRect();
      return {
        left: Math.round(r.left * 10) / 10,
        right: Math.round(r.right * 10) / 10,
        w: Math.round(r.width * 10) / 10,
        h: Math.round(r.height * 10) / 10,
      };
    };
    return {
      vw: window.innerWidth,
      layout: document.querySelector('.app')?.getAttribute('data-layout') ?? null,
      start: pickRect('.start'),
      calib: pickRect('.start .calib'),
      ctaBtn: pickRect('.start .cta .btn'),
      hscroll: document.documentElement.scrollWidth > window.innerWidth + 1,
    };
  });
  summary.d7[label] = info;
  check(!info.hscroll, `D7 ${label}: horizontal page scroll detected on first screen`);
  check(Boolean(info.start && info.ctaBtn), `D7 ${label}: start screen elements missing`);
  // 起始页没有校准这一步了（没导入时用内置的默认校准，校准数据在「更多」里）。
  check(info.calib === null, `D7 ${label}: start screen still shows a calibration card`);
  if (!info.start || !info.ctaBtn) return;
  if (vp.ipad) {
    check(info.start.w <= 720 && info.start.w >= 500, `D7 ${label}: .start width ${info.start.w} pt outside 500..720 pt`);
    check(info.ctaBtn.w <= 720, `D7 ${label}: primary CTA width ${info.ctaBtn.w} pt exceeds 720 pt`);
    const centerOff = Math.abs((info.start.left + info.start.right) / 2 - info.vw / 2);
    check(centerOff <= 2.0, `D7 ${label}: .start not centred (offset ${centerOff.toFixed(1)} pt)`);
  } else {
    check(info.start.w >= info.vw - 2, `D7 ${label}: phone .start width ${info.start.w} pt narrowed unexpectedly (vw=${info.vw})`);
    check(info.ctaBtn.w >= info.vw - 40, `D7 ${label}: phone primary CTA width ${info.ctaBtn.w} pt narrowed unexpectedly`);
  }
}

const browser = await webkit.launch({ headless: true, executablePath });
const ctx = await browser.newContext({
  viewport: { width: 1376, height: 1032 },
  deviceScaleFactor: 2,
  hasTouch: true,
  isMobile: true,
  userAgent: IPAD_UA,
});
await ctx.addInitScript(() => {
  const defineNav = (prop, value) => {
    try {
      Object.defineProperty(Navigator.prototype, prop, { configurable: true, get: () => value });
    } catch {
      Object.defineProperty(navigator, prop, { configurable: true, get: () => value });
    }
  };
  defineNav('maxTouchPoints', 5);
  defineNav('platform', 'MacIntel');
  window.__shareLog = [];
  Object.defineProperty(navigator, 'canShare', { configurable: true, value: (d) => Boolean(d && Array.isArray(d.files) && d.files.length > 0) });
  Object.defineProperty(navigator, 'share', {
    configurable: true,
    value: async (d) => { window.__shareLog.push((d?.files ?? []).length); },
  });
});

const page = await ctx.newPage();
page.on('pageerror', (err) => pageErrors.push(redact(err.message)));

try {
  await page.goto(`${base}/`, { waitUntil: 'domcontentloaded' });
  await page.waitForSelector('.app[data-core="1"]', { timeout: 60_000 });
  await page.waitForSelector('.start .cta .btn', { timeout: 30_000 });

  // D7：iPad 横屏与竖屏首屏内容限宽 <= 720 pt 且居中
  await auditStartLayout(page, 'ipad-landscape-1376x1032', { width: 1376, height: 1032, layout: 'ipad-landscape', ipad: true, safe: IPAD_SAFE });
  await auditStartLayout(page, 'ipad-portrait-1032x1376', { width: 1032, height: 1376, layout: 'ipad-portrait', ipad: true, safe: IPAD_SAFE });

  // D6（iPad 首屏 + 帮助表）：无「iPhone」，全部显示「iPad」
  await auditVisibleDeviceCopy(page, 'ipad-start', 'iPad', [
    '照片和相机配方文件只留在这台 iPad',
    '怎么把卡插到 iPad',
  ]);
  await page.locator('.start .help .lnk').tap();
  await page.waitForSelector('.ms', { timeout: 10_000 });
  await frame(page, 2);
  await auditVisibleDeviceCopy(page, 'ipad-help', 'iPad', ['怎么把卡插到 iPad', '把读卡器插到 iPad 底部']);
  await page.locator('.ms .hd .nb').tap();
  await page.waitForFunction(() => !document.querySelector('.ms'), null, { timeout: 10_000 });
  await frame(page, 2);

  await page.locator('input.filepick:not(.calibpick)').first().setInputFiles(manifest.files);
  await page.waitForFunction(
    () => {
      const sub = document.querySelector('.nav small')?.textContent ?? '';
      return !sub.includes('正在读取') && document.querySelectorAll('.cell').length >= 5 && !window.__gxrBook?.datBusy;
    },
    null,
    { timeout: 120_000 },
  );
  await frame(page, 3);

  // D6（iPad 图库 ⋯ 菜单）
  await page.locator('.nav button[aria-label="更多"]').tap();
  await page.waitForSelector('.ms', { timeout: 10_000 });
  await frame(page, 2);
  await auditVisibleDeviceCopy(page, 'ipad-grid-menu', 'iPad', ['怎么把卡插到 iPad']);
  // 校准数据在「更多」里：先看还没有校准时的那张卡（测试里内置的默认校准是关着的），再在这里换成自己的校准。
  await page.locator('.ms .lrow', { hasText: '校准数据' }).tap();
  await page.waitForSelector('.calib-sheet .calib[data-calib="none"]', { timeout: 10_000 });
  await frame(page, 2);
  await auditVisibleDeviceCopy(page, 'ipad-calib-sheet', 'iPad', ['用隔空投送发到这台 iPad']);
  await page.locator('input.calibpick').setInputFiles(manifest.pack);
  await page.waitForFunction(() => window.__gxrBook?.pack?.kind === 'user' && !window.__gxrBook.packBusy, null, { timeout: 60_000 });
  await page.waitForSelector('.calib-sheet .calib[data-calib="user"]', { timeout: 10_000 });
  await page.locator('.calib-sheet .hd .nb').tap();
  await page.waitForFunction(() => !document.querySelector('.ms'), null, { timeout: 10_000 });
  await frame(page, 2);

  // D4：iPad 三个尺寸 150–200 pt + 手机竖屏 3 列
  for (const vp of [
    { label: '1032x1376', width: 1032, height: 1376, layout: 'ipad-portrait', ipad: true, safe: IPAD_SAFE },
    { label: '1376x1032', width: 1376, height: 1032, layout: 'ipad-landscape', ipad: true, safe: IPAD_SAFE },
    { label: '820x1180', width: 820, height: 1180, layout: 'ipad-portrait', ipad: true, safe: IPAD_SAFE },
    { label: '390x844', width: 390, height: 844, layout: 'phone-portrait', ipad: false, safe: PHONE_PORT_SAFE },
  ]) {
    await auditGridViewport(page, vp);
  }

  // D5：初始无障碍名称 + 选中态「已选中」
  await auditGridViewport(page, { label: '1032x1376-d5', width: 1032, height: 1376, layout: 'ipad-portrait', ipad: true, safe: IPAD_SAFE });
  await auditD5Labels(page, 'initial');
  await page.locator('.nav .nb.bold').tap();
  await frame(page, 2);
  await page.locator('.gscroll .cell').first().tap();
  await frame(page, 2);
  await auditD5Labels(page, 'selected', { expectSelected: true });
  await page.locator('.nav .nb.bold').tap();
  await frame(page, 2);

  // D1 + D2 + D3：四个目标版式 + 手机竖屏
  const landscapeId = manifest.landscapeStem.toLowerCase();
  const freshRects = {};
  for (const vp of EDITOR_VIEWPORTS) {
    freshRects[vp.label] = await auditEditorViewport(page, vp, landscapeId, manifest.landscapeStem);
  }

  // N2 / N5：编辑器还开着，换到更窄的手机竖屏，只量带头。
  for (const vp of NARROW_PORTRAIT) {
    await page.setViewportSize({ width: vp.width, height: vp.height });
    await setSafeArea(page, vp.safe);
    await page.waitForFunction(() => document.querySelector('.app')?.getAttribute('data-layout') === 'phone-portrait', null, { timeout: 10_000 });
    await frame(page, 4);
    if ((await page.locator('.screen.editor').getAttribute('data-collapsed')) === '1') {
      await page.locator('.editor .toolbar [data-tool="recipe"]').tap();
      await frame(page, 3);
    }
    await auditPhonePortraitNoGpuHeader(page, vp, { check, frame, summary });
  }

  // L5：手机横屏（含 Safari 工具栏压矮的 844×300 / 932×330）「存到相册」完整可见，照片不被盖住。
  await auditPhoneLandscapeSave(page, { check, frame, setSafeArea, summary: summary.landscapeSave, prefix: 'L5 webkit' });

  // D3：编辑器内旋转 1032×1376 -> 1376×1032 -> 1032×1376，照片矩形位移 <= 1 px
  await toGrid(page);
  await page.setViewportSize({ width: 1032, height: 1376 });
  await setSafeArea(page, IPAD_SAFE);
  await page.locator(`.cell[data-id="${landscapeId}"]`).first().tap();
  await page.waitForFunction(
    (stem) => document.querySelector('.app')?.getAttribute('data-settled') === stem,
    manifest.landscapeStem,
    { timeout: 60_000 },
  );
  await frame(page, 3);

  // D6（iPad 编辑器 LUT 脚注文本 + 导出选项 HDR 说明）
  const lutFootText = await page.evaluate(() => (document.querySelector('.panel.lut .lut-foot')?.textContent || '').trim());
  summary.d6['ipad-lut-foot'] = { expectDev: 'iPad', hasExpect: lutFootText.includes('iPad'), hasOther: lutFootText.includes('iPhone') };
  check(lutFootText.includes('LUT 只存在这台 iPad 上') && !lutFootText.includes('iPhone'), `D6 ipad-lut-foot: unexpected text 「${lutFootText}」`);
  await page.locator('.editor .nav [data-act="editor-more"]').tap();
  await page.waitForSelector('[data-act="more-export-options"]', { timeout: 10_000 });
  await page.locator('[data-act="more-export-options"]').tap();
  await page.waitForSelector('[data-state="export-sheet"]', { timeout: 10_000 });
  await frame(page, 2);
  await auditVisibleDeviceCopy(page, 'ipad-export-options', 'iPad', ['在 iPad 上看']);
  await page.locator('[data-state="export-sheet"] [data-act="close"]').tap();
  await page.waitForFunction(() => !document.querySelector('[data-state="export-sheet"]'), null, { timeout: 10_000 });
  await frame(page, 2);

  const p1 = await measureStillPhoto(page);
  await page.setViewportSize({ width: 1376, height: 1032 });
  await page.waitForFunction(() => document.querySelector('.app')?.getAttribute('data-layout') === 'ipad-landscape', null, { timeout: 10_000 });
  await frame(page, 5);
  const l1 = await measureStillPhoto(page);
  await page.setViewportSize({ width: 1032, height: 1376 });
  await page.waitForFunction(() => document.querySelector('.app')?.getAttribute('data-layout') === 'ipad-portrait', null, { timeout: 10_000 });
  await frame(page, 5);
  const p2 = await measureStillPhoto(page);
  const rotLandShift = rectDiff(l1, freshRects['1376x1032']);
  const rotBackShift = rectDiff(p1, p2);
  summary.rotation = { p1, l1, p2, rotLandShift: Math.round(rotLandShift * 10) / 10, rotBackShift: Math.round(rotBackShift * 10) / 10 };
  check(rotLandShift <= 1.0, `D3 rotate landscape shift ${rotLandShift.toFixed(1)}px (${JSON.stringify(l1)} vs fresh ${JSON.stringify(freshRects['1376x1032'])})`);
  check(rotBackShift <= 1.0, `D3 rotate back to portrait shift ${rotBackShift.toFixed(1)}px (${JSON.stringify(p1)} vs ${JSON.stringify(p2)})`);

  // D5：回到网格验证已编辑照片带有「已编辑」无障碍名称
  await toGrid(page);
  await auditD5Labels(page, 'after-edit', { expectEdited: true });

  // D6 + D7（iPhone UA 竖屏与横屏）：文案保持「iPhone」，首屏手机版式不变
  const phoneCtx = await browser.newContext({
    viewport: { width: 390, height: 844 },
    deviceScaleFactor: 3,
    hasTouch: true,
    isMobile: true,
    userAgent: IPHONE_UA,
  });
  await phoneCtx.addInitScript(() => {
    const defineNav = (prop, value) => {
      try {
        Object.defineProperty(Navigator.prototype, prop, { configurable: true, get: () => value });
      } catch {
        Object.defineProperty(navigator, prop, { configurable: true, get: () => value });
      }
    };
    defineNav('maxTouchPoints', 5);
    defineNav('platform', 'iPhone');
  });
  const phonePage = await phoneCtx.newPage();
  phonePage.on('pageerror', (err) => pageErrors.push(redact(err.message)));
  try {
    await phonePage.goto(`${base}/`, { waitUntil: 'domcontentloaded' });
    await phonePage.waitForSelector('.app[data-core="1"]', { timeout: 60_000 });
    await phonePage.waitForSelector('.start .cta .btn', { timeout: 30_000 });
    await auditStartLayout(phonePage, 'phone-portrait-390x844', { width: 390, height: 844, layout: 'phone-portrait', ipad: false, safe: PHONE_PORT_SAFE });
    await auditVisibleDeviceCopy(phonePage, 'iphone-start', 'iPhone', [
      '照片和相机配方文件只留在这台 iPhone',
      '怎么把卡插到 iPhone',
    ]);
    await phonePage.locator('.start .help .lnk').tap();
    await phonePage.waitForSelector('.ms', { timeout: 10_000 });
    await frame(phonePage, 2);
    await auditVisibleDeviceCopy(phonePage, 'iphone-help', 'iPhone', ['怎么把卡插到 iPhone', '把读卡器插到 iPhone 底部']);
    await phonePage.locator('.ms .hd .nb').tap();
    await phonePage.waitForFunction(() => !document.querySelector('.ms'), null, { timeout: 10_000 });
    await phonePage.setViewportSize({ width: 844, height: 390 });
    await setSafeArea(phonePage, PHONE_LAND_SAFE);
    await phonePage.waitForFunction(() => document.querySelector('.app')?.getAttribute('data-layout') === 'phone-landscape', null, { timeout: 10_000 });
    await frame(phonePage, 3);
    await auditVisibleDeviceCopy(phonePage, 'iphone-landscape-start', 'iPhone', ['照片和相机配方文件只留在这台 iPhone']);
  } finally {
    await phoneCtx.close();
  }
} catch (err) {
  failures.push(redact(err?.stack || err?.message || String(err)));
} finally {
  await ctx.close();
  await browser.close();
}

check(pageErrors.length === 0, `pageerror: ${pageErrors.join(' | ')}`);
process.stdout.write(`${JSON.stringify({ ok: failures.length === 0, failures, pageErrors, summary })}\n`);
if (failures.length > 0) process.exit(1);
