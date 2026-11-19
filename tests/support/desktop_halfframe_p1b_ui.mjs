// Headless desktop browser driver for halfframe-p1b-ui acceptance & non-halfframe DOM snapshot.
// Privacy: NEVER prints photo names, capture times, or EXIF. Masks names/dates in screenshots.
import { readFileSync, writeFileSync, mkdirSync } from 'node:fs';
import { dirname } from 'node:path';
import { pathToFileURL } from 'node:url';

const [runtime, manifestPath] = process.argv.slice(2);
const m = JSON.parse(readFileSync(manifestPath, 'utf8'));
const { launchPinnedChromium, verifyPinnedRuntime } = await import(pathToFileURL(runtime).href);
verifyPinnedRuntime();

const browser = await launchPinnedChromium({});
const out = { pageErrors: [], mode: m.mode, steps: {}, requests: { puts: [], renders: [], exports: [], batchExports: [] } };
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

async function maskedScreenshot(page, destPath) {
  if (!destPath) return;
  mkdirSync(dirname(destPath), { recursive: true });
  await page.evaluate(() => {
    const saved = [];
    const maskEl = (el, replacement) => {
      if (!el) return;
      saved.push([el, el.textContent, false]);
      el.textContent = replacement;
    };
    document.querySelectorAll('.day h3').forEach((el) => {
      const span = el.querySelector('span');
      const countText = span ? span.textContent : '';
      saved.push([el, el.innerHTML, true]);
      el.textContent = '拍摄日（样稿隐藏）';
      if (span) {
        const s = document.createElement('span');
        s.textContent = countText;
        el.append(' ', s);
      }
    });
    document.querySelectorAll('.sidebar .it').forEach((btn) => {
      const first = btn.firstElementChild;
      if (first && /\d{4}[-/年]/.test(first.textContent || '')) maskEl(first, '拍摄日（样稿隐藏）');
    });
    const meta = document.querySelector('.tb-title .meta');
    if (meta) maskEl(meta, '第 1 张 + 第 2 张（文件名隐藏）');
    document.querySelectorAll('.dialog .seg button').forEach((btn) => {
      if (/\d{4}[-/年]\d{1,2}[-/月]\d{1,2}/.test(btn.textContent || '')) maskEl(btn, '拍摄日（样稿隐藏）的全部');
    });
    document.querySelectorAll('.dialog .row.form .ctl .num, .dialog .row.form .ctl input, .dialog .row.form .ctl .small, .dialog .row.form .ctl .dest-resolved').forEach((el) => {
      if (el.tagName === 'INPUT') {
        saved.push([el, el.value, 'value']);
        el.value = '~/Pictures/GXR 导出';
      } else if (/[A-Z]\d{4,}|_gxr/.test(el.textContent || '')) {
        maskEl(el, '<文件名隐藏>_gxr.jpg');
      } else if (/\/Volumes\/|\/tmp\/|pytest/.test(el.textContent || '')) {
        maskEl(el, '~/Pictures/GXR 导出');
      }
    });
    window.__p1bRestoreMask = () => {
      for (const [el, val, kind] of saved.reverse()) {
        if (kind === true) el.innerHTML = val;
        else if (kind === 'value') el.value = val;
        else el.textContent = val;
      }
      delete window.__p1bRestoreMask;
    };
  });
  await page.screenshot({ path: destPath, fullPage: false });
  await page.evaluate(() => { if (window.__p1bRestoreMask) window.__p1bRestoreMask(); });
}

async function runSnapshot(page) {
  await page.goto(`${m.base}/?token=${encodeURIComponent(m.token)}#/photos`, { waitUntil: 'domcontentloaded' });
  await waitFor(() => page.evaluate(() => document.querySelectorAll('.grid .cell').length > 0), 30000, 'gallery cells');
  await sleep(300);

  const gallery = await page.evaluate(() => {
    const normDate = (s) => (s || '').replace(/\d{4}[-年/.]\d{1,2}[-月/.]\d{1,2}日?/g, '<DATE>');
    const sidebar = [...document.querySelectorAll('.sidebar > *')].map((el) => ({
      tag: el.tagName.toLowerCase(),
      cls: el.className,
      text: normDate(el.textContent.trim().replace(/\s+/g, ' ')),
    }));
    const libbar = {
      title: document.querySelector('.libbar .t')?.textContent?.trim() || '',
      count: document.querySelector('#lib-count')?.textContent?.trim() || '',
    };
    const cells = [...document.querySelectorAll('.grid .cell')].map((el) => {
      const img = el.querySelector('img');
      return {
        cls: el.className.replace(/\bfocus\b/g, '').trim(),
        badges: [...el.querySelectorAll('.b .badge')].map((b) => b.textContent.trim()),
        objectFit: img ? getComputedStyle(img).objectFit : null,
      };
    });
    return { sidebar, libbar, cells };
  });

  // Select first photo and open export dialog to inspect row labels
  await page.click('.grid .cell .ck');
  await sleep(150);
  await page.click('#btn-export-top');
  await waitFor(() => page.evaluate(() => !!document.querySelector('.dialog .export-form')), 15000, 'export dialog');
  const exportRows = await page.evaluate(() => ({
    title: document.querySelector('.dialog .dh h2')?.textContent?.trim() || '',
    rows: [...document.querySelectorAll('.dialog .row.form > .lab')].map((el) => el.textContent.trim()),
  }));

  out.steps.snapshot = { gallery, exportRows };
}

async function runCard250(page) {
  page.on('response', async (res) => {
    const u = new URL(res.url());
    const p = u.pathname;
    const req = res.request();
    if (req.method() === 'PUT' && p.endsWith('/edit')) {
      out.requests.puts.push({ status: res.status(), path: p });
    }
    if (p.endsWith('/render')) {
      out.requests.renders.push({
        method: req.method(),
        status: res.status(),
        view: u.searchParams.get('view') || null,
        stage: u.searchParams.get('stage') || null,
        label: res.headers()['x-gxr-label'] || null,
      });
    }
    if (req.method() === 'POST' && p.endsWith('/export')) {
      let body = null;
      try { body = await res.json(); } catch { /* ignore */ }
      out.requests.exports.push({
        status: res.status(),
        fileCount: Array.isArray(body?.files) ? body.files.length : (body?.path ? 1 : 0),
        hasPath: !!body?.path,
      });
    }
    if (req.method() === 'POST' && p === '/api/export') {
      let body = null;
      try { body = await res.json(); } catch { /* ignore */ }
      let sent = null;
      try { sent = JSON.parse(req.postData() || '{}'); } catch { /* ignore */ }
      out.requests.batchExports.push({
        status: res.status(),
        sentCount: sent?.ids?.length || 0,
        halfframe: sent?.halfframe || null,
        jobId: body?.id || null,
      });
    }
  });

  const api = (path, opts) =>
    page.evaluate(async ([p, o]) => {
      const r = await fetch(p, o || {});
      const ct = r.headers.get('content-type') || '';
      return { status: r.status, body: ct.includes('json') ? await r.json() : null };
    }, [path, opts]);

  // 1. Gallery
  await page.goto(`${m.base}/?token=${encodeURIComponent(m.token)}#/photos`, { waitUntil: 'domcontentloaded' });
  await waitFor(() => page.evaluate(() => document.querySelectorAll('.grid .cell').length >= 8), 30000, 'card250 gallery');
  await waitFor(() => page.evaluate(() => {
    const imgs = [...document.querySelectorAll('.grid .cell img')];
    return imgs.length > 0 && imgs.every((img) => img.complete && img.naturalWidth > 0);
  }), 30000, 'gallery thumbs loaded');
  await sleep(250);

  const gallery = await page.evaluate((meta) => {
    const cells = [...document.querySelectorAll('.grid .cell')];
    const items = cells.map((el) => {
      const img = el.querySelector('img');
      return {
        id: el.dataset.id,
        isPair: el.classList.contains('pair'),
        objectFit: img ? getComputedStyle(img).objectFit : null,
        badges: [...el.querySelectorAll('.b .badge')].map((b) => b.textContent.trim()),
      };
    });
    const filterGrp = [...document.querySelectorAll('.sidebar .grp')].map((g) => g.textContent.trim());
    const hfBtn = [...document.querySelectorAll('.sidebar .it')].find((b) => b.textContent.includes('半格'));
    return {
      totalCells: cells.length,
      visibleIds: items.map((x) => x.id),
      pairCells: items.filter((x) => x.isPair),
      halfFirstCells: items.filter((x) => x.badges.includes('半格·未成对')),
      xpanCell: items.find((x) => x.id === meta.xpanId) || null,
      shownFirstIds: items.filter((x) => meta.pairFirstIds.includes(x.id)).map((x) => x.id),
      libCount: document.querySelector('#lib-count')?.textContent?.trim() || '',
      sidebarGroups: filterGrp,
      hfFilterText: hfBtn ? [...hfBtn.children].map((c) => c.textContent.trim()).join(' ') : null,
    };
  }, { pairFirstIds: m.pairFirstIds, xpanId: m.xpanId });
  out.steps.gallery = gallery;

  if (m.screenshots?.gallery) await maskedScreenshot(page, m.screenshots.gallery);

  // Filter click -> 3 pairs, then back to 全部照片
  await page.evaluate(() => {
    const btn = [...document.querySelectorAll('.sidebar .it')].find((b) => b.textContent.includes('半格'));
    if (btn) btn.click();
  });
  await sleep(250);
  const filteredCount = await page.evaluate(() => document.querySelectorAll('.grid .cell').length);
  await page.evaluate(() => {
    const btn = [...document.querySelectorAll('.sidebar .it')].find((b) => b.textContent.includes('全部照片'));
    if (btn) btn.click();
  });
  await sleep(250);
  const restoredCount = await page.evaluate(() => document.querySelectorAll('.grid .cell').length);
  out.steps.filter = { filteredCount, restoredCount };

  // 2. Selection + batch export of 1 pair cell
  const pairTick = `.grid .cell[data-id="${m.primaryPairSecondId}"] .ck`;
  await page.click(pairTick);
  await sleep(200);
  await page.click('#btn-export-top');
  await waitFor(() => page.evaluate(() => !!document.querySelector('.dialog .export-form')), 15000, 'batch export dialog');
  const selExportDialog = await page.evaluate(() => {
    const hfRow = [...document.querySelectorAll('.dialog .row.form')].find((r) => !r.hidden && r.querySelector('.lab')?.textContent?.trim() === '半格');
    return {
      title: document.querySelector('.dialog .dh h2')?.textContent?.trim() || '',
      hasHfRow: !!hfRow,
      pressedMode: hfRow?.querySelector('button[aria-pressed="true"]')?.textContent?.trim() || null,
    };
  });
  const batchIdx0 = out.requests.batchExports.length;
  const singleIdx0 = out.requests.exports.length;
  await page.click('.dialog .modal-foot button.primary');
  await waitFor(() => page.evaluate(() => {
    const res = document.querySelector('.dialog .export-result .res-main');
    return !!res && (res.textContent.includes('导出') || res.textContent.includes('已导出'));
  }), 60000, 'batch export done');
  await sleep(300);
  const batchResultText = await page.evaluate(() => document.querySelector('.dialog .export-result .res-main')?.textContent?.trim() || '');
  await page.click('.dialog .dh button');
  await sleep(200);
  await page.keyboard.press('Escape');
  await sleep(200);
  out.steps.selectionBatchExport = {
    dialog: selExportDialog,
    batchReqs: out.requests.batchExports.slice(batchIdx0),
    singleReqs: out.requests.exports.slice(singleIdx0),
    resultText: batchResultText,
  };

  // 3. Editor for primary pair
  await page.goto(`${m.base}/?token=${encodeURIComponent(m.token)}#/photo/${m.primaryPairSecondId}`, { waitUntil: 'domcontentloaded' });
  await waitFor(() => page.evaluate(() => {
    const img = document.querySelector('.stage-area .pv-img');
    const cards = document.querySelectorAll('.lcard');
    return !!img && img.complete && img.naturalWidth > 0 && cards.length > 0;
  }), 60000, 'editor pair loaded');
  // Wait for visible look-card thumb canvases to be painted in server mode
  await waitFor(() => page.evaluate(() => {
    const cvs = [...document.querySelectorAll('.lcard .thumb canvas')];
    return cvs.filter((c) => c.width > 0 && c.height > 0).length >= 4;
  }), 60000, 'look card thumbs');
  await sleep(300);

  const editorInitial = await page.evaluate(({ p1Name, p2Name, pairFirstIds }) => {
    const name = document.querySelector('.tb-title .name')?.textContent?.trim() || '';
    const meta = document.querySelector('.tb-title .meta')?.textContent?.trim() || '';
    const badge = document.querySelector('.tb-title .badge.pair')?.textContent?.trim() || '';
    const stageLabels = [...document.querySelectorAll('.stage-labels .tag')].map((t) => t.textContent.trim());
    const pressedCards = document.querySelectorAll('.lcard[aria-pressed="true"]').length;
    const totalCards = document.querySelectorAll('.lcard').length;
    const loadedThumbs = [...document.querySelectorAll('.lcard .thumb canvas')].filter((c) => c.width > 0 && c.height > 0).length;
    const recipeText = document.querySelector('.cur-look')?.textContent?.trim().replace(/\s+/g, ' ') || '';
    const pairHint = document.querySelector('.pair-hint')?.textContent?.trim().replace(/\s+/g, ' ') || '';
    const viewSeg = document.querySelector('.viewbar [aria-label="半格视图"]');
    const viewButtons = viewSeg ? [...viewSeg.querySelectorAll('button')].map((b) => ({ text: b.textContent.trim(), pressed: b.getAttribute('aria-pressed') === 'true' })) : [];
    const filmstripIds = [...document.querySelectorAll('.filmstrip .fthumb')].map((b) => b.dataset.id);
    const histShadowLit = document.querySelector('.hist-head .hist-clip[data-side="shadow"]')?.dataset.lit === 'true';
    return {
      titleName: name,
      hasBothNames: meta.includes(p1Name) && meta.includes(p2Name),
      pairBadge: badge,
      stageLabels,
      pressedCards,
      totalCards,
      loadedThumbs,
      recipeText,
      pairHint,
      viewButtons,
      filmstripCount: filmstripIds.length,
      filmstripHasFirst: filmstripIds.some((id) => pairFirstIds.includes(id)),
      histShadowLit,
    };
  }, { p1Name: m.primaryPairFirstName, p2Name: m.primaryPairSecondName, pairFirstIds: m.pairFirstIds });

  // Original toggle stage label check
  await page.keyboard.press('\\');
  await waitFor(() => page.evaluate(() => {
    const labels = [...document.querySelectorAll('.stage-labels .tag')].map((t) => t.textContent.trim());
    return labels.some((t) => t.includes('原图'));
  }), 15000, 'original stage label');
  const origStageLabels = await page.evaluate(() => [...document.querySelectorAll('.stage-labels .tag')].map((t) => t.textContent.trim()));
  await page.keyboard.press('\\');
  await sleep(300);

  if (m.screenshots?.editor) await maskedScreenshot(page, m.screenshots.editor);

  // Info tab lists both files
  await page.click('#tab-info');
  await sleep(250);
  const infoTab = await page.evaluate(({ p1Name, p2Name }) => {
    const text = document.querySelector('.panel-body')?.textContent || '';
    return {
      hasFirstLabel: text.includes('第 1 张'),
      hasSecondLabel: text.includes('第 2 张'),
      hasBothNames: text.includes(p1Name) && text.includes(p2Name),
    };
  }, { p1Name: m.primaryPairFirstName, p2Name: m.primaryPairSecondName });
  await page.click('#tab-recipe');
  await sleep(200);

  // Viewbar switch: 第 1 张 / 第 2 张 / 双联
  const sampleStageHash = () => page.evaluate(() => {
    const img = document.querySelector('.stage-area .pv-img');
    if (!img || !img.naturalWidth) return null;
    const c = document.createElement('canvas');
    c.width = 48; c.height = 32;
    const ctx = c.getContext('2d');
    ctx.drawImage(img, 0, 0, 48, 32);
    const d = ctx.getImageData(0, 0, 48, 32).data;
    let sum = 0;
    const samples = [];
    for (let i = 0; i < d.length; i += 48) { sum += d[i]; samples.push(d[i]); }
    return { w: img.naturalWidth, h: img.naturalHeight, sum, samples };
  });

  const dipSample = await sampleStageHash();
  const putsBeforeView = out.requests.puts.length;
  const rendersBeforeFirst = out.requests.renders.length;
  await page.click('.viewbar [aria-label="半格视图"] button:nth-child(2)');
  await waitFor(() => out.requests.renders.slice(rendersBeforeFirst).some((r) => r.view === 'first' && r.status === 200), 45000, 'view=first render');
  await sleep(400);
  const firstView = await page.evaluate(() => {
    const lcard = document.querySelector('.lcard');
    const lut = document.querySelector('.panel-body select');
    return {
      pressed: document.querySelector('.viewbar [aria-label="半格视图"] button[aria-pressed="true"]')?.textContent?.trim() || '',
      cardDisabled: lcard ? (lcard.disabled || lcard.getAttribute('aria-disabled') === 'true') : null,
      selectDisabled: lut ? lut.disabled : null,
      panelReadonly: !!document.querySelector('.panel.pair-readonly, .panel-body.readonly, [data-readonly="1"]'),
    };
  });
  const firstSample = await sampleStageHash();
  await page.evaluate(() => { const c = document.querySelector('.lcard'); if (c) c.click(); });
  await sleep(300);

  const rendersBeforeSecond = out.requests.renders.length;
  await page.click('.viewbar [aria-label="半格视图"] button:nth-child(3)');
  await waitFor(() => out.requests.renders.slice(rendersBeforeSecond).some((r) => r.view === 'second' && r.status === 200), 45000, 'view=second render');
  await sleep(400);
  const secondSample = await sampleStageHash();
  const putsDuringSingleViews = out.requests.puts.length - putsBeforeView;

  await page.click('.viewbar [aria-label="半格视图"] button:nth-child(1)');
  await sleep(400);
  const backToDiptych = await page.evaluate(() => {
    const lcard = document.querySelector('.lcard');
    return {
      pressed: document.querySelector('.viewbar [aria-label="半格视图"] button[aria-pressed="true"]')?.textContent?.trim() || '',
      cardDisabled: lcard ? (lcard.disabled || lcard.getAttribute('aria-disabled') === 'true') : null,
    };
  });

  // Step ←/→ across visible items and verify we never land on any pairFirstId
  const visitedIds = [];
  visitedIds.push(await page.evaluate(() => location.hash.split('/').pop()));
  for (let i = 0; i < 4; i++) {
    await page.keyboard.press('ArrowLeft');
    await sleep(350);
    visitedIds.push(await page.evaluate(() => location.hash.split('/').pop()));
  }
  for (let i = 0; i < 6; i++) {
    await page.keyboard.press('ArrowRight');
    await sleep(350);
    visitedIds.push(await page.evaluate(() => location.hash.split('/').pop()));
  }

  out.steps.editor = {
    initial: editorInitial,
    origStageLabels,
    infoTab,
    viewbar: {
      firstView,
      backToDiptych,
      putsDuringSingleViews,
      dipSample: dipSample ? { w: dipSample.w, h: dipSample.h, sum: dipSample.sum } : null,
      firstSample: firstSample ? { w: firstSample.w, h: firstSample.h, sum: firstSample.sum } : null,
      secondSample: secondSample ? { w: secondSample.w, h: secondSample.h, sum: secondSample.sum } : null,
      diffFirstSecond: firstSample && secondSample
        ? firstSample.samples.reduce((acc, v, i) => acc + Math.abs(v - secondSample.samples[i]), 0) / firstSample.samples.length
        : 0,
    },
    nav: {
      visitedCount: visitedIds.length,
      landedOnFirst: visitedIds.some((id) => m.pairFirstIds.includes(id)),
    },
  };

  // 4. Export dialog from editor on pair
  await page.goto(`${m.base}/?token=${encodeURIComponent(m.token)}#/photo/${m.primaryPairSecondId}`, { waitUntil: 'domcontentloaded' });
  await waitFor(() => page.evaluate(async () => {
    const img = document.querySelector('.stage-area .pv-img');
    const { state } = await import('/assets/state.js');
    return !!img && img.complete && img.naturalWidth > 0 && state.camdata?.ready === true;
  }), 60000, 'reopen pair for export');
  await page.click('#btn-export-top');
  await waitFor(() => page.evaluate(() => !!document.querySelector('.dialog .export-form')), 15000, 'pair export dialog');
  await sleep(300);

  if (m.screenshots?.export) await maskedScreenshot(page, m.screenshots.export);

  const readExportDialog = () => page.evaluate(() => {
    const visibleRows = [...document.querySelectorAll('.dialog .row.form')].filter((r) => !r.hidden);
    const rows = Object.fromEntries(visibleRows.map((r) => [
      r.querySelector('.lab')?.textContent?.trim() || '',
      r.querySelector('.ctl')?.textContent?.trim().replace(/\s+/g, ' ') || '',
    ]));
    const hfRow = visibleRows.find((r) => r.querySelector('.lab')?.textContent?.trim() === '半格');
    const hdrRow = visibleRows.find((r) => r.querySelector('.lab')?.textContent?.trim() === 'HDR');
    const hdrInput = hdrRow?.querySelector('input[type="checkbox"]');
    const note = document.querySelector('.dialog .export-form > p.hf-note');
    return {
      title: document.querySelector('.dialog .dh h2')?.textContent?.trim() || '',
      hasHfRow: !!hfRow,
      pressedMode: hfRow?.querySelector('button[aria-pressed="true"]')?.textContent?.trim() || null,
      sizeText: rows['成品'] || '',
      hdrDisabled: hdrInput ? hdrInput.disabled : null,
      hdrText: rows['HDR'] || '',
      footnote: note && !note.hidden ? note.textContent.trim() : '',
    };
  });

  const exportDefault = await readExportDialog();
  await page.evaluate(() => {
    const btn = [...document.querySelectorAll('.dialog .row.form button')].find((b) => b.textContent.trim() === '两张单格');
    if (btn) btn.click();
  });
  await sleep(250);
  const exportSingles = await readExportDialog();
  await page.evaluate(() => {
    const btn = [...document.querySelectorAll('.dialog .row.form button')].find((b) => b.textContent.trim() === '都要');
    if (btn) btn.click();
  });
  await sleep(250);
  const exportBoth = await readExportDialog();
  await page.evaluate(() => {
    const btn = [...document.querySelectorAll('.dialog .row.form button')].find((b) => b.textContent.trim() === '两张单格');
    if (btn) btn.click();
  });
  await sleep(250);
  const expIdx0 = out.requests.exports.length;
  await page.click('.dialog .modal-foot button.primary');
  await waitFor(() => out.requests.exports.length > expIdx0, 60000, 'singles export response');
  await sleep(400);
  await page.click('.dialog .dh button');
  await sleep(250);
  await page.click('#btn-export-top');
  await waitFor(() => page.evaluate(() => !!document.querySelector('.dialog .export-form')), 15000, 'reopen export dialog');
  await sleep(250);
  const exportReopened = await readExportDialog();
  await page.click('.dialog .dh button');
  await sleep(200);

  // Check ordinary (XPan) photo export dialog has NO 半格 row
  await page.goto(`${m.base}/?token=${encodeURIComponent(m.token)}#/photo/${m.xpanId}`, { waitUntil: 'domcontentloaded' });
  await waitFor(() => page.evaluate(() => {
    const img = document.querySelector('.stage-area .pv-img');
    return !!img && img.complete && img.naturalWidth > 0;
  }), 45000, 'xpan loaded');
  await page.click('#btn-export-top');
  await waitFor(() => page.evaluate(() => !!document.querySelector('.dialog .export-form')), 15000, 'xpan export dialog');
  const exportXpan = await readExportDialog();
  await page.click('.dialog .dh button');
  await sleep(200);

  // Check XPan crop preset menu does NOT show 取半格
  await page.click('#tab-crop');
  await waitFor(() => page.evaluate(() => !!document.querySelector('button.select[aria-label="比例"]')), 20000, 'xpan ratio btn');
  await page.click('button.select[aria-label="比例"]');
  await sleep(200);
  const xpanHasHalfCrop = await page.evaluate(() => [...document.querySelectorAll('.menu .mi')].some((b) => b.textContent.trim() === '取半格'));
  await page.keyboard.press('Escape');
  await sleep(150);

  out.steps.exportDialog = {
    exportDefault,
    exportSingles,
    exportBoth,
    singlesExportReqs: out.requests.exports.slice(expIdx0),
    exportReopened,
    exportXpan,
  };

  // 5. Crop preset 「取半格」 on a half_first shot
  await page.goto(`${m.base}/?token=${encodeURIComponent(m.token)}#/photo/${m.halfFirstId}`, { waitUntil: 'domcontentloaded' });
  await waitFor(() => page.evaluate(() => {
    const img = document.querySelector('.stage-area .pv-img');
    return !!img && img.complete && img.naturalWidth > 0;
  }), 45000, 'half_first loaded');
  await page.click('#tab-crop');
  await waitFor(() => page.evaluate(() => !!document.querySelector('button.select[aria-label="比例"]')), 20000, 'half_first ratio btn');
  await page.click('button.select[aria-label="比例"]');
  await waitFor(() => page.evaluate(() => [...document.querySelectorAll('.menu .mi')].some((b) => b.textContent.trim() === '取半格')), 15000, '取半格 option');
  await page.evaluate(() => {
    const btn = [...document.querySelectorAll('.menu .mi')].find((b) => b.textContent.trim() === '取半格');
    if (btn) btn.click();
  });
  await sleep(800);
  const cropState = await api(`/api/photos/${m.halfFirstId}/crop`);
  const ratioBtnText = await page.evaluate(() => document.querySelector('button.select[aria-label="比例"]')?.textContent?.trim() || '');
  out.steps.cropHalfFirst = {
    xpanHasHalfCrop,
    cropPressed: ratioBtnText === '取半格',
    ratioBtnText,
    crop: cropState.body,
  };
}

try {
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
  page.on('pageerror', (e) => out.pageErrors.push(line(e)));
  if (m.mode === 'snapshot') await runSnapshot(page);
  else await runCard250(page);
} catch (e) {
  out.error = line(e);
  try {
    const pages = browser.contexts().flatMap((c) => c.pages());
    if (pages[0]) {
      out.diag = await pages[0].evaluate(async () => {
        const panel = document.querySelector('.edit-panel');
        const thumbs = [...document.querySelectorAll('.lcard .thumb')];
        const { state } = await import('/assets/state.js');
        const { currentView } = await import('/assets/viewer.js');
        const v = currentView();
        return {
          hash: location.hash.split('/')[1] || '',
          camdataReady: state.camdata?.ready ?? null,
          gpuPreviewFlag: state.settings?.gpu_preview ?? null,
          edBusy: v?.ed?.busy ?? null,
          edV2: v?.ed?.v2 ?? null,
          caps: v?.ed?.caps ?? null,
          dataset: panel ? { ...panel.dataset } : null,
          thumbClasses: thumbs.slice(0, 4).map((t) => t.className),
          cardCount: thumbs.length,
        };
      });
    }
  } catch { /* ignore */ }
} finally {
  writeFileSync(m.out, JSON.stringify(out, null, 2));
  await browser.close();
}
