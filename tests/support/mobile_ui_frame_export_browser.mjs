// UI-M4 第 9 项：在浏览器里给真机照片配置边框、时间戳与合成自定义标志并走完整导出，
// 对照桌面 sidecar、export_finish 成片字节（exact === true）或 dev 二进制画布 + 冻结 Gate B（exact === false），
// 以及 HDR / 内存高水位 / 中文时间戳拒收 / 删除在用标志回退 GXR 标志 / export_ink 陷阱恢复。
// 照片字节、文件名和 EXIF 一律不写 stdout，仅写入 manifest.out 临时文件。

import { existsSync, readdirSync, renameSync, rmSync } from 'node:fs';
import path from 'node:path';
import {
  cellOf,
  exportFromGrid,
  pickAll,
  shareAll,
  sharedFiles,
  waitPhase,
} from './mobile_ui_export_parity.mjs';

const REPORTS_DIR = path.resolve(import.meta.dirname, '..', '..', 'mobile', '.reports');

async function openEditorFrameTab(page, stem) {
  await page.locator(cellOf(stem)).evaluate((node) => node.click());
  await page.waitForFunction(
    (want) => document.querySelector('.editor')?.dataset.photo === want,
    stem,
    { timeout: 120_000 },
  );
  await page.getByRole('tab', { name: '边框', exact: true }).click();
  await page.waitForFunction(
    () => {
      const cards = document.querySelectorAll('.panel.frame .rc .th.fr canvas');
      const skels = document.querySelectorAll('.panel.frame .rc .th.fr.skel');
      return cards.length === 13 && skels.length === 0;
    },
    null,
    { timeout: 120_000 },
  );
}

async function backToGrid(page) {
  await page.getByRole('button', { name: '照片', exact: true }).click();
  await page.waitForSelector('[data-screen="grid"]', { timeout: 30_000 });
}

async function pickFrameStyle(page, style) {
  const card = page.locator(`.panel.frame .rc[data-style="${style}"]`);
  await card.scrollIntoViewIfNeeded();
  await card.click();
  await page.waitForFunction(
    (want) => document.querySelector('.panel.frame .rc.on')?.dataset.style === want,
    style,
    { timeout: 15_000 },
  );
}

function collectCanvasBins(prefix, destDir) {
  const moved = [];
  if (!existsSync(REPORTS_DIR)) return moved;
  for (const name of readdirSync(REPORTS_DIR)) {
    if (name.startsWith(`${prefix}-`) && name.endsWith('.bin')) {
      const src = path.join(REPORTS_DIR, name);
      const dst = path.join(destDir, name);
      renameSync(src, dst);
      moved.push(name);
    }
  }
  return moved;
}

function cleanResidualReports() {
  if (!existsSync(REPORTS_DIR)) return;
  for (const name of readdirSync(REPORTS_DIR)) {
    if (name.startsWith('m4_') && name.endsWith('.bin')) {
      rmSync(path.join(REPORTS_DIR, name), { force: true });
    }
  }
}

async function frameBatchFacts(page) {
  return page.evaluate(() => {
    const batch = window.__devExport.flow.current();
    const marks = structuredClone(window.__devExport.core.marks);
    const facts = {};
    batch.items.forEach((item, index) => {
      const shot = batch.shots.get(item.id) || {};
      facts[item.stem] = {
        index,
        side: item.req.side,
        layout: item.req.layout,
        width: shot.width ?? null,
        height: shot.height ?? null,
        exact: batch.exacts.has(item.id) ? batch.exacts.get(item.id) : null,
        result: batch.results.get(item.id) || null,
        binding: item.req.binding,
      };
    });
    return {
      facts,
      marks,
      mem: JSON.parse(document.querySelector('.app').dataset.exportMem || '{}'),
      stagedHigh: batch.stagedHigh,
      cap: batch.cap,
      times: batch.times,
    };
  });
}

async function runBatch(page, { stems, size = '原始尺寸', quality = '高', hdr = false, prefix, outDir, fileTag }) {
  await page.evaluate(
    ({ pfx }) => {
      window.__devExport.core.stop();
      window.__devExport.canvasReportPrefix = pfx || null;
      window.__devExport.inkTrapAt = null;
      window.__devExport.trapAt = null;
      localStorage.removeItem('gxr.mobile.export_hdr');
    },
    { pfx: prefix },
  );
  const callsBefore = await page.evaluate(() => window.__shareLog.calls.length);
  const sheet = await exportFromGrid(page, stems, size, quality, async () => {
    if (hdr) {
      await page.locator('.ms.export input.switch').click();
    }
  });
  await waitPhase(page, ['ready']);
  const facts = await frameBatchFacts(page);
  const ready = await page.locator('.pdock').innerText();
  const shared = await shareAll(page);
  const files = outDir && fileTag ? await sharedFiles(page, outDir, fileTag, callsBefore) : [];
  await page.locator('.pdock [data-act="done"]').click();
  await page.waitForFunction(() => !document.querySelector('.pdock'), null, { timeout: 15_000 });
  await page.evaluate(() => {
    window.__devExport.canvasReportPrefix = null;
    localStorage.removeItem('gxr.mobile.export_hdr');
  });
  return { sheet, ready, facts, shared, files };
}

export async function parity({ newPage, manifest, out }) {
  cleanResidualReports();
  const page = await newPage('record');
  try {
    await page.evaluate(({ binding }) => {
      window.__devExport.binding = binding;
      window.__devExport.chunkFiles = 4;
    }, manifest);

    await pickAll(page, manifest, manifest.files);
    const s = manifest.stems;

    // Pair A：白边 + 默认时间戳；同时核对单张带边框尺寸行（05a export_output_sizes）
    await openEditorFrameTab(page, s.a);
    await pickFrameStyle(page, 'gallery');
    await page.locator('.panel.frame .stamp-row input.switch').click();
    await page.waitForFunction(() => Boolean(document.querySelector('.stage .pv.framed[data-decor="1"]')));

    await page.getByRole('button', { name: '导出这张', exact: true }).click();
    await page.waitForSelector('.ms.export');
    await page.waitForFunction(
      () => {
        const txt = document.querySelector('.ms.export .num.size')?.textContent ?? '';
        return txt.includes(' × ') && !txt.includes('正在计算');
      },
      null,
      { timeout: 30_000 },
    );
    const singleSizeOriginal = (await page.locator('.ms.export .num.size').textContent())?.trim() ?? '';
    await page.locator('.ms.export .frow').nth(0).locator('.seg button', { hasText: '小' }).click();
    await page
      .waitForFunction(
        (prev) => {
          const txt = (document.querySelector('.ms.export .num.size')?.textContent ?? '').trim();
          return txt.includes(' × ') && !txt.includes('正在计算') && txt !== prev;
        },
        singleSizeOriginal,
        { timeout: 30_000 },
      )
      .catch(() => undefined);
    const singleSizeSmall = (await page.locator('.ms.export .num.size').textContent())?.trim() ?? '';
    await page.locator('.ms.export .row button:has-text("取消")').click();
    await page.waitForFunction(() => !document.querySelector('.ms.export'));
    out.steps.singleSizes = { original: singleSizeOriginal, small: singleSizeSmall };
    await backToGrid(page);

    // Pair B：blur_fill。关掉参数和标志，使这张没有文字、也没有内置徽标，走 exact 的逐字节成片。
    await openEditorFrameTab(page, s.b);
    await pickFrameStyle(page, 'blur_fill');
    await page.locator('.panel.frame [data-act="frame-params"]').click();
    await page.waitForFunction(
      () => !document.querySelector('.panel.frame [data-act="frame-params"]')?.classList.contains('on'),
    );
    await page.locator('.panel.frame [data-act="frame-logo"]').click();
    await page.waitForSelector('.ms.logo-sheet[data-state="logo"]');
    await page.locator('.ms.logo-sheet [data-logo="none"]').click();
    await page.locator('.ms.logo-sheet .hd button:has-text("完成")').click();
    await page.waitForFunction(() => !document.querySelector('.ms.logo-sheet'));
    await backToGrid(page);

    // Pair C：instant + 时间戳落在拍立得边框上
    await openEditorFrameTab(page, s.c);
    await pickFrameStyle(page, 'instant');
    await page.locator('.panel.frame .stamp-row input.switch').click();
    await page.locator('.panel.frame [data-act="open-stamp"]').click();
    await page.waitForSelector('.ms.stamp-sheet');
    await page.locator('.ms.stamp-sheet .seg[aria-label="印记落点"] button:has-text("拍立得边框上")').click();
    await page.locator('.ms.stamp-sheet .hd button:has-text("完成")').click();
    await page.waitForFunction(() => !document.querySelector('.ms.stamp-sheet'));
    await backToGrid(page);

    // Pair D：leica + 内置 GXR 标志 + 参数 + 日期
    await openEditorFrameTab(page, s.d);
    await pickFrameStyle(page, 'leica');
    const leicaOn = await page.evaluate(() => ({
      logo: document.querySelector('.panel.frame [data-act="frame-logo"]')?.classList.contains('on') ?? false,
      params: document.querySelector('.panel.frame [data-act="frame-params"]')?.classList.contains('on') ?? false,
      date: document.querySelector('.panel.frame [data-act="frame-date"]')?.classList.contains('on') ?? false,
    }));
    out.steps.leicaOn = leicaOn;
    await backToGrid(page);

    // Pair E：film
    await openEditorFrameTab(page, s.e);
    await pickFrameStyle(page, 'film');
    await backToGrid(page);

    // Pair F：上传合成自定义标志 + exhibition_label
    await openEditorFrameTab(page, s.f);
    await pickFrameStyle(page, 'exhibition_label');
    await page.locator('.panel.frame [data-act="frame-logo"]').click();
    await page.waitForSelector('.ms.logo-sheet[data-state="logo"]');
    await page.locator('.ms.logo-sheet [data-logo-action="upload"]').click();
    await page.waitForSelector('.ms.logo-sheet[data-state="logo-upload"]');
    await page.locator('.ms.logo-sheet input.filepick').setInputFiles(manifest.logoPng);
    await page.waitForFunction(
      () => {
        const btn = document.querySelector('.ms.logo-sheet button[data-logo-action="save"]');
        return Boolean(btn && !btn.disabled);
      },
      null,
      { timeout: 20_000 },
    );
    await page.locator('.ms.logo-sheet button[data-logo-action="save"]').click();
    await page.waitForFunction(() => !document.querySelector('.ms.logo-sheet'), null, { timeout: 20_000 });
    await backToGrid(page);

    // JPG-only：darkroom + 默认时间戳
    await openEditorFrameTab(page, s.jpgOnly);
    await pickFrameStyle(page, 'darkroom');
    await page.locator('.panel.frame .stamp-row input.switch').click();
    await backToGrid(page);

    // 1. 全量 7 张导出：原始尺寸 / 高（SDR）
    const full = await runBatch(page, {
      stems: manifest.order,
      size: '原始尺寸',
      quality: '高',
      hdr: false,
      prefix: 'm4_full',
      outDir: manifest.outDir,
      fileTag: 'full-',
    });
    full.bins = collectCanvasBins('m4_full', manifest.canvasDir);
    out.steps.full = full;

    // 2. Pair A 与 Pair B 导出：小 / 低（SDR）
    const small = await runBatch(page, {
      stems: [s.a, s.b],
      size: '小',
      quality: '低',
      hdr: false,
      prefix: 'm4_small',
      outDir: manifest.outDir,
      fileTag: 'small-',
    });
    small.bins = collectCanvasBins('m4_small', manifest.canvasDir);
    out.steps.small = small;

    // 3. Pair A、Pair B 与 JPG-only 开 HDR 导出：原始尺寸 / 高（验证 hdr=true、frame_layout、no_raw）
    const hdr = await runBatch(page, {
      stems: [s.a, s.b, s.jpgOnly],
      size: '原始尺寸',
      quality: '高',
      hdr: true,
      prefix: 'm4_hdr',
      outDir: manifest.outDir,
      fileTag: 'hdr-',
    });
    hdr.bins = collectCanvasBins('m4_hdr', manifest.canvasDir);
    out.steps.hdr = hdr;

    // 4. 全尺寸 blur_fill（标志 + 参数 + 默认时间戳）：SDR 与 HDR 内存高水位。
    await openEditorFrameTab(page, s.b);
    await page.locator('.panel.frame [data-act="frame-params"]').click();
    await page.waitForFunction(() =>
      document.querySelector('.panel.frame [data-act="frame-params"]')?.classList.contains('on'),
    );
    await page.locator('.panel.frame .stamp-row input.switch').click();
    await page.locator('.panel.frame [data-act="frame-logo"]').click();
    await page.waitForSelector('.ms.logo-sheet[data-state="logo"]');
    await page.locator('.ms.logo-sheet [data-logo="ricoh"]').click();
    await page.locator('.ms.logo-sheet .hd button:has-text("完成")').click();
    await page.waitForFunction(() => !document.querySelector('.ms.logo-sheet'));
    await backToGrid(page);

    const memSdr = await runBatch(page, {
      stems: [s.b],
      size: '原始尺寸',
      quality: '高',
      hdr: false,
      prefix: 'm4_memsdr',
      outDir: manifest.outDir,
      fileTag: 'memsdr-',
    });
    memSdr.bins = collectCanvasBins('m4_memsdr', manifest.canvasDir);
    out.steps.memSdr = memSdr;

    const memHdr = await runBatch(page, {
      stems: [s.b],
      size: '原始尺寸',
      quality: '高',
      hdr: true,
      prefix: 'm4_memhdr',
      outDir: manifest.outDir,
      fileTag: 'memhdr-',
    });
    memHdr.bins = collectCanvasBins('m4_memhdr', manifest.canvasDir);
    out.steps.memHdr = memHdr;

    // 5. 时间戳文字输入中文：显示提示行且不落入存储 / sidecar
    await openEditorFrameTab(page, s.e);
    await page.locator('.panel.frame .stamp-row input.switch').click();
    await page.locator('.panel.frame [data-act="open-stamp"]').click();
    await page.waitForSelector('.ms.stamp-sheet');
    await page.locator('.ms.stamp-sheet .seg[aria-label="内容"] button:has-text("日期和文字")').click();
    const stampInput = page.locator('.ms.stamp-sheet input.txtbox');
    await stampInput.fill('东京京都');
    await page.waitForSelector('.ms.stamp-sheet .ferr:not([hidden])');
    const cjkErr = (await page.locator('.ms.stamp-sheet .ferr').textContent())?.trim() ?? '';
    const cjkInputAfter = await stampInput.inputValue();
    await page.locator('.ms.stamp-sheet .hd button:has-text("完成")').click();
    await page.waitForFunction(() => !document.querySelector('.ms.stamp-sheet'));
    await backToGrid(page);

    const cjkBatch = await runBatch(page, {
      stems: [s.e],
      size: '小',
      quality: '低',
      hdr: false,
      prefix: null,
      outDir: null,
      fileTag: null,
    });
    out.steps.cjkStamp = {
      err: cjkErr,
      inputValue: cjkInputAfter,
      sideStamp: cjkBatch.facts.facts[s.e]?.side?.stamp ?? null,
    };

    // 6. 删除正在使用的自定义标志：显示回退提示，导出改用 GXR 标志（在 leica 上绘制 GXR mark）
    await openEditorFrameTab(page, s.f);
    await pickFrameStyle(page, 'leica');
    await page.locator('.panel.frame [data-act="frame-logo"]').click();
    await page.waitForSelector('.ms.logo-sheet[data-state="logo"]');
    await page.locator('.ms.logo-sheet .logo-row button[aria-label*="更多操作"]').first().click();
    await page.locator('.ms.logo-sheet .logo-row-acts button:has-text("删除")').click();
    await page.locator('.ms.logo-sheet .logo-row-acts button.danger:has-text("删除")').click();
    await page.waitForFunction(() =>
      Boolean(document.querySelector('.ftoast')?.textContent?.includes('找不到这个标志，已改回 GXR 标志。')),
    );
    const deletedToast = (await page.locator('.ftoast').textContent())?.trim() ?? '';
    const gxrSelectedAfterDelete = await page.evaluate(() =>
      Boolean(document.querySelector('.ms.logo-sheet .logo-opt[data-logo="ricoh"]')?.classList.contains('on')),
    );
    await page.locator('.ms.logo-sheet .hd button:has-text("完成")').click();
    await page.waitForFunction(() => !document.querySelector('.ms.logo-sheet'));
    await backToGrid(page);

    const deletedExport = await runBatch(page, {
      stems: [s.f],
      size: '原始尺寸',
      quality: '高',
      hdr: false,
      prefix: 'm4_del',
      outDir: manifest.outDir,
      fileTag: 'del-',
    });
    deletedExport.bins = collectCanvasBins('m4_del', manifest.canvasDir);
    out.steps.deletedLogo = {
      toast: deletedToast,
      gxrSelected: gxrSelectedAfterDelete,
      export: deletedExport,
    };

    // 7. export_ink 开发态陷阱：报 M3 内存不足文案，重建 Worker 后下一张成功，点重试全部成功
    await page.evaluate(() => {
      window.__devExport.core.stop();
      window.__devExport.canvasReportPrefix = null;
      window.__devExport.inkTrapAt = 0;
    });
    await exportFromGrid(page, [s.a, s.c], '小', '低');
    await waitPhase(page, ['ready']);
    const inkTrapSnap = await page.evaluate(() => {
      const batch = window.__devExport.flow.current();
      return {
        staged: batch.staged.length,
        failures: [...batch.failures.values()].map((f) => ({ kind: f.kind, message: f.message })),
        dock: document.querySelector('.pdock')?.innerText ?? '',
        workers: window.__devExport.core.marks.workers,
      };
    });
    await page.evaluate(() => {
      window.__devExport.inkTrapAt = null;
    });
    await page.locator('.pdock [data-act="retry"]').click();
    await waitPhase(page, ['ready']);
    const inkTrapRetry = await page.evaluate(() => {
      const batch = window.__devExport.flow.current();
      return {
        staged: batch.staged.length,
        failures: [...batch.failures.values()].map((f) => ({ kind: f.kind, message: f.message })),
      };
    });
    await shareAll(page);
    await page.locator('.pdock [data-act="done"]').click();
    await page.waitForFunction(() => !document.querySelector('.pdock'), null, { timeout: 15_000 });
    out.steps.inkTrap = { first: inkTrapSnap, retry: inkTrapRetry };
  } finally {
    cleanResidualReports();
    await page.close();
  }
}
