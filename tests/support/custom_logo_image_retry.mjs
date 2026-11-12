// 第一次标志图片请求失败时，只要服务器目录里还有这个标志，就不能改回 RICOH。
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
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
try {
  const uploaded = await context.request.post(`${base}/api/logos?key_white=false&name=${encodeURIComponent('重试图')}`, {
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
    logo_size: 'medium',
    output_ratio: '4:5',
    model: true,
  };
  const put = await context.request.put(`${base}/api/photos/${photo}/edit`, { data: { frame, expected_rev: old.rev } });
  assert.equal(put.status(), 200, await put.text());
  let sawImageFailure = false;
  let allowImage = false;
  await context.route('**/api/logos/**', async (route) => {
    const pathname = new URL(route.request().url()).pathname;
    if (pathname.endsWith('/image') && !allowImage) {
      sawImageFailure = true;
      await route.fulfill({ status: 503, contentType: 'text/plain', body: 'busy' });
      return;
    }
    if (pathname === '/api/logos' && sawImageFailure) allowImage = true;
    await route.continue();
  });
  const page = await context.newPage();
  page.on('request', (req) => {
    if (req.method() === 'PUT' && req.url().includes('/edit')) savedEdits.push(req.postData() || '');
  });
  await page.goto(`${base}/#/photo/${photo}`);
  await page.getByRole('tab', { name: '边框', exact: true }).click();
  await page.waitForFunction(async ({ photoId, logoId }) => {
    try {
      const { api } = await import('/assets/api.js');
      const edit = await api(`/api/photos/${photoId}/edit`);
      const tile = document.querySelector(`.logo-tile[data-logo="${logoId}"] img`);
      return !!(edit.frame && edit.frame.logo === logoId && tile && tile.complete && tile.naturalWidth > 0);
    } catch {
      return false;
    }
  }, { photoId: photo, logoId: logo.id }, { timeout: 8000 });
  const edit = await (await context.request.get(`${base}/api/photos/${photo}/edit`)).json();
  assert.equal(edit.frame.logo, logo.id);
  assert.equal(savedEdits.some((body) => body.includes('"logo":"ricoh"') || body.includes('"logo": "ricoh"')), false);
  console.log(JSON.stringify({ logoId: logo.id, kept: true, pinnedExecutable: pinned.executablePath }));
} finally {
  await context.close();
  await browser.close();
}
