// 手机竖屏配方微调：每个参数片和滑块的中心必须点到自己，不能点到「配方 | LUT」切换键。
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

function hits(page) {
  return page.evaluate(() => {
    const round = (n) => Math.round(n * 10) / 10;
    const chipBar = document.querySelector('.editor .recipe-fine .knob-chips');
    if (chipBar) chipBar.scrollLeft = 0;
    const controls = [
      ...document.querySelectorAll('.editor .recipe-fine .chip, .editor .recipe-fine input[type="range"]'),
    ];
    const misses = [];
    let chipsTested = 0;
    let slidersTested = 0;
    const isClipped = (el, cx, cy) => {
      let clip = el.parentElement;
      while (clip && clip !== document.body) {
        const clipStyle = getComputedStyle(clip);
        if (/(auto|scroll|hidden)/.test(`${clipStyle.overflowX}${clipStyle.overflowY}`)) {
          const b = clip.getBoundingClientRect();
          if (cx < b.left + 0.5 || cx > b.right - 0.5 || cy < b.top + 0.5 || cy > b.bottom - 0.5) return true;
        }
        clip = clip.parentElement;
      }
      return false;
    };
    for (const el of controls) {
      const style = getComputedStyle(el);
      let r = el.getBoundingClientRect();
      if (style.display === 'none' || style.visibility === 'hidden' || r.width < 1 || r.height < 1) continue;
      let cx = r.left + r.width / 2;
      let cy = r.top + r.height / 2;
      if (cx < 0 || cx >= window.innerWidth || isClipped(el, cx, cy)) {
        el.scrollIntoView({ inline: 'center', block: 'nearest' });
        r = el.getBoundingClientRect();
        cx = r.left + r.width / 2;
        cy = r.top + r.height / 2;
      }
      if (cx < 0 || cy < 0 || cx >= window.innerWidth || cy >= window.innerHeight || isClipped(el, cx, cy)) {
        misses.push({
          cls: el.className,
          knob: el.dataset.knobChip || el.dataset.act || '',
          x: round(r.x),
          w: round(r.width),
          hit: 'offscreen',
        });
        continue;
      }
      if (el.matches('input[type="range"]')) slidersTested += 1;
      else chipsTested += 1;
      const hit = document.elementFromPoint(cx, cy);
      const own = Boolean(hit && (hit === el || el.contains(hit) || hit.contains(el)));
      if (!own || hit?.closest('.mode-switch')) {
        misses.push({
          cls: el.className,
          knob: el.dataset.knobChip || el.dataset.act || '',
          x: round(r.x),
          w: round(r.width),
          hit: hit?.className ?? 'null',
        });
      }
    }
    if (chipBar) chipBar.scrollLeft = 0;
    const sw = document.querySelector('.editor .panel.recipe > .mode-switch');
    const sr = sw?.getBoundingClientRect();
    return {
      layout: document.querySelector('.app')?.dataset.layout ?? '',
      fine: !document.querySelector('.editor .recipe-fine')?.hidden,
      chipsTested,
      slidersTested,
      tested: chipsTested + slidersTested,
      switchHidden: sw ? sw.hidden || getComputedStyle(sw).display === 'none' : null,
      switchBox: sr && sr.width ? { x: round(sr.x), y: round(sr.y), w: round(sr.width), h: round(sr.height) } : null,
      misses,
    };
  });
}

const out = { error: null, views: [] };

try {
  const page = await browser.newPage({ viewport: { width: 390, height: 844 }, deviceScaleFactor: 1, hasTouch: true, isMobile: true });
  await page.goto(url, { waitUntil: 'domcontentloaded' });
  await page.waitForSelector('.app[data-core="1"]', { timeout: 60_000 });
  await page.locator('input.calibpick').setInputFiles(manifest.pack);
  await page.waitForSelector('.calib[data-calib="ready"]', { timeout: 60_000 });
  await page.locator('input.filepick:not(.calibpick)').setInputFiles(manifest.files);
  await page.waitForFunction(
    () => !document.querySelector('.nav small')?.textContent?.includes('正在读取') && document.querySelectorAll('.cell').length >= 1,
    null,
    { timeout: 120_000 },
  );
  await page.locator('.cell').first().evaluate((node) => node.click());
  await page.waitForSelector('.editor', { timeout: 30_000 });
  await page.locator('.editor .toolbar [data-tool="recipe"]').click();
  await page.waitForFunction(
    () => document.querySelector('.editor .strip[data-recipes]')?.dataset.state === 'calibrated',
    null,
    { timeout: 120_000 },
  );
  await page.locator('.editor .panel.recipe .strip .rc:not([disabled])').first().evaluate((node) => node.click());
  if ((await page.locator('.editor .recipe-fine:not([hidden]) .chip').count()) === 0) {
    await page.locator('.editor .panel.recipe .strip .rc.on').evaluate((node) => node.click());
  }
  await page.waitForSelector('.editor .recipe-fine:not([hidden]) .chip', { timeout: 10_000 });

  for (const vp of [
    { label: '390x844', width: 390, height: 844 },
    { label: '440x956', width: 440, height: 956 },
  ]) {
    await page.setViewportSize({ width: vp.width, height: vp.height });
    await page.waitForTimeout(200);
    const view = await hits(page);
    out.views.push({ label: vp.label, ...view });
  }
} catch (err) {
  out.error = String(err && err.stack ? err.stack : err).replace(/\/(?:Volumes|Users|private|var\/folders|tmp)\/\S+/g, '[path]').slice(0, 500);
} finally {
  await browser.close();
}

process.stdout.write(`${JSON.stringify(out)}\n`);
if (out.error) process.exit(1);
