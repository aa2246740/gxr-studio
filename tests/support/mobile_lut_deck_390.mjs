// 手机竖屏 390×844 的 LUT 甲板度量（polish-2 第 1 项）。
// 量横条可见视口里的完整 LUT 卡、合并菜单瓷砖与菜单项热区、提示与甲板底部 22pt 的重叠，以及「配方 | LUT」切换键在两种模式下的 x。
// stdout 只打一行 JSON，不含照片名、拍摄时间或 EXIF。

import { readFileSync } from 'node:fs';
import { pathToFileURL } from 'node:url';

const [runtime, url, manifestPath] = process.argv.slice(2);
const manifest = JSON.parse(readFileSync(manifestPath, 'utf8'));
const { launchPinnedChromium, verifyPinnedRuntime } = await import(pathToFileURL(runtime).href);
verifyPinnedRuntime();

const browser = await launchPinnedChromium({
  args: ['--enable-unsafe-webgpu', '--enable-unsafe-swiftshader', '--use-angle=swiftshader', '--use-webgpu-adapter=swiftshader'],
});

function measure(page) {
  return page.evaluate(() => {
    const round = (n) => Math.round(n * 10) / 10;
    const box = (node) => {
      if (!node) return null;
      const r = node.getBoundingClientRect();
      const style = getComputedStyle(node);
      if (style.display === 'none' || r.width === 0 || r.height === 0) return null;
      return { x: round(r.x), y: round(r.y), w: round(r.width), h: round(r.height), bottom: round(r.bottom) };
    };
    const deck = document.querySelector('.editor .band-body');
    const deckBox = box(deck);
    const notice = document.querySelector('.editor .panel.lut [data-state="lut-status"]');
    const noticeBox = notice && !notice.hidden ? box(notice) : null;
    const foot = document.querySelector('.editor .panel.lut .lut-foot');
    const footBox = foot && !foot.hidden ? box(foot) : null;
    const sw = document.querySelector('.editor .panel.lut .mode-switch');
    const swBtns = [...document.querySelectorAll('.editor .panel.lut > .mode-switch .mode-btn')].map(box);
    const strip = document.querySelector('.editor .panel.lut .strip');
    const stripBox = box(strip);
    const cards = [...document.querySelectorAll('.editor .panel.lut .strip .rc')].map((node) => {
      const r = box(node);
      const name = node.querySelector('.nm');
      const nr = name ? box(name) : null;
      const nameBox = nr;
      const visibleW = r && stripBox ? Math.max(0, Math.min(r.x + r.w, stripBox.x + stripBox.w) - Math.max(r.x, stripBox.x)) : 0;
      const visibleH = r && stripBox ? Math.max(0, Math.min(r.bottom, stripBox.bottom) - Math.max(r.y, stripBox.y)) : 0;
      const fully = Boolean(r && visibleW >= r.w - 0.5 && visibleH >= r.h - 0.5);
      const nameClipped = name
        ? name.scrollWidth - name.clientWidth > 1 || (nr && deckBox && (nr.x < deckBox.x - 0.5 || nr.x + nr.w > deckBox.x + deckBox.w + 0.5))
        : false;
      return {
        lut: node.dataset.lut ?? '',
        box: r,
        name: name?.textContent ?? '',
        nameBox,
        visibleW: round(visibleW),
        fully,
        nameClipped,
      };
    });
    const photo = document.querySelector('.editor .stage .pv');
    const band = document.querySelector('.editor .band');
    const bar = document.querySelector('.editor .toolbar');
    const pin = document.querySelector('.editor .panel.lut .lut-pin');
    const menuTile = document.querySelector('.editor .panel.lut [data-act="lut-menu"]');
    const space = document.querySelector('.editor .panel.lut [data-act="lut-space"]');
    const overlap =
      noticeBox && deckBox ? round(Math.max(0, Math.min(noticeBox.bottom, deckBox.bottom) - Math.max(noticeBox.y, deckBox.bottom - 22))) : 0;
    const hitsName = (target) =>
      target
        ? cards.some((card) => {
            const name = card.nameBox;
            if (!name) return false;
            return target.x < name.x + name.w && target.x + target.w > name.x && target.y < name.y + name.h && target.y + target.h > name.y;
          })
        : false;
    const footClipped = foot
      ? foot.scrollWidth - foot.clientWidth > 1 || foot.scrollHeight - foot.clientHeight > 1
      : false;
    return {
      layout: document.querySelector('.app')?.dataset.layout ?? '',
      deck: deckBox,
      pin: box(pin),
      menuTile: box(menuTile),
      notice: noticeBox,
      noticeText: notice?.textContent ?? '',
      noticeOverlapBottom22: overlap,
      noticeHitsName: hitsName(noticeBox),
      footHitsName: hitsName(footBox),
      foot: footBox,
      footText: foot && !foot.hidden ? foot.textContent : '',
      footClipped,
      switchBox: box(sw),
      switchBtns: swBtns,
      space: box(space),
      spaceW: box(space)?.w ?? 0,
      spaceDisabled: space ? space.disabled : null,
      strip: stripBox,
      cards,
      imported: cards.filter((c) => c.lut !== 'none').map((c) => ({ w: c.box?.w ?? 0, visibleW: c.visibleW, fully: c.fully })),
      visibleCards: cards.filter((c) => c.fully && c.lut !== 'none').length,
      photo: box(photo),
      band: box(band),
      bar: box(bar),
    };
  });
}

async function probePinMenu(page) {
  await page.locator('.editor .panel.lut [data-act="lut-menu"]').click();
  await page.waitForSelector('.lut-menu-pop:popover-open', { timeout: 5_000 });
  const items = await page.evaluate(() => {
    const round = (n) => Math.round(n * 10) / 10;
    const inspect = (sel) => {
      const el = document.querySelector(`.lut-menu-pop:popover-open ${sel}`);
      if (!el) return null;
      const r = el.getBoundingClientRect();
      const cx = r.left + r.width / 2;
      const cy = r.top + r.height / 2;
      const hit = document.elementFromPoint(cx, cy);
      return {
        w: round(r.width),
        h: round(r.height),
        disabled: Boolean(el.disabled),
        text: el.textContent?.trim() ?? '',
        val: el.querySelector('.lut-menu-val')?.textContent?.trim() ?? '',
        hitOk: Boolean(hit && (hit === el || el.contains(hit))),
      };
    };
    return {
      importItem: inspect('[data-act="lut-menu-import"]'),
      spaceItem: inspect('[data-act="lut-menu-space"]'),
    };
  });
  await page.keyboard.press('Escape');
  await page.waitForFunction(() => !document.querySelector('.lut-menu-pop:popover-open'), null, { timeout: 5_000 });
  return items;
}

const out = { error: null, empty: null, emptyMenu: null, filled: null, selected: null, selectedMenu: null, photo: null, band: null, bar: null };

try {
  const page = await browser.newPage({ viewport: { width: 390, height: 844 }, deviceScaleFactor: 1, hasTouch: true, isMobile: true });
  await page.goto(url, { waitUntil: 'domcontentloaded' });
  await page.waitForSelector('.app[data-core="1"]', { timeout: 60_000 });
  await page.locator('input.filepick:not(.calibpick)').setInputFiles(manifest.files);
  await page.waitForFunction(
    () => !document.querySelector('.nav small')?.textContent?.includes('正在读取') && document.querySelectorAll('.cell').length >= 1,
    null,
    { timeout: 60_000 },
  );
  await page.locator('.cell').first().click();
  await page.waitForSelector('.editor', { timeout: 30_000 });
  const askLater = page.locator('.ask-sheet button', { hasText: '先不用' });
  if (await askLater.isVisible({ timeout: 2_000 }).catch(() => false)) {
    await askLater.click();
    await page.waitForFunction(() => !document.querySelector('.ask-sheet'), null, { timeout: 5_000 });
  }
  await page.locator('.editor .toolbar [data-tool="recipe"]').click();
  if (await askLater.isVisible({ timeout: 500 }).catch(() => false)) {
    await askLater.click();
    await page.waitForFunction(() => !document.querySelector('.ask-sheet'), null, { timeout: 5_000 });
  }
  await page.locator('.editor .panel.recipe .mode-switch [data-mode="lut"]').click();
  await page.waitForFunction(
    () => {
      const panel = document.querySelector('.editor .panel.lut');
      return panel && !panel.hidden;
    },
    null,
    { timeout: 15_000 },
  );
  out.empty = await measure(page);
  out.emptyMenu = await probePinMenu(page);

  await page.locator('input.lutpick').setInputFiles([manifest.lutA, manifest.lutB]);
  await page.waitForFunction(() => (window.__gxrLutBook?.list?.()?.length ?? 0) >= 2, null, { timeout: 30_000 });
  await page.locator('input.lutpick').setInputFiles(manifest.lutA);
  await page.waitForFunction(
    () => document.querySelector('.editor .panel.lut [data-state="lut-status"]')?.textContent === '已经导入过',
    null,
    { timeout: 15_000 },
  );
  out.filled = await measure(page);
  out.photo = out.filled.photo;
  out.band = out.filled.band;
  out.bar = out.filled.bar;

  // 选中第一张导入 LUT 后再测一次：左侧合并瓷砖不变宽，两张 78pt LUT 卡依然完整可见，且菜单内色彩空间变为可用。
  await page.locator('.editor .panel.lut .strip .rc:not([data-lut="none"])').first().click();
  await page.waitForSelector('.editor .panel.lut .strip .rc.on:not([data-lut="none"])', { timeout: 10_000 });
  out.selected = await measure(page);
  out.selectedMenu = await probePinMenu(page);

  await page.locator('.editor .panel.lut .mode-switch [data-mode="recipe"]').click();
  await page.waitForSelector('.panel.recipe:not([hidden]) .mode-switch', { timeout: 10_000 });
  out.recipeSwitch = await page.evaluate(() => {
    const node = document.querySelector('.editor .panel.recipe .mode-switch');
    const r = node.getBoundingClientRect();
    return { x: Math.round(r.x * 10) / 10, w: Math.round(r.width * 10) / 10 };
  });
} catch (err) {
  const text = String(err && err.stack ? err.stack : err).replace(/\/(?:Volumes|Users|private|var\/folders|tmp)\/\S+/g, '[path]');
  out.error = text.slice(0, 240);
} finally {
  await browser.close();
}

process.stdout.write(`${JSON.stringify(out)}\n`);
if (out.error) process.exit(1);
