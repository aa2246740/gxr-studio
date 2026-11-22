// 无头浏览器验收驱动：手机端「导出关机图」（GXROFF.JPG）。
// 隐私约束：绝不向 stdout 或报告输出任何真实照片文件名、拍摄时间或 EXIF。

import { readFileSync, writeFileSync } from 'node:fs';
import { pathToFileURL } from 'node:url';

const [runtime, url, manifestPath] = process.argv.slice(2);
const m = JSON.parse(readFileSync(manifestPath, 'utf8'));
const { launchPinnedChromium, verifyPinnedRuntime } = await import(pathToFileURL(runtime).href);
verifyPinnedRuntime();

const browser = await launchPinnedChromium({
  args: ['--enable-unsafe-webgpu', '--enable-unsafe-swiftshader', '--use-angle=swiftshader', '--use-webgpu-adapter=swiftshader'],
});

const out = { pageErrors: [], steps: {}, shared: {}, error: null };

async function installShareCapture(page) {
  await page.addInitScript(() => {
    const calls = [];
    window.__offShareCapture = calls;
    Object.defineProperty(navigator, 'canShare', {
      configurable: true,
      value: (data) => Boolean(data && Array.isArray(data.files) && data.files.length > 0),
    });
    Object.defineProperty(navigator, 'share', {
      configurable: true,
      value: async (data) => {
        if (window.__offShareReject) throw new DOMException('stubbed share failure', window.__offShareReject);
        const active = navigator.userActivation?.isActive ?? true;
        const files = [];
        for (const f of data?.files ?? []) {
          const buf = new Uint8Array(await f.arrayBuffer());
          let bin = '';
          for (let i = 0; i < buf.length; i += 0x8000) {
            bin += String.fromCharCode(...buf.subarray(i, i + 0x8000));
          }
          files.push({ name: f.name, type: f.type, size: f.size, b64: btoa(bin) });
        }
        calls.push({ active, files });
      },
    });
  });
}

async function waitSheetReady(page) {
  await page.waitForFunction(
    () => {
      const sheet = document.querySelector('.ms.off-image-sheet');
      const img = sheet?.querySelector('.oi-preview');
      const btn = sheet?.querySelector('[data-act="off-image-export"]');
      return (
        sheet &&
        sheet.dataset.ready === '1' &&
        img &&
        !img.hidden &&
        img.complete &&
        img.naturalWidth === 640 &&
        img.naturalHeight === 480 &&
        btn &&
        btn.disabled === false
      );
    },
    null,
    { timeout: 60_000 },
  );
}

async function run() {
  const ctx = await browser.newContext({ viewport: { width: 390, height: 844 }, deviceScaleFactor: 2 });
  const page = await ctx.newPage();
  page.on('pageerror', (err) => out.pageErrors.push(String(err).slice(0, 300)));
  await installShareCapture(page);

  await page.goto(url, { waitUntil: 'domcontentloaded' });
  await page.waitForSelector('.app[data-core="1"]', { timeout: 30_000 });

  // 选入合成测试 JPG，进入编辑器
  await page.locator('input.filepick').first().setInputFiles([m.photoJpg]);
  await page.waitForSelector('.cell', { timeout: 30_000 });
  await page.locator('.cell').first().click();
  await page.waitForSelector('.editor .nav [data-act="editor-more"]', { timeout: 30_000 });

  // 打开编辑器顶栏 ⋯ 菜单，检查并进入「做成关机图」
  await page.locator('.editor .nav [data-act="editor-more"]').click();
  await page.waitForSelector('.ms[data-state="editor-more"]', { timeout: 10_000 });
  out.steps.editorMoreLabels = (await page.locator('.ms[data-state="editor-more"] .lrow b').allInnerTexts()).map((s) => s.trim());
  await page.locator('.ms[data-state="editor-more"] [data-act="more-off-image"]').click();

  await page.waitForSelector('.ms.off-image-sheet', { timeout: 15_000 });
  await waitSheetReady(page);

  out.steps.initial = await page.evaluate(() => {
    const sheet = document.querySelector('.ms.off-image-sheet');
    const img = sheet.querySelector('.oi-preview');
    return {
      title: sheet.querySelector('h2')?.textContent?.trim() || '',
      naturalWidth: img.naturalWidth,
      naturalHeight: img.naturalHeight,
      srcIsBlob: img.src.startsWith('blob:'),
      meta: sheet.querySelector('[data-role="off-image-meta"]')?.textContent?.trim() || '',
      notes: [...sheet.querySelectorAll('.oi-notes p')].map((p) => p.textContent.trim()),
      exportDisabled: sheet.querySelector('[data-act="off-image-export"]').disabled,
      datState: sheet.dataset.datState || '',
    };
  });

  // 点「导出」：必须直接走 navigator.share（不经过相册导出条 .pdock）
  await page.locator('.ms.off-image-sheet [data-act="off-image-export"]').click();
  await page.waitForFunction(() => window.__offShareCapture.length === 1, null, { timeout: 15_000 });
  out.steps.pdockPresentAfterShare = (await page.locator('.pdock').count()) > 0;
  out.shared.photo = await page.evaluate(() => window.__offShareCapture[0]);

  // 换一张图：带透明通道 PNG
  const srcBeforePng = await page.locator('.ms.off-image-sheet .oi-preview').getAttribute('src');
  await page.locator('.ms.off-image-sheet [data-role="off-image-file"]').setInputFiles([m.altPng]);
  await page.waitForFunction(
    (oldSrc) => {
      const sheet = document.querySelector('.ms.off-image-sheet');
      const img = sheet?.querySelector('.oi-preview');
      return sheet && sheet.dataset.ready === '1' && img && img.src !== oldSrc && img.complete && img.naturalWidth === 640 && img.naturalHeight === 480;
    },
    srcBeforePng,
    { timeout: 60_000 },
  );
  await page.locator('.ms.off-image-sheet [data-act="off-image-export"]').click();
  await page.waitForFunction(() => window.__offShareCapture.length === 2, null, { timeout: 15_000 });
  out.shared.altPng = await page.evaluate(() => window.__offShareCapture[1]);

  // 换一张图：EXIF Orientation=6 竖拍 JPEG
  const srcBeforeRot = await page.locator('.ms.off-image-sheet .oi-preview').getAttribute('src');
  await page.locator('.ms.off-image-sheet [data-role="off-image-file"]').setInputFiles([m.altRot6]);
  await page.waitForFunction(
    (oldSrc) => {
      const sheet = document.querySelector('.ms.off-image-sheet');
      const img = sheet?.querySelector('.oi-preview');
      return sheet && sheet.dataset.ready === '1' && img && img.src !== oldSrc && img.complete && img.naturalWidth === 640 && img.naturalHeight === 480;
    },
    srcBeforeRot,
    { timeout: 60_000 },
  );
  await page.locator('.ms.off-image-sheet [data-act="off-image-export"]').click();
  await page.waitForFunction(() => window.__offShareCapture.length === 3, null, { timeout: 15_000 });
  out.shared.altRot6 = await page.evaluate(() => window.__offShareCapture[2]);

  // 共享失败：用户取消（AbortError）时留在面板、不出下载链接；别的失败要给出下载链接。
  const linkState = () =>
    page.evaluate(() => {
      const link = document.querySelector('.ms.off-image-sheet .oi-download-link');
      return link ? { visible: !link.closest('[hidden]'), download: link.getAttribute('download'), text: link.textContent.trim() } : null;
    });
  await page.evaluate(() => {
    window.__offShareReject = 'AbortError';
  });
  await page.locator('.ms.off-image-sheet [data-act="off-image-export"]').click();
  await page.waitForTimeout(800);
  out.steps.shareAbort = await linkState();
  await page.evaluate(() => {
    window.__offShareReject = 'NotAllowedError';
  });
  await page.locator('.ms.off-image-sheet [data-act="off-image-export"]').click();
  await page.waitForSelector('.ms.off-image-sheet .oi-download-link', { timeout: 10_000 });
  out.steps.shareRefused = await linkState();
  await page.evaluate(() => {
    window.__offShareReject = null;
  });

  // 强制触发真实 Rust WASM 校验失败：显示错误且禁用「导出」
  await page.evaluate(() => {
    window.__devOffImage.corruptTail = true;
  });
  await page.locator('.ms.off-image-sheet [data-role="off-image-file"]').setInputFiles([m.altPng]);
  await page.waitForFunction(
    () => {
      const sheet = document.querySelector('.ms.off-image-sheet');
      const err = sheet?.querySelector('[data-role="off-image-error"]');
      const btn = sheet?.querySelector('[data-act="off-image-export"]');
      return sheet && sheet.dataset.ready === '0' && err && !err.hidden && btn && btn.disabled === true;
    },
    null,
    { timeout: 30_000 },
  );
  out.steps.forcedFail = await page.evaluate(() => {
    const sheet = document.querySelector('.ms.off-image-sheet');
    window.__devOffImage.corruptTail = false;
    return {
      ready: sheet.dataset.ready,
      datState: sheet.dataset.datState || '',
      error: sheet.querySelector('[data-role="off-image-error"]')?.textContent?.trim() || '',
      exportDisabled: sheet.querySelector('[data-act="off-image-export"]').disabled,
      previewHidden: sheet.querySelector('.oi-preview').hidden,
    };
  });

  // 关闭面板后校验短命 Worker 与对象 URL 全部释放
  await page.locator('.ms.off-image-sheet [data-act="off-image-close"]').click();
  await page.waitForFunction(() => !document.querySelector('.ms.off-image-sheet'), null, { timeout: 10_000 });
  out.steps.cleanup = await page.evaluate(() => ({
    workersSpawned: window.__devOffImage?.workersSpawned ?? 0,
    workersAlive: window.__devOffImage?.workersAlive ?? -1,
    liveUrls: window.__devOffImage?.liveUrls ?? -1,
  }));

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
