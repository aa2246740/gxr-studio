// 真实浏览器（固定 Playwright 运行时，禁 WebGPU、开 reducedMotion）验证自定义标志完整交互流与预览导出一致。
import assert from 'node:assert/strict';
import path from 'node:path';
import { pathToFileURL } from 'node:url';

const [runtime, port, token, photo1, photo2, photo3, filesDir] = process.argv.slice(2);
const { launchPinnedChromium, verifyPinnedRuntime } = await import(pathToFileURL(runtime).href);
const pinned = verifyPinnedRuntime();
if (process.env.GXR_CHROME) {
  assert.equal(pinned.executablePath, process.env.GXR_CHROME, 'GXR_CHROME 必须与固定 Playwright Chromium 一致');
}
const browser = await launchPinnedChromium({
  args: ['--disable-gpu', '--disable-vulkan'],
});
const out = { errors: [], consoleErrors: [], pinnedExecutable: pinned.executablePath };
let page = null;
let releaseInitialCatalog = () => {};

function decoSignature(page) {
  return page.evaluate(() => {
    const cv = document.querySelector('.pv-deco');
    if (!cv || !cv.width || !cv.height) return null;
    const ctx = cv.getContext('2d');
    const data = ctx.getImageData(0, 0, cv.width, cv.height).data;
    let hash = 2166136261;
    for (let i = 0; i < data.length; i += 16) {
      hash ^= data[i];
      hash = Math.imul(hash, 16777619);
    }
    return `${cv.width}x${cv.height}:${hash >>> 0}`;
  });
}

function canvasHash(locator) {
  return locator.evaluate((cv) => {
    const ctx = cv.getContext('2d');
    const data = ctx.getImageData(0, 0, cv.width, cv.height).data;
    let hash = 2166136261;
    for (let i = 0; i < data.length; i += 4) {
      hash ^= data[i];
      hash = Math.imul(hash, 16777619);
    }
    return hash >>> 0;
  });
}

async function goPhoto(page, id) {
  await page.evaluate((pid) => {
    location.hash = `#/photo/${encodeURIComponent(pid)}`;
  }, id);
  await page.waitForFunction(
    (pid) => decodeURIComponent(location.hash.split('/photo/')[1] || '') === pid && Boolean(document.querySelector('.pv img, .pv canvas')),
    id,
  );
}

async function sampleStageSlotInkLuma(page) {
  return page.evaluate(async () => {
    const { frameLayout } = await import('/assets/finish_layout.js');
    const { metrics } = await import('/assets/finish_fonts.js');
    const { flushPhotos } = await import('/assets/saves.js');
    const { api } = await import('/assets/api.js');
    const photoId = decodeURIComponent(location.hash.split('/photo/')[1]);
    await flushPhotos([photoId]);
    const cv = document.querySelector('.pv-deco');
    const res = await api(`/api/photos/${encodeURIComponent(photoId)}/output-size`, { method: 'POST', body: {} });
    const lay = frameLayout(res.photo[0], res.photo[1], res.frame, res.meta, metrics);
    const slot = lay.items.find((it) => it.k === 'clogo');
    const sx = cv.width / lay.width;
    const sy = cv.height / lay.height;
    const x0 = Math.max(0, Math.floor(slot.x * sx));
    const y0 = Math.max(0, Math.floor(slot.y * sy));
    const w = Math.max(1, Math.ceil(slot.w * sx));
    const h = Math.max(1, Math.ceil(slot.h * sy));
    const data = cv.getContext('2d').getImageData(x0, y0, w, h).data;
    let weighted = 0;
    let alphaSum = 0;
    let maxAlpha = 0;
    let maxLuma = 0;
    for (let i = 0; i < data.length; i += 4) {
      const a = data[i + 3];
      if (a > maxAlpha) maxAlpha = a;
      if (a > 40) {
        const lum = 0.299 * data[i] + 0.587 * data[i + 1] + 0.114 * data[i + 2];
        if (lum > maxLuma) maxLuma = lum;
        weighted += lum * a;
        alphaSum += a;
      }
    }
    return { luma: alphaSum > 0 ? weighted / alphaSum : -1, maxLuma, maxAlpha, lay, slot, res };
  });
}

try {
  const context = await browser.newContext({
    viewport: { width: 1366, height: 860 },
    reducedMotion: 'reduce',
  });
  await context.addInitScript(() => {
    try {
      Object.defineProperty(navigator, 'gpu', { value: undefined, configurable: true });
    } catch {
      // ignore
    }
  });
  page = await context.newPage();
  const catalogGate = new Promise((resolve) => { releaseInitialCatalog = resolve; });
  let catalogHeld = false;
  await page.route('**/api/logos', async (route) => {
    if (!catalogHeld && route.request().method() === 'GET') {
      catalogHeld = true;
      await catalogGate;
    }
    await route.continue();
  });
  page.on('pageerror', (err) => out.errors.push(err.message));
  page.on('console', (msg) => {
    if (msg.type() === 'error') out.consoleErrors.push(msg.text());
  });

  await page.goto(`http://127.0.0.1:${port}/?token=${token}#/photo/${encodeURIComponent(photo1)}`);
  await page.locator('.pv:not([data-placeholder="1"]) > .pv-img, .pv > .pv-gpu').first().waitFor({ state: 'visible' });

  out.envCheck = await page.evaluate(() => ({
    hasWebGpu: Boolean(navigator.gpu),
    reducedMotion: matchMedia('(prefers-reduced-motion: reduce)').matches,
  }));
  assert.equal(out.envCheck.hasWebGpu, false);
  assert.equal(out.envCheck.reducedMotion, true);

  // 切到「边框」面板
  await page.getByRole('tab', { name: '边框' }).or(page.locator('button', { hasText: '边框' }).first()).first().click();
  await page.locator('.fcards').waitFor({ state: 'visible' });

  // 1) 不含标志位的四款样式不显示「标志」区块
  out.hiddenLogoStyles = {};
  for (const st of ['gallery', 'film', 'instant', 'darkroom']) {
    await page.locator(`.fcard[data-style="${st}"]`).click();
    out.hiddenLogoStyles[st] = await page.locator('.logo-tiles').count();
    assert.equal(out.hiddenLogoStyles[st], 0, `${st} 不应出现标志行`);
  }

  // 2) 切到徕卡水印：默认选中 RICOH，显示空态说明；记录 RICOH 时样式卡片像素哈希以便对比
  await page.locator('.fcard[data-style="leica"]').click();
  await page.locator('.logo-tiles').waitFor({ state: 'visible' });
  assert.equal(await page.locator('.logo-tile[data-logo="ricoh"]').getAttribute('aria-pressed'), 'true');
  assert.equal(await page.locator('.logo-empty').count(), 0, '目录响应前不应抢先显示空态');
  const catalogDone = page.waitForResponse((response) => new URL(response.url()).pathname === '/api/logos'
    && response.request().method() === 'GET');
  releaseInitialCatalog();
  const catalogResponse = await catalogDone;
  assert.equal(catalogResponse.status(), 200);
  await catalogResponse.finished();
  assert.equal(catalogHeld, true, '测试必须实际延迟过目录请求');
  await page.locator('.logo-empty').waitFor({ state: 'visible' });
  assert(await page.locator('.logo-empty').isVisible());
  assert.match(await page.locator('.logo-empty').innerText(), /还没有自己的标志/);
  await page.locator('.fcard[data-style="leica"] canvas').waitFor({ state: 'visible' });
  const leicaCardRicohHash = await canvasHash(page.locator('.fcard[data-style="leica"] canvas'));

  // 3) 打开上传弹窗：先传伪装成 .png 的 SVG 被拒，再传白底深色圆环图自动开「去掉白底」并调容差
  await page.locator('[data-logo-action="upload"]').click();
  const dialog = page.locator('#modal-root .dialog');
  await dialog.waitFor({ state: 'visible' });
  const fileInput = dialog.getByLabel('选择标志图片', { exact: true });
  await fileInput.setInputFiles(path.join(filesDir, 'sneaky_svg.png'));
  await dialog.getByText('这个文件打不开。请换一张 PNG、JPG 或 WebP 图片。', { exact: true }).first().waitFor();
  assert(await dialog.getByRole('button', { name: '保存并使用', exact: true }).isDisabled());

  await fileInput.setInputFiles(path.join(filesDir, 'white_ring.png'));
  const keySwitch = dialog.getByLabel('去掉白底', { exact: true });
  await page.waitForFunction(() => {
    const el = document.querySelector('#modal-root input[aria-label="去掉白底"]');
    return el && !el.disabled && el.checked === true;
  });
  assert(await keySwitch.isChecked());
  const rightPv = dialog.locator('.logo-pv-canvas').nth(1);
  const hashTol30 = await canvasHash(rightPv);
  const tolSlider = dialog.getByLabel('容差', { exact: true });
  await tolSlider.fill('55');
  await tolSlider.dispatchEvent('input');
  const hashTol55 = await canvasHash(rightPv);
  assert.notEqual(hashTol30, hashTol55, '调节去白底容差应实时刷新右侧棋盘格预览');
  await tolSlider.fill('30');
  await tolSlider.dispatchEvent('input');

  await dialog.getByLabel('名称', { exact: true }).fill('墨环印');
  const uploadDone = page.waitForResponse((r) => new URL(r.url()).pathname === '/api/logos' && r.request().method() === 'POST');
  await dialog.getByRole('button', { name: '保存并使用', exact: true }).click();
  assert.equal((await uploadDone).status(), 200);
  const uploadedTile = page.locator('.logo-tile[data-logo^="u_"][aria-pressed="true"]');
  await uploadedTile.waitFor();
  const logoId = await uploadedTile.getAttribute('data-logo');
  out.uploadedLogoId = logoId;
  assert.equal(await page.locator('.logo-empty').count(), 0);
  await page.waitForFunction(() => {
    const cv = document.querySelector('.pv-deco');
    return cv && cv.width > 0 && cv.height > 0;
  });
  // 等自定义标志图片加载并画进舞台与卡片
  await page.waitForFunction(async () => {
    const { customLogoImage } = await import('/assets/finish_frame_core.js');
    const cv = document.querySelector('.pv-deco');
    return Boolean(cv && cv.width > 0 && customLogoImage && customLogoImage(document.querySelector('.logo-tile[aria-pressed="true"]')?.dataset.logo));
  });
  await page.evaluate(() => new Promise(requestAnimationFrame));
  const leicaCardCustomHash = await canvasHash(page.locator('.fcard[data-style="leica"] canvas'));
  out.cardChangedFromRicoh = leicaCardRicohHash !== leicaCardCustomHash;

  // 4) 舞台即时重绘（0 次 /render 请求）：切大小、切深色触发自动反白提示、切颜色模式、切型号文字、切其它带标志样式
  let renderRequests = 0;
  const onReq = (req) => {
    if (/\/api\/photos\/[^/]+\/render/.test(new URL(req.url()).pathname)) renderRequests += 1;
  };
  page.on('request', onReq);

  const sigBeforeSize = await decoSignature(page);
  await page.locator('[aria-label="大小"] button', { hasText: '大' }).click();
  await page.evaluate(() => new Promise(requestAnimationFrame));
  const sigAfterSize = await decoSignature(page);
  assert.notEqual(sigBeforeSize, sigAfterSize, '切「大」后当帧应重绘舞台边框层');

  // 切 4:5 比例与黑色底：深色墨环印在黑底上自动反白并弹提示
  await page.getByLabel('成品比例', { exact: true }).selectOption('4:5');
  await page.locator('.swrow button[title="黑"]').click();
  await page.evaluate(() => new Promise(requestAnimationFrame));
  const sigBlackAuto = await decoSignature(page);
  assert.notEqual(sigAfterSize, sigBlackAuto);
  await page.getByText('标志和这个边框的颜色太接近，已自动改成白色', { exact: false }).first().waitFor();

  await page.locator('[aria-label="标志颜色"] button', { hasText: '原色' }).click();
  await page.evaluate(() => new Promise(requestAnimationFrame));
  const sigBlackOrig = await decoSignature(page);
  assert.notEqual(sigBlackAuto, sigBlackOrig, '原色与自动反白在黑底上像素应不同');
  const origSample = await sampleStageSlotInkLuma(page);
  out.stageSlotOrigDarkLuma = origSample.luma;
  out.stageSlotOrigDarkMax = origSample.maxLuma;

  await page.locator('[aria-label="标志颜色"] button', { hasText: '自动' }).click();
  await page.getByLabel('型号文字「GXR」', { exact: true }).click();
  await page.evaluate(() => new Promise(requestAnimationFrame));
  const sigNoModel = await decoSignature(page);
  assert.notEqual(sigBlackAuto, sigNoModel, '关闭型号文字 GXR 应即时改变版面');

  for (const st of ['hasselblad', 'thin_keyline', 'cinema', 'exhibition_label', 'slide_mount', 'postage_stamp', 'blur_fill', 'leica']) {
    await page.locator(`.fcard[data-style="${st}"]`).click();
    await page.evaluate(() => new Promise(requestAnimationFrame));
    assert.equal(await page.locator('.logo-tiles').count(), 1, `${st} 应显示标志行`);
  }
  await page.locator('.swrow button[title="黑"]').click();
  await page.evaluate(() => new Promise(requestAnimationFrame));
  page.off('request', onReq);
  out.renderRequestsDuringFrameClicks = renderRequests;
  assert.equal(renderRequests, 0, '切换标志、颜色、大小、样式不应触发服务器 /render');

  // 5) 核对舞台、卡片、尺寸行与原尺寸 PNG / JPEG 导出（清单第 8 项）
  await page.waitForFunction(() => {
    const el = document.querySelector('.frame-size');
    return el && /^成品 \d+ × \d+ 像素/.test(el.textContent || '');
  });
  await page.evaluate(async (id) => {
    const { flushPhotos } = await import('/assets/saves.js');
    await flushPhotos([id]);
  }, photo1);
  out.sizeLine = await page.locator('.frame-size').innerText();

  const autoSample = await sampleStageSlotInkLuma(page);
  out.stageSlotAutoWhiteLuma = autoSample.luma;
  out.stageSlotAutoWhiteMax = autoSample.maxLuma;
  out.outputSizeInfo = autoSample.res;
  out.logoInfoMap = await page.evaluate(async () => {
    const { getLogoMap } = await import('/assets/finish_frame_core.js');
    return getLogoMap();
  });

  const stageAndCard = await page.evaluate((slotInfo) => {
    const pv = document.querySelector('.pv').getBoundingClientRect();
    const cardCv = document.querySelector('.fcard[data-style="leica"] canvas');
    const cardRect = cardCv.getBoundingClientRect();
    const sx = cardCv.width / slotInfo.layW;
    const sy = cardCv.height / slotInfo.layH;
    const x0 = Math.max(0, Math.floor(slotInfo.x * sx));
    const y0 = Math.max(0, Math.floor(slotInfo.y * sy));
    const w = Math.max(1, Math.ceil(slotInfo.w * sx));
    const h = Math.max(1, Math.ceil(slotInfo.h * sy));
    const data = cardCv.getContext('2d').getImageData(x0, y0, w, h).data;
    let maxV = 0;
    for (let i = 0; i < data.length; i += 4) {
      const v = Math.max(data[i], data[i + 1], data[i + 2]);
      if (v > maxV) maxV = v;
    }
    return {
      stagePvAspect: pv.width / pv.height,
      cardCssAspect: cardRect.width / cardRect.height,
      cardBitmap: [cardCv.width, cardCv.height],
      cardSlotAutoWhiteMax: maxV,
    };
  }, {
    layW: autoSample.lay.width,
    layH: autoSample.lay.height,
    x: autoSample.slot.x,
    y: autoSample.slot.y,
    w: autoSample.slot.w,
    h: autoSample.slot.h,
  });
  Object.assign(out, stageAndCard);

  out.exportedPng = await page.evaluate(async (id) => {
    const { api } = await import('/assets/api.js');
    return api(`/api/photos/${encodeURIComponent(id)}/export`, { method: 'POST', body: { format: 'png', quality: 95 } });
  }, photo1);
  out.exportedJpeg = await page.evaluate(async (id) => {
    const { api } = await import('/assets/api.js');
    return api(`/api/photos/${encodeURIComponent(id)}/export`, { method: 'POST', body: { format: 'jpeg', quality: 92 } });
  }, photo1);

  // 6) 整页刷新后，未点「边框」标签时（默认在配方页）舞台叠加层也自动加载并画出已存自定义标志
  const reloads = Number(process.env.GXR_LOGO_RELOAD_STRESS || 3);
  out.reloadAttempts = reloads;
  for (let reload = 0; reload < reloads; reload++) {
    await page.reload();
    await page.locator('.pv:not([data-placeholder="1"]) > .pv-img, .pv > .pv-gpu').first().waitFor({ state: 'visible' }).catch(async (error) => {
      const diagnostic = await page.evaluate(() => ({
        hash: location.hash,
        stages: [...document.querySelectorAll('.pv')].map((pv) => ({
          style: pv.getAttribute('style'),
          width: pv.getBoundingClientRect().width,
          height: pv.getBoundingClientRect().height,
          media: [...pv.querySelectorAll('img, canvas')].map((media) => ({
            tag: media.tagName, class: media.className,
            width: media.getBoundingClientRect().width,
            height: media.getBoundingClientRect().height,
            naturalWidth: media.naturalWidth, naturalHeight: media.naturalHeight,
            complete: media.complete, style: media.getAttribute('style'),
          })),
        })),
      }));
      console.error('RELOAD_DIAGNOSTIC', JSON.stringify({ ...diagnostic, errors: out.errors, consoleErrors: out.consoleErrors }));
      await page.screenshot({ path: path.join(filesDir, 'reload-failure.png') });
      throw error;
    });
    await page.waitForFunction(() => {
      const cv = document.querySelector('.pv-deco');
      if (!cv || !cv.width || !cv.height) return false;
      const d = cv.getContext('2d').getImageData(0, 0, cv.width, cv.height).data;
      for (let i = 3; i < d.length; i += 64) if (d[i] > 180) return true;
      return false;
    });
    const reloadSample = await sampleStageSlotInkLuma(page);
    out.reloadStageLogoMaxAlpha = reloadSample.maxAlpha;
  }

  // 重新打开「边框」面板，点「设为默认」，并验证未编辑过的第三张照片自动继承该自定义标志
  await page.getByRole('tab', { name: '边框' }).or(page.locator('button', { hasText: '边框' }).first()).first().click();
  await page.locator(`.logo-tile[data-logo="${logoId}"][aria-pressed="true"]`).waitFor();
  const defSaved = page.waitForResponse((r) => new URL(r.url()).pathname === '/api/defaults/finish' && r.request().method() === 'PUT');
  await page.getByRole('button', { name: '设为默认', exact: true }).click();
  await (await defSaved).finished();
  await page.getByText('已设为默认', { exact: true }).first().waitFor();

  await goPhoto(page, photo3);
  await page.getByRole('tab', { name: '边框' }).or(page.locator('button', { hasText: '边框' }).first()).first().click();
  await page.locator(`.logo-tile[data-logo="${logoId}"][aria-pressed="true"]`).waitFor();
  out.inheritedLogoOnPhoto3 = await page.locator('.logo-tile[aria-pressed="true"]').getAttribute('data-logo');

  // 7) 先把 photo2 显式设成「无」边框，再回 photo1 复制边框并粘贴给 photo2，再撤销回「无」
  await goPhoto(page, photo2);
  await page.getByRole('tab', { name: '边框' }).or(page.locator('button', { hasText: '边框' }).first()).first().click();
  await page.locator('.fcard[data-style="none"]').click();
  await page.evaluate(async (id) => {
    const { flushPhotos } = await import('/assets/saves.js');
    await flushPhotos([id]);
  }, photo2);

  await goPhoto(page, photo1);
  await page.getByRole('tab', { name: '边框' }).or(page.locator('button', { hasText: '边框' }).first()).first().click();
  await page.locator('button[title="选择复制哪些内容"]').click();
  const copyDlg = page.locator('#modal-root .dialog');
  await copyDlg.waitFor({ state: 'visible' });
  const frameGroupBox = copyDlg.locator('input[type="checkbox"][data-group="frame"]');
  await frameGroupBox.waitFor({ state: 'visible' });
  if (!(await frameGroupBox.isChecked())) await frameGroupBox.check();
  await copyDlg.getByRole('button', { name: '复制', exact: true }).click();
  await page.locator('button[data-paste="1"]:not([aria-disabled="true"])').waitFor();

  await goPhoto(page, photo2);
  await page.getByRole('tab', { name: '边框' }).or(page.locator('button', { hasText: '边框' }).first()).first().click();
  const pasteResp = page.waitForResponse((r) => new URL(r.url()).pathname === '/api/edits/paste');
  await page.locator('button[data-paste="1"]').click();
  assert.equal((await pasteResp).status(), 200);
  await page.locator(`.logo-tile[data-logo="${logoId}"][aria-pressed="true"]`).waitFor();
  out.pastedStyleOnPhoto2 = await page.locator('.fcard[aria-pressed="true"]').getAttribute('data-style');
  assert.equal(out.pastedStyleOnPhoto2, 'leica');

  await page.locator('button[title^="撤销"]').click();
  await page.locator('.fcard[data-style="none"][aria-pressed="true"]').waitFor();
  await page.evaluate(async (id) => {
    const { flushPhotos } = await import('/assets/saves.js');
    await flushPhotos([id]);
  }, photo2);

  // 8) 回到 photo1，打开「管理我的标志…」改名并删除，验证照片与默认边框均回退到 RICOH
  await goPhoto(page, photo1);
  await page.getByRole('tab', { name: '边框' }).or(page.locator('button', { hasText: '边框' }).first()).first().click();
  await page.locator(`.logo-tile[data-logo="${logoId}"][aria-pressed="true"]`).waitFor();

  await page.locator('.logo-manage-link').click();
  const manageDlg = page.locator('#modal-root .dialog');
  await manageDlg.waitFor({ state: 'visible' });
  await manageDlg.locator('.logo-count-badge').waitFor({ state: 'visible' });
  assert.equal(await manageDlg.locator('.logo-count-badge').innerText(), '1 / 12');

  await manageDlg.locator('.logo-manage-row').getByRole('button', { name: '重命名', exact: true }).click();
  const renameInput = manageDlg.getByLabel('标志名称', { exact: true });
  await renameInput.fill('新墨环印');
  const renameDone = page.waitForResponse((r) => r.url().includes(`/api/logos/${logoId}`) && r.request().method() === 'PUT');
  await manageDlg.locator('.logo-manage-row').getByRole('button', { name: '保存', exact: true }).click();
  assert.equal((await renameDone).status(), 200);
  await manageDlg.getByText('新墨环印', { exact: true }).first().waitFor();

  await page.route(`**/api/logos/${encodeURIComponent(logoId)}`, async (route) => {
    if (route.request().method() !== 'DELETE') {
      await route.continue();
      return;
    }
    const resp = await route.fetch();
    out.deleteResponse = await resp.json();
    await route.fulfill({ response: resp });
  });
  await manageDlg.locator('.logo-manage-row').getByRole('button', { name: '删除', exact: true }).click();
  await manageDlg.locator('.logo-confirm-bar').waitFor({ state: 'visible' });
  const delDone = page.waitForResponse((r) => r.url().includes(`/api/logos/${logoId}`) && r.request().method() === 'DELETE');
  await manageDlg.locator('.logo-manage-row .btn.danger').click();
  assert.equal((await delDone).status(), 200);
  assert.equal(await manageDlg.locator('.logo-count-badge').innerText(), '0 / 12');
  await manageDlg.getByRole('button', { name: '完成', exact: true }).click();

  await page.locator('.logo-tile[data-logo="ricoh"][aria-pressed="true"]').waitFor();
  await page.locator('.logo-empty').waitFor({ state: 'visible' });
  await page.locator('.swrow button[title="白"]').click();
  await page.evaluate(async (id) => {
    const { flushPhotos } = await import('/assets/saves.js');
    await flushPhotos([id]);
  }, photo1);

  await page.locator('[data-logo-action="upload"]').click();
  const transparentDialog = page.locator('#modal-root .dialog');
  await transparentDialog.getByLabel('选择标志图片', { exact: true }).setInputFiles(path.join(filesDir, 'transparent_mark.png'));
  await transparentDialog.getByRole('button', { name: '保存并使用', exact: true }).waitFor();
  await page.waitForFunction(() => document.querySelector('#modal-root .dialog .modal-foot .primary')?.disabled === false);
  out.transparentKeyDisabled = await transparentDialog.getByLabel('去掉白底', { exact: true }).isDisabled();
  out.transparentKeyChecked = await transparentDialog.getByLabel('去掉白底', { exact: true }).isChecked();
  assert.equal(out.transparentKeyDisabled, true);
  assert.equal(out.transparentKeyChecked, false);
  await transparentDialog.getByRole('button', { name: '取消', exact: true }).click();

  await goPhoto(page, photo3);
  await page.getByRole('tab', { name: '边框' }).or(page.locator('button', { hasText: '边框' }).first()).first().click();
  await page.locator('.logo-tile[data-logo="ricoh"][aria-pressed="true"]').waitFor();

  assert.equal(out.errors.length, 0, JSON.stringify(out.errors));
  console.log(JSON.stringify(out));
} finally {
  releaseInitialCatalog();
  if (page) await page.unrouteAll({ behavior: 'wait' });
  await browser.close();
}
