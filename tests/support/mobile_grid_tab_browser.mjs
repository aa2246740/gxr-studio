// 电脑上网格只占一个 Tab 位（roving tabindex，backlog-4b）：30 张照片，从顶栏「选择」按一次 Tab 落到一格，再按一次就出网格；
// Shift-Tab 回来还是那一格，方向键走过以后再进来落在走到的那一格。有触摸的手机尺寸不变：每格都能 Tab 到。
// 顺带（同一页，P3-18 c）：编辑器里的照片信息面板，电脑上不画手机的拖动条，手机照旧。
// 隐私：报告里只有布尔和计数，没有照片名。

import { readFileSync, writeFileSync } from 'node:fs';
import { pathToFileURL } from 'node:url';

const [runtime, url, manifestPath] = process.argv.slice(2);
const manifest = JSON.parse(readFileSync(manifestPath, 'utf8'));
const { launchPinnedChromium, verifyPinnedRuntime } = await import(pathToFileURL(runtime).href);
verifyPinnedRuntime();
const out = { pageErrors: [], steps: {}, error: null };

async function settle(page) {
  for (let attempt = 0; attempt < 5; attempt += 1) {
    await page.waitForSelector('.app[data-core="1"]', { timeout: 60_000 });
    const reloaded = await page.waitForEvent('framenavigated', { timeout: 4_000 }).then(() => true).catch(() => false);
    if (!reloaded) return;
  }
  throw new Error('page kept reloading');
}

/** 焦点在哪儿：是不是网格里的一格、是第几格（DOM 顺序，从 0 数）、它的 id。 */
const where = (page) => page.evaluate(() => {
  const a = document.activeElement;
  const cells = [...document.querySelectorAll('.gscroll .cell')];
  return { cell: Boolean(a?.classList.contains('cell')), inGrid: Boolean(a && document.querySelector('.gscroll')?.contains(a)), index: cells.indexOf(a), id: a?.dataset?.id ?? null };
});

async function grid(browser, label, options) {
  const ctx = await browser.newContext({ deviceScaleFactor: 1, ...options });
  const page = await ctx.newPage();
  page.setDefaultTimeout(30_000);
  page.on('pageerror', (err) => out.pageErrors.push(`${label}: ${err.message.slice(0, 200)}`));
  try {
    await page.goto(url);
    await settle(page);
    await page.locator('input.filepick:not(.calibpick)').setInputFiles(manifest.files);
    await page.waitForFunction(
      (n) => {
        const sub = document.querySelector('.nav small');
        return sub && !sub.textContent.includes('正在读取') && sub.textContent.includes(`${n} 张`);
      },
      manifest.count,
      { timeout: 300_000 },
    );
    const step = {
      computer: await page.evaluate(() => navigator.maxTouchPoints === 0 && matchMedia('(pointer: fine)').matches),
      cells: await page.locator('.gscroll .cell').count(),
      tabbable: await page.locator('.gscroll .cell:not([tabindex="-1"])').count(),
    };
    await page.locator('.nav .nb.bold').focus();
    await page.keyboard.press('Tab');
    step.first = await where(page);
    await page.keyboard.press('Tab');
    step.second = await where(page);
    await page.keyboard.press('Shift+Tab');
    step.back = await where(page);
    if (step.back.cell) {
      await page.keyboard.press('ArrowRight');
      await page.keyboard.press('ArrowDown');
      step.moved = await where(page);
      await page.keyboard.press('Tab');
      step.out = await where(page);
      await page.keyboard.press('Shift+Tab');
      step.again = await where(page);
      step.tabbableAfter = await page.locator('.gscroll .cell:not([tabindex="-1"])').count();
      await page.keyboard.press('Enter');
      await page.waitForFunction(() => document.querySelector('.app')?.dataset.screen === 'editor', null, { timeout: 120_000 });
      step.enterOpens = true;
      // P3-18 c：编辑器 ⋯ → 照片信息，面板顶上的拖动条电脑上不画，手机照旧。
      await page.locator('[data-act="editor-more"]').click();
      await page.locator('[data-act="more-photo-info"]').click();
      await page.waitForSelector('[data-state="photo-info"]');
      step.grab = await page.evaluate(() => {
        const grab = document.querySelector('[data-state="photo-info"] .grab');
        const box = grab?.getBoundingClientRect();
        return { present: Boolean(grab), display: grab ? getComputedStyle(grab).display : '', height: box ? Math.round(box.height) : 0 };
      });
    }
    return step;
  } finally {
    await ctx.close();
  }
}

try {
  const chromium = await launchPinnedChromium({ args: [] });
  try {
    out.steps.computer = await grid(chromium, 'computer', { viewport: { width: 1440, height: 900 } });
    out.steps.phone = await grid(chromium, 'phone', { viewport: { width: 390, height: 844 }, hasTouch: true, isMobile: true });
  } finally {
    await chromium.close();
  }
} catch (err) {
  out.error = String(err?.message ?? err).split('\n')[0].slice(0, 300);
}
writeFileSync(manifest.reportPath, JSON.stringify(out));
process.stdout.write(`${JSON.stringify({ done: true, error: out.error !== null })}\n`);
