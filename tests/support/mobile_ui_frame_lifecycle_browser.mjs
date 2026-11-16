// UI-M4 生命周期自检清单（Items 1–6）的 Playwright 浏览器端到端回归测试。
// 在 Vite 页面中直接导入真实模块与真实 Web Worker / IndexedDB / Canvas / EventTarget。

import { pathToFileURL } from 'node:url';
import { readFileSync } from 'node:fs';

const [runtime, baseUrl, manifestPath] = process.argv.slice(2);
const manifest = JSON.parse(readFileSync(manifestPath, 'utf8'));
const logoBytes = Array.from(readFileSync(manifest.logoPng));
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

const out = { pageErrors: [] };

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

  out.results = await page.evaluate(async ({ logoBytesArr }) => {
    const { StageDecorController, buildBackdropCanvas } = await import('/src/app/stage_finish.ts');
    const { EditorView } = await import('/src/app/editor.ts');
    const { Book } = await import('/src/app/book.ts');
    const { FrameView } = await import('/src/app/frame_view.ts');
    const { openLogoSheet } = await import('/src/app/logo_sheet.ts');
    const { openFrameMoreSheet } = await import('/src/app/frame_more_sheet.ts');
    const { openStampSheet } = await import('/src/app/stamp_sheet.ts');
    const { logoStore } = await import('/src/app/logo_store.ts');
    const finishDrawMod = await import('/src/app/finish_draw.ts');
    const releaseFinishDrawResources = finishDrawMod.releaseFinishDrawResources ?? (() => {});
    const { primeBlurFrames } = await import('/src/app/finish_bg.ts');
    const { ensureFontsLoaded, loadFontFace } = await import('/src/app/font_loader.ts');
    const { ensureFrameMeta, resolveStageDeco } = await import('/src/app/frame_state.ts');
    const sheetLifecycle = await import('/src/app/sheet_lifecycle.ts').catch(() => ({
      cancelHostSheets: () => {},
    }));
    const { cancelHostSheets } = sheetLifecycle;
    const { DEFAULT_FRAME, DEFAULT_STAMP } = await import('/src/lib/frame_spec.ts');
    const { readFrameMeta, prepareLogo, computeFrameLayout } = await import('/src/core/api.ts');

    const fail = (msg) => {
      throw new Error(msg);
    };
    const assert = (cond, msg) => {
      if (!cond) fail(msg);
    };
    const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

    async function makeTestBitmap(w = 160, h = 120) {
      const cv = document.createElement('canvas');
      cv.width = w;
      cv.height = h;
      const ctx = cv.getContext('2d');
      ctx.fillStyle = '#336699';
      ctx.fillRect(0, 0, w, h);
      return createImageBitmap(cv);
    }

    function makePhoto(id, bitmap, borderPatch = {}, stampPatch = {}) {
      return {
        id,
        stem: id.toUpperCase(),
        version: 1,
        dng: null,
        jpg: null,
        lastModified: 1700000000000,
        captureMs: null,
        status: 'ready',
        gone: false,
        cached: null,
        checked: 1,
        bitmap,
        neutral: null,
        knobs: {
          exposure_ev: 0,
          contrast: 0,
          highlight: 0,
          shadow: 0,
          black_point: 0,
          warm: 0,
          tint: 0,
          saturation: 0,
          vibrance: 0,
          vignette: 0,
        },
        rev: 0,
        jpgMeta: null,
        dngMeta: null,
        border: { ...DEFAULT_FRAME, ...borderPatch },
        stamp: { ...DEFAULT_STAMP, ...stampPatch },
        frameMeta: null,
        choice: null,
        levels: new Map(),
        memory: new Map(),
        looks: new Map(),
      };
    }

    const res = {};

    // =========================================================================
    // CHECKLIST #1: Late Async Results
    // =========================================================================
    {
      const bmp = await makeTestBitmap();
      const photo = makePhoto('p1', bmp, { style: 'blur_fill' });
      const stage = document.createElement('div');
      stage.style.width = '360px';
      stage.style.height = '480px';
      stage.getBoundingClientRect = () => ({ width: 360, height: 480, top: 0, left: 0, right: 360, bottom: 480, x: 0, y: 0, toJSON() {} });
      const pv = document.createElement('div');
      const gpuCanvas = document.createElement('canvas');
      const still = document.createElement('canvas');
      still.width = 160;
      still.height = 120;
      pv.append(gpuCanvas, still);
      stage.append(pv);
      document.body.append(stage);

      let thumbResolve1 = null;
      let thumbResolve2 = null;
      let callIdx = 0;
      let backdropEvents = 0;
      const engine = {
        contentVersion: 1,
        finalSize: () => ({ w: 4000, h: 3000 }),
        thumb: () =>
          new Promise((resolve) => {
            callIdx += 1;
            if (callIdx === 1) thumbResolve1 = resolve;
            else thumbResolve2 = resolve;
          }),
      };

      const decor = new StageDecorController({
        pv,
        stage,
        photo,
        engine,
        gpuCanvas,
        still,
        mode: () => 'raw',
        frameTab: () => true,
        onBackdrop: () => {
          backdropEvents += 1;
        },
      });

      // Kick off first thumb via onMediaReady
      decor.onMediaReady();
      await sleep(10);
      assert(typeof thumbResolve1 === 'function', 'thumb 1 should be in-flight');

      // Change look before thumb 1 resolves -> invalidates backdropGen
      decor.onLookChanged(false);
      // Resolve stale thumb 1 with distinctive red pixels
      thumbResolve1({ w: 4, h: 4, data: new Uint8ClampedArray(4 * 4 * 4).fill(211) });
      await sleep(20);
      const staleBackdrop = decor.backdrop();
      // Stale thumb 1 must NOT be stored as gpuBackdrop (backdrop() should fall back or wait for thumb 2)
      assert(staleBackdrop === null || staleBackdrop.canvas.width !== 4, 'stale gpuBackdrop must not overwrite after onLookChanged');
      assert(backdropEvents === 0, 'onBackdrop must not fire for invalidated thumb');

      // Bump photo.version while thumb 2 is in flight -> thumb 2 also must be dropped
      photo.version += 1;
      await sleep(10);
      if (thumbResolve2) {
        thumbResolve2({ w: 6, h: 6, data: new Uint8ClampedArray(6 * 6 * 4).fill(99) });
        await sleep(20);
      }
      assert(backdropEvents === 0, 'thumb resolving after photo.version bump must be ignored');

      // FrameView tabClosed() invalidates in-flight card layout computation
      const shell = document.createElement('div');
      document.body.append(shell);
      const fvPhoto = makePhoto('fv1', bmp, { style: 'leica' });
      let aliveFlag = true;
      const fv = new FrameView(shell, fvPhoto, {
        ew: () => [4000, 3000],
        backdrop: () => still,
        onChange: () => {},
        isAlive: () => aliveFlag,
      });
      fv.tabOpened();
      fv.tabClosed();
      await sleep(200);
      const paintedCardsAfterClose = shell.querySelectorAll('.panel.frame .rc .th.fr canvas').length;
      const inRootCards = fv.panelEl().querySelectorAll('.rc .th.fr canvas').length;
      assert(inRootCards === 0 && paintedCardsAfterClose === 0, `cards must not paint after tabClosed: got ${inRootCards}`);

      // ensureFrameMeta & resolveStageDeco drop late replies when photo.version changes
      const metaPhoto = makePhoto('meta1', bmp, { style: 'leica' });
      const metaProm = ensureFrameMeta(metaPhoto);
      metaPhoto.version += 1;
      await metaProm;
      assert(metaPhoto.frameMeta === null, 'late frameMeta must not mutate bumped photo.version');

      const decoPhoto = makePhoto('deco1', bmp, { style: 'leica' });
      let decoAlive = true;
      const decoProm = resolveStageDeco(decoPhoto, null, () => {}, undefined, () => decoAlive);
      decoAlive = false;
      const staleDeco = await decoProm;
      assert(staleDeco === null, 'resolveStageDeco must return null when !isAlive after await');

      fv.destroy();
      decor.destroy();
      stage.remove();
      shell.remove();
      bmp.close();
      res.lateAsyncOk = true;
    }

    // =========================================================================
    // CHECKLIST #1 (LogoSheet re-pick) & #2 (Cancel paths) & #5 (Atomic IDB)
    // =========================================================================
    {
      const shell = document.createElement('div');
      document.body.append(shell);
      const bmp = await makeTestBitmap();
      const photo = makePhoto('ls1', bmp, { style: 'leica' });
      const logoFile = new File([new Uint8Array(logoBytesArr)], 'MyBrand.png', { type: 'image/png' });
      const badFile = new File([new Uint8Array([1, 2, 3, 4])], 'CorruptFirst.png', { type: 'image/png' });

      // Seed one logo cleanly so we can test rename/delete cancel & failure paths
      await logoStore.ensureLoaded();
      const seedRes = await logoStore.add(logoFile, { name: 'SeedLogo', keyWhite: true, tol: 30 });
      assert(seedRes.ok, 'seed logo save should succeed');
      const seededId = seedRes.logo.id;
      photo.border = { ...photo.border, logo: seededId, show_logo: true };
      const revBaseline = photo.rev;

      // (a) pointercancel on logo row must cancel 480ms long-press timer
      let onUpdateCalls = 0;
      openLogoSheet(shell, photo, () => {
        onUpdateCalls += 1;
      });
      await sleep(30);
      const opt = shell.querySelector(`.ms.logo-sheet .logo-opt[data-logo="${seededId}"]`);
      assert(opt, 'seeded logo row option should exist');
      opt.dispatchEvent(new PointerEvent('pointerdown', { bubbles: true, pointerId: 1 }));
      await sleep(30);
      opt.dispatchEvent(new PointerEvent('pointercancel', { bubbles: true, pointerId: 1 }));
      await sleep(530);
      assert(!shell.querySelector('.ms.logo-sheet .logo-row-acts'), 'pointercancel must not open long-press menu');

      // (b) Re-pick race in LogoSheet upload view: corrupt first file vs valid second file
      shell.querySelector('.ms.logo-sheet [data-logo-action="upload"]').click();
      await sleep(10);
      const fileInput = shell.querySelector('.ms.logo-sheet input.filepick');
      // Assign corrupt file and immediately overwrite with valid file synchronously/before resolve
      const dt1 = new DataTransfer();
      dt1.items.add(badFile);
      fileInput.files = dt1.files;
      fileInput.dispatchEvent(new Event('change'));
      const dt2 = new DataTransfer();
      dt2.items.add(logoFile);
      fileInput.files = dt2.files;
      fileInput.dispatchEvent(new Event('change'));
      let saveBtn = null;
      for (let i = 0; i < 100; i += 1) {
        saveBtn = shell.querySelector('.ms.logo-sheet [data-logo-action="save"]');
        if (saveBtn && !saveBtn.disabled) break;
        await sleep(50);
      }
      await sleep(80);
      const ferr = shell.querySelector('.ms.logo-sheet .ferr');
      const errVisible = Boolean(ferr && !ferr.hidden);
      assert(!errVisible && saveBtn && !saveBtn.disabled, 're-picked valid file must not be clobbered by stale badFile preview');

      // (c) Failed/Aborted IDB write in LogoSheet add, rename, delete:
      // Monkey-patch IDBObjectStore.prototype.put and delete temporarily to throw AbortError via tx abort
      const origPut = IDBObjectStore.prototype.put;
      const origDelete = IDBObjectStore.prototype.delete;
      const countBeforeAbort = logoStore.list().length;
      const storeRevBeforeAbort = logoStore.revision();

      IDBObjectStore.prototype.put = function () {
        this.transaction.abort();
        throw new DOMException('Aborted write', 'AbortError');
      };
      try {
        saveBtn.click();
        await sleep(150);
        // Must preserve state & re-enable saveBtn!
        assert(logoStore.list().length === countBeforeAbort, 'failed add must not add logo in memory');
        assert(logoStore.revision() === storeRevBeforeAbort, 'failed add must not bump store revision');
        assert(photo.border.logo === seededId && photo.rev === revBaseline, 'failed add must not change photo');
        assert(saveBtn.disabled === false, 'saveBtn must be re-enabled after AbortError/failed IDB write');
      } finally {
        IDBObjectStore.prototype.put = origPut;
      }

      // Close upload view, test rename & delete IDB abort recovery in list view
      cancelHostSheets(shell);
      openLogoSheet(shell, photo, () => {
        onUpdateCalls += 1;
      });
      await sleep(30);

      // Failed rename
      shell.querySelector('.ms.logo-sheet .logo-row button[aria-label*="更多操作"]').click();
      shell.querySelector('.ms.logo-sheet .logo-row-acts button').click(); // 重命名
      const renameInput = shell.querySelector('.ms.logo-sheet .logo-row-acts input.txtbox');
      renameInput.value = 'ShouldRollback';
      const renameSaveBtn = [...shell.querySelectorAll('.ms.logo-sheet .logo-row-acts button')].find((b) => b.textContent === '保存');
      IDBObjectStore.prototype.put = function () {
        this.transaction.abort();
        throw new DOMException('Aborted rename', 'AbortError');
      };
      try {
        renameSaveBtn.click();
        await sleep(100);
        assert(logoStore.info(seededId)?.name === 'SeedLogo', 'failed rename must keep original name');
      } finally {
        IDBObjectStore.prototype.put = origPut;
      }
      // Verify controls NOT stuck busy after error: try real rename now -> succeeds!
      renameInput.value = 'RecoveredName';
      renameSaveBtn.click();
      await sleep(100);
      assert(logoStore.info(seededId)?.name === 'RecoveredName', 'busy lock must reset after failed rename');

      // Failed delete
      shell.querySelector('.ms.logo-sheet .logo-row button[aria-label*="更多操作"]').click();
      [...shell.querySelectorAll('.ms.logo-sheet .logo-row-acts button')].find((b) => b.textContent === '删除').click();
      const delConfirmBtn = [...shell.querySelectorAll('.ms.logo-sheet .logo-row-acts button.danger')].find((b) => b.textContent === '删除');
      IDBObjectStore.prototype.delete = function () {
        this.transaction.abort();
        throw new DOMException('Aborted delete', 'AbortError');
      };
      try {
        delConfirmBtn.click();
        await sleep(100);
        assert(Boolean(logoStore.info(seededId)), 'failed delete must preserve logo in store');
        assert(photo.border.logo === seededId, 'failed delete must not reset photo.border.logo to ricoh');
        assert(photo.rev === revBaseline, 'failed delete must not bump photo.rev');
      } finally {
        IDBObjectStore.prototype.delete = origDelete;
      }
      // Clean up seeded logo for real
      delConfirmBtn.click();
      await sleep(100);
      assert(!logoStore.info(seededId), 'subsequent delete after error must work cleanly');
      cancelHostSheets(shell);

      // (d) frame_more_sheet & stamp_sheet external cancel clears debounce timers & restores text
      photo.border = { ...photo.border, style: 'leica', text: 'ORIG_TEXT' };
      photo.stamp = { ...photo.stamp, enabled: true, content: 'date+text', text: 'ORIG_STAMP' };
      const committedRev = photo.rev;
      let moreUpdates = 0;
      openFrameMoreSheet(shell, photo, () => {
        moreUpdates += 1;
      });
      const moreInput = shell.querySelector('.ms.frame-more-sheet input.txtbox');
      moreInput.value = 'UNCOMMITTED';
      moreInput.dispatchEvent(new Event('input'));
      // Cancel externally before 140ms debounce fires
      cancelHostSheets(shell);
      await sleep(220);
      assert(moreUpdates === 0, 'cancelled frame_more_sheet must clear debounce timer');
      assert(photo.border.text === 'ORIG_TEXT', `cancelled frame_more_sheet must restore initial text, got ${photo.border.text}`);
      assert(photo.rev === committedRev, 'cancelled frame_more_sheet must not bump photo.rev');
      assert(!shell.querySelector('.ms.frame-more-sheet'), 'frame_more_sheet must be removed on cancel');

      let stampUpdates = 0;
      openStampSheet(shell, photo, () => {
        stampUpdates += 1;
      });
      const stampInput = shell.querySelector('.ms.stamp-sheet input.txtbox');
      stampInput.value = 'UNCOMMITTED_ST';
      stampInput.dispatchEvent(new Event('input'));
      cancelHostSheets(shell);
      await sleep(220);
      assert(stampUpdates === 0, 'cancelled stamp_sheet must clear debounce timer');
      assert(photo.stamp.text === 'ORIG_STAMP', `cancelled stamp_sheet must restore initial text, got ${photo.stamp.text}`);
      assert(photo.rev === committedRev, 'cancelled stamp_sheet must not bump photo.rev');
      assert(!shell.querySelector('.ms.stamp-sheet'), 'stamp_sheet must be removed on cancel');

      shell.remove();
      bmp.close();
      res.cancelAndIdbOk = true;
    }

    // =========================================================================
    // CHECKLIST #3: Resources Freed (timers, rAF, idle, canvases, closed ImageBitmap)
    // =========================================================================
    {
      const activeTimers = new Set();
      const activeRafs = new Set();
      const activeIdles = new Set();
      const origSetTimeout = window.setTimeout.bind(window);
      const origClearTimeout = window.clearTimeout.bind(window);
      const origRaf = window.requestAnimationFrame.bind(window);
      const origCaf = window.cancelAnimationFrame.bind(window);
      const origIdle = window.requestIdleCallback?.bind(window);
      const origCancelIdle = window.cancelIdleCallback?.bind(window);

      window.setTimeout = (fn, ms, ...rest) => {
        const id = origSetTimeout((...args) => {
          activeTimers.delete(id);
          fn(...args);
        }, ms, ...rest);
        activeTimers.add(id);
        return id;
      };
      window.clearTimeout = (id) => {
        activeTimers.delete(id);
        origClearTimeout(id);
      };
      window.requestAnimationFrame = (fn) => {
        const id = origRaf((ts) => {
          activeRafs.delete(id);
          fn(ts);
        });
        activeRafs.add(id);
        return id;
      };
      window.cancelAnimationFrame = (id) => {
        activeRafs.delete(id);
        origCaf(id);
      };
      if (origIdle && origCancelIdle) {
        window.requestIdleCallback = (fn, opts) => {
          const id = origIdle((dl) => {
            activeIdles.delete(id);
            fn(dl);
          }, opts);
          activeIdles.add(id);
          return id;
        };
        window.cancelIdleCallback = (id) => {
          activeIdles.delete(id);
          origCancelIdle(id);
        };
      }

      try {
        const bmp = await makeTestBitmap(160, 120);
        const photo = makePhoto('res1', bmp, { style: 'blur_fill', text: '' }, { enabled: true });
        const stage = document.createElement('div');
        stage.style.width = '360px';
        stage.style.height = '480px';
        stage.getBoundingClientRect = () => ({ width: 360, height: 480, top: 0, left: 0, right: 360, bottom: 480, x: 0, y: 0, toJSON() {} });
        const pv = document.createElement('div');
        const still = document.createElement('canvas');
        still.width = 160;
        still.height = 120;
        pv.append(still);
        stage.append(pv);
        const shell = document.createElement('div');
        document.body.append(stage, shell);

        const decor = new StageDecorController({
          pv,
          stage,
          photo,
          engine: null,
          gpuCanvas: null,
          still,
          mode: () => 'still',
        });
        const fv = new FrameView(shell, photo, {
          ew: () => [4000, 3000],
          backdrop: () => decor.backdropCanvas(),
          onChange: () => void decor.sync(),
        });
        shell.append(fv.panelEl());
        await decor.sync();
        fv.tabOpened();
        // Open an uncommitted sheet & trigger a warm blur prime
        openStampSheet(shell, photo, () => {});
        const inp = shell.querySelector('.ms.stamp-sheet input.txtbox');
        if (inp) {
          inp.value = 'TEMP';
          inp.dispatchEvent(new Event('input'));
        }
        primeBlurFrames(
          still,
          [{ width: 300, height: 300, photo: [0, 0], bg: [0, 0, 0], items: [], bgfx: { kind: 'blur', sigma: 12, veil: 0.8, sat: 0.9 } }],
          () => false,
        );

        await sleep(50);
        fv.destroy();
        decor.destroy();
        releaseFinishDrawResources();

        assert(activeTimers.size === 0, `expected 0 dangling timers after destroy, got ${activeTimers.size}`);
        assert(activeRafs.size === 0, `expected 0 dangling rAFs after destroy, got ${activeRafs.size}`);
        assert(activeIdles.size === 0, `expected 0 dangling idle callbacks after destroy, got ${activeIdles.size}`);
        assert(shell.querySelectorAll('.ms').length === 0, 'open sheets must be removed on FrameView.destroy()');
        assert(pv.querySelectorAll('.pv-deco').length === 0, '.pv-deco must be removed on destroy');
        const nonZeroCards = [...fv.panelEl().querySelectorAll('canvas')].filter((c) => c.width > 0 || c.height > 0);
        assert(nonZeroCards.length === 0, `card canvases must be zeroed on destroy, found ${nonZeroCards.length}`);

        // Closed ImageBitmap must never throw on buildBackdropCanvas
        still.width = 0;
        still.height = 0;
        bmp.close();
        const afterClose = buildBackdropCanvas(photo, null, null, still, 'still');
        assert(afterClose === null, 'buildBackdropCanvas must ignore closed ImageBitmap without throwing');

        stage.remove();
        shell.remove();
      } finally {
        window.setTimeout = origSetTimeout;
        window.clearTimeout = origClearTimeout;
        window.requestAnimationFrame = origRaf;
        window.cancelAnimationFrame = origCaf;
        if (origIdle) window.requestIdleCallback = origIdle;
        if (origCancelIdle) window.cancelIdleCallback = origCancelIdle;
      }
      res.resourcesFreedOk = true;
    }

    // =========================================================================
    // CHECKLIST #4: Bounded Font Retries in Real Browser
    // =========================================================================
    {
      const origFetch = window.fetch.bind(window);
      let fontFetchCount = 0;
      window.fetch = async (input, init) => {
        const u = String(input);
        if (u.includes('fonts/') || u.includes('.woff2') || u.includes('.ttf')) {
          fontFetchCount += 1;
          return new Response('', { status: 404 });
        }
        return origFetch(input, init);
      };
      try {
        // Call ensureFontsLoaded rapidly with a broken font request
        for (let i = 0; i < 8; i++) {
          ensureFontsLoaded(['patrick']);
        }
        await sleep(30);
        assert(fontFetchCount <= 1, `font retries must back off instead of spinning immediately, got ${fontFetchCount}`);
        await loadFontFace('patrick').catch(() => {});
        assert(fontFetchCount <= 3, `font retries must stay bounded, got ${fontFetchCount}`);
      } finally {
        window.fetch = origFetch;
      }
      res.boundedRetryOk = true;
    }

    // =========================================================================
    // CHECKLIST #6: Hostile Values Across Real Web Worker Boundary
    // =========================================================================
    {
      // 1) frameMeta: hostile tzOffsetMin & mtimeMs must REJECT with CoreError (never saturate)
      for (const badTz of [1440, -1440, 1.5, Number.NaN, Number.POSITIVE_INFINITY, 2 ** 32 + 1]) {
        let rejected = false;
        let msg = '';
        try {
          await readFrameMeta({ jpg: null, dng: null, name: 'test.jpg', mtimeMs: 1700000000000, tzOffsetMin: badTz });
        } catch (err) {
          rejected = true;
          msg = String(err?.message ?? err);
        }
        assert(rejected && msg.includes('tz_offset_min'), `worker frameMeta must reject hostile tz=${badTz}, got rejected=${rejected} msg=${msg}`);
      }

      for (const badMtime of [Number.NaN, Number.POSITIVE_INFINITY, Number.NEGATIVE_INFINITY, 1e20, -1e20]) {
        let rejected = false;
        let msg = '';
        try {
          await readFrameMeta({ jpg: null, dng: null, name: 'test.jpg', mtimeMs: badMtime, tzOffsetMin: 0 });
        } catch (err) {
          rejected = true;
          msg = String(err?.message ?? err);
        }
        assert(rejected && msg.includes('mtime_ms'), `worker frameMeta must reject hostile mtime=${badMtime}, got rejected=${rejected} msg=${msg}`);
      }

      // 2) prepareLogo: hostile tol must return ok:false error (never clamp)
      const logoFile = new File([new Uint8Array(logoBytesArr)], 'Mark.png', { type: 'image/png' });
      for (const badTol of [Number.NaN, Number.POSITIVE_INFINITY, -1, 61, 1.5, 2 ** 32 + 1]) {
        const reply = await prepareLogo(logoFile, { name: 'Mark', keyWhite: true, tol: badTol });
        assert(reply.ok === false, `worker prepareLogo must reject hostile tol=${badTol} instead of clamping`);
      }

      // 3) computeFrameLayout: over-long frame text rejected by WASM CoreError
      let frameRejected = false;
      try {
        await computeFrameLayout({
          metaJson: JSON.stringify({ w: 4000, h: 3000, meta: {}, logos: [], mark: { w: 1951, h: 1240 } }),
          frameSpecJson: JSON.stringify({ ...DEFAULT_FRAME, style: 'leica', text: 'A'.repeat(25) }),
          stampMetaJson: null,
          stampSpecJson: null,
          fontKeys: ['atkinson', 'inter', 'inter_sb'],
        });
      } catch {
        frameRejected = true;
      }
      assert(frameRejected, 'worker computeFrameLayout must reject over-long text > 24 chars');

      res.hostileWorkerOk = true;
    }

    // =========================================================================
    // S1-1 & S1-2: Logo save cancel aborts IDB tx + late preview A dropped for B
    // =========================================================================
    {
      const { loadAllLogos, putStoredLogo } = await import('/src/lib/idb.ts');
      const makeSolidPng = async (rgba, name) => {
        const cv = document.createElement('canvas');
        cv.width = 64;
        cv.height = 64;
        const ctx = cv.getContext('2d');
        ctx.fillStyle = `rgba(${rgba[0]},${rgba[1]},${rgba[2]},${rgba[3] / 255})`;
        ctx.fillRect(0, 0, 64, 64);
        const blob = await new Promise((resolve) => cv.toBlob(resolve, 'image/png'));
        cv.width = 0;
        cv.height = 0;
        return new File([blob], name, { type: 'image/png' });
      };

      // S1-1 Phase A: cancel during real in-flight Worker logo-save never writes IDB or mutates photo
      const shell = document.createElement('div');
      document.body.append(shell);
      const bmp = await makeTestBitmap();
      const cancelPhoto = makePhoto('s1_cancel', bmp, { style: 'leica', logo: 'ricoh' });
      const initLogos = await loadAllLogos();
      const baseCount = initLogos.length;

      const origPostMessage = Worker.prototype.postMessage;
      let holdSave = false;
      let releaseSave = null;
      let holdPreviewA = false;
      let releasePreviewA = null;
      Worker.prototype.postMessage = function (msg, transfer) {
        if (holdSave && msg && msg.op === 'logo-save') {
          return new Promise((resolve) => {
            releaseSave = () => {
              resolve(origPostMessage.call(this, msg, transfer));
            };
          });
        }
        if (holdPreviewA && msg && msg.op === 'logo-preview' && msg.file?.name === 'synthetic_A.png') {
          holdPreviewA = false;
          return new Promise((resolve) => {
            releasePreviewA = () => {
              resolve(origPostMessage.call(this, msg, transfer));
            };
          });
        }
        return origPostMessage.call(this, msg, transfer);
      };

      try {
        const cancelFile = await makeSolidPng([0, 180, 0, 255], 'CancelMark.png');
        openLogoSheet(shell, cancelPhoto, () => {});
        await sleep(30);
        shell.querySelector('.ms.logo-sheet [data-logo-action="upload"]').click();
        await sleep(10);
        const fi1 = shell.querySelector('.ms.logo-sheet input.filepick');
        const dtC = new DataTransfer();
        dtC.items.add(cancelFile);
        fi1.files = dtC.files;
        fi1.dispatchEvent(new Event('change'));
        let saveC = null;
        for (let i = 0; i < 100; i += 1) {
          saveC = shell.querySelector('.ms.logo-sheet [data-logo-action="save"]');
          if (saveC && !saveC.disabled) break;
          await sleep(30);
        }
        assert(saveC && !saveC.disabled, 'S1-1 preview ready');

        holdSave = true;
        saveC.click();
        for (let i = 0; i < 50; i += 1) {
          if (releaseSave) break;
          await sleep(10);
        }
        holdSave = false;
        assert(typeof releaseSave === 'function', 'S1-1: logo-save must reach Worker.postMessage');
        const cancelBtn = [...shell.querySelectorAll('.ms.logo-sheet .hd button')].find((b) => b.textContent === '取消');
        assert(cancelBtn, 'upload view must have 取消 button');
        cancelBtn.click();
        releaseSave();
        await sleep(150);
        const afterCancelLogos = await loadAllLogos();
        assert(afterCancelLogos.length === baseCount, 'S1-1: cancel during in-flight logo save must not write to IDB');
        assert(cancelPhoto.border.logo === 'ricoh' && cancelPhoto.rev === 0, 'S1-1: cancel must not mutate photo');

        // S1-1 Phase B: abort signal while IDB write transaction has started triggers tx.abort() & keeps IDB empty
        const origAbort = IDBTransaction.prototype.abort;
        const origPut = IDBObjectStore.prototype.put;
        let txAbortCalls = 0;
        const abortCtrl = new AbortController();
        IDBTransaction.prototype.abort = function () {
          txAbortCalls += 1;
          return origAbort.apply(this, arguments);
        };
        IDBObjectStore.prototype.put = function () {
          const req = origPut.apply(this, arguments);
          abortCtrl.abort();
          return req;
        };
        let putRejected = false;
        try {
          await putStoredLogo(
            {
              id: 's1_aborted_logo',
              name: 'AbortedLogo',
              w: 64,
              h: 64,
              coverage: 1,
              opaque: true,
              lum: 100,
              rgba: new ArrayBuffer(64 * 64 * 4),
              createdAt: Date.now(),
            },
            12,
            abortCtrl.signal,
          );
        } catch {
          putRejected = true;
        } finally {
          IDBTransaction.prototype.abort = origAbort;
          IDBObjectStore.prototype.put = origPut;
        }
        assert(putRejected && txAbortCalls >= 1, `S1-1: started IDB write tx must call tx.abort() on cancel (abortCalls=${txAbortCalls})`);
        const afterTxAbort = await loadAllLogos();
        assert(!afterTxAbort.some((r) => r.id === 's1_aborted_logo'), 'S1-1: aborted tx row must not persist in IDB');

        // S1-2: Out-of-order preview race via real Worker: Red A starts -> Blue B starts & resolves -> Red A resolves late
        const photoB = makePhoto('s1_race', bmp, { style: 'leica' });
        const fileA = await makeSolidPng([200, 20, 20, 255], 'synthetic_A.png');
        const fileB = await makeSolidPng([20, 20, 200, 255], 'synthetic_B.png');
        holdPreviewA = true;

        openLogoSheet(shell, photoB, () => {});
        await sleep(30);
        shell.querySelector('.ms.logo-sheet [data-logo-action="upload"]').click();
        await sleep(10);
        const fi2 = shell.querySelector('.ms.logo-sheet input.filepick');
        const dtA = new DataTransfer();
        dtA.items.add(fileA);
        fi2.files = dtA.files;
        fi2.dispatchEvent(new Event('change'));
        for (let i = 0; i < 50; i += 1) {
          if (releasePreviewA) break;
          await sleep(10);
        }
        assert(typeof releasePreviewA === 'function', 'S1-2: preview A must reach Worker.postMessage');

        const dtB = new DataTransfer();
        dtB.items.add(fileB);
        fi2.files = dtB.files;
        fi2.dispatchEvent(new Event('change'));

        let saveB = null;
        for (let i = 0; i < 100; i += 1) {
          saveB = shell.querySelector('.ms.logo-sheet [data-logo-action="save"]');
          if (saveB && !saveB.disabled) break;
          await sleep(30);
        }
        assert(saveB && !saveB.disabled, 'S1-2: Blue B preview must become ready');
        // Now release late Red A!
        releasePreviewA();
        await sleep(150);
        const labelText = shell.querySelector('.ms.logo-sheet .fsline span.small')?.textContent ?? '';
        assert(labelText === 'synthetic_B.png', `S1-2: expected label synthetic_B.png, got ${labelText}`);
        assert(!saveB.disabled, 'S1-2: late A must not disable save button');
        saveB.click();
        for (let i = 0; i < 100; i += 1) {
          if (!shell.querySelector('.ms.logo-sheet')) break;
          await sleep(30);
        }
        const savedList = await loadAllLogos();
        const savedBlue = savedList.find((r) => r.id === photoB.border.logo);
        assert(savedBlue, 'S1-2: Blue B must be saved');
        const rgbaB = new Uint8ClampedArray(savedBlue.rgba);
        const midOff = (Math.floor(savedBlue.h / 2) * savedBlue.w + Math.floor(savedBlue.w / 2)) * 4;
        assert(
          rgbaB[midOff] === 20 && rgbaB[midOff + 1] === 20 && rgbaB[midOff + 2] === 200 && rgbaB[midOff + 3] === 255,
          `S1-2: expected saved Blue B pixel [20,20,200,255], got [${rgbaB[midOff]},${rgbaB[midOff + 1]},${rgbaB[midOff + 2]},${rgbaB[midOff + 3]}]`,
        );
        await logoStore.remove(savedBlue.id);
      } finally {
        Worker.prototype.postMessage = origPostMessage;
        shell.remove();
        bmp.close();
      }
      res.s1LogoCancelAndRaceOk = true;
    }

    // =========================================================================
    // S1-3: Text refusal DOM-to-model sync, flushText final draft, & IME deferral
    // =========================================================================
    {
      const shell = document.createElement('div');
      document.body.append(shell);
      const bmp = await makeTestBitmap();

      // Frame more sheet: type 'ABC', select-all & paste '中', click Done
      const pMore = makePhoto('s1_t1', bmp, { style: 'leica', text: 'ABC' });
      pMore.rev = 10;
      let moreUpdates = 0;
      openFrameMoreSheet(shell, pMore, () => {
        moreUpdates += 1;
      });
      const moreInp = shell.querySelector('.ms.frame-more-sheet input.txtbox');
      const moreErr = shell.querySelector('.ms.frame-more-sheet .ferr');
      const moreDone = shell.querySelector('.ms.frame-more-sheet .hd button.bold');
      moreInp.value = '中';
      moreInp.dispatchEvent(new Event('input', { bubbles: true }));
      assert(moreInp.value === '' && !moreErr.hidden, 'S1-3 frame_more: DOM cleared and error shown on CJK');
      assert(pMore.border.text === '', `S1-3 frame_more: model text must sync to empty, got ${pMore.border.text}`);
      moreDone.click();
      assert(pMore.border.text === '', 'S1-3 frame_more: Done must keep empty text');
      assert(pMore.rev === 11, `S1-3 frame_more: rev must bump from 10 to 11 on cleared ABC, got ${pMore.rev}`);

      // Frame more sheet: draft modified to XYZ then reverted to initial ABC -> Done writes ABC without bumping rev
      const pRevert = makePhoto('s1_t2', bmp, { style: 'leica', text: 'ABC' });
      pRevert.rev = 5;
      let revertUpdates = 0;
      openFrameMoreSheet(shell, pRevert, () => {
        revertUpdates += 1;
      });
      const rInp = shell.querySelector('.ms.frame-more-sheet input.txtbox');
      const rDone = shell.querySelector('.ms.frame-more-sheet .hd button.bold');
      rInp.value = 'XYZ';
      rInp.dispatchEvent(new Event('input', { bubbles: true }));
      assert(pRevert.border.text === 'XYZ');
      rInp.value = 'ABC';
      rInp.dispatchEvent(new Event('input', { bubbles: true }));
      rDone.click();
      assert(pRevert.border.text === 'ABC' && pRevert.rev === 5, 'S1-3: reverting to initial must write ABC without bumping rev');

      // Stamp sheet: IME compositionstart -> isComposing input -> compositionend('中') + Done
      const pStamp = makePhoto('s1_t3', bmp, {}, { enabled: true, content: 'date+text', text: 'ABC' });
      pStamp.rev = 20;
      openStampSheet(shell, pStamp, () => {});
      const stInp = shell.querySelector('.ms.stamp-sheet input.txtbox');
      const stErr = shell.querySelector('.ms.stamp-sheet .ferr');
      const stDone = shell.querySelector('.ms.stamp-sheet .hd button.bold');
      stInp.dispatchEvent(new CompositionEvent('compositionstart', { bubbles: true }));
      stInp.value = 'zhong';
      stInp.dispatchEvent(new InputEvent('input', { bubbles: true, isComposing: true }));
      assert(stErr.hidden === true && pStamp.stamp.text === 'ABC', 'S1-3 IME: composing input must not touch error or model');
      stInp.value = '中';
      stInp.dispatchEvent(new CompositionEvent('compositionend', { bubbles: true, data: '中' }));
      assert(stInp.value === '' && !stErr.hidden && pStamp.stamp.text === '', 'S1-3 IME: compositionend must validate & sync CJK refusal');
      stDone.click();
      assert(pStamp.stamp.text === '' && pStamp.rev === 21, 'S1-3 IME: Done after CJK refusal keeps empty model & bumps rev');

      shell.remove();
      bmp.close();
      res.s1TextRefusalOk = true;
    }

    // =========================================================================
    // S1-4: Style cards complete 13/13 despite no-op backdropUpdated(), & redraw on same-size swap
    // =========================================================================
    {
      const shell = document.createElement('div');
      document.body.append(shell);
      const bmp = await makeTestBitmap(128, 96);
      const photo = makePhoto('s1_cards', bmp, { style: 'leica' });
      const backdropCv = document.createElement('canvas');
      backdropCv.width = 128;
      backdropCv.height = 96;
      const bctx = backdropCv.getContext('2d');
      bctx.fillStyle = 'rgb(210, 30, 30)';
      bctx.fillRect(0, 0, 128, 96);
      let backdropSig = 'red-1';

      const pendingRafs = [];
      const origRaf = window.requestAnimationFrame.bind(window);
      const origCaf = window.cancelAnimationFrame.bind(window);
      let nextRafId = 1;
      const rafMap = new Map();
      window.requestAnimationFrame = (cb) => {
        const id = nextRafId++;
        rafMap.set(id, cb);
        pendingRafs.push(id);
        return id;
      };
      window.cancelAnimationFrame = (id) => {
        rafMap.delete(id);
      };

      const flushRafs = () => {
        let safety = 0;
        while (pendingRafs.length > 0 && safety < 50) {
          safety += 1;
          const id = pendingRafs.shift();
          const cb = rafMap.get(id);
          if (cb) {
            rafMap.delete(id);
            cb(performance.now());
          }
        }
      };

      try {
        const fv = new FrameView(shell, photo, {
          ew: () => [4000, 3000],
          backdrop: () => backdropCv,
          backdropKey: () => backdropSig,
          onChange: () => {},
        });
        shell.append(fv.panelEl());
        fv.tabOpened();
        for (let i = 0; i < 60; i += 1) {
          if (pendingRafs.length > 0) break;
          await sleep(20);
        }
        const renderedMid = fv.panelEl().querySelectorAll('.rc .th.fr:not(.skel)').length;
        assert(renderedMid === 3, `S1-4: expected 3 cards rendered in batch 1, got ${renderedMid}`);
        // Trigger no-op backdropUpdated & refresh mid-batch -> must NOT stall at 3/13!
        fv.backdropUpdated();
        fv.refresh();
        flushRafs();
        const renderedEnd = fv.panelEl().querySelectorAll('.rc .th.fr:not(.skel)').length;
        assert(renderedEnd === 13, `S1-4: all 13 cards must finish painting after no-op refresh, got ${renderedEnd}`);

        // Swap same-size 128x96 red backdrop -> 128x96 blue backdrop with updated key
        bctx.fillStyle = 'rgb(20, 40, 220)';
        bctx.fillRect(0, 0, 128, 96);
        backdropSig = 'blue-2';
        fv.backdropUpdated();
        await sleep(20);
        flushRafs();
        const noneCardCv = fv.panelEl().querySelector('.rc[data-style="none"] canvas');
        assert(noneCardCv, 'S1-4: none card canvas must exist');
        const px = noneCardCv.getContext('2d').getImageData(Math.floor(noneCardCv.width / 2), Math.floor(noneCardCv.height / 2), 1, 1).data;
        assert(px[2] > 180 && px[0] < 60, `S1-4: same-size backdrop swap must redraw cards blue, got rgba(${px[0]},${px[1]},${px[2]},${px[3]})`);
        fv.destroy();
      } finally {
        window.requestAnimationFrame = origRaf;
        window.cancelAnimationFrame = origCaf;
        shell.remove();
        bmp.close();
      }
      res.s1CardsAndBackdropOk = true;
    }

    // =========================================================================
    // S1-5: ensureFrameMeta uses photo.stem so frame_no ends in 1234 without extension
    // =========================================================================
    {
      const bmp = await makeTestBitmap(64, 64);
      const fakeJpg = new File([new Uint8Array([0xff, 0xd8, 0xff, 0xd9])], 'R0001234.JPG', { type: 'image/jpeg', lastModified: 1700000000000 });
      const fakeDng = new File([new Uint8Array([0x49, 0x49, 0x2a, 0x00])], 'R0001234.DNG', { type: 'application/octet-stream', lastModified: 1700000000000 });

      const pJpgOnly = makePhoto('r0001234', bmp);
      pJpgOnly.stem = 'R0001234';
      pJpgOnly.jpg = fakeJpg;
      pJpgOnly.dng = null;
      const metaJpg = await ensureFrameMeta(pJpgOnly);
      assert(metaJpg && metaJpg.frame.frame_no.endsWith('1234') && !metaJpg.frame.frame_no.includes('.JPG'),
        `S1-5 JPG-only: expected frame_no ending in 1234, got ${metaJpg?.frame.frame_no}`);

      const pRawJpg = makePhoto('r0001234', bmp);
      pRawJpg.stem = 'R0001234';
      pRawJpg.jpg = fakeJpg;
      pRawJpg.dng = fakeDng;
      const metaRaw = await ensureFrameMeta(pRawJpg);
      assert(metaRaw && metaRaw.frame.frame_no.endsWith('1234') && !metaRaw.frame.frame_no.includes('.DNG'),
        `S1-5 RAW+JPG: expected frame_no ending in 1234, got ${metaRaw?.frame.frame_no}`);

      bmp.close();
      res.s1FrameNoOk = true;
    }

    // =========================================================================
    // S2-1 (Checklist #2): Unconfirmed drafts & open sheets cancel BEFORE tokens/destroyed flags invalidate
    // Covers frame x stamp x (EditorView.destroy, EditorView.open switch, FrameView.destroy) + logo_sheet
    // =========================================================================
    {
      const { loadAllLogos } = await import('/src/lib/idb.ts');
      const book = new Book();
      const makeRealEditorHost = () => {
        const shell = document.createElement('div');
        shell.className = 'app';
        shell.style.width = '390px';
        shell.style.height = '844px';
        const view = document.createElement('div');
        shell.append(view);
        document.body.append(shell);
        return { shell, view };
      };
      const makeStubApi = (list) => {
        const map = new Map(list.map((p) => [p.id, p]));
        return {
          photos: () => list,
          photo: (id) => map.get(id),
          order: () => 'desc',
          gap: () => 15,
          ignored: () => 0,
          hasDat: () => false,
          loading: () => ({ done: list.length, total: list.length }),
          setOrder: () => {},
          setGap: () => {},
          open: () => {},
          remove: () => {},
          clear: () => {},
          addPhotos: () => {},
          showHelp: () => {},
          back: () => {},
          swipe: () => {},
          markBad: () => {},
          markGone: () => {},
          readFull: async () => ({ ok: false, gone: false, stale: false }),
          uploadRaw: async () => false,
          takeWarm: () => null,
          keepWarm: () => {},
          gpu: () => false,
          session: () => 1,
          cardOut: () => false,
          still: async () => null,
          watch: () => {},
          perf: () => {},
          book: () => book,
          pickDat: () => {},
          pickCalib: () => {},
          showCalib: () => {},
          exportPhotos: () => {},
          takeCalib: () => {},
        };
      };

      const draftCases = [];
      for (const kind of ['frame', 'stamp']) {
        for (const exit of ['editor-destroy', 'editor-switch', 'frame-destroy']) {
          const bmp1 = await makeTestBitmap(120, 90);
          const bmp2 = await makeTestBitmap(120, 90);
          const p1 = makePhoto(
            `s2_${kind}_${exit}_1`,
            bmp1,
            { style: 'leica', text: 'ORIGINAL' },
            { enabled: true, content: 'date+text', text: 'ORIGINAL' },
          );
          p1.rev = 7;
          const p2 = makePhoto(`s2_${kind}_${exit}_2`, bmp2, { style: 'none', text: '' });
          p2.rev = 1;

          const { shell, view } = makeRealEditorHost();
          const api = makeStubApi([p1, p2]);
          const editor = new EditorView(view, shell, api, null);

          if (exit === 'frame-destroy') {
            // Direct FrameView mount with real View lifecycle callback
            let fvDestroyed = false;
            const fv = new FrameView(shell, p1, {
              ew: () => [4000, 3000],
              backdrop: () => null,
              onChange: () => {},
              isAlive: () => !fvDestroyed,
            });
            shell.append(fv.panelEl());
            fv.tabOpened();
            const openBtn = kind === 'frame'
              ? fv.panelEl().querySelector('[data-act="frame-more"]')
              : fv.panelEl().querySelector('[data-act="open-stamp"]');
            assert(openBtn, `S2 FrameView missing button for ${kind}`);
            openBtn.click();
            const inp = shell.querySelector('.ms input.txtbox');
            assert(inp, `S2 FrameView missing text input for ${kind}`);
            inp.value = 'DRAFT';
            inp.dispatchEvent(new Event('input', { bubbles: true }));
            const midText = kind === 'frame' ? p1.border.text : p1.stamp.text;
            assert(midText === 'DRAFT' && p1.rev === 7, `S2 pre-cancel draft missing for ${kind}/${exit}`);
            fv.destroy();
          } else {
            editor.open(p1);
            const frameTabBtn = [...view.querySelectorAll('.tabs [role="tab"]')].find((b) => b.textContent === '边框');
            assert(frameTabBtn, 'S2 EditorView missing 边框 tab');
            frameTabBtn.click();
            const openBtn = kind === 'frame'
              ? view.querySelector('.panel.frame [data-act="frame-more"]')
              : view.querySelector('.panel.frame [data-act="open-stamp"]');
            assert(openBtn, `S2 EditorView missing trigger for ${kind}`);
            openBtn.click();
            const inp = shell.querySelector('.ms input.txtbox');
            assert(inp, `S2 EditorView missing text input for ${kind}`);
            inp.value = 'DRAFT';
            inp.dispatchEvent(new Event('input', { bubbles: true }));
            const midText = kind === 'frame' ? p1.border.text : p1.stamp.text;
            assert(midText === 'DRAFT' && p1.rev === 7, `S2 pre-cancel draft missing for ${kind}/${exit}`);

            if (exit === 'editor-destroy') {
              editor.destroy();
            } else {
              editor.open(p2);
            }
          }

          await sleep(180);
          const gotText = kind === 'frame' ? p1.border.text : p1.stamp.text;
          const openSheets = shell.querySelectorAll('.ms').length;
          draftCases.push({ kind, exit, gotText, rev: p1.rev, openSheets });

          if (exit === 'editor-switch') editor.destroy();
          shell.remove();
          bmp1.close();
          bmp2.close();
        }
      }
      const badDrafts = draftCases.filter((c) => c.openSheets !== 0 || c.gotText !== 'ORIGINAL' || c.rev !== 7);
      assert(
        badDrafts.length === 0,
        `S2 draft rollback failures (${badDrafts.length}/${draftCases.length}): ${JSON.stringify(badDrafts)}`,
      );

      // Also verify logo_sheet registered on the host cancels & aborts in-flight save across
      // EditorView.destroy(), EditorView.open(switch), and FrameView.destroy()
      const makeSolidPng = async (rgba, name) => {
        const cv = document.createElement('canvas');
        cv.width = 64;
        cv.height = 64;
        const ctx = cv.getContext('2d');
        ctx.fillStyle = `rgba(${rgba[0]},${rgba[1]},${rgba[2]},${rgba[3] / 255})`;
        ctx.fillRect(0, 0, 64, 64);
        const blob = await new Promise((resolve) => cv.toBlob(resolve, 'image/png'));
        cv.width = 0;
        cv.height = 0;
        return new File([blob], name, { type: 'image/png' });
      };
      const initLogos = await loadAllLogos();
      const baseLogoCount = initLogos.length;
      const origPostMessage = Worker.prototype.postMessage;
      let holdSave = false;
      let releaseSave = null;
      Worker.prototype.postMessage = function (msg, transfer) {
        if (holdSave && msg && msg.op === 'logo-save') {
          return new Promise((resolve) => {
            releaseSave = () => resolve(origPostMessage.call(this, msg, transfer));
          });
        }
        return origPostMessage.call(this, msg, transfer);
      };
      try {
        for (const exit of ['editor-destroy', 'editor-switch', 'frame-destroy']) {
          const bmp1 = await makeTestBitmap(120, 90);
          const bmp2 = await makeTestBitmap(120, 90);
          const p1 = makePhoto(`s2_logo_${exit}_1`, bmp1, { style: 'leica', logo: 'ricoh' });
          p1.rev = 7;
          const p2 = makePhoto(`s2_logo_${exit}_2`, bmp2, { style: 'none' });
          const { shell, view } = makeRealEditorHost();
          const api = makeStubApi([p1, p2]);
          const editor = new EditorView(view, shell, api, null);
          let fv = null;
          if (exit === 'frame-destroy') {
            fv = new FrameView(shell, p1, { ew: () => [4000, 3000], backdrop: () => null, onChange: () => {} });
            shell.append(fv.panelEl());
            fv.panelEl().querySelector('[data-act="frame-logo"]').click();
          } else {
            editor.open(p1);
            [...view.querySelectorAll('.tabs [role="tab"]')].find((b) => b.textContent === '边框').click();
            view.querySelector('.panel.frame [data-act="frame-logo"]').click();
          }
          await sleep(30);
          shell.querySelector('.ms.logo-sheet [data-logo-action="upload"]').click();
          await sleep(10);
          const logoFile = await makeSolidPng([10, 160, 80, 255], `S2Cancel_${exit}.png`);
          const fi = shell.querySelector('.ms.logo-sheet input.filepick');
          const dt = new DataTransfer();
          dt.items.add(logoFile);
          fi.files = dt.files;
          fi.dispatchEvent(new Event('change'));
          let saveBtn = null;
          for (let i = 0; i < 100; i += 1) {
            saveBtn = shell.querySelector('.ms.logo-sheet [data-logo-action="save"]');
            if (saveBtn && !saveBtn.disabled) break;
            await sleep(25);
          }
          assert(saveBtn && !saveBtn.disabled, `S2 logo/${exit}: preview failed`);
          releaseSave = null;
          holdSave = true;
          saveBtn.click();
          for (let i = 0; i < 50; i += 1) {
            if (releaseSave) break;
            await sleep(10);
          }
          holdSave = false;
          assert(typeof releaseSave === 'function', `S2 logo/${exit}: save did not reach Worker`);

          if (exit === 'frame-destroy') fv.destroy();
          else if (exit === 'editor-destroy') editor.destroy();
          else editor.open(p2);

          assert(shell.querySelectorAll('.ms').length === 0, `S2 logo/${exit}: sheet not removed`);
          releaseSave();
          await sleep(120);
          const after = await loadAllLogos();
          assert(after.length === baseLogoCount, `S2 logo/${exit}: cancelled save wrote to IDB`);
          assert(p1.border.logo === 'ricoh' && p1.rev === 7, `S2 logo/${exit}: mutated photo`);

          if (exit === 'editor-switch') editor.destroy();
          shell.remove();
          bmp1.close();
          bmp2.close();
        }
      } finally {
        Worker.prototype.postMessage = origPostMessage;
      }

      res.s2DraftOrderAndSheetsOk = true;
    }

    // =========================================================================
    // S3 (Checklist #5): Real-IndexedDB commit-boundary matrix (5 phases x 6 exits = 30 cases)
    // =========================================================================
    {
      const { Pool } = await import('/src/core/pool.ts');
      const { activeSheetCount } = await import('/src/app/sheet_lifecycle.ts');
      const { loadAllLogos } = await import('/src/lib/idb.ts');

      const until = async (pred, msg, timeoutMs = 8000) => {
        const deadline = Date.now() + timeoutMs;
        while (Date.now() < deadline) {
          const val = pred();
          if (val) return val;
          await sleep(15);
        }
        fail(msg);
      };

      const phases = [
        'worker-pending',
        'transaction-active',
        'transaction-committed',
        'committed-pending',
        'completed',
      ];
      const exits = [
        'replace-frame',
        'replace-stamp',
        'replace-logo',
        'editor-destroy',
        'editor-switch',
        'frame-destroy',
      ];

      const book = new Book();
      const makeRealHost = () => {
        const shell = document.createElement('div');
        shell.className = 'app';
        shell.style.width = '390px';
        shell.style.height = '844px';
        const view = document.createElement('div');
        shell.append(view);
        document.body.append(shell);
        return { shell, view };
      };
      const makeEditorApi = (list) => {
        const map = new Map(list.map((p) => [p.id, p]));
        return {
          photos: () => list,
          photo: (id) => map.get(id),
          order: () => 'desc',
          gap: () => 15,
          ignored: () => 0,
          hasDat: () => false,
          loading: () => ({ done: list.length, total: list.length }),
          setOrder: () => {},
          setGap: () => {},
          open: () => {},
          remove: () => {},
          clear: () => {},
          addPhotos: () => {},
          showHelp: () => {},
          back: () => {},
          swipe: () => {},
          markBad: () => {},
          markGone: () => {},
          readFull: async () => ({ ok: false, gone: false, stale: false }),
          uploadRaw: async () => false,
          takeWarm: () => null,
          keepWarm: () => {},
          gpu: () => false,
          session: () => 1,
          cardOut: () => false,
          still: async () => null,
          watch: () => {},
          perf: () => {},
          book: () => book,
          pickDat: () => {},
          pickCalib: () => {},
          showCalib: () => {},
          exportPhotos: () => {},
          takeCalib: () => {},
        };
      };

      await logoStore.ensureLoaded();
      const baselineIds = new Set((await loadAllLogos()).map((r) => r.id));
      const syntheticLogoFile = new File([new Uint8Array(logoBytesArr)], 'SyntheticMark.png', { type: 'image/png' });
      const failures = [];
      let matrixCount = 0;

      for (const phase of phases) {
        for (const exit of exits) {
          matrixCount += 1;
          const bmp1 = await makeTestBitmap(120, 90);
          const bmp2 = await makeTestBitmap(120, 90);
          const p1 = makePhoto(`s3_${phase}_${exit}_1`, bmp1, { style: 'leica', logo: 'ricoh', show_logo: true });
          p1.rev = 7;
          const p2 = makePhoto(`s3_${phase}_${exit}_2`, bmp2, { style: 'leica', logo: 'ricoh', show_logo: true });
          p2.rev = 0;

          const { shell, view } = makeRealHost();
          const api = makeEditorApi([p1, p2]);
          const editor = new EditorView(view, shell, api, null);

          const origRequest = Pool.prototype.request;
          const origAdd = logoStore.add;
          const origPut = IDBObjectStore.prototype.put;
          const origAbort = IDBTransaction.prototype.abort;
          const origTx = IDBDatabase.prototype.transaction;

          let exitCalled = false;
          let workerHeld = false;
          let commitHeld = false;
          let abortCalls = 0;
          let addPromise = null;
          let addResult = null;
          let releaseWorker = () => {};
          let releaseCommit = () => {};
          const workerGate = new Promise((r) => {
            releaseWorker = r;
          });
          const commitGate = new Promise((r) => {
            releaseCommit = r;
          });

          const doExit = () => {
            if (exitCalled) return;
            exitCalled = true;
            if (exit === 'replace-frame') openFrameMoreSheet(shell, p1, () => {});
            else if (exit === 'replace-stamp') openStampSheet(shell, p1, () => {});
            else if (exit === 'replace-logo') openLogoSheet(shell, p1, () => {});
            else if (exit === 'editor-destroy') editor.destroy();
            else if (exit === 'editor-switch') editor.open(p2);
            else if (exit === 'frame-destroy') editor.frames.destroy();
          };

          try {
            editor.open(p1);
            const frameTab = [...view.querySelectorAll('.tabs [role="tab"]')].find((b) => b.textContent === '边框');
            assert(frameTab, `S3 ${phase}/${exit}: missing 边框 tab`);
            frameTab.click();
            const logoChip = view.querySelector('.panel.frame [data-act="frame-logo"]');
            assert(logoChip, `S3 ${phase}/${exit}: missing 标志 chip`);
            logoChip.click();

            await until(
              () => shell.querySelector('.ms.logo-sheet [data-logo-action="upload"]'),
              `S3 ${phase}/${exit}: logo list upload button missing`,
            );
            shell.querySelector('.ms.logo-sheet [data-logo-action="upload"]').click();

            const picker = shell.querySelector('.ms.logo-sheet input.filepick');
            const dt = new DataTransfer();
            dt.items.add(syntheticLogoFile);
            picker.files = dt.files;
            picker.dispatchEvent(new Event('change', { bubbles: true }));
            await until(
              () => {
                const btn = shell.querySelector('.ms.logo-sheet [data-logo-action="save"]');
                return btn && !btn.disabled;
              },
              `S3 ${phase}/${exit}: preview save button never enabled`,
            );

            const oldSheet = shell.querySelector('.ms.logo-sheet');
            Pool.prototype.request = async function (...args) {
              const outReq = await origRequest.apply(this, args);
              if (phase === 'worker-pending' && args[0] === 'logo-save') {
                workerHeld = true;
                await workerGate;
              }
              return outReq;
            };
            logoStore.add = function (...args) {
              addPromise = (async () => {
                const r = await origAdd.apply(this, args);
                addResult = r;
                if (phase === 'committed-pending' && r.ok) {
                  commitHeld = true;
                  await commitGate;
                }
                return r;
              })();
              return addPromise;
            };
            IDBTransaction.prototype.abort = function (...args) {
              abortCalls += 1;
              return origAbort.apply(this, args);
            };
            IDBDatabase.prototype.transaction = function (...args) {
              const tx = origTx.apply(this, args);
              if (
                phase === 'transaction-committed' &&
                args[1] === 'readwrite' &&
                tx.objectStoreNames.contains('logos')
              ) {
                // Real IDB commit boundary: `complete` event fires after durability, before app's `tx.oncomplete`.
                tx.addEventListener('complete', doExit, { once: true });
              }
              return tx;
            };
            IDBObjectStore.prototype.put = function (...args) {
              const req = origPut.apply(this, args);
              if (phase === 'transaction-active' && this.name === 'logos') {
                doExit();
              }
              return req;
            };

            shell.querySelector('.ms.logo-sheet [data-logo-action="save"]').click();

            if (phase === 'worker-pending') {
              await until(() => workerHeld, `S3 ${phase}/${exit}: worker-pending not reached`);
              doExit();
              releaseWorker();
            } else if (phase === 'transaction-active') {
              await until(() => exitCalled, `S3 ${phase}/${exit}: transaction-active not reached`);
            } else if (phase === 'transaction-committed') {
              await until(() => exitCalled, `S3 ${phase}/${exit}: transaction-committed not reached`);
            } else if (phase === 'committed-pending') {
              await until(() => commitHeld, `S3 ${phase}/${exit}: committed-pending not reached`);
              const midRows = await loadAllLogos();
              assert(
                midRows.some((r) => r.id === addResult.logo.id),
                `S3 ${phase}/${exit}: row must already be durable before committed-pending exit`,
              );
              doExit();
              releaseCommit();
            } else {
              await until(
                () => !oldSheet.isConnected && p1.border.logo !== 'ricoh',
                `S3 ${phase}/${exit}: save did not complete before exit`,
              );
              doExit();
            }

            await addPromise;
            await sleep(80);

            // Restore prototypes before verification & fresh upload check
            IDBObjectStore.prototype.put = origPut;
            IDBDatabase.prototype.transaction = origTx;
            IDBTransaction.prototype.abort = origAbort;
            Pool.prototype.request = origRequest;
            logoStore.add = origAdd;

            const stored = (await loadAllLogos()).filter((r) => !baselineIds.has(r.id));
            const cached = logoStore.list().filter((r) => !baselineIds.has(r.id));
            const committed =
              phase === 'transaction-committed' || phase === 'committed-pending' || phase === 'completed';
            const expectedCount = committed ? 1 : 0;
            const expectedSheets = exit.startsWith('replace-') ? 1 : 0;
            const openSheets = shell.querySelectorAll('.ms').length;
            const regCount = activeSheetCount(shell);
            const liveReplacementHasSaved =
              exit !== 'replace-logo' || !committed || stored.every((s) => Boolean(shell.querySelector(`.ms.logo-sheet [data-logo="${s.id}"]`)));

            // Reopen logo catalogue and verify persisted vs cached vs UI rows + fresh upload save button
            openLogoSheet(shell, p2, () => {});
            await sleep(20);
            const reopenedHasSaved = stored.every((s) =>
              Boolean(shell.querySelector(`.ms.logo-sheet [data-logo="${s.id}"]`)),
            );
            const reopenedRowCount = shell.querySelectorAll('.ms.logo-sheet .logo-row').length - baselineIds.size;

            shell.querySelector('.ms.logo-sheet [data-logo-action="upload"]').click();
            const picker2 = shell.querySelector('.ms.logo-sheet input.filepick');
            const dt2 = new DataTransfer();
            dt2.items.add(syntheticLogoFile);
            picker2.files = dt2.files;
            picker2.dispatchEvent(new Event('change', { bubbles: true }));
            await until(
              () => {
                const btn2 = shell.querySelector('.ms.logo-sheet [data-logo-action="save"]');
                return btn2 && !btn2.disabled;
              },
              `S3 ${phase}/${exit}: fresh upload save button stuck disabled`,
            );

            // Also execute an actual fresh save on transaction-committed/replace-logo to prove end-to-end save works afterwards
            let freshSavedOk = true;
            if (phase === 'transaction-committed' && exit === 'replace-logo') {
              shell.querySelector('.ms.logo-sheet [data-logo-action="save"]').click();
              await until(() => p2.border.logo !== 'ricoh', 'fresh save after transaction-committed did not commit to p2');
              freshSavedOk = p2.rev === 1 && Boolean(logoStore.info(p2.border.logo));
              if (p2.border.logo !== 'ricoh') {
                await logoStore.remove(p2.border.logo);
                p2.border.logo = 'ricoh';
                p2.rev = 0;
              }
            }

            const expectedPhotoRev = phase === 'completed' ? 8 : 7;
            const expectedPhotoLogo = phase === 'completed' ? (stored[0]?.id ?? '') : 'ricoh';

            const okCell =
              stored.length === expectedCount &&
              cached.length === expectedCount &&
              Boolean(addResult?.ok) === committed &&
              !oldSheet.isConnected &&
              openSheets === expectedSheets &&
              regCount === expectedSheets &&
              liveReplacementHasSaved &&
              reopenedHasSaved &&
              reopenedRowCount === expectedCount &&
              freshSavedOk &&
              p1.rev === expectedPhotoRev &&
              p1.border.logo === expectedPhotoLogo &&
              p2.rev === 0 &&
              (phase !== 'transaction-active' || abortCalls > 0);

            if (!okCell) {
              failures.push({
                phase,
                exit,
                persisted: stored.length,
                cached: cached.length,
                addOk: Boolean(addResult?.ok),
                reopenedRowCount,
                reopenedHasSaved,
                liveReplacementHasSaved,
                p1Rev: p1.rev,
                p1Logo: p1.border.logo,
                openSheets,
                regCount,
                abortCalls,
              });
            }

            cancelHostSheets(shell);
            for (const row of await loadAllLogos()) {
              if (!baselineIds.has(row.id)) {
                await logoStore.remove(row.id);
              }
            }
          } finally {
            releaseWorker();
            releaseCommit();
            IDBObjectStore.prototype.put = origPut;
            IDBDatabase.prototype.transaction = origTx;
            IDBTransaction.prototype.abort = origAbort;
            Pool.prototype.request = origRequest;
            logoStore.add = origAdd;
            editor.destroy();
            shell.remove();
            bmp1.close();
            bmp2.close();
          }
        }
      }

      assert(matrixCount === 30, `Expected 30 commit-boundary cases, ran ${matrixCount}`);
      assert(
        failures.length === 0,
        `S3 commit-boundary failures (${failures.length}/30): ${JSON.stringify(failures)}`,
      );
      res.s3CommitBoundaryMatrixOk = true;
    }

    // =========================================================================
    // S3 Same-Family Audit: Logo delete/rename commit boundaries + post-commit text preservation
    // =========================================================================
    {
      const { loadAllLogos } = await import('/src/lib/idb.ts');
      const until = async (pred, msg, timeoutMs = 8000) => {
        const deadline = Date.now() + timeoutMs;
        while (Date.now() < deadline) {
          const val = pred();
          if (val) return val;
          await sleep(15);
        }
        fail(msg);
      };

      const book = new Book();
      const makeRealHost = () => {
        const shell = document.createElement('div');
        shell.className = 'app';
        shell.style.width = '390px';
        shell.style.height = '844px';
        const view = document.createElement('div');
        shell.append(view);
        document.body.append(shell);
        return { shell, view };
      };
      const makeEditorApi = (list) => {
        const map = new Map(list.map((p) => [p.id, p]));
        return {
          photos: () => list,
          photo: (id) => map.get(id),
          order: () => 'desc',
          gap: () => 15,
          ignored: () => 0,
          hasDat: () => false,
          loading: () => ({ done: list.length, total: list.length }),
          setOrder: () => {},
          setGap: () => {},
          open: () => {},
          remove: () => {},
          clear: () => {},
          addPhotos: () => {},
          showHelp: () => {},
          back: () => {},
          swipe: () => {},
          markBad: () => {},
          markGone: () => {},
          readFull: async () => ({ ok: false, gone: false, stale: false }),
          uploadRaw: async () => false,
          takeWarm: () => null,
          keepWarm: () => {},
          gpu: () => false,
          session: () => 1,
          cardOut: () => false,
          still: async () => null,
          watch: () => {},
          perf: () => {},
          book: () => book,
          pickDat: () => {},
          pickCalib: () => {},
          showCalib: () => {},
          exportPhotos: () => {},
          takeCalib: () => {},
        };
      };

      await logoStore.ensureLoaded();
      const syntheticLogoFile = new File([new Uint8Array(logoBytesArr)], 'AuditMark.png', { type: 'image/png' });

      // 1. Delete pre-commit cancel (transaction-active) vs post-commit cancel (transaction-committed)
      for (const exit of ['replace-frame', 'replace-logo', 'editor-switch']) {
        const seeded = await logoStore.add(syntheticLogoFile, { name: `Del_${exit}`, keyWhite: true, tol: 30 });
        assert(seeded.ok, `seed failed for Del_${exit}`);
        const logoId = seeded.logo.id;

        const bmp1 = await makeTestBitmap(120, 90);
        const bmp2 = await makeTestBitmap(120, 90);
        const p1 = makePhoto(`audit_del_${exit}_1`, bmp1, { style: 'leica', logo: logoId, show_logo: true });
        p1.rev = 7;
        const p2 = makePhoto(`audit_del_${exit}_2`, bmp2, { style: 'leica', logo: logoId, show_logo: true });
        p2.rev = 3;

        const { shell, view } = makeRealHost();
        const api = makeEditorApi([p1, p2]);
        const editor = new EditorView(view, shell, api, null);
        const origDelete = IDBObjectStore.prototype.delete;
        const origTx = IDBDatabase.prototype.transaction;
        const origAbort = IDBTransaction.prototype.abort;

        try {
          editor.open(p1);
          [...view.querySelectorAll('.tabs [role="tab"]')].find((b) => b.textContent === '边框').click();
          view.querySelector('.panel.frame [data-act="frame-logo"]').click();
          await until(
            () => shell.querySelector(`.ms.logo-sheet [data-logo-row="${logoId}"]`),
            `missing seeded row ${logoId}`,
          );

          // (a) Pre-commit cancel during active delete transaction -> rolls back cleanly
          if (exit === 'replace-frame') {
            let abortCount = 0;
            IDBTransaction.prototype.abort = function (...args) {
              abortCount += 1;
              return origAbort.apply(this, args);
            };
            IDBObjectStore.prototype.delete = function (...args) {
              const req = origDelete.apply(this, args);
              if (this.name === 'logos') {
                openFrameMoreSheet(shell, p1, () => {});
              }
              return req;
            };
            shell.querySelector(`.ms.logo-sheet [data-logo-row="${logoId}"] button[aria-label*="更多操作"]`).click();
            [...shell.querySelectorAll('.ms.logo-sheet .logo-row-acts button')].find((b) => b.textContent === '删除').click();
            [...shell.querySelectorAll('.ms.logo-sheet .logo-row-acts button.danger')].find((b) => b.textContent === '删除').click();
            await sleep(80);
            IDBObjectStore.prototype.delete = origDelete;
            IDBTransaction.prototype.abort = origAbort;

            assert(abortCount > 0, 'active delete transaction must abort on sheet replacement');
            assert((await loadAllLogos()).some((r) => r.id === logoId), 'aborted delete must keep row in IDB');
            assert(Boolean(logoStore.info(logoId)), 'aborted delete must keep row in LogoStore');
            assert(p1.border.logo === logoId && p1.rev === 7, 'aborted delete must keep p1.border.logo');

            // Reopen logo sheet for committed-boundary test
            openLogoSheet(shell, p1, () => editor.frames?.refresh());
            await until(
              () => shell.querySelector(`.ms.logo-sheet [data-logo-row="${logoId}"]`),
              'missing row on reopen',
            );
          }

          // (b) Commit-boundary exit (`complete` fires before `tx.oncomplete`)
          let exited = false;
          IDBDatabase.prototype.transaction = function (...args) {
            const tx = origTx.apply(this, args);
            if (args[1] === 'readwrite' && tx.objectStoreNames.contains('logos')) {
              tx.addEventListener(
                'complete',
                () => {
                  if (exited) return;
                  exited = true;
                  if (exit === 'replace-frame') openFrameMoreSheet(shell, p1, () => {});
                  else if (exit === 'replace-logo') openLogoSheet(shell, p1, () => {});
                  else editor.open(p2);
                },
                { once: true },
              );
            }
            return tx;
          };

          shell.querySelector(`.ms.logo-sheet [data-logo-row="${logoId}"] button[aria-label*="更多操作"]`).click();
          [...shell.querySelectorAll('.ms.logo-sheet .logo-row-acts button')].find((b) => b.textContent === '删除').click();
          [...shell.querySelectorAll('.ms.logo-sheet .logo-row-acts button.danger')].find((b) => b.textContent === '删除').click();

          await until(() => exited, `delete commit-boundary exit did not run for ${exit}`);
          await sleep(80);
          IDBDatabase.prototype.transaction = origTx;

          assert(!(await loadAllLogos()).some((r) => r.id === logoId), `deleted logo ${logoId} must be gone from IDB`);
          assert(!logoStore.info(logoId), `deleted logo ${logoId} must be gone from LogoStore`);
          assert(
            p1.border.logo === 'ricoh' && p1.rev === 8,
            `committed delete with ${exit} must revert p1.border.logo to ricoh (got ${p1.border.logo}, rev=${p1.rev})`,
          );
          if (exit === 'replace-frame') {
            assert(
              !shell.querySelector('.ms.frame-more-sheet [aria-label="标志颜色"]'),
              'replace-frame sheet must remove custom logo controls once logo deletion commits',
            );
          } else if (exit === 'replace-logo') {
            assert(
              !shell.querySelector(`.ms.logo-sheet [data-logo="${logoId}"]`),
              'replace-logo sheet must not list deleted logo',
            );
            assert(
              shell.querySelector('.ms.logo-sheet [data-logo="ricoh"]')?.classList.contains('on'),
              'replace-logo sheet must mark GXR logo active after deleted-logo fallback',
            );
          } else if (exit === 'editor-switch') {
            assert(
              p2.border.logo === 'ricoh' && p2.rev === 4,
              `switched-to p2 using deleted logo must also revert via FrameView catalogue watch (got ${p2.border.logo}, rev=${p2.rev})`,
            );
          }
        } finally {
          IDBObjectStore.prototype.delete = origDelete;
          IDBDatabase.prototype.transaction = origTx;
          IDBTransaction.prototype.abort = origAbort;
          if (logoStore.info(logoId)) await logoStore.remove(logoId);
          editor.destroy();
          shell.remove();
          bmp1.close();
          bmp2.close();
        }
      }

      // 2. Rename pre-commit cancel vs commit-boundary exit
      {
        const seeded = await logoStore.add(syntheticLogoFile, { name: 'OrigName', keyWhite: true, tol: 30 });
        assert(seeded.ok, 'seed failed for rename audit');
        const logoId = seeded.logo.id;
        const bmp = await makeTestBitmap(120, 90);
        const p1 = makePhoto('audit_ren_1', bmp, { style: 'leica', logo: logoId, show_logo: true });
        const { shell, view } = makeRealHost();
        const editor = new EditorView(view, shell, makeEditorApi([p1]), null);
        const origPut = IDBObjectStore.prototype.put;
        const origTx = IDBDatabase.prototype.transaction;

        try {
          editor.open(p1);
          [...view.querySelectorAll('.tabs [role="tab"]')].find((b) => b.textContent === '边框').click();
          view.querySelector('.panel.frame [data-act="frame-logo"]').click();
          await until(() => shell.querySelector(`.ms.logo-sheet [data-logo-row="${logoId}"]`), 'rename row missing');

          // (a) Pre-commit cancel during active rename transaction
          IDBObjectStore.prototype.put = function (...args) {
            const req = origPut.apply(this, args);
            if (this.name === 'logos') openLogoSheet(shell, p1, () => {});
            return req;
          };
          shell.querySelector(`.ms.logo-sheet [data-logo-row="${logoId}"] button[aria-label*="更多操作"]`).click();
          [...shell.querySelectorAll('.ms.logo-sheet .logo-row-acts button')].find((b) => b.textContent === '重命名').click();
          const inp1 = shell.querySelector('.ms.logo-sheet .logo-row-acts input.txtbox');
          inp1.value = 'AbortedName';
          [...shell.querySelectorAll('.ms.logo-sheet .logo-row-acts button')].find((b) => b.textContent === '保存').click();
          await sleep(80);
          IDBObjectStore.prototype.put = origPut;

          const idbAfterAbort = (await loadAllLogos()).find((r) => r.id === logoId);
          assert(idbAfterAbort?.name === 'OrigName', `aborted rename must keep IDB name OrigName, got ${idbAfterAbort?.name}`);
          assert(logoStore.info(logoId)?.name === 'OrigName', 'aborted rename must keep LogoStore name OrigName');

          // (b) Commit-boundary exit (`complete` before `tx.oncomplete`)
          let renExited = false;
          IDBDatabase.prototype.transaction = function (...args) {
            const tx = origTx.apply(this, args);
            if (args[1] === 'readwrite' && tx.objectStoreNames.contains('logos')) {
              tx.addEventListener(
                'complete',
                () => {
                  if (renExited) return;
                  renExited = true;
                  openLogoSheet(shell, p1, () => {});
                },
                { once: true },
              );
            }
            return tx;
          };
          shell.querySelector(`.ms.logo-sheet [data-logo-row="${logoId}"] button[aria-label*="更多操作"]`).click();
          [...shell.querySelectorAll('.ms.logo-sheet .logo-row-acts button')].find((b) => b.textContent === '重命名').click();
          const inp2 = shell.querySelector('.ms.logo-sheet .logo-row-acts input.txtbox');
          inp2.value = 'CommittedName';
          [...shell.querySelectorAll('.ms.logo-sheet .logo-row-acts button')].find((b) => b.textContent === '保存').click();
          await until(() => renExited, 'rename commit-boundary exit did not run');
          await sleep(80);
          IDBDatabase.prototype.transaction = origTx;

          const idbAfterCommit = (await loadAllLogos()).find((r) => r.id === logoId);
          assert(idbAfterCommit?.name === 'CommittedName', 'committed rename must persist in IDB');
          assert(logoStore.info(logoId)?.name === 'CommittedName', 'committed rename must update LogoStore');
          const uiTitle = shell.querySelector(`.ms.logo-sheet [data-logo="${logoId}"] b`)?.textContent;
          assert(uiTitle === 'CommittedName', `replacement logo_sheet must show CommittedName, got ${uiTitle}`);
        } finally {
          IDBObjectStore.prototype.put = origPut;
          IDBDatabase.prototype.transaction = origTx;
          await logoStore.remove(logoId);
          editor.destroy();
          shell.remove();
          bmp.close();
        }
      }

      // 3. Post-commit cancel on frame_more_sheet & stamp_sheet preserves committed text
      {
        const bmp = await makeTestBitmap(80, 60);
        const p = makePhoto(
          'audit_text_1',
          bmp,
          { style: 'leica', text: 'INIT' },
          { enabled: true, content: 'date+text', text: 'INIT_S' },
        );
        p.rev = 5;
        const shell = document.createElement('div');
        document.body.append(shell);

        openFrameMoreSheet(shell, p, () => {});
        const fInp = shell.querySelector('.ms.frame-more-sheet input.txtbox');
        fInp.value = 'COMMITTED_F';
        fInp.dispatchEvent(new Event('change', { bubbles: true }));
        assert(p.border.text === 'COMMITTED_F' && p.rev === 6, 'change event must commit frame text');
        cancelHostSheets(shell);
        assert(p.border.text === 'COMMITTED_F' && p.rev === 6, 'post-commit cancel must preserve committed frame text');

        openStampSheet(shell, p, () => {});
        const sInp = shell.querySelector('.ms.stamp-sheet input.txtbox');
        sInp.value = 'COMMITTED_S';
        sInp.dispatchEvent(new Event('change', { bubbles: true }));
        assert(p.stamp.text === 'COMMITTED_S' && p.rev === 7, 'change event must commit stamp text');
        cancelHostSheets(shell);
        assert(p.stamp.text === 'COMMITTED_S' && p.rev === 7, 'post-commit cancel must preserve committed stamp text');

        shell.remove();
        bmp.close();
      }

      res.s3SameFamilyPersistenceOk = true;
    }

    return res;
  }, { logoBytesArr: logoBytes });
} finally {
  await browser.close();
}

process.stdout.write(`${JSON.stringify(out)}\n`);
