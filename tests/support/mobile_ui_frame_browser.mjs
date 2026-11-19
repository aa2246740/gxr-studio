// UI-M4 边框、更多选项、时间戳、自定义标志与帮助页（OFL 字体许可）的 Playwright 浏览器端到端测试。

import { pathToFileURL } from 'node:url';
import { readFileSync } from 'node:fs';
import path from 'node:path';

const [runtime, baseUrl, manifestPath] = process.argv.slice(2);
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

const out = {
  pageErrors: [],
  licences: [],
  cardsCount: 0,
  cardsNonBlank: 0,
  galleryControls: null,
  slideColors: [],
  leicaChips: [],
  decorActive: false,
  decoPixels: 0,
  blurPixels: 0,
  noneCleared: false,
  cjkFrameErr: '',
  cjkStampErr: '',
  instantHasPlacement: false,
  stampPixelsOnly: 0,
  logoCountAfterSave: '',
  logoRenamed: false,
  logoDeletedFallback: false,
  logoTooBigErr: false,
};

async function canvasAlphaSum(page, selector) {
  return page.evaluate((sel) => {
    const cv = document.querySelector(sel);
    if (!cv || cv.width === 0 || cv.height === 0) return 0;
    const ctx = cv.getContext('2d');
    if (!ctx) return 0;
    const data = ctx.getImageData(0, 0, cv.width, cv.height).data;
    let painted = 0;
    for (let i = 3; i < data.length; i += 4) if (data[i] > 10) painted += 1;
    return painted;
  }, selector);
}

try {
  const context = await browser.newContext({ viewport: { width: 390, height: 844 } });
  const page = await context.newPage();
  page.on('pageerror', (err) => out.pageErrors.push(err.message));

  await page.goto(baseUrl);
  for (let attempt = 0; attempt < 5; attempt += 1) {
    await page.waitForSelector('.app[data-core="1"]', { timeout: 30_000 });
    const reloaded = await page
      .waitForEvent('framenavigated', { timeout: 3_500 })
      .then(() => true)
      .catch(() => false);
    if (!reloaded) break;
  }

  // 1. 起始页帮助弹窗包含 6 套 OFL 字体许可链接且不提及 ricoh
  await page.locator('.help button.lnk').click();
  await page.waitForSelector('.licence-links a', { timeout: 10_000 });
  out.licences = await page.locator('.licence-links a').evaluateAll((els) =>
    els.map((el) => ({ text: el.textContent?.trim() ?? '', href: el.getAttribute('href') ?? '' })),
  );
  await page.locator('.ms .hd button:has-text("完成")').click();

  // 2. 选片并进入编辑器
  await page.locator('input.filepick:not(.calibpick)').setInputFiles(manifest.pairs);
  await page.waitForFunction(
    () => {
      const sub = document.querySelector('.nav small');
      return sub && !sub.textContent?.includes('正在读取') && document.querySelector('.cell canvas');
    },
    null,
    { timeout: 60_000 },
  );
  await page.locator('.cell').first().click();
  await page.waitForSelector('[data-screen="editor"]', { timeout: 30_000 });
  await page.waitForTimeout(600);

  // 3. 切到「边框」页，等待 13 张缩略卡画完
  const openFrame = async () => {
    await page.locator('.editor .toolbar [data-tool="more"]').click();
    await page.locator('.more-sheet [data-more="frame"]').click();
    await page.waitForSelector('.panel.frame');
  };
  const openFrameOpts = async () => {
    const back = page.locator('.panel.frame [data-act="frame-back"]');
    if (!(await back.isVisible())) {
      await page.locator('.editor .band-frame-hd [data-act="frame-opts"]').click();
      await page.waitForSelector('.panel.frame [data-act="frame-back"]');
    }
  };
  const closeFrameOpts = async () => {
    const back = page.locator('.panel.frame [data-act="frame-back"]');
    if (await back.isVisible()) {
      await back.click();
      await page.waitForSelector('.panel.frame .strip:not([hidden])');
    }
  };
  await openFrame();
  await page.waitForFunction(
    () => {
      const cards = document.querySelectorAll('.panel.frame .rc .th.fr canvas');
      const skels = document.querySelectorAll('.panel.frame .rc .th.fr.skel');
      return cards.length === 13 && skels.length === 0;
    },
    null,
    { timeout: 30_000 },
  );
  const cardMetrics = await page.evaluate(() => {
    const canvases = [...document.querySelectorAll('.panel.frame .rc .th.fr canvas')];
    let nonBlank = 0;
    for (const cv of canvases) {
      if (cv.width <= 0 || cv.height <= 0) continue;
      const d = cv.getContext('2d').getImageData(0, 0, cv.width, cv.height).data;
      let lit = 0;
      for (let i = 3; i < d.length; i += 16) if (d[i] > 10) lit += 1;
      if (lit > 10) nonBlank += 1;
    }
    return { count: canvases.length, nonBlank };
  });
  out.cardsCount = cardMetrics.count;
  out.cardsNonBlank = cardMetrics.nonBlank;

  // 4. 点选「白边」检查颜色（3 色）与粗细（细/中/粗）
  await page.locator('.panel.frame .rc[data-style="gallery"]').click();
  await openFrameOpts();
  await page.waitForSelector('.panel.frame .swc[data-color="cream"]');
  out.galleryControls = await page.evaluate(() => ({
    colors: [...document.querySelectorAll('.panel.frame .swc')].map((el) => el.dataset.color),
    weights: [...document.querySelectorAll('.panel.frame .seg[aria-label="粗细"] button')].map((el) => el.dataset.weight),
  }));

  // 5. 点选「幻灯片」仅显示米白/黑 2 色
  await closeFrameOpts();
  await page.locator('.panel.frame .rc[data-style="slide_mount"]').click();
  await openFrameOpts();
  await page.waitForFunction(() => document.querySelectorAll('.panel.frame .swc').length === 2);
  out.slideColors = await page.locator('.panel.frame .swc').evaluateAll((els) => els.map((e) => e.dataset.color));

  // 6. 点选「徕卡水印」-> 舞台出现 .pv[data-decor="1"].framed 与非空 .pv-deco 画布
  await closeFrameOpts();
  await page.locator('.panel.frame .rc[data-style="leica"]').click();
  await page.waitForFunction(() => Boolean(document.querySelector('.stage .pv.framed[data-decor="1"] .pv-deco')));
  await page.waitForTimeout(300);
  out.decorActive = await page.evaluate(() => Boolean(document.querySelector('.stage .pv.framed[data-decor="1"]')));
  out.decoPixels = await canvasAlphaSum(page, '.stage .pv .pv-deco');
  await openFrameOpts();
  out.leicaChips = await page.locator('.panel.frame .chips .chip[data-act^="frame-"]').evaluateAll((els) => els.map((e) => e.textContent?.trim() ?? ''));

  // 7. 模糊背景也能画出非空叠加画布
  await closeFrameOpts();
  await page.locator('.panel.frame .rc[data-style="blur_fill"]').click();
  await page.waitForTimeout(350);
  out.blurPixels = await canvasAlphaSum(page, '.stage .pv .pv-deco');

  // 切回「徕卡水印」做「更多选项」与 CJK 拒收测试
  await page.locator('.panel.frame .rc[data-style="leica"]').click();
  await page.waitForTimeout(200);
  await openFrameOpts();
  await page.locator('.panel.frame [data-act="frame-more"]').click();
  await page.waitForSelector('.ms.frame-more-sheet');
  const frameTextInput = page.locator('.ms.frame-more-sheet input.txtbox');
  await frameTextInput.fill('GXR 2026 上海');
  await page.waitForSelector('.ms.frame-more-sheet .ferr:not([hidden])');
  out.cjkFrameErr = (await page.locator('.ms.frame-more-sheet .ferr').textContent())?.trim() ?? '';
  const cleanedFrameText = await frameTextInput.inputValue();
  if (/[\u2e80-\uffff]/.test(cleanedFrameText)) {
    throw new Error(`CJK not stripped from frame text: ${cleanedFrameText}`);
  }
  await page.locator('.ms.frame-more-sheet .hd button:has-text("完成")').click();

  // 8. 自定义标志：上传、去白底、1/12 计数、重命名、删除回退 + 超过 5 MB 拦截
  await page.locator('.panel.frame [data-act="frame-logo"]').click();
  await page.waitForSelector('.ms.logo-sheet[data-state="logo"]');
  await page.locator('.ms.logo-sheet [data-logo-action="upload"]').click();
  await page.waitForSelector('.ms.logo-sheet[data-state="logo-upload"]');

  if (manifest.tooBigLogo) {
    await page.locator('.ms.logo-sheet input.filepick').setInputFiles(manifest.tooBigLogo);
    await page.waitForFunction(() => document.querySelector('.ftoast')?.textContent?.includes('5 MB'));
    out.logoTooBigErr = true;
  }

  await page.locator('.ms.logo-sheet input.filepick').setInputFiles(manifest.logoPng);
  await page.waitForFunction(
    () => {
      const btn = document.querySelector('.ms.logo-sheet button[data-logo-action="save"]');
      return btn && !btn.disabled;
    },
    null,
    { timeout: 15_000 },
  );
  await page.locator('.ms.logo-sheet button[data-logo-action="save"]').click();
  await page.waitForFunction(() => !document.querySelector('.ms.logo-sheet'));
  await page.locator('.panel.frame [data-act="frame-logo"]').click();
  await page.waitForSelector('.ms.logo-sheet[data-state="logo"]');
  out.logoCountAfterSave = await page.locator('.ms.logo-sheet .fsline .num').first().textContent();

  // 重命名再删除回退
  const rowMore = page.locator('.ms.logo-sheet .logo-row button[aria-label*="更多操作"]').first();
  await rowMore.click();
  await page.locator('.ms.logo-sheet .logo-row-acts button:has-text("重命名")').click();
  const renameBox = page.locator('.ms.logo-sheet .logo-row-acts input.txtbox');
  await renameBox.fill('StudioMark');
  await page.locator('.ms.logo-sheet .logo-row-acts button:has-text("保存")').click();
  await page.waitForFunction(() => document.body.innerText.includes('StudioMark'));
  out.logoRenamed = true;

  await page.locator('.ms.logo-sheet .logo-row button[aria-label*="StudioMark"]').click();
  await page.locator('.ms.logo-sheet .logo-row-acts button:has-text("删除")').click();
  await page.locator('.ms.logo-sheet .logo-row-acts button.danger:has-text("删除")').click();
  await page.waitForFunction(() => document.querySelector('.ms.logo-sheet .logo-opt[data-logo="ricoh"]')?.classList.contains('on'));
  out.logoDeletedFallback = true;
  await page.locator('.ms.logo-sheet .hd button:has-text("完成")').click();

  // 9. 切回「无」边框：舞台恢复干净；开「时间戳」独立画到照片上
  await closeFrameOpts();
  await page.locator('.panel.frame .rc[data-style="none"]').click();
  await page.waitForFunction(() => !document.querySelector('.stage .pv[data-decor="1"]'));
  out.noneCleared = await page.evaluate(
    () => !document.querySelector('.stage .pv')?.dataset.decor && !document.querySelector('.stage .pv .pv-deco'),
  );

  await page.locator('.editor .toolbar [data-tool="more"]').click();
  await page.locator('.more-sheet [data-more="stamp"]').click();
  await page.locator('.stamp-band input.switch').click();
  await page.waitForFunction(() => Boolean(document.querySelector('.stage .pv[data-decor="1"] .pv-deco')));
  await page.waitForTimeout(200);
  out.stampPixelsOnly = await canvasAlphaSum(page, '.stage .pv .pv-deco');

  // 10. 切到「拍立得」打开时间戳面板验证「印记落点」与文字 CJK 拒收
  await openFrame();
  await page.locator('.panel.frame .rc[data-style="instant"]').click();
  await openFrameOpts();
  await page.locator('.panel.frame [data-act="open-stamp"]').click();
  await page.waitForSelector('.ms.stamp-sheet');
  out.instantHasPlacement = await page.locator('.ms.stamp-sheet .seg[aria-label="印记落点"]').count().then((n) => n === 1);
  await page.locator('.ms.stamp-sheet .seg[aria-label="内容"] button:has-text("日期和文字")').click();
  const stampInput = page.locator('.ms.stamp-sheet input.txtbox');
  await stampInput.fill('ABC 日本');
  await page.waitForSelector('.ms.stamp-sheet .ferr:not([hidden])');
  out.cjkStampErr = (await page.locator('.ms.stamp-sheet .ferr').textContent())?.trim() ?? '';
  await page.locator('.ms.stamp-sheet .hd button:has-text("完成")').click();

  // 11. 可选：如果要求截 4 张图，走 ?shot=frame* 导出 PNG 到 shotDir
  if (manifest.shotDir) {
    for (const shot of ['frame', 'frame-more', 'frame-stamp', 'frame-logo']) {
      const sp = await context.newPage();
      sp.on('pageerror', (err) => out.pageErrors.push(`${shot}: ${err.message}`));
      await sp.goto(new URL(`?shot=${shot}`, baseUrl).href);
      await sp.waitForFunction((expected) => document.querySelector('.app')?.dataset.shot === expected, shot, {
        timeout: 45_000,
      });
      await sp.waitForTimeout(200);
      await sp.screenshot({ path: path.join(manifest.shotDir, `${shot}.png`) });
      await sp.close();
    }
  }
} finally {
  await browser.close();
}

process.stdout.write(`${JSON.stringify(out)}\n`);
