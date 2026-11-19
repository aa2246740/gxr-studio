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
    await page.waitForSelector('.edit-panel', { timeout: 90000 });
    await page.waitForFunction(() => document.querySelectorAll('.lcard').length > 0, null, { timeout: 90000 });
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
  await page.waitForSelector('.top, .grid, .empty', { timeout: 90000 });
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
      { timeout: 90000 },
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

  // 3b) Startup race: the viewer's panel is drawn before the first camera-data status arrives.
  // Delay /api/camdata/status so the recipe cards are drawn disabled; they must come alive once
  // the status lands (viewer_camdata.js rebuilds the recipe pane on 'camdata-ready-changed').
  await page.route('**/api/camdata/status', async (route) => {
    await sleep(8000);
    // A poll still waiting here when the route is removed must not crash the driver.
    await route.continue().catch(() => {});
  });
  await page.goto(`${m.base}/`);
  await page.waitForSelector('.top, .grid, .empty', { timeout: 90000 });
  await page.evaluate((id) => {
    location.hash = `#/photo/${id}`;
  }, m.p2);
  await page.waitForFunction(
    (pid) =>
      import('/assets/viewer.js').then((v) => {
        const c = v.currentView();
        return !!c && c.photo.id === pid && !!c.ed;
      }),
    m.p2,
    { timeout: 90000 },
  );
  await page.evaluate(() => import('/assets/viewer.js').then((v) => v.openViewerTab('recipe')));
  await page.waitForFunction(() => document.querySelectorAll('.lcard').length > 0, null, { timeout: 90000 });
  const raceEarly = await panelState();
  await page.waitForFunction(() => import('/assets/camdata.js').then((cd) => cd.camdataReady()), null, { timeout: 90000 });
  let raceEnabled = true;
  try {
    await page.waitForFunction(
      () => {
        const cards = [...document.querySelectorAll('.lcard')];
        return cards.length > 0 && cards.every((c) => !c.disabled && c.getAttribute('aria-disabled') !== 'true');
      },
      null,
      { timeout: 20000 },
    );
  } catch {
    raceEnabled = false;
  }
  await page.unroute('**/api/camdata/status');
  out.steps.startupRace = { cards: raceEarly.cards, earlyDisabled: raceEarly.disabled, enabledAfterStatus: raceEnabled };

  // 3c) W01: a readiness change that arrives mid-drag must not rebuild the panel under the finger.
  // Start a change on the recipe tab (as a slider press does), fire the event, and check the cards are the
  // same nodes and the refresh is deferred; endChange then runs it.
  out.steps.midDragGuard = await page.evaluate(async () => {
    const [v, pv, st] = await Promise.all([import('/assets/viewer.js'), import('/assets/preview.js'), import('/assets/state.js')]);
    const view = v.currentView();
    const before = document.querySelector('.lcard');
    pv.beginChange(view);
    st.emit('camdata-ready-changed');
    const kept = !!before && before.isConnected && document.querySelector('.lcard') === before;
    const deferred = view.panesStale === true;
    pv.endChange(view);
    const after = document.querySelector('.lcard');
    return { kept, deferred, flushed: view.panesStale === false, rebuilt: !!after && after !== before };
  });

  // 3d) On another tab the event must not touch the open pane; the recipe pane is rebuilt when shown again.
  out.steps.otherTabGuard = await page.evaluate(async () => {
    const [v, st] = await Promise.all([import('/assets/viewer.js'), import('/assets/state.js')]);
    const view = v.currentView();
    const oldCard = document.querySelector('.lcard');
    v.openViewerTab('adjust');
    await new Promise((r) => setTimeout(r, 50));
    const slider = document.querySelector('input[type=range]');
    st.emit('camdata-ready-changed');
    const untouched = !!slider && slider.isConnected;
    const dirty = view.panes.recipe.dirty === true;
    v.openViewerTab('recipe');
    await new Promise((r) => setTimeout(r, 50));
    const card = document.querySelector('.lcard');
    return { untouched, dirty, rebuiltOnShow: !!card && card !== oldCard };
  });

  // 3d2) Focus inside the recipe pane: the rebuild waits until focus leaves the pane (not on an internal move).
  out.steps.recipeFocusGuard = await page.evaluate(async () => {
    const [v, st] = await Promise.all([import('/assets/viewer.js'), import('/assets/state.js')]);
    v.openViewerTab('recipe');
    await new Promise((r) => setTimeout(r, 50));
    const view = v.currentView();
    const content = view.panes.recipe.content;
    const fields = [...content.querySelectorAll('select, input, button')].filter((el) => !el.disabled);
    if (fields.length < 2) return { field: false, count: fields.length };
    const [field, other] = fields;
    const before = document.querySelector('.lcard');
    field.focus();
    st.emit('camdata-ready-changed');
    await new Promise((r) => setTimeout(r, 30));
    const waited = document.querySelector('.lcard') === before;
    other.focus(); // a move inside the pane must keep waiting
    await new Promise((r) => setTimeout(r, 50));
    const waitedAfterMove = document.querySelector('.lcard') === before && document.activeElement === other;
    other.blur();
    await new Promise((r) => setTimeout(r, 50));
    const after = document.querySelector('.lcard');
    return { field: true, waited, waitedAfterMove, rebuiltAfterBlur: !!after && after !== before };
  });

  // 3e) A change in progress on another tab (e.g. stamp text) must not have its pane rebuilt when it ends.
  out.steps.stampTabGuard = await page.evaluate(async () => {
    const [v, pv, st] = await Promise.all([import('/assets/viewer.js'), import('/assets/preview.js'), import('/assets/state.js')]);
    const view = v.currentView();
    v.openViewerTab('stamp');
    await new Promise((r) => setTimeout(r, 50));
    const node = view.panes.stamp.content;
    pv.beginChange(view);
    st.emit('camdata-ready-changed');
    pv.endChange(view);
    await new Promise((r) => setTimeout(r, 50));
    const same = !!node && view.panes.stamp.content === node && node.isConnected;
    const dirty = view.panes.recipe.dirty === true;
    v.openViewerTab('recipe');
    await new Promise((r) => setTimeout(r, 50));
    return { same, dirty };
  });

  // 3f) Focus in a text field outside the recipe pane (e.g. a settings dialog) must not leave the recipe pane stale.
  out.steps.focusGuard = await page.evaluate(async () => {
    const [v, st] = await Promise.all([import('/assets/viewer.js'), import('/assets/state.js')]);
    const oldCard = document.querySelector('.lcard');
    v.openViewerTab('adjust');
    await new Promise((r) => setTimeout(r, 50));
    const field = document.createElement('input');
    field.type = 'text';
    document.body.append(field);
    field.focus();
    st.emit('camdata-ready-changed');
    field.blur();
    field.remove();
    v.openViewerTab('recipe');
    await new Promise((r) => setTimeout(r, 50));
    const card = document.querySelector('.lcard');
    return { rebuiltOnShow: !!card && card !== oldCard };
  });

  // 4) Reload -> check stage, recipe state, and edited thumbnail
  out.renders.length = 0;
  const rawBeforeReload = out.rawReqs.length;
  const bakeBeforeReload = out.bakeReqs.length;
  await page.goto(`${m.base}/`);
  await page.waitForSelector('.top, .grid, .empty', { timeout: 90000 });
  await openPhoto();
  await page.evaluate(() => import('/assets/viewer.js').then((v) => v.openViewerTab('recipe')));
  await page.waitForFunction(
    () => {
      const cards = [...document.querySelectorAll('.lcard')];
      return cards.length > 0 && cards.every((c) => !c.disabled && c.getAttribute('aria-disabled') !== 'true');
    },
    null,
    { timeout: 90000 },
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

  // 6) GPU on: 第 2 张 (server) → 双联 (GPU) → 第 1 张 must never show the leftover 第 2 张 image
  if (m.gpu) {
    await page.evaluate(() => document.querySelector('.dialog .dh button')?.click());
    await sleep(300);
    const viewState = () =>
      page.evaluate(() =>
        import('/assets/viewer.js').then((v) => {
          const c = v.currentView();
          const img = document.querySelector('.stage-area .pv-img');
          return {
            pv: c?.pairView || 'diptych',
            gpuActive: c?.gpuActive ?? null,
            busy: !!c?.ed?.busy,
            finalUrl: c?.ed?.urls?.final || null,
            imgSrc: img ? img.getAttribute('src') : null,
            imgReady: !!img && img.complete && img.naturalWidth > 0,
          };
        }),
      );
    const pick = (idx) => page.click(`.viewbar [aria-label="半格视图"] button:nth-child(${idx})`);
    await pick(3);
    await waitFor(async () => {
      const s = await viewState();
      return s.pv === 'second' && !s.busy && s.imgReady && !!s.finalUrl && s.imgSrc === s.finalUrl;
    }, 90000, 'second view render');
    const leftover = (await viewState()).finalUrl;
    await pick(1);
    await waitFor(async () => {
      const s = await viewState();
      return s.pv === 'diptych' && s.gpuActive === 'final' && !s.busy;
    }, 90000, 'diptych back on the GPU');
    await pick(2);
    const seen = [];
    let reachedFirst = false;
    const t0 = Date.now();
    while (Date.now() - t0 < 90000) {
      const s = await viewState();
      seen.push(s.imgSrc);
      if (s.pv === 'first' && !s.busy && s.imgReady && s.imgSrc && s.imgSrc === s.finalUrl && s.imgSrc !== leftover) {
        reachedFirst = true;
        break;
      }
      await sleep(25);
    }
    out.steps.pairViewStale = { leftover: !!leftover, showedLeftover: seen.includes(leftover), reachedFirst, samples: seen.length };
    await pick(1);
  }

  await page.close();
} catch (e) {
  out.error = line(e);
} finally {
  writeFileSync(m.out, JSON.stringify(out, null, 1));
  await browser.close();
}
console.log('done');
