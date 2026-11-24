// Sol r1 regressions in headless Chrome (SwiftShader WebGPU + real WASM Workers):
// - P2-01: stale async Worker reply on repick, navigation, undo-after-paste, and clear/delete
// - P2-02: ruler pointercancel restores committed state without committing or calling WASM
// - P2-03: RAW without DAT and uncalibrated RAW redraw cropped neutral cards cleanly with empty error banner

import { readFileSync, writeFileSync } from 'node:fs';
import { pathToFileURL } from 'node:url';
import { cellOf, pickAll } from './mobile_ui_export_parity.mjs';

const [runtime, url, manifestPath] = process.argv.slice(2);
const manifest = JSON.parse(readFileSync(manifestPath, 'utf8'));
const { launchPinnedChromium, verifyPinnedRuntime } = await import(pathToFileURL(runtime).href);
verifyPinnedRuntime();

const browser = await launchPinnedChromium({
  args: ['--enable-unsafe-webgpu', '--enable-unsafe-swiftshader', '--use-angle=swiftshader', '--use-webgpu-adapter=swiftshader'],
});

const out = { pageErrors: [], steps: {} };

function installWorkerGate() {
  const heldOps = new Set();
  const heldQueue = [];
  const sentOps = [];
  const origPost = Worker.prototype.postMessage;
  const inflightByWorker = new WeakMap();
  const desc = Object.getOwnPropertyDescriptor(Worker.prototype, 'onmessage');

  Object.defineProperty(Worker.prototype, 'onmessage', {
    configurable: true,
    get() {
      return desc.get.call(this);
    },
    set(fn) {
      if (typeof fn !== 'function') {
        desc.set.call(this, fn);
        return;
      }
      const worker = this;
      desc.set.call(worker, function (ev) {
        const id = ev?.data?.id;
        const map = inflightByWorker.get(worker);
        const op = map?.get(id);
        if (op) map.delete(id);
        if (op && heldOps.has(op)) {
          heldQueue.push({ worker, fn, ev, op });
          return;
        }
        return fn.call(this, ev);
      });
    },
  });

  Worker.prototype.postMessage = function (message, ...rest) {
    if (message && typeof message === 'object' && typeof message.op === 'string' && typeof message.id === 'number') {
      sentOps.push({ op: message.op, id: message.id });
      let map = inflightByWorker.get(this);
      if (!map) {
        map = new Map();
        inflightByWorker.set(this, map);
      }
      map.set(message.id, message.op);
    }
    return origPost.call(this, message, ...rest);
  };

  window.__workerGate = {
    hold(op) {
      heldOps.add(op);
    },
    unhold(op) {
      heldOps.delete(op);
    },
    pendingCount(op) {
      return heldQueue.filter((item) => !op || item.op === op).length;
    },
    sentCount(op) {
      return sentOps.filter((item) => !op || item.op === op).length;
    },
    flush(op) {
      const keep = [];
      const fire = [];
      for (const item of heldQueue) {
        if (!op || item.op === op) fire.push(item);
        else keep.push(item);
      }
      heldQueue.length = 0;
      heldQueue.push(...keep);
      for (const item of fire) item.fn?.call(item.worker, item.ev);
    },
  };
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
  await page.addInitScript(installWorkerGate);
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

async function selectPreset(page, stem, id) {
  const chip = page.locator(`.crop-panel .chips button[data-preset="${id}"]`);
  await chip.scrollIntoViewIfNeeded();
  await chip.click();
  await page.waitForFunction(
    ({ s, want }) => {
      const row = window.__gxrEdits?.().find((r) => r.stem === s);
      return row && row.crop?.preset === want;
    },
    { s: stem, want: id },
    { timeout: 30_000 },
  );
}

async function editRow(page, stem) {
  return page.evaluate((s) => window.__gxrEdits?.().find((r) => r.stem === s) ?? null, stem);
}

async function runStaleAndCancel() {
  const page = await newPage();
  const { stemA, stemB, jpgA, files } = manifest;
  await pickAll(page, manifest, files);

  await page.evaluate(() => {
    window.__geomLog = [];
    const eng = window.__gxrEngine;
    if (eng) {
      const orig = eng.setGeometry.bind(eng);
      eng.setGeometry = (g) => {
        window.__geomLog.push({
          stem: document.querySelector('.editor')?.dataset.photo ?? null,
          straighten: g?.straighten ?? 0,
          box: g?.box ? [...g.box] : null,
        });
        return orig(g);
      };
    }
  });

  // 1) P2-02: Ruler pointercancel must restore angle/crop/rev without calling WASM
  await openPhoto(page, stemA);
  await page.locator('.editor .toolbar [data-tool="crop"]').click();
  await page.waitForSelector('.crop-panel');
  await selectPreset(page, stemA, '16:9');
  const beforeCancel = await editRow(page, stemA);
  const geomCallsBefore = await page.evaluate(() => window.__workerGate.sentCount('geom-geometry-write'));
  const rulerRect = await page.locator('.crop-panel .ruler').boundingBox();
  const rMidX = Math.round(rulerRect.x + rulerRect.width / 2);
  const rMidY = Math.round(rulerRect.y + rulerRect.height / 2);
  const cdp = await page.context().newCDPSession(page);
  await cdp.send('Input.dispatchTouchEvent', {
    type: 'touchStart',
    touchPoints: [{ x: rMidX, y: rMidY, id: 1 }],
  });
  await cdp.send('Input.dispatchTouchEvent', {
    type: 'touchMove',
    touchPoints: [{ x: rMidX - 36, y: rMidY, id: 1 }],
  });
  const duringDrag = await page.evaluate(() => document.querySelector('.app')?.dataset.drag);
  const duringAngle = await page.evaluate(() => document.querySelector('.editor .band-crop-hd .crop-angle')?.textContent?.trim());
  await cdp.send('Input.dispatchTouchEvent', {
    type: 'touchCancel',
    touchPoints: [],
  });
  await page.waitForTimeout(100);
  const afterDrag = await page.evaluate(() => document.querySelector('.app')?.dataset.drag);
  const afterAngle = await page.evaluate(() => document.querySelector('.editor .band-crop-hd .crop-angle')?.textContent?.trim());
  const rulerDragState = { duringDrag, duringAngle, afterDrag, afterAngle };
  const afterCancel = await editRow(page, stemA);
  const geomCallsAfter = await page.evaluate(() => window.__workerGate.sentCount('geom-geometry-write'));
  out.steps.rulerCancel = {
    ...rulerDragState,
    beforeRev: beforeCancel.rev,
    afterRev: afterCancel.rev,
    beforeStraighten: beforeCancel.geometry.straighten,
    afterStraighten: afterCancel.geometry.straighten,
    sameCrop: JSON.stringify(beforeCancel.crop) === JSON.stringify(afterCancel.crop),
    wasmCallsDelta: geomCallsAfter - geomCallsBefore,
  };

  // Straighten 2.0° normally so rev becomes 2
  const rulerBox = await page.locator('.crop-panel .ruler').boundingBox();
  const rcx = rulerBox.x + rulerBox.width / 2;
  const rcy = rulerBox.y + rulerBox.height / 2;
  await page.mouse.move(rcx, rcy);
  await page.mouse.down();
  await page.mouse.move(rcx - 36, rcy, { steps: 6 });
  await page.mouse.up();
  await page.waitForFunction((s) => Math.abs((window.__gxrEdits?.().find((r) => r.stem === s)?.geometry?.straighten ?? 0) - 2) < 0.05, stemA, { timeout: 30_000 });

  // 2) P2-01 (repick): Hold real 1:1 WASM reply, go back, re-pick same JPG, flush delayed reply
  await page.evaluate(() => window.__workerGate.hold('geom-crop-write'));
  await page.locator('.crop-panel .chips button[data-preset="1:1"]').click();
  await page.waitForFunction(() => window.__workerGate.pendingCount('geom-crop-write') === 1, null, { timeout: 10_000 });
  await backToGrid(page);
  await page.locator('input.filepick:not(.calibpick)').setInputFiles([jpgA]);
  await page.waitForFunction(
    (s) => {
      const sub = document.querySelector('.nav small');
      return sub && !sub.textContent.includes('正在读取') && window.__gxrEdits?.().some((r) => r.stem === s);
    },
    stemA,
    { timeout: 30_000 },
  );
  await page.evaluate(() => {
    window.__workerGate.unhold('geom-crop-write');
    window.__workerGate.flush('geom-crop-write');
  });
  await page.waitForTimeout(200);
  out.steps.staleRepick = await editRow(page, stemA);

  // 3) P2-01 (navigation): Hold ArrowRight 0.1° straighten reply on A, open B, flush A's reply
  await openPhoto(page, stemA);
  await page.locator('.editor .toolbar [data-tool="crop"]').click();
  await page.waitForSelector('.crop-panel');
  await page.evaluate(() => window.__workerGate.hold('geom-geometry-write'));
  await page.locator('.crop-panel .ruler').focus();
  await page.keyboard.press('ArrowRight');
  await page.waitForFunction(() => window.__workerGate.pendingCount('geom-geometry-write') === 1, null, { timeout: 10_000 });
  await backToGrid(page);
  await openPhoto(page, stemB);
  const logStartB = await page.evaluate(() => window.__geomLog.length);
  await page.evaluate(() => {
    window.__workerGate.unhold('geom-geometry-write');
    window.__workerGate.flush('geom-geometry-write');
  });
  await page.waitForTimeout(250);
  const logAfterFlushB = await page.evaluate((from) => window.__geomLog.slice(from), logStartB);
  out.steps.staleNavigation = {
    editB: await editRow(page, stemB),
    engineCallsDuringB: logAfterFlushB,
  };

  // 4) P2-01 (undo after paste): Copy A (16:9, 2°), apply to B, start 1:1 on B (held), undo paste on grid, flush 1:1
  await backToGrid(page);
  await openPhoto(page, stemA);
  await page.locator('.editor .nav [data-act="editor-more"]').click();
  await page.locator('.ms[data-state="editor-more"] [data-act="more-copy"]').click();
  await page.waitForSelector('.paste-sheet[data-state="copy-options"]');
  for (const grp of ['geometry', 'crop']) {
    const row = page.locator(`.paste-sheet .optrow[data-group="${grp}"]`);
    if (!(await row.evaluate((n) => n.classList.contains('on')))) await row.click();
  }
  await page.locator('.paste-sheet .btn.primary').click();
  await page.waitForSelector('.paste-sheet[data-state="copied-where"]');
  await page.locator('.paste-sheet [data-act="apply-group"]').click();
  await page.waitForSelector('.paste-dlg[data-state="confirm"]');
  await page.locator('.paste-dlg [data-act="confirm"]').click();
  await page.waitForSelector('.pastebar[data-state="applied"]', { timeout: 30_000 });

  await openPhoto(page, stemB);
  await page.locator('.editor .toolbar [data-tool="crop"]').click();
  await page.waitForSelector('.crop-panel');
  await page.evaluate(() => window.__workerGate.hold('geom-crop-write'));
  await page.locator('.crop-panel .chips button[data-preset="1:1"]').click();
  await page.waitForFunction(() => window.__workerGate.pendingCount('geom-crop-write') === 1, null, { timeout: 10_000 });
  await backToGrid(page);
  await page.locator('.pastebar [data-act="undo"]').click();
  await page.waitForSelector('.pastebar[data-state="info"]', { timeout: 10_000 });
  await page.evaluate(() => {
    window.__workerGate.unhold('geom-crop-write');
    window.__workerGate.flush('geom-crop-write');
  });
  await page.waitForTimeout(200);
  out.steps.staleUndoAfterPaste = await editRow(page, stemB);

  // 5) P2-01 (clear/delete): Hold 4:5 on B, go back & clear all photos, flush delayed reply
  await openPhoto(page, stemB);
  await page.locator('.editor .toolbar [data-tool="crop"]').click();
  await page.waitForSelector('.crop-panel');
  await page.evaluate(() => window.__workerGate.hold('geom-crop-write'));
  await page.locator('.crop-panel .chips button[data-preset="4:5"]').click();
  await page.waitForFunction(() => window.__workerGate.pendingCount('geom-crop-write') === 1, null, { timeout: 10_000 });
  await backToGrid(page);
  await page.locator('.nav .nb[aria-label="更多"]').click();
  await page.locator('.ms .lrow', { hasText: '清空照片，重新选' }).click();
  await page.waitForSelector('[data-screen="start"]', { timeout: 10_000 });
  await page.evaluate(() => {
    window.__workerGate.unhold('geom-crop-write');
    window.__workerGate.flush('geom-crop-write');
  });
  await page.waitForTimeout(200);
  out.steps.staleClearCount = await page.evaluate(() => window.__gxrEdits?.().length ?? -1);

  await page.close();
}

async function checkRawNeutralCropCase(files, wantState, stem) {
  const page = await newPage();
  await pickAll(page, manifest, files);
  await openPhoto(page, stem);
  await page.waitForFunction(
    (s) => document.querySelector('.strip[data-recipes]')?.dataset.state === s,
    wantState,
    { timeout: 60_000 },
  );
  // Wait for initial 192x128 neutral card canvases to render (at least all visible cards)
  await page.waitForFunction(() => document.querySelectorAll('.strip[data-recipes] .rc .th canvas').length >= 8, null, { timeout: 60_000 });
  const beforeCard = await page.evaluate(() => {
    const c = document.querySelector('.strip[data-recipes] .rc .th canvas');
    return c ? { w: c.width, h: c.height } : null;
  });

  await page.locator('.editor .toolbar [data-tool="crop"]').click();
  await page.waitForSelector('.crop-panel');
  await selectPreset(page, stem, 'xpan-65-24');

  // Wait for new XPan cropped neutral cards (landscape width > 2 * height) to redraw cleanly
  await page.waitForFunction(
    () => {
      const canvases = [...document.querySelectorAll('.strip[data-recipes] .rc .th[data-cropped="1"] canvas')];
      return canvases.length >= 8 && canvases.every((c) => c.width > c.height * 2);
    },
    null,
    { timeout: 60_000 },
  );
  await page.waitForTimeout(150);

  const errHidden = await page.locator('.crop-panel .crop-err').evaluate((n) => n.hidden);
  const errText = (await page.locator('.crop-panel .crop-err').textContent())?.trim() ?? '';
  const edit = await editRow(page, stem);
  const afterCard = await page.evaluate(() => {
    const c = document.querySelector('.strip[data-recipes] .rc .th canvas');
    return c ? { w: c.width, h: c.height } : null;
  });
  await page.close();
  return {
    state: wantState,
    errHidden,
    errText,
    rev: edit?.rev ?? null,
    preset: edit?.crop?.preset ?? null,
    beforeCard,
    afterCard,
  };
}

async function runRawClosedBitmap() {
  const { dng, jpg, dat, stem } = manifest;
  // 无相机配方文件时 RAW+JPG 默认显示相机 JPG；纯 RAW 仍走中性转换小图（验证 P2-03 裁切重画）。
  out.steps.noDat = await checkRawNeutralCropCase([dng], 'no-recipe-file', stem);
  out.steps.uncalibrated = await checkRawNeutralCropCase([dng, jpg, dat], 'uncalibrated', stem);
}

async function runNeutralReopen() {
  const { dng, jpgB, dngC, stemA, stemB } = manifest;
  const page = await newPage();
  await page.evaluate(async () => {
    const { EditorView } = await import('/src/app/editor.ts');
    const origOpen = EditorView.prototype.open;
    EditorView.prototype.open = function (...args) {
      window.__editor = this;
      return origOpen.apply(this, args);
    };
  });
  await pickAll(page, manifest, [dng, jpgB]);
  await openPhoto(page, stemA);
  await page.waitForFunction(() => document.querySelector('.strip[data-recipes]')?.dataset.state === 'no-recipe-file', null, { timeout: 60_000 });
  await page.waitForFunction(() => document.querySelectorAll('.strip[data-recipes] .rc .th canvas').length >= 8, null, { timeout: 60_000 });

  await page.evaluate(() => {
    const engine = window.__gxrEngine;
    const origThumb = engine.thumb.bind(engine);
    let holdOnce = true;
    window.__releaseNeutralThumb = null;
    window.__neutralThumbHeld = false;
    engine.thumb = async (...args) => {
      const reply = await origThumb(...args);
      if (holdOnce) {
        holdOnce = false;
        window.__neutralThumbHeld = true;
        await new Promise((resolve) => {
          window.__releaseNeutralThumb = resolve;
        });
      }
      return reply;
    };
  });

  await page.locator('.editor .toolbar [data-tool="crop"]').click();
  await page.waitForSelector('.crop-panel');
  const xpan = page.locator('.crop-panel .chips button[data-preset="xpan-65-24"]');
  await xpan.scrollIntoViewIfNeeded();
  await xpan.click();
  await page.waitForFunction(
    (s) => {
      const row = window.__gxrEdits?.().find((r) => r.stem === s);
      return row?.rev === 1 && row?.crop?.preset === 'xpan-65-24' && window.__neutralThumbHeld === true;
    },
    stemA,
    { timeout: 60_000 },
  );

  // A -> B -> A with same version & rev while first EditorView's Engine.thumb reply is held
  await backToGrid(page);
  await openPhoto(page, stemB);
  await backToGrid(page);
  await openPhoto(page, stemA);
  await page.waitForFunction(
    () => {
      const canvases = [...document.querySelectorAll('.strip[data-recipes] .rc .th[data-cropped="1"] canvas')];
      return canvases.length >= 8 && canvases.every((c) => c.width > c.height * 2);
    },
    null,
    { timeout: 60_000 },
  );

  const before = await page.evaluate(() => {
    const ed = window.__editor;
    window.__freshNeutral = ed.photo.neutral;
    const row = {
      freshWidth: window.__freshNeutral?.bitmap?.width ?? 0,
      rev: ed.photo.rev,
      version: ed.photo.version,
      session: ed.api.session(),
      cards: document.querySelectorAll('.strip[data-recipes] .rc .th canvas').length,
    };
    window.__releaseNeutralThumb?.();
    return row;
  });
  await page.waitForTimeout(350);
  const after = await page.evaluate(() => {
    const ed = window.__editor;
    const p = ed.photo;
    return {
      sameNeutral: p.neutral === window.__freshNeutral,
      freshWidth: window.__freshNeutral?.bitmap?.width ?? 0,
      liveWidth: p.neutral?.bitmap?.width ?? 0,
      viewMatchesModel: ed.recipes?.neutralBitmap === p.neutral?.bitmap,
      rev: p.rev,
      version: p.version,
      session: ed.api.session(),
    };
  });

  // Import real DAT and confirm neutral cards remain visible across refresh
  await page.locator('input.filepick:not(.calibpick)').setInputFiles([manifest.dat]);
  await page.waitForFunction(() => document.querySelector('.strip[data-recipes]')?.dataset.state === 'uncalibrated', null, { timeout: 60_000 });
  await page.waitForTimeout(200);
  const refresh = await page.evaluate(() => ({
    cards: document.querySelectorAll('.strip[data-recipes] .rc .th canvas').length,
    slots: document.querySelectorAll('.strip[data-recipes] .rc .th').length,
    cropErr: document.querySelector('.crop-err:not([hidden])')?.textContent?.trim() ?? null,
  }));

  // Also test stale load() continuation after showRaw() does not call prefetch() once EditorView advances
  await backToGrid(page);
  await pickAll(page, { count: 3 }, [dngC]);
  await openPhoto(page, stemB);
  const prefetchGuard = await page.evaluate(async ({ sB, sC }) => {
    const { CropStage } = await import('/src/app/crop_stage.ts');
    const ed = window.__editor;
    const photos = ed.api.photos();
    const photoC = photos.find((p) => p.stem === sC);
    const prevIdx = photos.findIndex((p, i) => i < photos.length - 1 && p.stem !== sC && Boolean(photos[i + 1].dng));
    const prevPhoto = prevIdx >= 0 ? photos[prevIdx] : photos.find((p) => p.stem === sB);
    const prevNext = photos[photos.indexOf(prevPhoto) + 1];
    const readFullCalls = [];
    const origReadFull = ed.api.readFull.bind(ed.api);
    ed.api.readFull = (p) => {
      readFullCalls.push(p.stem);
      return origReadFull(p);
    };
    const origApply = CropStage.prototype.applyPreviewGeometry;
    let releaseBox = null;
    let holdOnce = true;
    CropStage.prototype.applyPreviewGeometry = function (...args) {
      if (holdOnce) {
        holdOnce = false;
        return new Promise((resolve) => {
          releaseBox = () => {
            releaseBox = null;
            resolve(origApply.apply(this, args));
          };
        });
      }
      return origApply.apply(this, args);
    };
    try {
      ed.open(photoC);
      const loadC = ed.settled;
      for (let i = 0; i < 400 && !releaseBox; i += 1) await new Promise((r) => setTimeout(r, 25));
      if (!releaseBox) throw new Error('applyPreviewGeometry was not reached during load(photoC)');
      const holdRelease = releaseBox;
      ed.open(prevPhoto);
      await ed.settled;
      const countAfterReopen = readFullCalls.length;
      holdRelease();
      await loadC;
      await new Promise((r) => setTimeout(r, 50));
      return {
        staleCalls: readFullCalls.slice(countAfterReopen),
        hadNextRaw: Boolean(prevNext?.dng),
      };
    } finally {
      CropStage.prototype.applyPreviewGeometry = origApply;
    }
  }, { sB: stemB, sC: dngC.replace(/^.*\//, '').replace(/\.[^.]+$/, '') });

  await page.close();
  out.steps.neutralReopen = { before, after, refresh, prefetchGuard };
}

try {
  if (manifest.mode === 'staleAndCancel') await runStaleAndCancel();
  else if (manifest.mode === 'rawClosedBitmap') await runRawClosedBitmap();
  else if (manifest.mode === 'neutralReopen') await runNeutralReopen();
  else throw new Error(`unknown regression mode: ${manifest.mode}`);
} catch (error) {
  out.error = String(error?.stack ?? error);
} finally {
  await browser.close();
}

writeFileSync(manifest.out, JSON.stringify(out));
console.log(JSON.stringify({ done: true, error: out.error ? out.error.split('\n').slice(0, 2).join(' | ') : null }));
if (out.error) process.exit(1);
