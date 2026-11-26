// 电脑上的键盘和鼠标（2026-11-26，desktop-keys）：Esc 一次只关最上面的一层、面板拿焦点、网格回来时的位置、
// Shift/Cmd 点选、方向键、悬停和清空前确认；编辑器里 ←/→ 换照片。Chromium 电脑（没有触摸点、指针精确），
// WebKit 电脑做一遍引擎核对，WebKit 手机和 iPad（有触摸）确认不变。
// 隐私：报告里只有计数、布尔和界面上的固定文案；截图前把照片名、日期和时间换成占位。

import { existsSync, readdirSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import path from 'node:path';
import { pathToFileURL } from 'node:url';
import { pickAll } from './mobile_ui_export_parity.mjs';

const [runtime, url, manifestPath] = process.argv.slice(2);
const manifest = JSON.parse(readFileSync(manifestPath, 'utf8'));
const { launchPinnedChromium, pinnedRuntime, verifyPinnedRuntime } = await import(pathToFileURL(runtime).href);
verifyPinnedRuntime();
const BROWSER_DIRS = [process.env.PLAYWRIGHT_BROWSERS_PATH, pinnedRuntime.browsersPath].filter(Boolean);
const GPU_ARGS = ['--enable-unsafe-webgpu', '--enable-unsafe-swiftshader', '--use-angle=swiftshader', '--use-webgpu-adapter=swiftshader'];
const IPAD_UA = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/26.0 Safari/605.1.15';
const out = { pageErrors: [], steps: {}, error: null };
const only = new Set(manifest.only ?? []);
let lastPage = null;
/** 报告里不出照片名：每个 R+数字 的 id 换成 p1、p2…（同一张始终同一个）。 */
const anonIds = new Map();
const anon = (_key, value) => {
  if (typeof value !== 'string' || !/^r\d{4,}$/i.test(value)) return value;
  if (!anonIds.has(value.toLowerCase())) anonIds.set(value.toLowerCase(), `p${anonIds.size + 1}`);
  return anonIds.get(value.toLowerCase());
};
const want = (name) => only.size === 0 || only.has(name);

function ipadTouch() {
  Object.defineProperty(Navigator.prototype, 'maxTouchPoints', { configurable: true, get: () => 5 });
  Object.defineProperty(Navigator.prototype, 'platform', { configurable: true, get: () => 'MacIntel' });
}

async function settle(page) {
  for (let attempt = 0; attempt < 5; attempt += 1) {
    await page.waitForSelector('.app[data-core="1"]', { timeout: 60_000 });
    const reloaded = await page.waitForEvent('framenavigated', { timeout: 4_000 }).then(() => true).catch(() => false);
    if (!reloaded) return;
  }
  throw new Error('page kept reloading');
}

async function openPage(browser, label, options) {
  const ctx = await browser.newContext({ deviceScaleFactor: 1, ...options });
  const page = await ctx.newPage();
  page.setDefaultTimeout(30_000);
  lastPage = page;
  page.on('pageerror', (err) => out.pageErrors.push(`${label}: ${err.message.slice(0, 200)}`));
  await page.goto(url);
  await settle(page);
  await page.evaluate(() => localStorage.clear());
  return { ctx, page };
}

const frames = (page, n = 2) => page.evaluate((k) => new Promise((done) => {
  const step = (left) => (left <= 0 ? done() : requestAnimationFrame(() => step(left - 1)));
  step(k);
}), n);

async function esc(page) {
  await page.keyboard.press('Escape');
  await frames(page, 3);
}

/** 现在焦点在哪儿：在不在某个面板里、是不是某个选择器。 */
function focusFacts(page, sheetSel, extra = {}) {
  return page.evaluate(([sel, ex]) => {
    const a = document.activeElement;
    const res = { tag: a?.tagName ?? null, inSheet: Boolean(sel && a && document.querySelector(sel)?.contains(a)) };
    for (const [k, s] of Object.entries(ex)) res[k] = Boolean(a && a === document.querySelector(s));
    return res;
  }, [sheetSel, extra]);
}

const count = (page, sel) => page.locator(sel).count();
const screen = (page) => page.evaluate(() => document.querySelector('.app')?.dataset.screen ?? '');
const title = (page) => page.evaluate(() => document.querySelector('.nav .t b')?.textContent?.trim() ?? '');

/** 没有这些按键的老版本上量完一条后回到普通网格（用按钮），后面几条照样能量。 */
async function ensureGrid(page) {
  if ((await screen(page)) === 'editor') await backByButton(page);
  if (await count(page, '.nav .nb.pick-cancel')) await page.locator('.nav .nb.pick-cancel').click();
  if ((await title(page)).startsWith('已选')) await page.locator('.nav .nb.bold').click();
  await frames(page, 2);
}

/** 打开一个面板（`open` 点开），量焦点，按 Tab 看还在不在里面，按一次 Esc，看它关没关、还在不在同一屏。 */
async function escSheet(page, open, sheetSel, extraFocus = {}) {
  await open();
  await page.waitForSelector(sheetSel, { timeout: 30_000 });
  const before = await screen(page);
  await frames(page, 2);
  const focusOpen = await focusFacts(page, sheetSel);
  await page.keyboard.press('Tab');
  const focusTab = await focusFacts(page, sheetSel);
  await page.keyboard.press('Shift+Tab');
  await page.keyboard.press('Shift+Tab');
  const focusBack = await focusFacts(page, sheetSel);
  await esc(page);
  const closed = (await count(page, sheetSel)) === 0;
  const after = await focusFacts(page, null, extraFocus);
  if (!closed) {
    await page.locator('.scrim').last().click({ position: { x: 4, y: 4 } }).catch(() => {});
    await frames(page, 2);
  }
  return { closed, sameScreen: (await screen(page)) === before, focusOpen: focusOpen.inSheet, focusTab: focusTab.inSheet, focusBack: focusBack.inSheet, after };
}

/** 截图前把照片名、日期和时间换成占位（隐私）。 */
async function redact(page) {
  await page.evaluate(() => {
    for (const node of document.querySelectorAll('.nav small, .gh .rng, .day, .dock small, .pastebar small')) node.textContent = '〈时〉';
  });
}

/** 网格里按 DOM 顺序数格子，返回焦点在第几格（从 1 数）和它在不在可见区里。 */
function activeCell(page) {
  return page.evaluate(() => {
    const cells = [...document.querySelectorAll('.gscroll .cell')];
    const a = document.activeElement;
    const at = cells.indexOf(a);
    const box = a?.getBoundingClientRect();
    const sc = document.querySelector('.gscroll')?.getBoundingClientRect();
    return {
      id: a?.dataset?.id ?? null,
      domIndex: at,
      isFirst: at === 0,
      isLast: at >= 0 && at === cells.length - 1,
      visible: Boolean(box && sc && box.top >= sc.top - 1 && box.bottom <= sc.bottom + 1),
    };
  });
}

/** 网格的版式事实：列数、第一组有几张、按 id 排好的格子顺序（只有 id 的序号，不出文件名）。 */
function gridFacts(page) {
  return page.evaluate(() => {
    const inner = document.querySelector('.gscroll .inner');
    const cells = [...document.querySelectorAll('.gscroll .cell')];
    const first = cells[0]?.getBoundingClientRect();
    const sameRow = cells.filter((c) => Math.abs(c.getBoundingClientRect().top - (first?.top ?? 0)) < 1).length;
    return { cols: Number(inner?.style.getPropertyValue('--grid-cols') || 0), firstRow: sameRow, rendered: cells.length };
  });
}

/** 网格的「下一行同一列」：按格子的位置量，不靠列数。 */
function belowOf(page, id) {
  return page.evaluate((want) => {
    const cells = [...document.querySelectorAll('.gscroll .cell')];
    const me = cells.find((c) => c.dataset.id === want)?.getBoundingClientRect();
    if (!me) return null;
    const rows = [...new Set(cells.map((c) => Math.round(c.getBoundingClientRect().top)))].sort((a, b) => a - b);
    const next = rows.find((t) => t > Math.round(me.top));
    const line = cells.filter((c) => Math.round(c.getBoundingClientRect().top) === next);
    const col = cells.filter((c) => Math.round(c.getBoundingClientRect().top) === Math.round(me.top)).findIndex((c) => c.dataset.id === want);
    return line[Math.min(col, line.length - 1)]?.dataset.id ?? null;
  }, id);
}

async function openCellByKey(page, id) {
  await page.locator(`.gscroll .cell[data-id="${id}"]`).focus();
  await page.keyboard.press('Enter');
  await page.waitForFunction(() => document.querySelector('.app')?.dataset.screen === 'editor' && document.querySelector('.editor .nav'), null, { timeout: 120_000 });
  await page.waitForSelector('.editor .toolbar [data-act="export"]', { timeout: 60_000 });
}

/** 没选相机配方文件时编辑器会弹「还差相机配方文件」：一次 Esc 关掉它，记下关没关、还在不在编辑器。 */
async function dismissAsk(page, step, wait = 2_500) {
  const shown = await page.waitForSelector('.ask-sheet', { timeout: wait }).then(() => true).catch(() => false);
  if (!shown) return;
  await esc(page);
  step.askEsc ??= [];
  step.askEsc.push({ closed: (await count(page, '.ask-sheet')) === 0, screen: await screen(page) });
}

const pos = (page) => page.evaluate(() => document.querySelector('.editor .nav .t small')?.textContent?.trim() ?? '');

async function backByButton(page) {
  await page.locator('.editor .nav .nav-back').click();
  await page.waitForFunction(() => document.querySelector('.app')?.dataset.screen === 'grid', null, { timeout: 30_000 });
  await frames(page, 2);
}

/** 电脑 1440×900：网格的全部条目，再加编辑器里 iPad 版式下的面板。 */
async function computerGrid(browser) {
  const { ctx, page } = await openPage(browser, 'computer', { viewport: { width: 1440, height: 900 } });
  const step = {};
  out.steps.computer = step;
  step.env = await page.evaluate(() => ({ touch: navigator.maxTouchPoints, fine: matchMedia('(pointer: fine)').matches, hover: matchMedia('(hover: hover)').matches }));

  // 开始页的帮助面板
  step.help = await escSheet(page, () => page.locator('.help .lnk').click(), '.ms:has(.lrow .step)', { opener: '.help .lnk' });

  await page.locator('input.filepick:not(.calibpick)').setInputFiles(manifest.files53);
  await page.waitForFunction(
    (n) => {
      const sub = document.querySelector('.nav small');
      return sub && !sub.textContent.includes('正在读取') && sub.textContent.includes(`${n} 张`);
    },
    manifest.count53,
    { timeout: 600_000 },
  );
  step.layout = await page.evaluate(() => document.querySelector('.app')?.dataset.layout ?? '');
  step.grid = await gridFacts(page);

  // 悬停：手形和提亮
  const firstId = await page.locator('.gscroll .cell').first().getAttribute('data-id');
  const hoverOf = () => page.evaluate(() => {
    const cell = document.querySelector('.gscroll .cell');
    const cv = cell?.querySelector('canvas');
    return { cursor: getComputedStyle(cell).cursor, filter: cv ? getComputedStyle(cv).filter : '', ring: getComputedStyle(cell, '::before').boxShadow };
  });
  step.hoverBefore = await hoverOf();
  await page.locator('.gscroll .cell').first().hover();
  await page.waitForTimeout(250);
  step.hover = await hoverOf();
  await redact(page);
  await page.screenshot({ path: path.join(manifest.shots, 'computer-hover.png') });
  await page.mouse.move(5, 450);

  // 网格 ⋯ 菜单
  step.menu = await escSheet(page, () => page.locator('.nav .nb.plain').click(), '.ms:has(.seg)', { opener: '.nav .nb.plain' });

  // 「选择」：Shift 点一段、Cmd/Ctrl 点一张、Cmd/Ctrl+A、Esc 离开
  const mod = await page.evaluate(() => (/Mac/i.test(navigator.platform) ? 'Meta' : 'Control'));
  const ids = await page.locator('.gscroll .cell').evaluateAll((cells) => cells.slice(0, 6).map((c) => c.dataset.id));
  await page.locator('.nav .nb.bold').click();
  await page.locator(`.cell[data-id="${ids[0]}"]`).click();
  await page.locator(`.cell[data-id="${ids[4]}"]`).click({ modifiers: ['Shift'] });
  step.shiftRange = await title(page);
  // 再 Shift 点第 3 张：换成锚点到这张这一段（访达的做法），再点回第 5 张
  await page.locator(`.cell[data-id="${ids[2]}"]`).click({ modifiers: ['Shift'] });
  step.shiftShrink = await title(page);
  await page.locator(`.cell[data-id="${ids[4]}"]`).click({ modifiers: ['Shift'] });
  await page.locator(`.cell[data-id="${ids[2]}"]`).click({ modifiers: [mod] });
  step.modOff = await title(page);
  await page.locator(`.cell[data-id="${ids[2]}"]`).click({ modifiers: [mod] });
  step.modOn = await title(page);
  await page.locator(`.cell[data-id="${ids[0]}"]`).focus();
  await page.keyboard.press(`${mod}+A`);
  step.selectAll = await title(page);
  await esc(page);
  step.selectEsc = { title: await title(page), sel: await count(page, '.gscroll.sel'), choose: (await page.locator('.nav .nb.bold').innerText()).trim() };
  await ensureGrid(page);
  // 不在选择模式：Cmd/Ctrl 点一张进选择模式并勾上它，不打开编辑器
  await page.locator(`.cell[data-id="${ids[3]}"]`).click({ modifiers: [mod] });
  await frames(page, 2);
  step.modEnter = { title: await title(page), screen: await screen(page) };
  await esc(page);
  step.modEnterEsc = await title(page);
  await ensureGrid(page);

  // 方向键：从一整行（9 张）的第一张开始，下一行同一组，↓ 落在往后第 9 张（「第 1 张 → 第 10 张」）
  const start = await page.evaluate(() => {
    const rows = [...document.querySelectorAll('.gscroll .cells')];
    const row = rows.find((r) => r.children.length === 9 && r.nextElementSibling?.classList.contains('cells'));
    const cells = [...document.querySelectorAll('.gscroll .cell')];
    return row ? { id: row.firstElementChild.dataset.id, at: cells.indexOf(row.firstElementChild) } : null;
  });
  step.arrowStart = start ? start.at : null;
  const from = start?.id ?? firstId;
  await page.locator(`.cell[data-id="${from}"]`).focus();
  const below = await belowOf(page, from);
  await page.keyboard.press('ArrowDown');
  step.arrowDown = { ...(await activeCell(page)), want: below };
  await page.keyboard.press('ArrowRight');
  step.arrowRight = await activeCell(page);
  step.arrowRightWant = await page.evaluate((id) => {
    const cells = [...document.querySelectorAll('.gscroll .cell')];
    return cells[cells.findIndex((c) => c.dataset.id === id) + 1]?.dataset.id ?? null;
  }, below);
  await page.keyboard.press('ArrowLeft');
  step.arrowLeft = await activeCell(page);
  await page.keyboard.press('ArrowUp');
  step.arrowUp = { ...(await activeCell(page)), want: from };
  await page.keyboard.press('End');
  await frames(page, 2);
  step.end = await activeCell(page);
  await page.keyboard.press('Home');
  await frames(page, 2);
  step.home = { ...(await activeCell(page)), want: firstId };
  // Enter 仍然是按钮的「点开」
  await page.keyboard.press('Enter');
  await page.waitForFunction(() => document.querySelector('.app')?.dataset.screen === 'editor', null, { timeout: 120_000 }).catch(() => {});
  step.enterOpens = await screen(page);
  await esc(page);
  await page.waitForFunction(() => document.querySelector('.app')?.dataset.screen === 'grid', null, { timeout: 30_000 }).catch(() => {});
  step.escFromEditor = await screen(page);
  await ensureGrid(page);

  // 回到网格时的位置：滚到底、点开最后一张、〈 照片
  await page.locator('.gscroll').evaluate((s) => { s.scrollTop = s.scrollHeight; });
  await frames(page, 3);
  const lastId = await page.locator('.gscroll .cell').last().getAttribute('data-id');
  const topBefore = await page.locator('.gscroll').evaluate((s) => s.scrollTop);
  await page.locator(`.cell[data-id="${lastId}"]`).click();
  await page.waitForFunction(() => document.querySelector('.app')?.dataset.screen === 'editor', null, { timeout: 120_000 });
  await page.waitForSelector('.editor .nav .nav-back');
  await backByButton(page);
  step.position = { topBefore, topAfter: await page.locator('.gscroll').evaluate((s) => s.scrollTop), focus: await activeCell(page), want: lastId };

  // 点开第一张，编辑器里按 → 换到下一张，再 Esc 回网格：焦点在第二张
  await page.locator('.gscroll').evaluate((s) => { s.scrollTop = 0; });
  await frames(page, 3);
  const pair = await page.locator('.gscroll .cell').evaluateAll((cells) => cells.slice(0, 2).map((c) => c.dataset.id));
  await openCellByKey(page, pair[0]);
  await dismissAsk(page, step);
  const posBefore = await pos(page);
  await page.evaluate(() => document.activeElement instanceof HTMLElement && document.activeElement.blur());
  await page.keyboard.press('ArrowRight');
  await page.waitForFunction((p) => document.querySelector('.editor .nav .t small')?.textContent?.trim() !== p, posBefore, { timeout: 30_000 }).catch(() => {});
  step.editorArrow = { before: posBefore, after: await pos(page) };
  await dismissAsk(page, step);
  await page.waitForSelector('.editor .toolbar [data-act="export"]', { timeout: 60_000 });
  // 电脑上单击照片不再收起面板（desktop-stage）；在「调整」里按 F 进全图：Esc 先展开、还是「调整」，不回网格
  await page.locator('.editor .toolbar [data-tool="adjust"]').click();
  await page.evaluate(() => document.activeElement instanceof HTMLElement && document.activeElement.blur());
  await page.keyboard.press('f');
  await page.waitForFunction(() => document.querySelector('.screen.editor')?.dataset.collapsed === '1', null, { timeout: 5_000 }).catch(() => {});
  step.collapsed = await page.evaluate(() => document.querySelector('.screen.editor')?.dataset.collapsed ?? '');
  await page.evaluate(() => document.activeElement instanceof HTMLElement && document.activeElement.blur());
  await esc(page);
  step.collapsedEsc = {
    collapsed: await page.evaluate(() => document.querySelector('.screen.editor')?.dataset.collapsed ?? ''),
    screen: await screen(page),
    tool: await page.evaluate(() => document.querySelector('.editor .toolbar .tbtn.on')?.dataset.tool ?? ''),
  };
  // 滑块拿着方向键
  await dismissAsk(page, step, 10_000);
  await page.locator('.editor .toolbar [data-tool="adjust"]').click();
  const slider = page.locator('.editor .band .panel.adjust input[type="range"]').first();
  await slider.waitFor({ state: 'visible', timeout: 30_000 });
  const posSlider = await pos(page);
  const v0 = await slider.inputValue();
  await slider.focus();
  await page.keyboard.press('ArrowRight');
  await frames(page, 2);
  step.sliderArrow = { posBefore: posSlider, posAfter: await pos(page), valueBefore: v0, valueAfter: await slider.inputValue(), disabled: await slider.isDisabled() };
  await page.evaluate(() => document.activeElement instanceof HTMLElement && document.activeElement.blur());
  // 拖滑块时按 Esc：不切屏，松手后 data-drag 不留着
  const sbox = await slider.boundingBox();
  if (sbox && !step.sliderArrow.disabled) {
    await page.mouse.move(sbox.x + sbox.width * 0.5, sbox.y + sbox.height / 2);
    await page.mouse.down();
    await page.mouse.move(sbox.x + sbox.width * 0.6, sbox.y + sbox.height / 2, { steps: 4 });
    const midDrag = await page.evaluate(() => document.querySelector('.app')?.dataset.drag ?? '');
    await page.keyboard.press('Escape');
    await frames(page, 2);
    const screenMid = await screen(page);
    await page.mouse.up();
    await frames(page, 3);
    step.dragEsc = { midDrag, screenMid, dragAfter: await page.evaluate(() => document.querySelector('.app')?.dataset.drag ?? '') };
  }
  await esc(page);
  await page.waitForFunction(() => document.querySelector('.app')?.dataset.screen === 'grid', null, { timeout: 30_000 }).catch(() => {});
  step.second = { focus: await activeCell(page), want: pair[1] };
  await ensureGrid(page);

  // 编辑器顶栏 ⋯（键盘打开）、导出选项、关机图（自己处理 Esc）、照片信息
  await openCellByKey(page, pair[0]);
  await dismissAsk(page, step);
  await page.locator('.editor .nav [data-act="editor-more"]').focus();
  step.editorMore = await escSheet(page, () => page.keyboard.press('Enter'), '.ms[data-state="editor-more"]', { opener: '.editor .nav [data-act="editor-more"]' });
  const viaMore = (act, sel) => escSheet(page, async () => {
    await page.locator('.editor .nav [data-act="editor-more"]').click();
    await page.locator(`.ms[data-state="editor-more"] [data-act="${act}"]`).click();
  }, sel, { opener: '.editor .nav [data-act="editor-more"]' });
  step.exportOptions = await viaMore('more-export-options', '.ms.export');
  step.photoInfo = await viaMore('more-photo-info', '.ms.paste-sheet');
  step.offImage = await viaMore('more-off-image', '.ms.off-image-sheet');
  step.editorStill = await screen(page);

  // 复制调整 → 选照片… → 网格的「选要套用的照片」：Cmd/Ctrl+A 全选，Esc 离开
  await page.locator('.editor .toolbar [data-tool="adjust"]').click();
  await slider.evaluate((input) => {
    input.value = String(Number(input.min) + (Number(input.max) - Number(input.min)) * 0.7);
    input.dispatchEvent(new Event('input', { bubbles: true }));
    input.dispatchEvent(new Event('change', { bubbles: true }));
  });
  await frames(page, 2);
  step.copySheet = await escSheet(page, async () => {
    await page.locator('.editor .nav [data-act="editor-more"]').click();
    await page.locator('.ms[data-state="editor-more"] [data-act="more-copy"]').click();
  }, '.ms[data-state="copy-options"]', { opener: '.editor .nav [data-act="editor-more"]' });
  await page.locator('.editor .nav [data-act="editor-more"]').click();
  await page.locator('.ms[data-state="editor-more"] [data-act="more-copy"]').click();
  await page.locator('.ms[data-state="copy-options"] .btn.primary').click();
  await page.waitForSelector('.ms[data-state="copied-where"]', { timeout: 30_000 });
  await page.locator('.ms[data-state="copied-where"] .acts .btn:not(.ghost)').click();
  await page.waitForFunction(() => document.querySelector('.app')?.dataset.screen === 'grid' && document.querySelector('.nav .t b')?.textContent === '选要套用的照片', null, { timeout: 30_000 });
  const mod2 = mod;
  await page.locator('.gscroll .cell').nth(2).click();
  await page.locator('.gscroll .cell').nth(5).click({ modifiers: ['Shift'] });
  step.pasteShift = await page.evaluate(() => document.querySelector('.nav .t small')?.textContent?.trim() ?? '');
  await page.keyboard.press(`${mod2}+A`);
  step.pasteAll = await page.evaluate(() => ({ sub: document.querySelector('.nav .t small')?.textContent?.trim() ?? '', go: document.querySelector('[data-act="paste-picked"]')?.textContent?.trim() ?? '' }));
  await page.locator('.nav .nb.bold').click(); // 「全选」按钮：和 Cmd+A 一样
  step.pasteAllButton = await page.evaluate(() => document.querySelector('[data-act="paste-picked"]')?.textContent?.trim() ?? '');
  await esc(page);
  step.pasteEsc = await title(page);
  await ensureGrid(page);

  // 清空前确认
  await page.locator('.nav .nb.plain').click();
  await page.locator('.ms .lrow:has(b.bad)').click();
  await page.waitForSelector('.dlg[data-state="confirm-clear"]', { timeout: 10_000 }).catch(() => {});
  step.clear = { dialog: (await count(page, '.dlg[data-state="confirm-clear"]')) === 1, text: (await page.locator('.dlg[data-state="confirm-clear"]').innerText().catch(() => '')).replace(/\s+/g, ' ').trim(), focusIn: (await focusFacts(page, '.dlg[data-state="confirm-clear"]')).inSheet };
  if (step.clear.dialog) {
    await redact(page);
    await page.screenshot({ path: path.join(manifest.shots, 'computer-clear-confirm.png') });
  }
  if (!step.clear.dialog) {
    step.clearGo = await screen(page);
    await ctx.close();
    return;
  }
  await esc(page);
  step.clearEsc = { dialog: await count(page, '.dlg[data-state="confirm-clear"]'), screen: await screen(page), cells: (await count(page, '.gscroll .cell')) > 0 };
  await page.locator('.nav .nb.plain').click();
  await page.locator('.ms .lrow:has(b.bad)').click();
  await page.locator('.dlg[data-state="confirm-clear"] [data-act="clear-confirm"]').click().catch(() => {});
  await frames(page, 3);
  step.clearGo = await screen(page);
  await ctx.close();
}

/** 电脑小窗口（手机版式）：底栏「更多」面板、曲线、配方筛选、配方状态、LUT 色彩空间、标志的文字框。 */
async function computerPhoneLayout(browser) {
  const { ctx, page } = await openPage(browser, 'computerPhone', { viewport: { width: 430, height: 900 } });
  const step = {};
  out.steps.computerPhone = step;
  await pickAll(page, { count: 2 }, manifest.files2);
  step.layout = await page.evaluate(() => document.querySelector('.app')?.dataset.layout ?? '');
  const ids = await page.locator('.gscroll .cell').evaluateAll((cells) => cells.slice(0, 2).map((c) => c.dataset.id));
  const edits = (id) => page.evaluate((want) => {
    const row = window.__gxrEdits?.().find((item) => item.stem.toLowerCase() === want) ?? null;
    return row ? JSON.stringify({ choice: row.choice, levels: row.levels, knobs: row.knobs, curve: row.curve }) : null;
  }, id);
  await openCellByKey(page, ids[0]);
  const s0 = await edits(ids[0]);
  step.more = await escSheet(page, () => page.locator('.editor .toolbar [data-tool="more"]').click(), '.more-sheet', { opener: '.editor .toolbar [data-tool="more"]' });
  const s1 = await edits(ids[0]);
  await page.locator('.editor .toolbar [data-tool="more"]').click();
  await page.locator('.more-sheet [data-act="close"]').click();
  step.moreState = { esc: s1 === s0, button: (await edits(ids[0])) === s0 };

  // 曲线：同样拖一下，一张用 Esc 关，另一张用「完成」关，两张的结果一样
  const curveDrag = async () => {
    await page.locator('.editor .toolbar [data-tool="more"]').click();
    await page.locator('.more-sheet [data-more="curves"]').click();
    await page.waitForSelector('.curves-sheet svg.curve');
    const box = await page.locator('.curves-sheet svg.curve').boundingBox();
    await page.mouse.move(box.x + box.width * 0.5, box.y + box.height * 0.5);
    await page.mouse.down();
    await page.mouse.move(box.x + box.width * 0.5, box.y + box.height * 0.35, { steps: 3 });
    await page.mouse.up();
    await frames(page, 3);
    return focusFacts(page, '.curves-sheet');
  };
  const focusCurve = await curveDrag();
  await esc(page);
  step.curvesEsc = { closed: (await count(page, '.curves-sheet')) === 0, screen: await screen(page), focusInSheet: focusCurve.inSheet };
  if (!step.curvesEsc.closed) await page.locator('.curves-sheet [data-act="close"]').click();
  const curveEsc = await edits(ids[0]);
  await page.keyboard.press('ArrowRight');
  await page.waitForFunction(() => document.querySelector('.editor .nav .t small')?.textContent?.trim().startsWith('2'), null, { timeout: 60_000 }).catch(() => {});
  step.arrowNext = await pos(page);
  if (!step.arrowNext.startsWith('2')) {
    await backByButton(page);
    await openCellByKey(page, ids[1]);
  }
  await page.waitForSelector('.editor .toolbar [data-act="export"]');
  await curveDrag();
  await page.locator('.curves-sheet [data-act="close"]').click();
  await frames(page, 2);
  const curveDone = await edits(ids[1]);
  step.curvesState = { same: curveEsc !== null && curveEsc === curveDone, changed: curveEsc !== s0 };

  // 时间戳：打了字、焦点在按钮上按 Esc 关，字要留下（和点遮罩、「完成」一样；评审 r1 P1）
  const openStamp = async () => {
    step.stampAt = 'more';
    await page.locator('.editor .toolbar [data-tool="more"]').click();
    step.stampAt = 'row';
    await page.locator('.more-sheet [data-more="stamp"]').click();
    step.stampAt = 'detail';
    // 「时间戳」先换到时间戳条，再点条上的「更多选项」才是这张工作表
    await page.waitForSelector('.stamp-band [data-act="stamp-sheet"]', { state: 'attached' });
    await page.evaluate(() => document.querySelector('.stamp-band [data-act="stamp-sheet"]').click());
    await page.waitForSelector('.stamp-sheet');
    return page.locator('.stamp-sheet');
  };
  let stamp = await openStamp();
  step.stampAt = 'content';
  // 时间戳关着时内容按钮是灰的：先打开开关
  if (!(await stamp.locator('input.switch').first().isChecked())) await stamp.locator('input.switch').first().click({ force: true });
  await stamp.getByRole('button', { name: '文字', exact: true }).click();
  step.stampAt = 'typed';
  await stamp.locator('.txtbox').fill('ESC KEEP');
  await page.waitForTimeout(400); // 过了 120 ms 的预览刷新
  await stamp.getByRole('button', { name: '文字', exact: true }).focus();
  await esc(page);
  const stampClosed = (await count(page, '.stamp-sheet')) === 0;
  stamp = await openStamp();
  const kept = (await stamp.locator('.txtbox').inputValue().catch(() => '')) === 'ESC KEEP';
  await esc(page);
  step.stampEsc = { closed: stampClosed, kept };

  // 配方筛选、配方状态（需要相机配方文件）
  await page.locator('.editor .toolbar [data-tool="recipe"]').click();
  await page.waitForSelector('.editor .panel.recipe', { timeout: 60_000 });
  if (await page.locator('.editor .panel.recipe [data-act="recipe-filter"]').count()) {
    step.filter = await escSheet(page, () => page.locator('.editor .panel.recipe [data-act="recipe-filter"]').first().click(), '.filter-sheet', { opener: '.editor .panel.recipe [data-act="recipe-filter"]' });
  } else step.filter = { missing: true };
  if (await page.locator('.editor .panel.recipe [data-act="recipe-status"]').count()) {
    step.status = await escSheet(page, () => page.locator('.editor .panel.recipe [data-act="recipe-status"]').first().click(), '.recipe-info-sheet');
  } else step.status = { missing: true };

  // LUT：导入一份没写色彩空间的 .cube，点它会先问输入色彩空间
  await page.locator('.editor .band .panel.recipe [data-mode="lut"]').click().catch(() => {});
  await page.locator('input.lutpick').setInputFiles(manifest.lut);
  const lutCard = page.locator('.editor .band .panel.lut .strip .rc[data-lut]:not([data-lut="none"]):not([data-lut="deleted"])').first();
  await lutCard.waitFor({ state: 'visible', timeout: 30_000 }).catch(() => {});
  if (await page.locator('.ms.lut-space-sheet').count()) {
    step.lutSpace = await escSheet(page, async () => undefined, '.ms.lut-space-sheet');
  } else {
    step.lutSpace = await escSheet(page, () => lutCard.click(), '.ms.lut-space-sheet');
  }
  await page.locator('.editor .band .panel.lut [data-mode="recipe"]').click().catch(() => {});

  // 标志的「名称」文字框里按 Esc：只归文字框，不关面板、不回网格
  await page.locator('.editor .toolbar [data-tool="more"]').click();
  await page.locator('.more-sheet [data-more="frame"]').click();
  await page.locator('.editor .band .panel.frame .strip .rc[data-style="leica"], .editor .band .panel.frame .strip .rc:not([data-style="none"])').first().click();
  await page.locator('.editor .band .band-frame-hd [data-act="frame-opts"], .editor .band .panel.frame .strip .rc.on').first().click();
  await page.locator('.editor .band .panel.frame [data-act="frame-logo"]').click();
  await page.locator('.ms.logo-sheet [data-logo-action="upload"]').click();
  const nameBox = page.locator('.ms.logo-sheet input[aria-label="名称"]');
  await nameBox.click();
  await nameBox.type('ab');
  await page.keyboard.press('Escape');
  await frames(page, 3);
  await page.keyboard.press('Escape');
  await frames(page, 3);
  step.logoField = { sheet: await count(page, '.ms.logo-sheet'), screen: await screen(page), focus: await page.evaluate(() => document.activeElement?.getAttribute('aria-label') ?? '') };
  await page.locator('.ms.logo-sheet .hd .nb', { hasText: '取消' }).click().catch(() => {});
  await ctx.close();
}

/** 网格「读不了」、拔卡、相机配方文件不匹配：各一次 Esc。 */
async function computerOthers(browser) {
  const step = {};
  out.steps.others = step;
  {
    const { ctx, page } = await openPage(browser, 'bad', { viewport: { width: 1440, height: 900 } });
    await pickAll(page, { count: 2 }, manifest.badFiles);
    const bad = page.locator('.gscroll .cell.err').first();
    await bad.waitFor({ timeout: 60_000 });
    // 读失败时这张面板会自己弹出来：先用 Esc 关掉它，再点格子打开一次量焦点。
    const auto = await page.waitForSelector('[data-state="unreadable"]', { timeout: 5_000 }).then(() => true).catch(() => false);
    if (auto) {
      await esc(page);
      step.unreadableAuto = (await count(page, '[data-state="unreadable"]')) === 0;
      if (!step.unreadableAuto) await page.locator('.scrim').last().click({ position: { x: 4, y: 4 } });
    }
    step.unreadable = await escSheet(page, () => bad.click(), '[data-state="unreadable"]', { opener: '.gscroll .cell.err' });
    await ctx.close();
  }
  {
    const { ctx, page } = await openPage(browser, 'card', { viewport: { width: 1440, height: 900 } });
    // 缩略图还在读的时候拔卡（删掉文件夹）：网格弹出拔卡提示。
    await page.locator('input.filepick:not(.calibpick)').setInputFiles(manifest.cardFiles);
    rmSync(manifest.cardDir, { recursive: true, force: true });
    await page.waitForSelector('[data-state="card-removed"]', { timeout: 120_000 }).catch(async () => {
      step.cardScreen = await screen(page);
    });
    step.card = await escSheet(page, async () => undefined, '[data-state="card-removed"]');
    await ctx.close();
  }
  {
    const { ctx, page } = await openPage(browser, 'mismatch', { viewport: { width: 1440, height: 900 } });
    await pickAll(page, { count: 1 }, manifest.mismatchFiles);
    step.mismatch = await escSheet(page, async () => {
      await page.locator('.gscroll .cell').first().click();
    }, '.ask-sheet[data-state="camfiles-mismatch"]');
    await ctx.close();
  }
}

/** 3b5b535d 上没有这些：拿得到的事实先记下，Python 一条条判。 */
async function webkitComputer(webkit) {
  const browser = await webkit.launch({ headless: true, executablePath: webkitExecutable(webkit) });
  try {
    const { ctx, page } = await openPage(browser, 'webkitComputer', { viewport: { width: 1440, height: 900 } });
    const step = {};
    out.steps.webkitComputer = step;
    step.env = await page.evaluate(() => ({ touch: navigator.maxTouchPoints, fine: matchMedia('(pointer: fine)').matches }));
    await pickAll(page, { count: 2 }, manifest.files2);
    step.cursor = await page.evaluate(() => getComputedStyle(document.querySelector('.gscroll .cell')).cursor);
    step.menu = await escSheet(page, () => page.locator('.nav .nb.plain').click(), '.ms:has(.seg)');
    await page.locator('.nav .nb.bold').click();
    await page.locator('.gscroll .cell').first().click();
    await page.locator('.gscroll .cell').nth(1).click({ modifiers: ['Shift'] });
    step.shift = await title(page);
    await esc(page);
    step.selectEsc = await title(page);
    await ensureGrid(page);
    const first = await page.locator('.gscroll .cell').first().getAttribute('data-id');
    await openCellByKey(page, first);
    await page.evaluate(() => document.activeElement instanceof HTMLElement && document.activeElement.blur());
    const p0 = await pos(page);
    await page.keyboard.press('ArrowRight');
    await page.waitForFunction((p) => document.querySelector('.editor .nav .t small')?.textContent?.trim() !== p, p0, { timeout: 30_000 }).catch(() => {});
    step.editorArrow = { before: p0, after: await pos(page) };
    await esc(page);
    await page.waitForFunction(() => document.querySelector('.app')?.dataset.screen === 'grid', null, { timeout: 30_000 }).catch(() => {});
    step.escBack = await screen(page);
    step.focus = await activeCell(page);
    await ensureGrid(page);
    await page.locator('.nav .nb.plain').click();
    await page.locator('.ms .lrow:has(b.bad)').click();
    await frames(page, 2);
    step.clearDialog = await count(page, '.dlg[data-state="confirm-clear"]');
    await ctx.close();
  } finally {
    await browser.close();
  }
}

/** 有触摸的 WebKit 手机和 iPad：格子不变成手形、清空仍是点一下。 */
async function webkitTouch(webkit) {
  const browser = await webkit.launch({ headless: true, executablePath: webkitExecutable(webkit) });
  try {
    for (const [label, options, init] of [
      ['webkitPhone', { viewport: { width: 390, height: 844 }, deviceScaleFactor: 2 }, null],
      ['webkitIpad', { viewport: { width: 1032, height: 1376 }, deviceScaleFactor: 2, userAgent: IPAD_UA }, ipadTouch],
    ]) {
      const ctx = await browser.newContext({ ...options, hasTouch: true, isMobile: true });
      if (init) await ctx.addInitScript(init);
      const page = await ctx.newPage();
      page.on('pageerror', (err) => out.pageErrors.push(`${label}: ${err.message.slice(0, 200)}`));
      await page.goto(url);
      await settle(page);
      const step = {};
      out.steps[label] = step;
      step.env = await page.evaluate(() => ({ touch: navigator.maxTouchPoints, fine: matchMedia('(pointer: fine)').matches, hover: matchMedia('(hover: hover)').matches }));
      await pickAll(page, { count: 2 }, manifest.files2);
      step.cursor = await page.evaluate(() => getComputedStyle(document.querySelector('.gscroll .cell')).cursor);
      step.ring = await page.evaluate(() => getComputedStyle(document.querySelector('.gscroll .cell'), '::before').boxShadow);
      await page.locator('.nav .nb.plain').tap();
      await page.locator('.ms .lrow:has(b.bad)').tap();
      await frames(page, 3);
      step.clearDialog = await count(page, '.dlg[data-state="confirm-clear"]');
      step.clearScreen = await screen(page);
      await ctx.close();
    }
  } finally {
    await browser.close();
  }
}

function webkitExecutable(webkit) {
  const direct = webkit.executablePath();
  if (direct && existsSync(direct)) return direct;
  const base = direct ? path.basename(direct) : 'pw_run.sh';
  for (const dir of BROWSER_DIRS) {
    if (!existsSync(dir)) continue;
    for (const entry of readdirSync(dir)) {
      if (!entry.startsWith('webkit-')) continue;
      const candidate = path.join(dir, entry, base);
      if (existsSync(candidate)) return candidate;
    }
  }
  return null;
}

async function guarded(name, fn) {
  if (!want(name)) return;
  try {
    await fn();
  } catch (err) {
    out.steps[`${name}Error`] = String(err?.message ?? err).split('\n').slice(0, 2).join(' | ').slice(0, 400);
    if (manifest.debugShots && lastPage) await lastPage.screenshot({ path: path.join(manifest.debugShots, `${name}-error.png`) }).catch(() => {});
  }
}

try {
  const chromium = await launchPinnedChromium({ args: GPU_ARGS });
  try {
    await guarded('computer', () => computerGrid(chromium));
    await guarded('computerPhone', () => computerPhoneLayout(chromium));
    await guarded('others', () => computerOthers(chromium));
  } finally {
    await chromium.close();
  }
  const { webkit } = await import(pathToFileURL(pinnedRuntime.modulePath).href);
  if (!webkitExecutable(webkit)) out.steps.webkitMissing = true;
  else {
    await guarded('webkitComputer', () => webkitComputer(webkit));
    await guarded('webkitTouch', () => webkitTouch(webkit));
  }
} catch (err) {
  out.error = String(err?.message ?? err).split('\n')[0].slice(0, 300);
}
writeFileSync(manifest.reportPath, JSON.stringify(out, anon));
process.stdout.write(`${JSON.stringify({ done: true, error: out.error !== null })}\n`);
