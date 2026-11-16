// UI-M3 第 9 项的流程：一张的导出面板和成品尺寸、做到第 2 张时停止、轮到之前文件被删再重试、Worker 陷阱后重建、
// 批次进行中滚网格、清空时停下、没有共享面板时的下载链接。没有相机配方文件（只看导出这一路），照片走相机 JPG。

import { copyFileSync, rmSync } from 'node:fs';
import { cellOf, exportFromGrid, pickAll, sharedFiles, shareAll, waitPhase } from './mobile_ui_export_parity.mjs';

const flowOf = (page) =>
  page.evaluate(() => {
    const f = window.__devExport.flow;
    const b = f.current();
    return {
      phase: b ? b.phase : null,
      staged: b ? b.staged.length : 0,
      ok: b ? b.okCount() : 0,
      failures: b ? [...b.failures.values()].map((x) => ({ stem: x.stem, kind: x.kind, message: x.message })) : [],
      alive: window.__devExport.core.alive(),
      workers: window.__devExport.core.marks.workers,
      dock: document.querySelector('.pdock')?.innerText ?? null,
      shared: window.__shareLog?.calls?.length ?? 0,
    };
  });

/** 清空照片，回到起始页（「更多 → 清空照片，重新选」）。 */
async function wipe(page) {
  await page.locator('.nav .nb.plain').click();
  await page.getByRole('button', { name: /清空照片，重新选/ }).click();
  await page.waitForSelector('[data-screen="start"]');
}

async function closeCardDialog(page) {
  const keep = page.getByRole('button', { name: '先用读进来的' });
  if (await keep.count()) await keep.click();
}

export async function flows({ newPage, manifest, out }) {
  const page = await newPage('record');
  const all = manifest.files;

  // 1. 编辑器里「导出这张」：一张的面板写成品尺寸（Worker 里从文件头算），没有相机配方文件时有桌面那句说明。
  await pickAll(page, { count: manifest.stems.length }, all);
  await page.locator(cellOf(manifest.stems[0])).evaluate((node) => node.click());
  await page.waitForSelector('.editor');
  const editorButton = await page.locator('.editor .actbar [data-act="export"]').innerText();
  await page.locator('.editor .actbar [data-act="export"]').click();
  await page.waitForSelector('.ms.export');
  await page.waitForFunction(() => /像素/.test(document.querySelector('.ms.export .size')?.textContent ?? ''), null, { timeout: 60_000 });
  const sizeOriginal = await page.locator('.ms.export .size').innerText();
  await page.locator('.ms.export .frow').nth(0).locator('.seg button', { hasText: '小' }).click();
  const sizeSmall = await page.locator('.ms.export .size').innerText();
  const sheetText = await page.locator('.ms.export').innerText();
  await page.locator('.ms.export .row .nb').click();
  await page.getByRole('button', { name: '照片', exact: true }).click();
  await page.waitForSelector('[data-screen="grid"]');
  await page.locator('.nav .nb.bold').click();
  const selectBar = await page.locator('.dock').innerText();
  await page.locator('.nav .nb.bold').click();
  out.steps.single = { editorButton, sizeOriginal, sizeSmall, sheetText, selectBar };

  // 2. 五张，做完第 2 张就点「停止」（同一帧里点，第 3 张来不及做完）。
  await exportFromGrid(page, manifest.stems.slice(0, 5));
  await page.evaluate(
    () =>
      new Promise((done) => {
        const tick = () => {
          const b = window.__devExport.flow.current();
          if (b && b.okCount() >= 2) {
            [...document.querySelectorAll('.pdock button')].find((x) => x.textContent === '停止')?.click();
            done();
          } else requestAnimationFrame(tick);
        };
        tick();
      }),
  );
  const stopped = await flowOf(page);
  // 一张新的导出照常能做。
  await page.waitForTimeout(300);
  await exportFromGrid(page, manifest.stems.slice(0, 1));
  await waitPhase(page, ['ready'], 300_000);
  out.steps.stop = { stopped, after: await flowOf(page) };
  await shareAll(page);
  await page.locator('.pdock [data-act="done"]').click();

  // 3. Worker 陷阱：三张里第 2 张在 prepare 之后模拟陷阱。内存那句、Worker 重建、第 3 张做好。
  await page.evaluate(() => {
    window.__devExport.trapAt = 1;
  });
  await exportFromGrid(page, manifest.stems.slice(0, 3));
  await waitPhase(page, ['ready'], 300_000);
  out.steps.trap = await flowOf(page);
  await page.evaluate(() => {
    window.__devExport.trapAt = undefined;
  });
  // 重试这张：成功，三张都做好。
  await page.locator('.pdock [data-act="retry"]').click();
  await waitPhase(page, ['ready'], 300_000);
  out.steps.trapRetry = await flowOf(page);
  await shareAll(page);
  await page.locator('.pdock [data-act="done"]').click();

  // 4. 批次进行中滚网格：记下 rAF 的间隔（SwiftShader，只记不断言帧率）；然后清空：批次停下，没有晚到的文件。
  const before = await page.evaluate(() => window.__shareLog.calls.length);
  await exportFromGrid(page, manifest.stems.slice(0, 5));
  await waitPhase(page, ['running']);
  const scroll = await page.evaluate(
    () =>
      new Promise((done) => {
        const scroller = document.querySelector('.gscroll');
        const gaps = [];
        let last = performance.now();
        const start = last;
        const tick = (now) => {
          gaps.push(now - last);
          last = now;
          if (scroller) scroller.scrollTop = (scroller.scrollTop + 7) % Math.max(1, scroller.scrollHeight - scroller.clientHeight);
          if (now - start < 2000) requestAnimationFrame(tick);
          else done({ ticks: gaps.length, maxGap: Math.max(...gaps), phase: window.__devExport.flow.current()?.phase });
        };
        requestAnimationFrame(tick);
      }),
  );
  await wipe(page);
  const wiped = await flowOf(page);
  await page.waitForTimeout(3000);
  out.steps.scroll = scroll;
  out.steps.clear = { wiped, later: await flowOf(page), sharedBefore: before };

  // 5. 轮到之前文件被删：06f 写读不了那一句；重新选这张、点「重试这张」就做好。
  await pickAll(page, { count: 2 }, [manifest.first, manifest.victim]);
  rmSync(manifest.victim, { force: true });
  await exportFromGrid(page, [manifest.firstStem, manifest.victimStem]);
  await waitPhase(page, ['ready'], 300_000);
  await closeCardDialog(page);
  const failed = await flowOf(page);
  copyFileSync(manifest.victimSource, manifest.victim);
  await page.locator('input.filepick:not(.calibpick)').setInputFiles([manifest.victim]);
  await page.waitForTimeout(500);
  await closeCardDialog(page);
  await page.locator('.pdock [data-act="retry"]').click();
  await waitPhase(page, ['ready'], 300_000);
  const retried = await flowOf(page);
  const calls = await page.evaluate(() => window.__shareLog.calls.length);
  await shareAll(page);
  const names = (await sharedFiles(page, manifest.outDir, 'retry-', calls)).flatMap((c) => c.files.map((f) => f.name));
  out.steps.readFail = { failed, retried, names };
  await page.close();

  // 6. 没有共享面板的浏览器：一句话加每个文件的下载链接。
  const bare = await newPage('none');
  await pickAll(bare, { count: 1 }, [manifest.first]);
  await exportFromGrid(bare, [manifest.firstStem]);
  await waitPhase(bare, ['ready'], 300_000);
  await bare.locator('.pdock [data-act="share"]').click();
  await bare.waitForSelector('.pdock .links[data-state="no-share"]');
  out.steps.noShare = { text: await bare.locator('.pdock .links').innerText(), links: await bare.locator('.pdock a[download]').count() };
  await bare.close();
}
