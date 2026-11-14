// 无头 Chrome 里走一遍选片、网格、编辑、拖滑块、长按、滑动和坏文件。不断言 SwiftShader 的帧率。

import { pathToFileURL } from 'node:url';
import { readFileSync } from 'node:fs';

const [runtime, url, manifestPath] = process.argv.slice(2);
const manifest = JSON.parse(readFileSync(manifestPath, 'utf8'));
const { launchPinnedChromium, verifyPinnedRuntime } = await import(pathToFileURL(runtime).href);
verifyPinnedRuntime();

const browser = await launchPinnedChromium({
  args: [
    '--enable-unsafe-webgpu',
    '--enable-unsafe-swiftshader',
    '--use-angle=swiftshader',
    '--use-webgpu-adapter=swiftshader',
  ],
});

const out = { pageErrors: [], orderBefore: [], orderAfter: [], groups: 0, perf: null, swipe: [], bad: '' };

function ids(page) {
  return page.locator('.cell').evaluateAll((nodes) => nodes.map((node) => node.dataset.id || ''));
}

try {
  const page = await browser.newPage({ viewport: { width: 390, height: 844 } });
  const notes = [];
  page.on('pageerror', (error) => out.pageErrors.push(error.message));
  page.on('console', (msg) => {
    const text = msg.text();
    if (text.includes('[vite]') || text.includes('reload')) notes.push(text.slice(0, 240));
  });
  page.on('framenavigated', (frame) => {
    if (frame === page.mainFrame()) notes.push(`nav ${frame.url()}`);
  });
  await page.goto(url);
  // 描述 Worker 第一次编译时，开发服务器可能整页刷新一次。等它停稳再选文件。
  for (let attempt = 0; attempt < 5; attempt += 1) {
    await page.waitForSelector('.app[data-core="1"]', { timeout: 30_000 });
    const reloaded = await page.waitForEvent('framenavigated', { timeout: 4_000 }).then(() => true).catch(() => false);
    if (!reloaded) break;
    if (attempt === 4) throw new Error(`page kept reloading\n${notes.join('\n')}`);
  }
  await page.locator('input.filepick:not(.calibpick)').setInputFiles(manifest.pairs.flat());
  await page.waitForFunction(() => {
    const sub = document.querySelector('.nav small');
    return sub && !sub.textContent.includes('正在读取') && document.querySelector('.cell canvas');
  }, null, { timeout: 60_000 });
  out.groups = await page.locator('[data-kind="group"]').count();
  out.orderBefore = await ids(page);
  await page.getByRole('button', { name: '从晚到早' }).click();
  await page.waitForFunction((before) => {
    const now = [...document.querySelectorAll('.cell')].map((node) => node.dataset.id || '');
    return now.length === before.length && now.join() !== before.join();
  }, out.orderBefore);
  out.orderAfter = await ids(page);
  await page.locator('.cell').first().evaluate((node) => node.click());
  try {
    await page.waitForFunction(() => document.querySelector('.app')?.dataset.frame === '1', null, { timeout: 90_000 });
  } catch (err) {
    const text = await page.locator('body').innerText();
    const screen = await page.locator('.app').getAttribute('data-screen');
    throw new Error(`${err instanceof Error ? err.message : err}\nscreen=${screen}\n${notes.join('\n')}\n${text.slice(0, 500)}`);
  }
  const slider = page.locator('input[data-key="exposure_ev"]');
  await slider.scrollIntoViewIfNeeded();
  const box = await slider.boundingBox();
  if (!box) throw new Error('曝光滑块没有尺寸');
  await page.mouse.move(box.x + 8, box.y + box.height / 2);
  await page.mouse.down();
  const started = Date.now();
  while (Date.now() - started < 3000) {
    const t = (Date.now() - started) / 3000;
    await page.mouse.move(box.x + 8 + t * (box.width - 16), box.y + box.height / 2);
    await page.waitForTimeout(40);
  }
  await page.mouse.up();
  await page.waitForFunction(() => document.querySelector('.perf')?.dataset.perf, null, { timeout: 5000 });
  out.perf = JSON.parse(await page.locator('.perf').getAttribute('data-perf'));
  const stage = page.locator('.stage');
  const stageBox = await stage.boundingBox();
  if (!stageBox) throw new Error('照片区域没有尺寸');
  const sx = stageBox.x + stageBox.width / 2;
  const sy = stageBox.y + stageBox.height / 2;
  await page.mouse.move(sx, sy);
  await page.mouse.down();
  await page.waitForTimeout(600);
  out.held = await page.locator('.lbl').innerText();
  await page.mouse.up();
  const beforeSwipe = await page.locator('.nav b').innerText();
  await page.mouse.move(sx, sy);
  await page.mouse.down();
  await page.mouse.move(sx - 140, sy, { steps: 8 });
  await page.mouse.up();
  await page.waitForFunction((prev) => document.querySelector('.nav b')?.textContent !== prev, beforeSwipe).catch(async (err) => {
    throw new Error(`${err.message}\nerrors=${JSON.stringify(out.pageErrors)}\n${(await page.locator('body').innerText()).slice(0, 600)}`);
  });
  out.swipe = [beforeSwipe, await page.locator('.nav b').innerText()];
  await page.getByRole('button', { name: '照片', exact: true }).click();
  await page.waitForSelector('[data-screen="grid"]');
  await page.locator('input.filepick:not(.calibpick)').setInputFiles(manifest.bad);
  await page.waitForSelector('[data-state="unreadable"]');
  out.bad = await page.locator('[data-state="unreadable"]').innerText();
  const kept = await ids(page);
  if (!kept.includes(out.orderAfter[0]) && !kept.includes(out.orderBefore[0])) throw new Error('坏文件之后原来的照片不在了');
} finally {
  await browser.close();
}

process.stdout.write(`${JSON.stringify(out)}\n`);
