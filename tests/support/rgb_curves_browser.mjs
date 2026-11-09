import assert from 'node:assert/strict';
import { pathToFileURL } from 'node:url';
import { waitForPage } from './browser_wait.mjs';

const [runtime, url] = process.argv.slice(2);
const { launchPinnedChromium, verifyPinnedRuntime } = await import(pathToFileURL(runtime).href);
verifyPinnedRuntime();
const browser = await launchPinnedChromium();
const out = { pageErrors: [], httpErrors: [] };
try {
  const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
  await page.addInitScript(() => localStorage.setItem('gxr-studio.gpu-preview', 'off'));
  page.on('pageerror', (error) => out.pageErrors.push(error.message));
  page.on('response', (response) => { if (response.status() >= 500) out.httpErrors.push({ url: new URL(response.url()).pathname, status: response.status() }); });
  const writes = [];
  page.on('request', (request) => {
    if (request.method() === 'PUT' && request.url().endsWith('/edit')) writes.push(request.postDataJSON());
  });
  await page.goto(url);
  await waitForPage(page, async () => (await import('/assets/state.js')).state.photos.length > 0);
  const id = await page.evaluate(async () => {
    const { state } = await import('/assets/state.js');
    return state.photos.find((p) => p.has_jpg && p.name.startsWith('R0000102')).id;
  });
  await page.evaluate((id) => { location.hash = `#/photo/${id}`; }, id);
  await waitForPage(page, async () => Boolean((await import('/assets/viewer.js')).currentView()?.ed));
  await page.getByRole('tab', { name: '调整', exact: true }).click();
  await page.waitForFunction(() => document.querySelector('.pv-img')?.src.startsWith('blob:'));
  await waitForPage(page, async () => (await import('/assets/histogram.js')).histogramState()?.samples > 0);
  const read = () => page.evaluate(async () => {
    const v = (await import('/assets/viewer.js')).currentView();
    await (await import('/assets/preview.js')).flushEdits(v);
    return structuredClone(v.ed.curve);
  });
  const image = () => page.evaluate(async () => {
    const img = document.querySelector('.pv-img');
    await img.decode();
    const canvas = document.createElement('canvas');
    canvas.width = img.naturalWidth;
    canvas.height = img.naturalHeight;
    const ctx = canvas.getContext('2d');
    ctx.drawImage(img, 0, 0);
    const pixels = ctx.getImageData(0, 0, canvas.width, canvas.height).data;
    const data = new Uint8Array(await crypto.subtle.digest('SHA-256', pixels));
    return Array.from(data).join(',');
  });
  const hist = () => page.evaluate(async () => (await import('/assets/histogram.js')).histogramState());
  const choose = (channel) => page.locator(`.curve-channels [data-channel="${channel}"]`).click();
  async function point(channel, x, y) {
    await choose(channel);
    const svg = page.locator('svg.curve');
    await svg.scrollIntoViewIfNeeded();
    const box = await svg.boundingBox();
    await page.mouse.move(box.x + x / 255 * box.width, box.y + (255 - x) / 255 * box.height);
    await page.mouse.down();
    await page.mouse.move(box.x + x / 255 * box.width, box.y + (255 - y) / 255 * box.height, { steps: 8 });
    await page.mouse.up();
    return read();
  }
  const beforeImage = await image();
  const beforeHist = await hist();
  const blue = await point('b', 64, 105);
  out.blueSaveKeys = Object.keys(writes.at(-1).curve);
  out.blueMarked = await page.locator('.curve-channels [data-channel="b"]').getAttribute('data-changed') === 'true';
  await waitForPage(page, async (old) => {
    const h = (await import('/assets/histogram.js')).histogramState();
    return h.updates > old.updates && h.mean !== old.mean;
  }, beforeHist);
  out.blueChangedHistogram = true;
  out.blueChangedImage = beforeImage !== await image();
  await point('r', 192, 160);
  await point('g', 128, 140);
  await point('master', 128, 134);
  await choose('b');
  const all = await read();
  assert.deepEqual(all.b, blue.b);
  const circles = await page.locator('.curve-point').evaluateAll((nodes) => nodes.map((n) => [Number(n.getAttribute('cx')), 255 - Number(n.getAttribute('cy'))]));
  out.switchKeptPoints = JSON.stringify(circles) === JSON.stringify(blue.b);
  out.ghosts = await page.locator('.curve-ghost').evaluateAll((nodes) => nodes.map((n) => n.dataset.channel).sort());
  const box = await page.locator('svg.curve').boundingBox();
  await page.mouse.dblclick(box.x + blue.b[1][0] / 255 * box.width, box.y + (255 - blue.b[1][1]) / 255 * box.height);
  const withoutBlue = { ...all };
  delete withoutBlue.b;
  assert.deepEqual(await read(), withoutBlue);
  out.doubleClickRemovedOnlyBlue = true;
  await point('b', 64, 105);
  await page.getByRole('button', { name: '重置此通道', exact: true }).click();
  assert.deepEqual(await read(), withoutBlue);
  out.resetOneKeptOthers = true;
  const restored = await point('b', 64, 105);
  await page.getByRole('button', { name: '重置全部曲线', exact: true }).click();
  assert.equal(await read(), null);
  await page.keyboard.press('Meta+z');
  assert.deepEqual(await read(), restored);
  out.undoKeptAll = true;
  await page.keyboard.press('Meta+Shift+z');
  assert.equal(await read(), null);
  out.redoResetAll = true;
  await page.keyboard.press('Meta+z');
  await read();
  await page.reload();
  const reread = await waitForPage(page, async (expected) => {
    const v = (await import('/assets/viewer.js')).currentView();
    return v?.ed && JSON.stringify(v.ed.curve) === JSON.stringify(expected) ? structuredClone(v.ed.curve) : false;
  }, restored);
  assert.deepEqual(reread, restored);
  out.reloadedAll = true;
  console.log(JSON.stringify(out));
} finally {
  await browser.close();
}
