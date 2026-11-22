// 无头浏览器验收驱动：桌面端「导出关机图」（GXROFF.JPG）。
// 隐私约束：绝不打印任何照片文件名、拍摄时间或 EXIF。

import { readFileSync, writeFileSync } from 'node:fs';
import { pathToFileURL } from 'node:url';

const [runtime, manifestPath] = process.argv.slice(2);
const m = JSON.parse(readFileSync(manifestPath, 'utf8'));
const { launchPinnedChromium, verifyPinnedRuntime } = await import(pathToFileURL(runtime).href);
verifyPinnedRuntime();

const browser = await launchPinnedChromium({});
const out = { pageErrors: [], steps: {}, error: null };

async function run() {
  const ctx = await browser.newContext({ viewport: { width: 1280, height: 820 } });
  const page = await ctx.newPage();
  page.on('pageerror', (err) => out.pageErrors.push(String(err).slice(0, 300)));

  await page.goto(`${m.base}/?token=${encodeURIComponent(m.token)}#/photo/${encodeURIComponent(m.shotId)}`, {
    waitUntil: 'domcontentloaded',
  });
  await page.waitForSelector('#btn-export-top', { timeout: 30_000 });
  await page.waitForSelector('#btn-export-menu', { timeout: 30_000 });

  out.steps.topExportLabel = (await page.locator('#btn-export-top').innerText()).trim();

  // 打开导出旁边的下拉菜单，检查两项文案后点「做成关机图…」
  await page.locator('#btn-export-menu').click();
  await page.waitForSelector('.export-menu', { timeout: 10_000 });
  out.steps.menuLabels = (await page.locator('.export-menu button').allInnerTexts()).map((s) => s.trim());
  await page.locator('.export-menu button', { hasText: '做成关机图…' }).click();

  await page.waitForSelector('.off-img-dialog', { timeout: 15_000 });
  await page.waitForFunction(
    () => {
      const dlg = document.querySelector('.off-img-dialog');
      const img = dlg?.querySelector('.off-img-preview');
      const btn = dlg?.querySelector('[data-act="export-off-image-submit"]');
      return dlg && img && !img.hidden && img.complete && img.naturalWidth === 640 && img.naturalHeight === 480 && btn && !btn.disabled;
    },
    null,
    { timeout: 30_000 },
  );

  out.steps.initial = await page.evaluate(() => {
    const dlg = document.querySelector('.off-img-dialog');
    const modal = dlg.closest('.dialog') || document;
    const img = dlg.querySelector('.off-img-preview');
    const exportBtn = dlg.querySelector('[data-act="export-off-image-submit"]');
    const meta = dlg.querySelector('[data-role="off-meta"]')?.textContent?.trim() || '';
    const notes = [...dlg.querySelectorAll('.off-img-notes p')].map((p) => p.textContent.trim());
    const title = modal.querySelector('h2, h3, .modal-title')?.textContent?.trim() || '';
    const fileLabel = [...dlg.querySelectorAll('.row.form')].find((r) => r.querySelector('.lab')?.textContent?.trim() === '文件名')?.querySelector('.ctl')?.textContent?.trim() || '';
    return {
      title,
      naturalWidth: img.naturalWidth,
      naturalHeight: img.naturalHeight,
      srcIsDataUrl: img.src.startsWith('data:image/jpeg;base64,'),
      meta,
      notes,
      fileLabel,
      exportDisabled: exportBtn.disabled,
    };
  });

  // 点「导出」把当前照片保存为 GXROFF.JPG 到临时导出目录
  await page.locator('.off-img-dialog [data-act="export-off-image-submit"]').click();
  await page.waitForSelector('.off-img-dialog .export-result .result.ok', { timeout: 30_000 });
  out.steps.photoExportStatus = (await page.locator('.off-img-dialog .export-result .res-main').innerText()).trim();

  // 使用「换一张图…」替换为另一张含透明通道的 PNG
  const prevSrc = await page.locator('.off-img-dialog .off-img-preview').getAttribute('src');
  await page.locator('.off-img-dialog input[type="file"]').setInputFiles(m.altPng);
  await page.waitForFunction(
    (oldSrc) => {
      const dlg = document.querySelector('.off-img-dialog');
      const img = dlg?.querySelector('.off-img-preview');
      const btn = dlg?.querySelector('[data-act="export-off-image-submit"]');
      return dlg && img && !img.hidden && img.src !== oldSrc && img.complete && img.naturalWidth === 640 && img.naturalHeight === 480 && btn && !btn.disabled;
    },
    prevSrc,
    { timeout: 30_000 },
  );
  out.steps.afterReplace = await page.evaluate(() => {
    const dlg = document.querySelector('.off-img-dialog');
    const img = dlg.querySelector('.off-img-preview');
    return {
      naturalWidth: img.naturalWidth,
      naturalHeight: img.naturalHeight,
      meta: dlg.querySelector('[data-role="off-meta"]')?.textContent?.trim() || '',
      exportDisabled: dlg.querySelector('[data-act="export-off-image-submit"]').disabled,
    };
  });

  // 再次导出替换后的图片到独立子目录
  const destInput = page.locator('.off-img-dialog input:not([type="file"])').first();
  await destInput.fill(m.altOutDir);
  await page.locator('.off-img-dialog [data-act="export-off-image-submit"]').click();
  await page.waitForSelector('.off-img-dialog .export-result .result.ok', { timeout: 30_000 });
  out.steps.altExportStatus = (await page.locator('.off-img-dialog .export-result .res-main').innerText()).trim();

  // 尝试导出到已挂载的存储卡目录：必须被拒绝
  await destInput.fill(m.cardDestDir);
  await page.locator('.off-img-dialog [data-act="export-off-image-submit"]').click();
  await page.waitForSelector('.off-img-dialog .export-result .result.bad', { timeout: 15_000 });
  out.steps.cardRefusedError = (await page.locator('.off-img-dialog .export-result .result.bad').innerText()).trim();

  // 模拟校验失败响应：必须显示原因并禁用「导出」按钮
  await page.route('**/api/off-image/preview*', async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        ok: false,
        bytes: 34000,
        quality: 10,
        width: 640,
        height: 480,
        dat_state: 2,
        errors: [{ code: 'over_max_bytes', message: '34000 B 超过上限 32768 B。' }],
        sha256: '0'.repeat(64),
        preview_data_url: null,
        preview_token: null,
      }),
    });
  });
  await page.locator('.off-img-dialog input[type="file"]').setInputFiles(m.altPng);
  await page.waitForFunction(
    () => {
      const dlg = document.querySelector('.off-img-dialog');
      const err = dlg?.querySelector('.export-result .result.bad');
      const btn = dlg?.querySelector('[data-act="export-off-image-submit"]');
      const img = dlg?.querySelector('.off-img-preview');
      return dlg && err && err.textContent.includes('32768') && btn && btn.disabled === true && img && img.hidden === true;
    },
    null,
    { timeout: 15_000 },
  );
  out.steps.forcedFail = await page.evaluate(() => {
    const dlg = document.querySelector('.off-img-dialog');
    return {
      error: dlg.querySelector('.export-result .result.bad')?.textContent?.trim() || '',
      exportDisabled: dlg.querySelector('[data-act="export-off-image-submit"]').disabled,
      previewHidden: dlg.querySelector('.off-img-preview').hidden,
    };
  });

  await ctx.close();
}

try {
  await run();
} catch (err) {
  out.error = err instanceof Error ? err.message : String(err);
} finally {
  writeFileSync(m.out, JSON.stringify(out, null, 2));
  await browser.close();
}
process.exit(out.error ? 1 : 0);
