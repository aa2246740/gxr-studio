// 手机竖屏 390×844 的 LUT 甲板度量（polish-2 第 1 项）。
// 只量可见 LUT 卡、提示与甲板的重叠、以及「配方 | LUT」切换键在两种模式下的 x。
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
    const cards = [...document.querySelectorAll('.editor .panel.lut .strip .rc')].map((node) => {
      const r = box(node);
      const name = node.querySelector('.nm');
      const nr = name ? box(name) : null;
      const nameBox = nr;
      const fully =
        r && deckBox
          ? r.x >= deckBox.x - 0.5 && r.x + r.w <= deckBox.x + deckBox.w + 0.5
          : false;
      const nameClipped = name
        ? name.scrollWidth - name.clientWidth > 1 || (nr && deckBox && (nr.x < deckBox.x - 0.5 || nr.x + nr.w > deckBox.x + deckBox.w + 0.5))
        : false;
      return {
        lut: node.dataset.lut ?? '',
        box: r,
        name: name?.textContent ?? '',
        nameBox,
        fully,
        nameClipped,
      };
    });
    const photo = document.querySelector('.editor .stage .pv');
    const band = document.querySelector('.editor .band');
    const bar = document.querySelector('.editor .toolbar');
    const space = document.querySelector('.editor .panel.lut [data-act="lut-space"]');
    const overlap =
      noticeBox && deckBox ? round(Math.max(0, Math.min(noticeBox.bottom, deckBox.bottom) - Math.max(noticeBox.y, deckBox.bottom - 22))) : 0;
    const hitsName = (box) =>
      box
        ? cards.some((card) => {
            const name = card.nameBox;
            if (!name) return false;
            return box.x < name.x + name.w && box.x + box.w > name.x && box.y < name.y + name.h && box.y + box.h > name.y;
          })
        : false;
    const footClipped = foot
      ? foot.scrollWidth - foot.clientWidth > 1 || foot.scrollHeight - foot.clientHeight > 1
      : false;
    return {
      layout: document.querySelector('.app')?.dataset.layout ?? '',
      deck: deckBox,
      notice: noticeBox,
      noticeText: notice?.textContent ?? '',
      noticeOverlapBottom22: overlap,
      noticeHitsName: hitsName(noticeBox),
      footHitsName: hitsName(footBox),
      foot: footBox,
      footText: foot && !foot.hidden ? foot.textContent : '',
      footClipped,
      switchBox: box(sw),
      space: box(space),
      spaceDisabled: space ? space.disabled : null,
      cards,
      visibleCards: cards.filter((c) => c.fully && c.lut !== 'none').length,
      photo: box(photo),
      band: box(band),
      bar: box(bar),
    };
  });
}

const out = { error: null, empty: null, filled: null, photo: null, band: null, bar: null };

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
  await page.locator('.editor .toolbar [data-tool="recipe"]').click();
  await page.locator('.editor .panel.recipe .mode-switch [data-mode="lut"]').evaluate((node) => node.click());
  await page.waitForFunction(
    () => {
      const panel = document.querySelector('.editor .panel.lut');
      return panel && !panel.hidden;
    },
    null,
    { timeout: 15_000 },
  );
  out.empty = await measure(page);

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
  await page.locator('.editor .panel.lut .mode-switch [data-mode="recipe"]').evaluate((node) => node.click());
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
