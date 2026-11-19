// Real desktop editor browser driver for halfframe-p1a (card250 pair photo 2).
// Never prints photo names, capture times, or EXIF. Writes a JSON report to manifest.out.
import { readFileSync, writeFileSync } from 'node:fs';
import { pathToFileURL } from 'node:url';

const [runtime, manifestPath] = process.argv.slice(2);
const m = JSON.parse(readFileSync(manifestPath, 'utf8'));
const { launchPinnedChromium, verifyPinnedRuntime } = await import(pathToFileURL(runtime).href);
verifyPinnedRuntime();

const launchOpts = m.gpu
  ? {
      args: [
        '--enable-unsafe-webgpu',
        '--enable-unsafe-swiftshader',
        '--use-angle=swiftshader',
        '--use-webgpu-adapter=swiftshader',
      ],
    }
  : {};
const browser = await launchPinnedChromium(launchOpts);

const out = {
  gpuFlag: !!m.gpu,
  pageErrors: [],
  steps: {},
  puts: [],
  renders: [],
  rawReqs: [],
  bakeReqs: [],
  exports: [],
  cameraJpgReqs: [],
  gpuResponses: [],
};
const line = (e) => (e instanceof Error ? e.message : String(e)).split('\n')[0].slice(0, 300);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const waitFor = async (pred, timeoutMs = 45000, label = 'condition') => {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (await pred()) return;
    await sleep(150);
  }
  throw new Error(`timeout waiting for ${label}`);
};

try {
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
  page.on('pageerror', (e) => out.pageErrors.push(line(e)));
  page.on('request', (rq) => {
    const u = new URL(rq.url());
    if (u.pathname === `/api/photos/${m.p2}/camera-jpg`) {
      out.cameraJpgReqs.push(`${rq.method()} /api/photos/P2/camera-jpg`);
    }
  });
  page.on('response', async (res) => {
    const u = new URL(res.url());
    const p = u.pathname;
    const req = res.request();
    if (req.method() === 'PUT' && p === `/api/photos/${m.p2}/edit`) {
      let body = null;
      try { body = await res.json(); } catch { /* ignore */ }
      out.puts.push({
        status: res.status(),
        sent: Object.keys(JSON.parse(req.postData() || '{}')),
        error: body?.error || null,
      });
    }
    if (p === `/api/photos/${m.p2}/render`) {
      out.renders.push({
        method: req.method(),
        status: res.status(),
        q: u.search.replace(/token=[^&]+/, ''),
        label: res.headers()['x-gxr-label'] || null,
        pred: res.headers()['x-gxr-prediction'] || null,
        render: res.headers()['x-gxr-render'] || null,
      });
    }
    if (p === `/api/photos/${m.p2}/raw`) {
      out.rawReqs.push({ status: res.status(), half: u.searchParams.get('half') });
    }
    if (p === `/api/photos/${m.p2}/bake`) {
      out.bakeReqs.push({
        status: res.status(),
        half: u.searchParams.get('half'),
        hasRecipe: u.searchParams.has('recipe'),
      });
    }
    if (p === `/api/photos/${m.p2}/gpu`) {
      let body = null;
      try { body = await res.json(); } catch { /* ignore */ }
      out.gpuResponses.push({ status: res.status(), body });
    }
  });
  await page.route(
    (u) => u.pathname === `/api/photos/${m.p2}/export`,
    async (route) => {
      if (route.request().method() !== 'POST') return route.fallback();
      try {
        const res = await route.fetch({ timeout: 90000 });
        let body = null;
        try { body = await res.json(); } catch { /* ignore */ }
        out.exports.push({ status: res.status(), body });
        await route.fulfill({ response: res });
      } catch (err) {
        out.exports.push({ status: 0, body: null, error: line(err) });
        await route.abort().catch(() => {});
      }
    },
  );

  const api = (path, opts) =>
    page.evaluate(
      async ([p, o]) => {
        const r = await fetch(p, o || {});
        const ct = r.headers.get('content-type') || '';
        return { status: r.status, body: ct.includes('json') ? await r.json() : null };
      },
      [path, opts],
    );

  const captureStageImage = async (destPath) => {
    if (m.gpu) {
      const info = await page.evaluate(async () => {
        const { gpuEngine } = await import('/assets/gpu_stage.js');
        const eng = gpuEngine();
        if (!eng) throw new Error('gpuEngine missing');
        const rb = await eng.readback();
        const c = document.createElement('canvas');
        c.width = rb.w;
        c.height = rb.h;
        const ctx = c.getContext('2d');
        const imgData = new ImageData(
          new Uint8ClampedArray(rb.data.buffer, rb.data.byteOffset, rb.data.byteLength),
          rb.w,
          rb.h,
        );
        ctx.putImageData(imgData, 0, 0);
        const dataUrl = c.toDataURL('image/png');
        return {
          b64: dataUrl.slice(dataUrl.indexOf(',') + 1),
          w: rb.w,
          h: rb.h,
        };
      });
      writeFileSync(destPath, Buffer.from(info.b64, 'base64'));
      return { w: info.w, h: info.h };
    }
    await page.waitForFunction(() => {
      const img = document.querySelector('.stage-area img');
      return !!img && img.complete && img.naturalWidth > 0 && img.naturalHeight > 0;
    }, null, { timeout: 60000 });
    const info = await page.evaluate(() => {
      const img = document.querySelector('.stage-area img');
      const w = img.naturalWidth;
      const h = img.naturalHeight;
      const c = document.createElement('canvas');
      c.width = w;
      c.height = h;
      const ctx = c.getContext('2d');
      ctx.drawImage(img, 0, 0, w, h);
      const dataUrl = c.toDataURL('image/png');
      return {
        b64: dataUrl.slice(dataUrl.indexOf(',') + 1),
        w,
        h,
      };
    });
    writeFileSync(destPath, Buffer.from(info.b64, 'base64'));
    return { w: info.w, h: info.h };
  };

  const panelState = async () =>
    page.evaluate(async () => {
      const { currentView } = await import('/assets/viewer.js');
      const v = currentView();
      const cards = [...document.querySelectorAll('.lcard')];
      return {
        cards: cards.length,
        disabled: cards.filter((c) => c.disabled || c.getAttribute('aria-disabled') === 'true').length,
        dimmed: document.querySelectorAll('.look-group.dimmed-block').length,
        pressed: cards.filter((c) => c.getAttribute('aria-pressed') === 'true').map((c) => c.dataset.look),
        notices: [...document.querySelectorAll('.notice')].map((n) => n.textContent.trim().slice(0, 120)),
        stageLabels: document.querySelector('.stage-labels')?.textContent.trim().slice(0, 120) || '',
        gpuActive: !!document.querySelector('.gpu'),
        viewGpuActive: v?.gpuActive || null,
        edLabel: v?.ed?.label || null,
        edCalibrated: v?.ed?.calibrated ?? null,
        gpuFinishKey: v?.gpuFinishKey || null,
        lutDisabled: [...document.querySelectorAll('select[aria-label="LUT"] option')]
          .filter((o) => o.disabled)
          .map((o) => o.value),
      };
    });

  const isStageRender = (r) => r.status === 200 && !String(r.q || '').includes('max_edge=');

  // 相机数据状态先到，再打开照片：app.js 先画页面、后补相机数据，第一次状态不触发重画；
  // 面板若先于状态画出，配方卡会一直灰着（与半格无关的启动竞态，单独处理）。这里只测半格放行。
  const waitCamdata = async () =>
    page.waitForFunction(() => import('/assets/camdata.js').then((cd) => cd.camdataReady()), null, { timeout: 60000 });

  const openPhoto = async () => {
    await waitCamdata();
    await page.goto(`${m.base}/#/photo/${m.p2}`);
    await page.waitForFunction(
      (pid) =>
        Promise.all([import('/assets/viewer.js'), import('/assets/camdata.js')]).then(([v, cd]) => {
          const c = v.currentView();
          return !!c && c.photo.id === pid && !!c.ed && !!c.ed.caps && cd.camdataReady();
        }),
      m.p2,
      { timeout: 60000 },
    );
    await page.waitForSelector('.edit-panel', { timeout: 30000 });
    await page.waitForFunction(() => document.querySelectorAll('.lcard').length > 0, null, { timeout: 30000 });
    if (m.gpu) {
      await waitFor(
        () =>
          page.evaluate(() =>
            Promise.all([import('/assets/viewer.js'), import('/assets/gpu_stage.js')]).then(([v, gs]) => {
              const c = v.currentView();
              const eng = gs.gpuEngine();
              return !!c && c.gpuActive === 'final' && !!c.gpuFinishKey && !!eng && !!eng.currentKey();
            }),
          ),
        60000,
        'initial GPU stage frame',
      );
    } else {
      await waitFor(() => out.renders.some(isStageRender), 60000, 'initial stage render');
    }
    await sleep(600);
  };

  await page.goto(`${m.base}/?token=${encodeURIComponent(m.token)}`);
  await page.waitForSelector('.top, .grid, .empty', { timeout: 30000 });
  out.steps.settings = (await api('/api/settings')).body;

  await openPhoto();
  out.steps.initialPanel = await panelState();
  out.steps.initialStage = await captureStageImage(m.stageInitialPath);
  out.steps.initialRenders = out.renders.filter(isStageRender);
  out.steps.initialRawReqs = [...out.rawReqs];
  out.steps.initialBakeReqs = [...out.bakeReqs];

  // 1) Choose a non-pressed builtin recipe card -> saves & re-renders composite
  const putsBeforePick = out.puts.length;
  const rendersBeforePick = out.renders.length;
  const prevStageSrc = m.gpu ? '' : await page.evaluate(() => document.querySelector('.stage-area img')?.src || '');
  const prevFinishKey = m.gpu
    ? await page.evaluate(() => import('/assets/viewer.js').then((v) => v.currentView()?.gpuFinishKey || ''))
    : '';
  const pick = await page.evaluate(() => {
    const cards = [...document.querySelectorAll('.lcard')].filter(
      (c) => c.dataset.look && c.dataset.look.startsWith('builtin:') && c.getAttribute('aria-pressed') !== 'true',
    );
    const c = cards[1] || cards[0];
    if (!c) return null;
    c.click();
    return { look: c.dataset.look, disabled: !!c.disabled };
  });
  await waitFor(() => out.puts.length > putsBeforePick, 30000, 'recipe PUT /edit');
  if (m.gpu) {
    await waitFor(
      () =>
        page.evaluate(
          (prev) =>
            import('/assets/viewer.js').then((v) => {
              const c = v.currentView();
              return !!c && c.gpuActive === 'final' && !!c.gpuFinishKey && c.gpuFinishKey !== prev;
            }),
          prevFinishKey,
        ),
      60000,
      'recipe GPU stage re-render',
    );
  } else {
    await waitFor(
      () => out.renders.slice(rendersBeforePick).some(isStageRender),
      60000,
      'recipe full stage re-render',
    );
    await page.waitForFunction(
      (prev) => {
        const img = document.querySelector('.stage-area img');
        return !!img && img.complete && img.naturalWidth > 0 && img.src !== prev;
      },
      prevStageSrc,
      { timeout: 30000 },
    );
  }
  await sleep(300);
  out.steps.recipePick = {
    pick,
    puts: out.puts.slice(putsBeforePick),
    renders: out.renders.slice(rendersBeforePick).filter(isStageRender),
    stored: (await api(`/api/photos/${m.p2}/edit`)).body?.recipe || null,
    panel: await panelState(),
    stage: await captureStageImage(m.stageAfterRecipePath),
  };

  // 2) Log LUT then Display LUT through the editor's LUT select
  for (const [kind, id] of [
    ['log', m.logLut],
    ['display', m.dispLut],
  ]) {
    const before = out.puts.length;
    const res = await page.evaluate((lutId) => {
      const s = document.querySelector('select[aria-label="LUT"]');
      if (!s) return 'no-select';
      const o = [...s.options].find((x) => x.value === lutId);
      if (!o) return 'no-option';
      if (o.disabled) return 'option-disabled';
      s.value = lutId;
      s.dispatchEvent(new Event('change', { bubbles: true }));
      return 'changed';
    }, id);
    await waitFor(() => out.puts.length > before, 30000, `${kind} LUT PUT /edit`);
    await sleep(400);
    out.steps[`lut_${kind}`] = {
      res,
      puts: out.puts.slice(before),
      stored: (await api(`/api/photos/${m.p2}/edit`)).body?.lut || null,
    };
  }

  // 3) Exposure and Curve on 调整 tab
  await page.evaluate(() => import('/assets/viewer.js').then((v) => v.openViewerTab('adjust')));
  await page.waitForSelector('input[type=range][aria-label="曝光"]', { timeout: 15000 });
  {
    const before = out.puts.length;
    const ex = page.locator('input[type=range][aria-label="曝光"]').first();
    await ex.focus();
    for (let i = 0; i < 5; i += 1) await page.keyboard.press('ArrowRight');
    await ex.dispatchEvent('change');
    await waitFor(() => out.puts.length > before, 30000, 'exposure PUT /edit');
    await sleep(400);
    out.steps.exposure = {
      puts: out.puts.slice(before),
      stored: (await api(`/api/photos/${m.p2}/edit`)).body?.adjust || null,
    };
  }
  {
    const before = out.puts.length;
    const svg = page.locator('svg.curve').first();
    await svg.scrollIntoViewIfNeeded();
    await sleep(200);
    const box = await svg.boundingBox();
    if (box) {
      await page.mouse.move(box.x + box.width * 0.5, box.y + box.height * 0.5);
      await page.mouse.down();
      await page.mouse.move(box.x + box.width * 0.5, box.y + box.height * 0.32, { steps: 6 });
      await page.mouse.up();
    }
    await waitFor(() => out.puts.length > before, 30000, 'curve PUT /edit');
    await sleep(400);
    out.steps.curve = {
      box: !!box,
      puts: out.puts.slice(before),
      stored: (await api(`/api/photos/${m.p2}/edit`)).body?.curve || null,
    };
  }

  // 4) Reload -> check stage, recipe state, and edited thumbnail
  out.renders.length = 0;
  const rawBeforeReload = out.rawReqs.length;
  const bakeBeforeReload = out.bakeReqs.length;
  await page.goto(`${m.base}/`);
  await page.waitForSelector('.top, .grid, .empty', { timeout: 30000 });
  await openPhoto();
  await page.evaluate(() => import('/assets/viewer.js').then((v) => v.openViewerTab('recipe')));
  await page.waitForFunction(
    () => {
      const cards = [...document.querySelectorAll('.lcard')];
      return cards.length > 0 && cards.every((c) => !c.disabled && c.getAttribute('aria-disabled') !== 'true');
    },
    null,
    { timeout: 30000 },
  );
  out.steps.afterReload = {
    edit: (await api(`/api/photos/${m.p2}/edit`)).body,
    panel: await panelState(),
    renders: out.renders.filter(isStageRender),
    rawReqs: out.rawReqs.slice(rawBeforeReload),
    bakeReqs: out.bakeReqs.slice(bakeBeforeReload),
    stage: await captureStageImage(m.stageAfterReloadPath),
  };

  // 5) Default export from the export dialog
  await page.evaluate(() => import('/assets/viewer.js').then((v) => v.openViewerTab('export')));
  await page.waitForSelector('.modal-foot button', { timeout: 15000 });
  const exBefore = out.exports.length;
  const clicked = await page.evaluate(() => {
    const b = [...document.querySelectorAll('.modal-foot button')].find((x) => x.textContent.trim() === '导出');
    if (!b) return false;
    b.click();
    return true;
  });
  await waitFor(() => out.exports.length > exBefore, 90000, 'export POST response');
  out.steps.export = { clicked, results: out.exports.slice(exBefore) };

  await page.close();
} catch (e) {
  out.error = line(e);
} finally {
  writeFileSync(m.out, JSON.stringify(out, null, 1));
  await browser.close();
}
console.log('done');
