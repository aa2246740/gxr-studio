// UI-M3 在无头 Chrome 里：解码门禁（decode）、和桌面逐字节比的整条导出（parity）、停止 / 读不了 / 陷阱 / 清空等流程（flows）。
// 照片字节、文件和 EXIF 一律不打到 stdout：结果写进 manifest.out（测试的临时目录），stdout 只有一行 "done"。
// 假的共享面板用 addInitScript 装上，记下每次 share() 的文件、调用时是否还在用户激活里、canShare 的调用。

import { pathToFileURL } from 'node:url';
import { readFileSync, rmSync, writeFileSync } from 'node:fs';

const [runtime, url, manifestPath] = process.argv.slice(2);
const manifest = JSON.parse(readFileSync(manifestPath, 'utf8'));
const { launchPinnedChromium, verifyPinnedRuntime } = await import(pathToFileURL(runtime).href);
verifyPinnedRuntime();

const browser = await launchPinnedChromium({
  args: ['--enable-unsafe-webgpu', '--enable-unsafe-swiftshader', '--use-angle=swiftshader', '--use-webgpu-adapter=swiftshader'],
});

const out = { pageErrors: [], steps: {} };

async function settle(page) {
  for (let attempt = 0; attempt < 5; attempt += 1) {
    await page.waitForSelector('.app[data-core="1"]', { timeout: 60_000 });
    const reloaded = await page.waitForEvent('framenavigated', { timeout: 4_000 }).then(() => true).catch(() => false);
    if (!reloaded) return;
  }
  throw new Error('page kept reloading');
}

/** 假的共享面板。`mode`：'record'（记下、立刻 resolve）、'none'（没有 share，走下载链接）。 */
function fakeShare(mode) {
  const scope = window;
  scope.__shareLog = { canShare: 0, calls: [] };
  if (mode === 'none') {
    Object.defineProperty(navigator, 'share', { value: undefined, configurable: true });
    Object.defineProperty(navigator, 'canShare', { value: undefined, configurable: true });
    return;
  }
  Object.defineProperty(navigator, 'canShare', {
    configurable: true,
    value: (data) => {
      scope.__shareLog.canShare += 1;
      return Array.isArray(data?.files) && data.files.length > 0 && data.files.every((f) => f instanceof File);
    },
  });
  Object.defineProperty(navigator, 'share', {
    configurable: true,
    value: (data) => {
      const active = navigator.userActivation ? navigator.userActivation.isActive : null;
      scope.__shareLog.calls.push({ active, files: data.files });
      return Promise.resolve();
    },
  });
}

async function newPage(shareMode = 'record') {
  const page = await browser.newPage({ viewport: { width: 390, height: 844 } });
  page.on('pageerror', (error) => out.pageErrors.push(error.message));
  await page.addInitScript(fakeShare, shareMode);
  await page.goto(url);
  await settle(page);
  return page;
}

const hex = `(buf) => [...new Uint8Array(buf)].map((b) => b.toString(16).padStart(2, '0')).join('')`;

// ---- decode gate

async function decodeGate() {
  const page = await newPage();
  await page.evaluate(() => {
    const input = document.createElement('input');
    input.type = 'file';
    input.multiple = true;
    input.id = 'devdecode';
    input.style.display = 'none';
    document.body.append(input);
  });
  await page.locator('#devdecode').setInputFiles(manifest.jpgs);
  const rows = [];
  for (let i = 0; i < manifest.jpgs.length; i += 1) {
    const row = await page.evaluate(
      async ({ i, hexSrc }) => {
        const toHex = eval(hexSrc);
        const file = document.querySelector('#devdecode').files[i];
        const reply = await window.__devExport.core.decode(file);
        if (!reply.ok) return { ok: false };
        const sha = toHex(await crypto.subtle.digest('SHA-256', reply.rgb));
        window.__lastRgb = reply.rgb;
        return { ok: true, width: reply.width, height: reply.height, sha };
      },
      { i, hexSrc: hex },
    );
    // 和 Pillow 不同时把 RGB 存到测试的临时目录，Python 那边只报个数和第一个位置。
    if (row.ok && manifest.want && row.sha !== manifest.want[i] && manifest.dumpDir) {
      const b64 = await page.evaluate(() => {
        const bytes = new Uint8Array(window.__lastRgb);
        let text = '';
        for (let k = 0; k < bytes.length; k += 0x8000) text += String.fromCharCode(...bytes.subarray(k, k + 0x8000));
        return btoa(text);
      });
      writeFileSync(`${manifest.dumpDir}/decoded-${i}.rgb`, Buffer.from(b64, 'base64'));
      row.dumped = true;
    }
    rows.push(row);
  }
  out.steps.decode = rows;
  await page.close();
}

try {
  if (manifest.mode === 'decode') await decodeGate();
  else if (manifest.mode === 'parity') await (await import(pathToFileURL(manifest.parityDriver).href)).parity({ browser, newPage, manifest, out });
  else if (manifest.mode === 'flows') await (await import(pathToFileURL(manifest.flowsDriver).href)).flows({ browser, newPage, manifest, out });
  else throw new Error(`unknown mode ${manifest.mode}`);
} catch (err) {
  out.error = err instanceof Error ? err.message.split('\n')[0].slice(0, 300) : String(err).slice(0, 300);
} finally {
  writeFileSync(manifest.out, JSON.stringify(out));
  await browser.close();
  if (manifest.cleanup) for (const path of manifest.cleanup) rmSync(path, { force: true });
}
console.log('done');
