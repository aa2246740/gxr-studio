// UI-M4 第 5 & 第 8 项在无头 Chrome（SwiftShader WebGPU）里：
// - finalSize：对 DNG 和 JPG 在每档预设比例、横竖向和拉直下，核对 Engine.finalSize() 与核心 export_output_sizes 的导出尺寸相同，且配方卡出裁剪小图；
// - parity：设置 A..D、JPG-only、HDR E 的裁剪与拉直（含真拖动 .cbox、拉直尺 rAF 帧记录、重选保留、清空丢弃），导出 原始尺寸/高、小/低 和 HDR E，把 side 与成品写给 Python 和桌面对拍。

import { pathToFileURL } from 'node:url';
import { readFileSync, writeFileSync } from 'node:fs';
import { exportFromGrid, pickAll, shareAll, sharedFiles, waitPhase, cellOf } from './mobile_ui_export_parity.mjs';

const [runtime, url, manifestPath] = process.argv.slice(2);
const manifest = JSON.parse(readFileSync(manifestPath, 'utf8'));
const { launchPinnedChromium, verifyPinnedRuntime } = await import(pathToFileURL(runtime).href);
verifyPinnedRuntime();

const browser = await launchPinnedChromium({
  args: ['--enable-unsafe-webgpu', '--enable-unsafe-swiftshader', '--use-angle=swiftshader', '--use-webgpu-adapter=swiftshader'],
});

const out = { pageErrors: [], steps: {} };

function fakeShare() {
  const scope = window;
  scope.__shareLog = { canShare: 0, calls: [] };
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

async function settle(page) {
  for (let attempt = 0; attempt < 5; attempt += 1) {
    await page.waitForSelector('.app[data-core="1"]', { timeout: 60_000 });
    const reloaded = await page.waitForEvent('framenavigated', { timeout: 4_000 }).then(() => true).catch(() => false);
    if (!reloaded) return;
  }
  throw new Error('page kept reloading');
}

async function newPage() {
  const page = await browser.newPage({ viewport: { width: 390, height: 844 } });
  page.on('pageerror', (error) => out.pageErrors.push(error.message));
  await page.addInitScript(fakeShare);
  await page.goto(url);
  await settle(page);
  return page;
}

async function openPhoto(page, stem) {
  await page.locator(cellOf(stem)).evaluate((node) => node.click());
  await page.waitForFunction((want) => document.querySelector('.editor')?.dataset.photo === want, stem, { timeout: 120_000 });
  await page.waitForFunction((want) => document.querySelector('.app')?.dataset.settled === want, stem, { timeout: 120_000 });
  await page.waitForFunction(() => document.querySelector('.app')?.dataset.frame === '1', null, { timeout: 120_000 });
}

async function backToGrid(page) {
  await page.getByRole('button', { name: '照片', exact: true }).click();
  await page.waitForSelector('[data-screen="grid"]');
}

async function waitGeo(page, prevSeq) {
  await page.waitForFunction((prev) => Number(document.querySelector('.app')?.dataset.geoSeq || '0') > prev, prevSeq, { timeout: 60_000 });
  return Number(await page.evaluate(() => document.querySelector('.app')?.dataset.geoSeq || '0'));
}

async function selectPreset(page, id, orient) {
  const stem = await page.locator('.editor').getAttribute('data-photo');
  const curPreset = await page.evaluate((s) => window.__gxrEdits?.().find((r) => r.stem === s)?.crop?.preset ?? null, stem);
  const needChip = id === 'original' ? curPreset !== null && curPreset !== 'original' : curPreset !== id;
  if (needChip) {
    const sel = `.crop-panel .chips button[data-preset="${id}"]`;
    const chip = page.locator(sel);
    await chip.scrollIntoViewIfNeeded();
    await chip.click();
    await page.waitForFunction(
      ({ s, want }) => {
        const row = window.__gxrEdits?.().find((r) => r.stem === s);
        if (!row) return false;
        return want === 'original' ? row.crop === null || row.crop.preset === 'original' : row.crop?.preset === want;
      },
      { s: stem, want: id },
      { timeout: 30_000 },
    );
  }
  if (orient) {
    const flipBtn = page.locator('.crop-panel [data-act="crop-flip"]');
    if ((await flipBtn.isEnabled()) && (await flipBtn.getAttribute('data-orient')) !== orient) {
      await flipBtn.click();
      await page.waitForFunction(
        (wantOn) => document.querySelector('.crop-panel [data-act="crop-flip"]')?.dataset.orient === wantOn,
        orient,
        { timeout: 30_000 },
      );
    }
  }
}

async function dragStraighten(page, targetDeg, recordRaf = false) {
  const stem = await page.locator('.editor').getAttribute('data-photo');
  const box = await page.locator('.crop-panel .ruler').boundingBox();
  const startX = targetDeg >= 0 ? box.x + box.width - 16 : box.x + 16;
  const startY = box.y + box.height / 2;
  const dx = -targetDeg * 18;
  if (recordRaf) {
    await page.evaluate(() => {
      window.__rafTicks = [];
      window.__rafStop = false;
      const tick = (t) => {
        window.__rafTicks.push(t);
        if (!window.__rafStop) requestAnimationFrame(tick);
      };
      requestAnimationFrame(tick);
    });
  }
  await page.mouse.move(startX, startY);
  await page.mouse.down();
  const dragAttr = await page.evaluate(() => document.querySelector('.app')?.dataset.drag);
  await page.mouse.move(startX + dx, startY, { steps: 12 });
  const duringDragAttr = await page.evaluate(() => document.querySelector('.app')?.dataset.drag);
  await page.mouse.up();
  let rafCount = 0;
  if (recordRaf) {
    rafCount = await page.evaluate(() => {
      window.__rafStop = true;
      return window.__rafTicks.length;
    });
  }
  await page.waitForFunction(
    ({ s, want }) => {
      const row = window.__gxrEdits?.().find((r) => r.stem === s);
      return row && Math.abs((row.geometry?.straighten ?? 0) - want) < 0.05;
    },
    { s: stem, want: targetDeg },
    { timeout: 30_000 },
  );
  const afterDragAttr = await page.evaluate(() => document.querySelector('.app')?.dataset.drag);
  return { dragAttr, duringDragAttr, afterDragAttr, rafCount };
}

async function dragCropBox(page) {
  const stem = await page.locator('.editor').getAttribute('data-photo');
  const beforeResize = await page.evaluate((s) => window.__gxrEdits?.().find((r) => r.stem === s)?.crop ?? null, stem);
  // 1) 拖角缩放（右下角往左上方收进）
  const corner = await page.locator('.cbox .ch.corner.br').boundingBox();
  await page.mouse.move(corner.x + corner.width / 2, corner.y + corner.height / 2);
  await page.mouse.down();
  await page.mouse.move(corner.x - 45, corner.y - 30, { steps: 6 });
  const dragBadge = await page.evaluate(() => {
    const m = /^(\d+)\s*×\s*(\d+)$/.exec(document.querySelector('.cbox .csize')?.textContent?.trim() || '');
    return m ? [Number(m[1]), Number(m[2])] : null;
  });
  await page.mouse.up();
  await page.waitForFunction(
    ({ s, prev }) => {
      const cur = window.__gxrEdits?.().find((r) => r.stem === s)?.crop?.rect;
      return cur && (!prev || cur[2] < prev.rect[2] - 0.01 || cur[3] < prev.rect[3] - 0.01);
    },
    { s: stem, prev: beforeResize },
    { timeout: 30_000 },
  );
  const afterResize = await page.evaluate((s) => window.__gxrEdits?.().find((r) => r.stem === s)?.crop, stem);

  // 2) 横着拖框内部：在裁剪页应该移动框、不能切到下一张照片
  const box = await page.locator('.cbox').boundingBox();
  await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
  await page.mouse.down();
  await page.mouse.move(box.x + box.width / 2 + 24, box.y + box.height / 2 + 12, { steps: 6 });
  await page.mouse.up();
  await page.waitForFunction(
    ({ s, prev }) => {
      const cur = window.__gxrEdits?.().find((r) => r.stem === s)?.crop?.rect;
      return cur && (cur[0] > prev.rect[0] + 0.005 || cur[1] > prev.rect[1] + 0.005);
    },
    { s: stem, prev: afterResize },
    { timeout: 30_000 },
  );
  const currentStem = await page.locator('.editor').getAttribute('data-photo');
  const afterMove = await page.evaluate((s) => window.__gxrEdits?.().find((r) => r.stem === s)?.crop, stem);
  return { stayedSamePhoto: currentStem === stem, dragBadge, afterResize, afterMove };
}

async function batchFacts(page) {
  return page.evaluate(() => {
    const batch = window.__devExport.flow.current();
    const outMap = {};
    for (const item of batch.items) {
      const shot = batch.shots.get(item.id) || {};
      outMap[item.stem] = { side: item.req.side, width: shot.width ?? null, height: shot.height ?? null, result: batch.results.get(item.id) || null, binding: item.req.binding };
    }
    return { facts: outMap };
  });
}

async function runFinalSize(page) {
  await pickAll(page, manifest, manifest.files);
  const results = [];
  for (const stem of manifest.stems) {
    await openPhoto(page, stem);
    await page.locator('.editor .toolbar [data-tool="crop"]').click();
    await page.waitForSelector('.crop-panel');

    const cases = [
      ['original', null, 0],
      ['free', null, 0],
      ['1:1', null, 0],
      ['4:5', 'portrait', 0],
      ['4:5', 'landscape', 0],
      ['3:2', 'landscape', 0],
      ['3:2', 'portrait', 0],
      ['16:9', 'landscape', 0],
      ['16:9', 'portrait', 0],
      ['xpan-65-24', 'landscape', 0],
      ['xpan-65-24', 'portrait', 0],
      ['4:3', 'portrait', 0],
      ['4:3', 'landscape', 0],
      ['xpan-65-24', 'landscape', 2.5],
    ];
    for (const [preset, orient, angle] of cases) {
      await page.locator('.editor .toolbar [data-tool="crop"]').click();
      await page.waitForSelector('.crop-panel');
      if (angle !== 0) await dragStraighten(page, angle, false);
      await selectPreset(page, preset, orient);
      let dragBadge = null;
      if (preset === 'free') {
        const drag = await dragCropBox(page);
        dragBadge = drag.dragBadge;
      }
      await page.waitForFunction(
        () => {
          const b = document.querySelector('.cbox .csize')?.textContent?.trim() || '';
          const l = document.querySelector('.crop-size-line')?.textContent || '';
          const m1 = /^(\d+)\s*×\s*(\d+)$/.exec(b);
          const m2 = /成品\s*(\d+)\s*×\s*(\d+)\s*像素/.exec(l);
          return Boolean(m1 && m2 && m1[1] === m2[1] && m1[2] === m2[2]);
        },
        null,
        { timeout: 30_000 },
      );
      const badgeSize = await page.evaluate(() => {
        const m = /^(\d+)\s*×\s*(\d+)$/.exec(document.querySelector('.cbox .csize')?.textContent?.trim() || '');
        return m ? [Number(m[1]), Number(m[2])] : null;
      });
      const side = await page.evaluate(async (s) => (await window.__gxrRequest(s))?.side ?? null, stem);
      const seq = Number(await page.evaluate(() => document.querySelector('.app')?.dataset.geoSeq || '0'));
      await page.locator('.editor .toolbar [data-tool="adjust"]').click();
      await waitGeo(page, seq);
      const finalSize = await page.evaluate(() => window.__gxrEngine?.finalSize?.() ?? null);
      const layerSize = await page.evaluate(() => window.__gxrEngine?.layerSize?.() ?? null);
      results.push({ stem, preset, orient, angle, side, finalSize, layerSize, badgeSize, dragBadge });
      // reset straighten to 0 before next non-zero test if needed
      if (angle !== 0) {
        await page.locator('.editor .toolbar [data-tool="crop"]').click();
        await page.locator('.crop-panel .ruler').dblclick();
        await page.waitForFunction(
          (s) => Math.abs(window.__gxrEdits?.().find((r) => r.stem === s)?.geometry?.straighten ?? 99) < 0.05,
          stem,
          { timeout: 30_000 },
        );
      }
    }
    if (stem === manifest.stems[0]) {
      // Check recipe card cropped thumbnail marker when XPan is active (RAW photos only; JPG-only uses disabled JPEG preview)
      await page.locator('.editor .toolbar [data-tool="crop"]').click();
      await selectPreset(page, 'xpan-65-24', 'landscape');
      await page.locator('.editor .toolbar [data-tool="recipe"]').click();
      await page.waitForFunction(() => document.querySelectorAll('.strip[data-recipes] .rc .th[data-cropped="1"]').length > 0, null, { timeout: 60_000 });
      out.steps[`croppedCards_${stem}`] = await page.locator('.strip[data-recipes] .rc .th[data-cropped="1"]').count();
    }
    await backToGrid(page);
  }
  out.steps.finalSize = results;
}

async function runParity(page) {
  await page.evaluate(({ binding }) => {
    window.__devExport.binding = binding;
  }, manifest);
  await pickAll(page, manifest, manifest.files);

  const edits = [];
  for (const plan of manifest.plans) {
    await openPhoto(page, plan.stem);
    await page.locator('.editor .toolbar [data-tool="crop"]').click();
    await page.waitForSelector('.crop-panel');
    if (plan.angleDeg) {
      const dragInfo = await dragStraighten(page, plan.angleDeg, !!plan.recordRaf);
      if (plan.recordRaf) out.steps.dragStats = dragInfo;
    }
    if (plan.preset) await selectPreset(page, plan.preset, plan.orient || null);
    if (plan.dragBox) out.steps.freeDrag = await dragCropBox(page);
    const editRow = await page.evaluate((s) => window.__gxrEdits?.().find((r) => r.stem === s) ?? null, plan.stem);
    edits.push(editRow);
    await backToGrid(page);
  }
  out.steps.edits = edits;

  // SDR 原始尺寸 / 高（A..D + JPG-only）
  const sdrStems = manifest.sdrOrder;
  await exportFromGrid(page, sdrStems, '原始尺寸', '高', null, { hdr: false });
  await waitPhase(page, ['ready']);
  const fullFacts = await batchFacts(page);
  await shareAll(page);
  await sharedFiles(page, manifest.outDir, 'full-');
  await page.waitForFunction(() => !document.querySelector('.pdock'), null, { timeout: 10_000 });
  out.steps.full = fullFacts;

  // 小 / 低（A 和 D）
  await page.evaluate(() => window.__devExport.core.stop());
  const c1 = await page.evaluate(() => window.__shareLog.calls.length);
  await exportFromGrid(page, manifest.smallOrder, '小', '低', null, { hdr: false });
  await waitPhase(page, ['ready']);
  const smallFacts = await batchFacts(page);
  await shareAll(page);
  await sharedFiles(page, manifest.outDir, 'small-', c1);
  await page.waitForFunction(() => !document.querySelector('.pdock'), null, { timeout: 10_000 });
  out.steps.small = smallFacts;

  // HDR 原始尺寸 / 高（pair E）
  await page.evaluate(() => window.__devExport.core.stop());
  const c2 = await page.evaluate(() => window.__shareLog.calls.length);
  await exportFromGrid(page, [manifest.hdrStem], '原始尺寸', '高', null, { hdr: true });
  await waitPhase(page, ['ready']);
  const hdrFacts = await batchFacts(page);
  await shareAll(page);
  await sharedFiles(page, manifest.outDir, 'hdr-', c2);
  await page.waitForFunction(() => !document.querySelector('.pdock'), null, { timeout: 10_000 });
  out.steps.hdr = hdrFacts;

  // 重新选同一张照片保留裁剪；清空全部丢掉。
  await page.locator('input.filepick:not(.calibpick)').setInputFiles(manifest.repickPairA);
  await page.waitForFunction(
    (s) => {
      const sub = document.querySelector('.nav small');
      return sub && !sub.textContent.includes('正在读取') && window.__gxrEdits?.().some((r) => r.stem === s);
    },
    manifest.plans[0].stem,
    { timeout: 60_000 },
  );
  const afterRepick = await page.evaluate((s) => window.__gxrEdits?.().find((r) => r.stem === s) ?? null, manifest.plans[0].stem);
  await page.locator('.nav .nb[aria-label="更多"]').click();
  await page.locator('.ms .lrow', { hasText: '清空照片，重新选' }).click();
  await page.waitForSelector('[data-screen="start"]', { timeout: 10_000 });
  out.steps.repickCrop = afterRepick?.crop ?? null;
  out.steps.repickGeom = afterRepick?.geometry ?? null;
  out.steps.afterClearCount = await page.evaluate(() => window.__gxrEdits?.().length ?? -1);
}

try {
  const page = await newPage();
  if (manifest.mode === 'finalSize') await runFinalSize(page);
  else await runParity(page);
  await page.close();
} catch (error) {
  out.error = String(error?.stack ?? error);
} finally {
  await browser.close();
}

writeFileSync(manifest.out, JSON.stringify(out));
console.log(JSON.stringify({ done: true, error: out.error ? out.error.split('\n')[0] : null }));
if (out.error) process.exit(1);
