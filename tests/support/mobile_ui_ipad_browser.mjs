// iPad 三档宽度：打开、编辑、裁剪、LUT、边框、复制、导出，以及转屏。输出里不写照片名。

import { mkdirSync } from 'node:fs';
import { readFileSync } from 'node:fs';
import { pathToFileURL } from 'node:url';

const [runtime, url, manifestPath, shotDir] = process.argv.slice(2);
const manifest = JSON.parse(readFileSync(manifestPath, 'utf8'));
const { launchPinnedChromium, verifyPinnedRuntime } = await import(pathToFileURL(runtime).href);
verifyPinnedRuntime();
if (shotDir) mkdirSync(shotDir, { recursive: true });

const browser = await launchPinnedChromium({
  args: ['--enable-unsafe-webgpu', '--enable-unsafe-swiftshader', '--use-angle=swiftshader', '--use-webgpu-adapter=swiftshader'],
});

const out = { pageErrors: [], small: [], checks: {} };

function fail(message) {
  const error = new Error(message);
  error.report = out;
  throw error;
}

async function settle(page) {
  await page.evaluate(() => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve))));
}

async function probe(page) {
  return page.evaluate(() => {
    const visible = (node) => {
      if (!node) return false;
      const style = getComputedStyle(node);
      if (style.display === 'none' || style.visibility === 'hidden') return false;
      const box = node.getBoundingClientRect();
      return box.width > 1 && box.height > 1;
    };
    const box = (node) => {
      if (!node) return null;
      const rect = node.getBoundingClientRect();
      return { x: rect.x, y: rect.y, w: rect.width, h: rect.height };
    };
    const app = document.querySelector('.app');
    const grid = document.querySelector('.slot-grid');
    const stage = document.querySelector('.screen.editor .stage');
    const sheet = document.querySelector('.screen.editor .sheet');
    const gb = box(grid);
    const sb = box(stage);
    const hb = box(sheet);
    const targets = [...document.querySelectorAll('.tabs button, .actbar .btn, .nb, .cell, .gh .go, .chip, .fbtn')].filter(visible);
    const small = targets
      .filter((node) => {
        const rect = node.getBoundingClientRect();
        return rect.width < 43.5 || rect.height < 43.5;
      })
      .slice(0, 8)
      .map((node) => node.className.slice(0, 48));
    const canvas = document.querySelector('canvas.gpu');
    const pv = canvas?.parentElement?.getBoundingClientRect();
    const ms = document.querySelector('.ms, .dlg');
    const mb = box(ms);
    return {
      layout: app?.dataset.layout || '',
      screen: app?.dataset.screen || '',
      drag: app?.dataset.drag || '',
      open: app?.dataset.open || '',
      tab: document.querySelector('.screen.editor .tabs button.on')?.textContent || '',
      cols: document.querySelector('.gscroll')?.dataset.cols || '',
      scroll: document.querySelector('.gscroll')?.scrollTop || 0,
      selected: document.querySelectorAll('.cell.on').length,
      grid: !!gb && gb.w > 40,
      stage: !!sb && sb.w > 40 && sb.h > 40,
      sheet: !!hb && hb.w > 40,
      sideBySide: !!(gb && sb && hb && gb.x + gb.w <= sb.x + 2 && sb.x + sb.w <= hb.x + 2),
      sheetBelow: !!(sb && hb && hb.y >= sb.y + sb.h - 4),
      gesture: visible(document.querySelector('.gesture')),
      histInSheet: !!document.querySelector('.screen.editor .sheet .hist'),
      histInStage: !!document.querySelector('.screen.editor .stage .hist'),
      small,
      canvas: canvas ? { w: canvas.width, h: canvas.height, pvW: pv?.width || 0, dpr: window.devicePixelRatio || 1 } : null,
      sheetBox: mb,
      pdock: !!document.querySelector('.pdock'),
      crop: visible(document.querySelector('.crop-panel')) && !document.querySelector('.crop-panel')?.hidden,
      lut: visible(document.querySelector('.panel.lut')) && !document.querySelector('.panel.lut')?.hidden,
      frame: visible(document.querySelector('.panel.frame')) && !document.querySelector('.panel.frame')?.hidden,
    };
  });
}

async function shot(page, name) {
  if (!shotDir) return;
  await page.screenshot({ path: `${shotDir}/${name}.png` });
}

async function dismissAsk(page) {
  const later = page.getByRole('button', { name: '先不用，只看中性转换' });
  if (await later.count()) await later.click();
  await page.waitForFunction(() => !document.querySelector('.ask-scrim')).catch(() => undefined);
}

async function tab(page, name) {
  await dismissAsk(page);
  await page.locator('.screen.editor .tabs button', { hasText: name }).evaluate((node) => node.click());
  await page.waitForFunction((label) => document.querySelector('.screen.editor .tabs button.on')?.textContent === label, name);
  await dismissAsk(page);
  await settle(page);
}

try {
  const context = await browser.newContext({ viewport: { width: 1376, height: 1032 }, deviceScaleFactor: 2 });
  const page = await context.newPage();
  const notes = [];
  page.on('pageerror', (error) => out.pageErrors.push(String(error.message).slice(0, 180)));
  page.on('console', (msg) => {
    const text = msg.text();
    if (text.includes('[vite]')) notes.push('vite');
  });
  await page.goto(url);
  for (let attempt = 0; attempt < 5; attempt += 1) {
    await page.waitForSelector('.app[data-core="1"]', { timeout: 30_000 });
    const reloaded = await page.waitForEvent('framenavigated', { timeout: 4_000 }).then(() => true).catch(() => false);
    if (!reloaded) break;
    if (attempt === 4) fail('page kept reloading');
  }
  await page.locator('input.filepick:not(.calibpick)').setInputFiles(manifest.pairs);
  await page.waitForFunction(() => {
    const sub = document.querySelector('.nav small');
    return sub && !sub.textContent.includes('正在读取') && document.querySelector('.cell canvas');
  }, null, { timeout: 60_000 });
  let view = await probe(page);
  if (view.layout !== 'columns') fail(`expected columns, got ${view.layout}`);
  if (view.cols !== '3') fail(`left column cols ${view.cols}`);
  if (!view.grid) fail('grid missing in landscape');
  await page.locator('.slot-grid .cell').first().evaluate((node) => node.click());
  await page.waitForFunction(() => {
    const settled = document.querySelector('.app')?.dataset.settled;
    return document.querySelector('.screen.editor') && settled && settled !== '0';
  }, null, { timeout: 90_000 });
  view = await probe(page);
  const openId = view.open;
  if (!openId) fail('editor did not record the open photo');
  if (!view.sideBySide) fail('landscape is not three columns');
  if (!view.gesture) fail('gesture hint missing');
  if (!view.histInSheet || view.histInStage) fail('histogram is not kept in 调整');
  if (view.screen !== 'editor') fail(`screen ${view.screen}`);
  out.checks.landscape = true;

  const slider = page.locator('input[data-key="exposure_ev"]');
  await slider.scrollIntoViewIfNeeded();
  const box = await slider.boundingBox();
  if (!box) fail('slider has no box');
  await page.mouse.move(box.x + 12, box.y + box.height / 2);
  await page.mouse.down();
  await page.mouse.move(box.x + box.width * 0.72, box.y + box.height / 2, { steps: 6 });
  view = await probe(page);
  if (view.drag !== '0') fail(`columns entered drag mode ${view.drag}`);
  if (!view.stage || view.stage === false) fail('stage hidden while dragging');
  const stageMid = await page.locator('.screen.editor .stage').boundingBox();
  if (!stageMid || stageMid.height < 80) fail('stage collapsed while dragging');
  await page.mouse.up();
  out.checks.slider = true;

  const tabs = ['配方', '调整', '裁剪', '边框', 'LUT', '曲线'];
  for (const name of tabs) {
    await tab(page, name);
    await shot(page, `land-${name === 'LUT' ? 'lut' : name}`);
  }
  await tab(page, '裁剪');
  await page.locator('.crop-panel .chips button[data-preset="1:1"]').click();
  await page.waitForFunction(() => document.querySelector('.crop-panel .chips button.on')?.dataset.preset === '1:1');
  view = await probe(page);
  if (!view.crop) fail('crop panel hidden');
  out.checks.crop = true;
  await tab(page, 'LUT');
  view = await probe(page);
  if (!view.lut) fail('lut panel hidden');
  out.checks.lut = true;
  await tab(page, '边框');
  view = await probe(page);
  if (!view.frame) fail('frame panel hidden');
  out.checks.frame = true;
  const stage = page.locator('.screen.editor .stage');
  const stageBox = await stage.boundingBox();
  if (!stageBox) fail('no stage for hold');
  const sx = stageBox.x + stageBox.width / 2;
  const sy = stageBox.y + Math.min(stageBox.height / 2, 80);
  await page.mouse.move(sx, sy);
  await page.mouse.down();
  await page.waitForTimeout(700);
  const held = await page.locator('.lbl').innerText();
  await page.mouse.up();
  if (!held.includes('原图')) fail('long-press did not show the original');
  out.checks.hold = true;

  await page.getByRole('button', { name: '复制调整' }).click();
  await page.waitForSelector('.paste-sheet');
  view = await probe(page);
  if (!view.sheetBox || view.sheetBox.w > 560 || view.sheetBox.x < 80) fail('copy sheet is not a centred card');
  await page.locator('.paste-sheet').getByRole('button', { name: '取消' }).click();
  await page.waitForFunction(() => !document.querySelector('.paste-sheet'));
  out.checks.copy = true;

  await page.locator('.slot-grid').getByRole('button', { name: '选择' }).click();
  await page.locator('.slot-grid .cell').nth(1).evaluate((node) => node.click());
  const scroller = page.locator('.slot-grid .gscroll');
  await scroller.evaluate((node) => {
    node.scrollTop = 80;
  });
  view = await probe(page);
  const selected = view.selected;
  const scroll = view.scroll;
  if (selected < 1) fail('selection did not stick');
  await tab(page, '边框');
  await page.setViewportSize({ width: 1032, height: 1376 });
  await settle(page);
  view = await probe(page);
  if (view.layout !== 'pad') fail(`portrait layout ${view.layout}`);
  if (view.open !== openId) fail('photo changed on rotate to portrait');
  if (view.tab !== '边框') fail(`tab changed on rotate: ${view.tab}`);
  if (view.drag !== '0') fail('drag stuck after rotate');
  if (!view.sheetBelow) fail('portrait sheet is not under the photo');
  if (view.grid && view.sideBySide) fail('portrait still shows three columns');
  await shot(page, 'port-editor');
  out.checks.portrait = true;

  await page.setViewportSize({ width: 600, height: 1032 });
  await settle(page);
  view = await probe(page);
  if (view.layout !== 'phone') fail(`split layout ${view.layout}`);
  if (view.open !== openId) fail('photo changed in split view');
  if (view.cols && view.cols !== '3' && view.grid) fail(`split cols ${view.cols}`);
  out.checks.split = true;

  await page.setViewportSize({ width: 1376, height: 1032 });
  await settle(page);
  view = await probe(page);
  if (view.layout !== 'columns' || !view.sideBySide) fail('did not return to three columns');
  if (view.open !== openId) fail('photo changed on return');
  if (view.tab !== '边框') fail(`tab changed on return: ${view.tab}`);
  if (view.selected !== selected) fail('selection count changed');
  if (Math.abs(view.scroll - scroll) > 2) fail('grid scroll changed');
  out.small = view.small;
  if (view.small.length) fail(`touch targets under 44: ${view.small.join(',')}`);
  const canvas = view.canvas;
  if (canvas && canvas.w > 0) {
    if (canvas.w > 16384 || canvas.h > 16384) fail('canvas exceeds texture limit');
    const expectW = Math.round(canvas.pvW * canvas.dpr);
    if (Math.abs(canvas.w - expectW) > 4) fail(`canvas ${canvas.w} is not the centre column at dpr ${canvas.dpr}`);
  }
  out.checks.return = true;

  await tab(page, '调整');
  const slider2 = page.locator('input[data-key="exposure_ev"]');
  await slider2.scrollIntoViewIfNeeded();
  const box2 = await slider2.boundingBox();
  if (!box2) fail('second slider missing');
  await page.mouse.move(box2.x + 16, box2.y + box2.height / 2);
  await page.mouse.down();
  await page.setViewportSize({ width: 1032, height: 1376 });
  await settle(page);
  view = await probe(page);
  await page.mouse.up();
  if (view.drag !== '0') fail('mid-drag rotate left drag mode on');
  if (view.open !== openId) fail('photo changed mid-drag');
  out.checks.midDrag = true;

  await page.setViewportSize({ width: 1376, height: 1032 });
  await settle(page);
  await page.getByRole('button', { name: '导出这张' }).click();
  await page.waitForSelector('.ms.export');
  view = await probe(page);
  if (!view.sheetBox || view.sheetBox.w > 560 || view.sheetBox.x < 80) fail('export sheet is not a centred card');
  await shot(page, 'land-export');
  await page.locator('.ms.export').getByRole('button', { name: '存到相册' }).click();
  await page.waitForSelector('.pdock', { timeout: 20_000 });
  await page.setViewportSize({ width: 1032, height: 1376 });
  await settle(page);
  view = await probe(page);
  if (!view.pdock) fail('export progress disappeared on rotate');
  if (view.open !== openId) fail('photo changed mid-export');
  if (view.pageErrors) fail('unexpected');
  out.checks.midExport = true;
  out.checks.shots = shotDir ? true : false;
} catch (error) {
  if (error instanceof Error && error.report) out.failed = error.message;
  else out.failed = error instanceof Error ? error.message : 'driver failed';
  process.stdout.write(`${JSON.stringify(out)}\n`);
  process.exitCode = 1;
} finally {
  await browser.close();
}

if (!out.failed) {
  if (out.pageErrors.length) {
    out.failed = 'page errors';
    process.stdout.write(`${JSON.stringify(out)}\n`);
    process.exitCode = 1;
  } else process.stdout.write(`${JSON.stringify(out)}\n`);
}
