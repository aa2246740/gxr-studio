// 标志 PNG 在首次画出来之后被移走：当前页重画和导出必须一起退回 RICOH。
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { pathToFileURL } from 'node:url';

const [runtime, port, token, photo, pngPath, homeDir] = process.argv.slice(2);
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
const evidence = { pinnedExecutable: pinned.executablePath, tokenLeaked: false };
let parked = '';
try {
  const uploaded = await context.request.post(`${base}/api/logos?key_white=false&name=${encodeURIComponent('宽洋红')}`, {
    data: fs.readFileSync(pngPath),
    headers: { 'content-type': 'image/png' },
  });
  assert.equal(uploaded.status(), 200, await uploaded.text());
  const logo = (await uploaded.json()).logo;
  evidence.logoId = logo.id;
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
  const page = await context.newPage();
  await page.goto(`${base}/#/photo/${photo}`);
  await page.getByRole('tab', { name: '边框', exact: true }).click();
  await page.locator('.pv-deco').waitFor({ state: 'visible', timeout: 120000 });
  await page.waitForFunction((id) => {
    const tile = document.querySelector(`.logo-tile[data-logo="${id}"] img`);
    return !!(tile && tile.complete && tile.naturalWidth > 0);
  }, logo.id, { timeout: 20000 });
  const png = path.join(homeDir, 'logos', `${logo.id}.png`);
  parked = `${png}.missing-parked`;
  fs.renameSync(png, parked);
  await page.locator('[aria-label="大小"] button').filter({ hasText: '大' }).click();
  await page.waitForFunction(() => document.body.innerText.includes('找不到这个标志'), null, { timeout: 8000 });
  evidence.preview = await page.evaluate(async ({ photoId, logoId }) => {
    const { flushPhotos } = await import('/assets/saves.js');
    const { api } = await import('/assets/api.js');
    const { getLogoMap, customLogoImage } = await import('/assets/finish_frame_core.js');
    const { frameLayout } = await import('/assets/finish_layout.js');
    const { metrics } = await import('/assets/finish_fonts.js');
    await flushPhotos([photoId]);
    const sized = await api(`/api/photos/${photoId}/output-size`, { method: 'POST', body: {} });
    const layout = frameLayout(...sized.photo, sized.frame, sized.meta, metrics);
    return {
      sized,
      catalogHasLogo: !!getLogoMap()[logoId],
      cachedImage: !!customLogoImage(logoId),
      message: document.body.innerText.includes('找不到这个标志'),
      marks: layout.items.filter((item) => item.k === 'clogo' || item.k === 'logo').map((item) => ({
        k: item.k, id: item.id ?? null, x: item.x, y: item.y, w: item.w, h: item.h,
      })),
    };
  }, { photoId: photo, logoId: logo.id });
  const exported = await context.request.post(`${base}/api/photos/${photo}/export`, { data: { format: 'png', size: 'original' } });
  evidence.exportStatus = exported.status();
  evidence.exported = evidence.exportStatus === 200 ? await exported.json() : await exported.text();
  evidence.agreed = evidence.preview.message
    && !evidence.preview.catalogHasLogo
    && !evidence.preview.cachedImage
    && evidence.preview.marks.some((item) => item.k === 'logo')
    && evidence.preview.marks.every((item) => item.k !== 'clogo');
  assert.equal(evidence.agreed, true, JSON.stringify(evidence.preview));
  assert.equal(evidence.exportStatus, 200);
  const dumped = JSON.stringify(evidence);
  evidence.tokenLeaked = dumped.includes(token);
  assert.equal(evidence.tokenLeaked, false);
} finally {
  if (parked && fs.existsSync(parked)) fs.renameSync(parked, parked.replace(/\.missing-parked$/, ''));
  await context.close();
  await browser.close();
}
console.log(JSON.stringify(evidence));
