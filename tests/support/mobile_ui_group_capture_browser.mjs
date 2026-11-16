// UI-M3-GROUP：选进照片、等缩略图读完，输出每张照片的拍摄时间（dev 钩子 __gxrEdits 的 ms）。只输出一行 JSON。

import { pathToFileURL } from 'node:url';
import { readFileSync } from 'node:fs';

const [runtime, url, manifestPath] = process.argv.slice(2);
const manifest = JSON.parse(readFileSync(manifestPath, 'utf8'));
const { launchPinnedChromium, verifyPinnedRuntime } = await import(pathToFileURL(runtime).href);
verifyPinnedRuntime();
const browser = await launchPinnedChromium({ args: ['--enable-unsafe-webgpu', '--enable-unsafe-swiftshader', '--use-angle=swiftshader', '--use-webgpu-adapter=swiftshader'] });
const out = { pageErrors: [] };
try {
  const page = await browser.newPage({ viewport: { width: 440, height: 956 } });
  page.on('pageerror', (error) => out.pageErrors.push(error.message));
  await page.goto(url);
  await page.waitForSelector('.app[data-core="1"]', { timeout: 30_000 });
  await page.locator('input.filepick:not(.calibpick)').setInputFiles(manifest.files);
  await page.waitForFunction(
    (n) => {
      const sub = document.querySelector('.nav small');
      return sub && !sub.textContent.includes('正在读取') && document.querySelectorAll('.cell canvas').length >= n && window.__gxrEdits;
    },
    manifest.photos,
    { timeout: 120_000 },
  );
  out.photos = await page.evaluate(() => window.__gxrEdits().map((row) => ({ stem: row.stem, ms: row.ms })));
  out.heads = await page.locator('.gh[data-row] .cnt').allInnerTexts();
} catch (error) {
  out.error = String(error?.stack ?? error);
} finally {
  await browser.close();
}
console.log(JSON.stringify(out));
if (out.error) process.exit(1);
