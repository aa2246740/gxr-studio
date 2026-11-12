// 标志图片接口出故障（文件和目录都还在）：
// 1. 短暂失败：不点任何东西，大图过一会儿自己补上标志。
// 2. 失败几轮后接口恢复：不点任何东西，到下一次重试时大图自己补上，提示收起。
// 3. 一直失败：面板上有提示，重画不会刷请求；接口恢复后点「现在重试」，大图马上补上，提示收起。
// 两段都不能把用户选的标志改回 RICOH。
import assert from 'node:assert/strict';
import fs from 'node:fs';
import { pathToFileURL } from 'node:url';

const [runtime, port, token, photo, pngPath] = process.argv.slice(2);
const { launchPinnedChromium, verifyPinnedRuntime } = await import(pathToFileURL(runtime).href);
const pinned = verifyPinnedRuntime();
if (process.env.GXR_CHROME) {
  assert.equal(pinned.executablePath, process.env.GXR_CHROME, 'GXR_CHROME 必须与固定 Playwright Chromium 一致');
}
const browser = await launchPinnedChromium({ args: ['--disable-gpu', '--disable-vulkan'] });
const base = `http://127.0.0.1:${port}`;
const context = await browser.newContext({
  viewport: { width: 1440, height: 1000 },
  reducedMotion: 'reduce',
  extraHTTPHeaders: { 'X-GXR-Token': token },
});
const savedEdits = [];
const pageErrors = [];
const out = { pinnedExecutable: pinned.executablePath };

function magenta(page) {
  return page.evaluate(() => {
    const cv = document.querySelector('.pv-deco');
    if (!cv || !cv.width || !cv.height) return -1;
    const data = cv.getContext('2d').getImageData(0, 0, cv.width, cv.height).data;
    let n = 0;
    for (let i = 0; i < data.length; i += 4) if (data[i] > 200 && data[i + 1] < 80 && data[i + 2] > 200) n += 1;
    return n;
  });
}

function hasMagenta(page, timeout) {
  return page.waitForFunction(() => {
    const cv = document.querySelector('.pv-deco');
    if (!cv || !cv.width || !cv.height) return false;
    const data = cv.getContext('2d').getImageData(0, 0, cv.width, cv.height).data;
    for (let i = 0; i < data.length; i += 4) if (data[i] > 200 && data[i + 1] < 80 && data[i + 2] > 200) return true;
    return false;
  }, null, { timeout });
}

try {
  const uploaded = await context.request.post(`${base}/api/logos?key_white=false&name=${encodeURIComponent('故障图')}`, {
    data: fs.readFileSync(pngPath),
    headers: { 'content-type': 'image/png' },
  });
  assert.equal(uploaded.status(), 200, await uploaded.text());
  const logo = (await uploaded.json()).logo;
  const old = await (await context.request.get(`${base}/api/photos/${photo}/edit`)).json();
  const frame = {
    ...(old.frame || {}),
    style: 'leica',
    color: 'white',
    logo: logo.id,
    show_logo: true,
    logo_mode: 'orig',
    logo_size: 'large',
    output_ratio: '4:5',
    model: true,
  };
  const put = await context.request.put(`${base}/api/photos/${photo}/edit`, { data: { frame, expected_rev: old.rev } });
  assert.equal(put.status(), 200, await put.text());

  // mode：'fail-first' 只让前 failBudget 个图片请求失败；'fail' 全部失败；'ok' 全部放行。
  let mode = 'fail-first';
  let failBudget = 2;
  const imageRequests = [];
  await context.route('**/api/logos/**', async (route) => {
    const pathname = new URL(route.request().url()).pathname;
    if (pathname === `/api/logos/${logo.id}/image`) {
      imageRequests.push(Date.now());
      if (mode === 'fail' || (mode === 'fail-first' && failBudget > 0)) {
        if (mode === 'fail-first') failBudget -= 1;
        await route.fulfill({ status: 503, contentType: 'text/plain', body: 'busy' });
        return;
      }
    }
    await route.continue();
  });
  const page = await context.newPage();
  page.on('pageerror', (err) => pageErrors.push(String(err)));
  page.on('request', (req) => {
    if (req.method() === 'PUT' && req.url().includes('/edit')) savedEdits.push(req.postData() || '');
  });

  // 1. 短暂失败：不碰页面，等大图自己补上。
  await page.goto(`${base}/#/photo/${photo}`);
  await page.getByRole('tab', { name: '边框', exact: true }).click();
  await page.locator('.pv-deco').waitFor({ state: 'visible', timeout: 120000 });
  await page.waitForFunction(() => {
    const cv = document.querySelector('.pv-deco');
    if (!cv || !cv.width || !cv.height) return false;
    const data = cv.getContext('2d').getImageData(0, 0, cv.width, cv.height).data;
    for (let i = 0; i < data.length; i += 4) if (data[i] > 200 && data[i + 1] < 80 && data[i + 2] > 200) return true;
    return false;
  }, null, { timeout: 15000 });
  out.transient = { failedRequests: 2 - failBudget, totalRequests: imageRequests.length, magenta: await magenta(page) };
  assert.equal(failBudget, 0, '前两个图片请求应当已经失败过');
  assert.ok(out.transient.magenta > 0);

  // 2. 失败几轮后接口恢复：不点任何东西，等下一次重试。
  mode = 'fail';
  await page.reload();
  await page.getByRole('tab', { name: '边框', exact: true }).click();
  await page.locator('.pv-deco').waitFor({ state: 'visible', timeout: 120000 });
  const hint = page.locator(`[data-logo-image-error="${logo.id}"]`);
  await hint.waitFor({ state: 'visible', timeout: 15000 });
  await page.waitForTimeout(1800);
  const outageRequests = imageRequests.length;
  assert.equal(await magenta(page), 0);
  mode = 'ok';
  const restoredAt = Date.now();
  await hasMagenta(page, 40000);
  await hint.waitFor({ state: 'detached', timeout: 8000 });
  out.autoRecovered = { requestsDuringOutage: outageRequests, secondsAfterRestore: (Date.now() - restoredAt) / 1000 };

  // 3. 一直失败：重新打开页面，所有图片请求都失败。
  mode = 'fail';
  await page.reload();
  await page.getByRole('tab', { name: '边框', exact: true }).click();
  await page.locator('.pv-deco').waitFor({ state: 'visible', timeout: 120000 });
  await hint.waitFor({ state: 'visible', timeout: 15000 });
  const hintText = await hint.innerText();
  assert.ok(hintText.includes('没读出来'), hintText);
  const atHint = imageRequests.length;
  const sizes = [[1300, 950], [1440, 1000], [1200, 900], [1440, 1000], [1360, 980], [1440, 1000]];
  for (const [width, height] of sizes) {
    await page.setViewportSize({ width, height });
    await page.waitForTimeout(500);
  }
  out.persistent = {
    requestsDuringRedraws: imageRequests.length - atHint,
    magenta: await magenta(page),
    hintVisible: await hint.isVisible(),
  };
  assert.ok(out.persistent.requestsDuringRedraws <= 6, `重画期间请求太多：${out.persistent.requestsDuringRedraws}`);
  assert.equal(out.persistent.magenta, 0);
  assert.equal(out.persistent.hintVisible, true);

  // 接口恢复，点「现在重试」。
  mode = 'ok';
  const beforeRetry = imageRequests.length;
  await page.locator(`[data-logo-retry="${logo.id}"]`).click();
  await page.waitForFunction(() => {
    const cv = document.querySelector('.pv-deco');
    if (!cv || !cv.width || !cv.height) return false;
    const data = cv.getContext('2d').getImageData(0, 0, cv.width, cv.height).data;
    for (let i = 0; i < data.length; i += 4) if (data[i] > 200 && data[i + 1] < 80 && data[i + 2] > 200) return true;
    return false;
  }, null, { timeout: 8000 });
  await hint.waitFor({ state: 'detached', timeout: 8000 });
  out.recovered = { requests: imageRequests.length - beforeRetry, magenta: await magenta(page) };
  assert.ok(out.recovered.magenta > 0);

  const edit = await (await context.request.get(`${base}/api/photos/${photo}/edit`)).json();
  assert.equal(edit.frame.logo, logo.id);
  assert.equal(savedEdits.some((body) => /"logo"\s*:\s*"ricoh"/.test(body)), false, '不能把标志存成 RICOH');
  assert.deepEqual(pageErrors, []);
  console.log(JSON.stringify(out));
} finally {
  await context.close();
  await browser.close();
}
