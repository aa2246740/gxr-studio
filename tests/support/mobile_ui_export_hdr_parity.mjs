// UI-M3-HDR 第 5 项：四对 RAW+JPG 和一张只有 JPG 的照片在界面里编辑，打开 HDR 导出（原始尺寸 / 高），再用记住的开关
// 导出小 / 低。假共享面板收下的文件写进 manifest.outDir，Python 那边和桌面 export_finish(hdr=True) 逐字节比。
// 页面上的字只回报给测试（写进报告文件），不打到 stdout。

import { batchFacts, edit, exportFromGrid, pickAll, shareAll, sharedFiles, waitPhase, cellOf } from './mobile_ui_export_parity.mjs';

/** 面板上 HDR 那一行：开关停没停用、开没开、那一行的字、混着只有 JPG 的提示（看不看得见）。 */
async function hdrRow(page) {
  const sheet = page.locator('.ms.export');
  const sw = sheet.locator('input.switch');
  const mix = sheet.locator('[data-state="hdr-mix"]');
  return {
    disabled: await sw.isDisabled(),
    checked: await sw.isChecked(),
    role: await sw.getAttribute('role'),
    text: await sheet.locator('.hdrrow').innerText(),
    mix: (await mix.count()) ? { visible: await mix.isVisible(), text: await mix.innerText() } : null,
  };
}

/** 一批跑着的时候 05b 的两行字。 */
async function runningText(page) {
  await page.waitForFunction(() => document.querySelector('.pdock')?.dataset.phase === 'running', null, { timeout: 60_000 });
  return page.locator('.pdock .top .tx').innerText();
}

export async function parity({ newPage, manifest, out }) {
  const page = await newPage('record');
  await page.evaluate(({ binding }) => {
    window.__devExport.binding = binding;
    window.__devExport.chunkFiles = 20;
    localStorage.removeItem('gxr.mobile.export_hdr');
  }, manifest);
  await pickAll(page, manifest, manifest.files);
  const edits = [];
  for (const [stem, plan] of Object.entries(manifest.plans)) edits.push(await edit(page, stem, plan));
  out.steps.edits = edits;

  // 原始尺寸 / 高：U1 首次默认开 HDR；先关掉确认混杂提示隐藏，再重新打开并记住。
  const full = {};
  full.sheet = await exportFromGrid(page, manifest.order, '原始尺寸', '高', async () => {
    full.before = await hdrRow(page);
    await page.locator('.ms.export input.switch').click();
    full.afterOff = await hdrRow(page);
    await page.locator('.ms.export input.switch').click();
    full.after = await hdrRow(page);
    full.stored = await page.evaluate(() => localStorage.getItem('gxr.mobile.export_hdr'));
  });
  full.running = await runningText(page);
  await waitPhase(page, ['ready']);
  if (await page.locator('.pdock [data-act="toggle-details"]').count()) {
    await page.locator('.pdock [data-act="toggle-details"]').click();
  }
  full.ready = await page.locator('.pdock').innerText();
  full.facts = await batchFacts(page);
  full.cap = await page.evaluate(() => window.__devExport.flow.current().cap);
  full.shared = await shareAll(page);
  full.files = await sharedFiles(page, manifest.outDir, 'full-');
  await page.waitForFunction(() => !document.querySelector('.pdock'), null, { timeout: 10_000 });
  out.steps.full = full;

  // 小 / 低：开关记住了，不再点。先关掉上一批闲着的 Worker（只在开发服务器里），内存单独量。
  await page.evaluate(() => window.__devExport.core.stop());
  const calls = await page.evaluate(() => window.__shareLog.calls.length);
  const small = {};
  small.sheet = await exportFromGrid(page, manifest.smallOrder, '小', '低', async () => {
    small.row = await hdrRow(page);
  });
  small.running = await runningText(page);
  await waitPhase(page, ['ready']);
  if (await page.locator('.pdock [data-act="toggle-details"]').count()) {
    await page.locator('.pdock [data-act="toggle-details"]').click();
  }
  small.ready = await page.locator('.pdock').innerText();
  small.facts = await batchFacts(page);
  small.shared = await shareAll(page);
  small.files = await sharedFiles(page, manifest.outDir, 'small-', calls);
  await page.waitForFunction(() => !document.querySelector('.pdock'), null, { timeout: 10_000 });
  out.steps.small = small;

  // 只选那张只有 JPG 的：开关停用，写「HDR 需要 RAW 文件」，记住的选择不变。
  await page.locator('.nav .nb.bold').click();
  await page.locator(cellOf(manifest.jpgOnly)).evaluate((node) => node.click());
  await page.locator('.dock [data-act="export"]').click();
  await page.waitForSelector('.ms.export');
  out.steps.jpgOnly = { row: await hdrRow(page), stored: await page.evaluate(() => localStorage.getItem('gxr.mobile.export_hdr')) };
  await page.close();
}
