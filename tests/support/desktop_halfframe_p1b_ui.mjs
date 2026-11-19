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

async function runEditorSnapshot(page) {
  await page.goto(`${m.base}/?token=${encodeURIComponent(m.token)}#/photos`, { waitUntil: 'domcontentloaded' });
  await waitFor(() => page.evaluate(() => document.querySelectorAll('.grid .cell').length > 0), 30000, 'gallery cells');
  const firstId = await page.evaluate(() => document.querySelector('.grid .cell')?.dataset?.id || '');
  await page.goto(`${m.base}/?token=${encodeURIComponent(m.token)}#/photo/${firstId}`, { waitUntil: 'domcontentloaded' });
  await waitFor(() => page.evaluate(() => {
    const img = document.querySelector('.stage-area .pv-img');
    const gpu = document.querySelector('.stage-area .pv-gpu');
    const stageReady = (!!img && img.complete && img.naturalWidth > 0) || (!!gpu && gpu.width > 0);
    const thumbs = [...document.querySelectorAll('.filmstrip .fthumb img')];
    return stageReady && thumbs.length > 0 && thumbs.every((t) => t.complete && t.naturalWidth > 0);
  }), 45000, 'editor non-halfframe loaded');
  await sleep(300);

  out.steps.editorSnapshot = await page.evaluate(() => {
    const norm = (html) => (html || '')
      .replace(/\/api\/photos\/[0-9a-f]{8,}/g, '/api/photos/<ID>')
      .replace(/data-id="[0-9a-f]{8,}"/g, 'data-id="<ID>"')
      .replace(/R\d{4,}(?:_[0-9a-f]+)?(?:\.[A-Za-z0-9]+)?/g, '<NAME>')
      .replace(/\d{4}[-年/.]\d{1,2}[-月/.]\d{1,2}日?/g, '<DATE>')
      .replace(/\b\d{1,2}:\d{2}(?::\d{2})?\b/g, '<TIME>')
      .replace(/class="ready"/g, 'class="ready"');

    // Measure computed .fthumb .b position on a temporary non-pair filmstrip button
    const film = document.querySelector('.filmstrip');
    let fthumbBadgePos = null;
    if (film) {
      const probe = document.createElement('button');
      probe.type = 'button';
      probe.className = 'fthumb';
      const b = document.createElement('span');
      b.className = 'b';
      probe.append(b);
      film.append(probe);
      const cs = getComputedStyle(b);
      fthumbBadgePos = { left: cs.left, bottom: cs.bottom };
      probe.remove();
    }

    return {
      titlebar: norm(document.querySelector('#tb-edit')?.innerHTML || ''),
      viewbar: norm(document.querySelector('.viewbar')?.innerHTML || ''),
      filmstrip: norm(film?.innerHTML || ''),
      fthumbBadgePos,
      tooltabs: norm(document.querySelector('.tooltabs')?.innerHTML || ''),
      panelFoot: norm(document.querySelector('.panel-foot')?.innerHTML || ''),
      stripMaskCount: document.querySelectorAll('.pair-strip-mask').length,
    };
  });
}

async function runJpgOnlyP1(page) {
  page.on('response', async (res) => {
    const u = new URL(res.url());
    if (u.pathname.endsWith('/render')) {
      out.requests.renders.push({
        method: res.request().method(),
        status: res.status(),
        view: u.searchParams.get('view') || null,
        stage: u.searchParams.get('stage') || null,
        label: res.headers()['x-gxr-label'] || null,
      });
    }
  });

  const sampleCenter = () => page.evaluate(() => {
    const img = document.querySelector('.stage-area .pv-img');
    if (!img || !img.naturalWidth) return null;
    const c = document.createElement('canvas');
    c.width = 64; c.height = 42;
    const ctx = c.getContext('2d');
    ctx.drawImage(img, 0, 0, 64, 42);
    const d = ctx.getImageData(30, 6, 4, 30).data;
    let sum = 0;
    for (let i = 0; i < d.length; i += 4) sum += (d[i] + d[i + 1] + d[i + 2]) / 3;
    return { w: img.naturalWidth, h: img.naturalHeight, centerMean: sum / (d.length / 4) };
  });

  await page.goto(`${m.base}/?token=${encodeURIComponent(m.token)}#/photo/${m.pairSecondId}`, { waitUntil: 'domcontentloaded' });
  await waitFor(() => page.evaluate(() => {
    const img = document.querySelector('.stage-area .pv-img');
    return !!img && img.complete && img.naturalWidth > 0 && !!document.querySelector('.viewbar [aria-label="半格视图"]');
  }), 45000, 'jpgonly pair loaded');
  await sleep(300);

  const initial = await page.evaluate(() => ({
    pairHint: document.querySelector('.pair-hint')?.textContent?.trim() || '',
    noticeText: document.querySelector('.edit-panel .notice')?.textContent?.trim().replace(/\s+/g, ' ') || '',
    stripMaskCount: document.querySelectorAll('.pair-strip-mask').length,
  }));
  const dipCenter = await sampleCenter();

  const r0 = out.requests.renders.length;
  await page.click('.viewbar [aria-label="半格视图"] button:nth-child(2)');
  await waitFor(() => out.requests.renders.slice(r0).some((r) => r.view === 'first'), 30000, 'jpgonly view=first');
  await sleep(500);
  const firstReqs = out.requests.renders.slice(r0).filter((r) => r.view === 'first');
  const firstCenter = await sampleCenter();
  const firstMask = await page.evaluate(() => {
    const el = document.querySelector('.pair-strip-mask');
    const lcard = document.querySelector('.lcard');
    return {
      hasMask: !!el,
      cardDisabled: lcard ? (lcard.disabled || lcard.getAttribute('aria-disabled') === 'true') : null,
    };
  });

  const r1 = out.requests.renders.length;
  await page.click('.viewbar [aria-label="半格视图"] button:nth-child(3)');
  await waitFor(() => out.requests.renders.slice(r1).some((r) => r.view === 'second'), 30000, 'jpgonly view=second');
  await sleep(400);
  const secondReqs = out.requests.renders.slice(r1).filter((r) => r.view === 'second');

  // JPG-only half_first crop preset 「取半格」 -> camera-JPG column 1100
  await page.goto(`${m.base}/?token=${encodeURIComponent(m.token)}#/photo/${m.jpgHalfFirstId}`, { waitUntil: 'domcontentloaded' });
  await waitFor(() => page.evaluate(() => {
    const img = document.querySelector('.stage-area .pv-img');
    return !!img && img.complete && img.naturalWidth > 0;
  }), 45000, 'jpgonly half_first loaded');
  await page.click('#tab-crop');
  await waitFor(() => page.evaluate(() => !!document.querySelector('button.select[aria-label="比例"]')), 20000, 'jpgonly ratio btn');
  await page.click('button.select[aria-label="比例"]');
  await waitFor(() => page.evaluate(() => [...document.querySelectorAll('.menu .mi')].some((b) => b.textContent.trim() === '取半格')), 15000, 'jpgonly 取半格');
  await page.evaluate(() => {
    const btn = [...document.querySelectorAll('.menu .mi')].find((b) => b.textContent.trim() === '取半格');
    if (btn) btn.click();
  });
  await sleep(600);
  const cropBody = await page.evaluate(async (id) => (await fetch(`/api/photos/${id}/crop`)).json(), m.jpgHalfFirstId);

  out.steps.jpgOnlyP1 = {
    initial,
    dipCenter,
    firstReqs,
    firstCenter,
    firstMask,
    secondReqs,
    jpgHalfFirstCrop: cropBody,
  };
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
        sentIds: sent?.ids || [],
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

  // P2-1 + P3-5: Select pair + XPan -> switch to 半格 filter -> hidden XPan pruned with undo toast, wording unified
  const pairTick = `.grid .cell[data-id="${m.primaryPairSecondId}"] .ck`;
  const xpanTick = `.grid .cell[data-id="${m.xpanId}"] .ck`;
  await page.click(pairTick);
  await page.click(xpanTick);
  await sleep(200);
  const mixedSel = await page.evaluate(async () => {
    const { state } = await import('/assets/state.js');
    const pasteBtn = [...document.querySelectorAll('#selbar button')].find((b) => b.textContent.includes('粘贴'));
    return {
      selSize: state.selection.size,
      selbarCount: document.querySelector('#selbar .t')?.textContent?.trim() || '',
      exportBtn: document.querySelector('#btn-export-top span')?.textContent?.trim() || '',
      pasteBtn: pasteBtn?.textContent?.trim() || '',
    };
  });

  await page.evaluate(() => {
    const btn = [...document.querySelectorAll('.sidebar .it')].find((b) => b.textContent.includes('半格'));
    if (btn) btn.click();
  });
  await sleep(250);
  const filteredState = await page.evaluate(async (xpanId) => {
    const { state } = await import('/assets/state.js');
    const pasteBtn = [...document.querySelectorAll('#selbar button')].find((b) => b.textContent.includes('粘贴'));
    const toastEl = document.querySelector('#toast-root');
    const toastCard = document.querySelector('#toast-root .toast');
    const selbar = document.querySelector('#selbar');
    const tr = toastCard?.getBoundingClientRect();
    const sr = selbar && !selbar.hidden ? selbar.getBoundingClientRect() : null;
    return {
      filteredCount: document.querySelectorAll('.grid .cell').length,
      libTitle: document.querySelector('.libbar .t')?.textContent?.trim() || '',
      libCount: document.querySelector('#lib-count')?.textContent?.trim() || '',
      selSize: state.selection.size,
      hasHiddenXpan: state.selection.has(xpanId),
      selbarCount: document.querySelector('#selbar .t')?.textContent?.trim() || '',
      exportBtn: document.querySelector('#btn-export-top span')?.textContent?.trim() || '',
      pasteBtn: pasteBtn?.textContent?.trim() || '',
      toastText: toastEl?.textContent?.trim().replace(/\s+/g, ' ') || '',
      hasUndoBtn: [...document.querySelectorAll('#toast-root button')].some((b) => b.textContent.trim() === '恢复选择'),
      toastPos: tr && sr
        ? { toastBottom: Math.round(tr.bottom), selbarTop: Math.round(sr.top), aboveSelbar: tr.bottom <= sr.top - 4 }
        : null,
    };
  }, m.xpanId);

  // Batch export while filtered: must only export the visible pair (2 IDs -> 1 diptych), never hidden XPan
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

  // Click undo toast 恢复选择 -> returns to 全部照片 with pair + XPan restored
  await page.evaluate(() => {
    const b = [...document.querySelectorAll('#toast-root button')].find((el) => el.textContent.trim() === '恢复选择');
    if (b) b.click();
  });
  await sleep(250);
  const afterUndo = await page.evaluate(async (xpanId) => {
    const { state } = await import('/assets/state.js');
    return {
      restoredCount: document.querySelectorAll('.grid .cell').length,
      selSize: state.selection.size,
      hasXpan: state.selection.has(xpanId),
    };
  }, m.xpanId);
  if (afterUndo.restoredCount < 8) {
    await page.evaluate(() => {
      const btn = [...document.querySelectorAll('.sidebar .it')].find((b) => b.textContent.includes('全部照片'));
      if (btn) btn.click();
    });
    await sleep(250);
  }
  await page.keyboard.press('Escape');
  await sleep(200);

  out.steps.filter = {
    mixedSel,
    ...filteredState,
    restoredCount: afterUndo.restoredCount,
    afterUndo,
  };
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
    const filmstripIds = [...document.querySelectorAll('.filmstrip .fthumb')].map((b) => {
      const src = b.querySelector('img')?.getAttribute('src') || '';
      return src.match(/\/api\/photos\/([^/?]+)/)?.[1] || '';
    });
    const histShadowLit = document.querySelector('.hist-head .hist-clip[data-side="shadow"]')?.dataset.lit === 'true';
    const stripMaskCount = document.querySelectorAll('.pair-strip-mask').length;
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
      stripMaskCount,
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

  const readStripMask = () => page.evaluate(() => {
    const el = document.querySelector('.pair-strip-mask');
    const pv = document.querySelector('.pv');
    if (!el || !pv) return { present: false };
    const r = el.getBoundingClientRect();
    const pr = pv.getBoundingClientRect();
    return {
      present: true,
      w: Math.round(r.width),
      h: Math.round(r.height),
      pvW: Math.round(pr.width),
      pvH: Math.round(pr.height),
      insidePv: r.left >= pr.left - 2 && r.right <= pr.right + 2 && r.top >= pr.top - 2 && r.bottom <= pr.bottom + 2,
      smallerThanPv: r.width < pr.width - 4 || r.height < pr.height - 4,
    };
  });

  const readRoNote = () => page.evaluate(() => {
    const el = document.querySelector('.pair-ro-note');
    if (!el || el.hidden) return { present: false };
    const r = el.getBoundingClientRect();
    const st = document.querySelector('.stage-area')?.getBoundingClientRect();
    const panel = document.querySelector('.panel');
    const viewbar = document.querySelector('.viewbar');
    const overlapsStage = !!st && r.right > st.left && r.left < st.right && r.bottom > st.top && r.top < st.bottom;
    return {
      present: r.width > 0 && r.height > 0,
      text: el.textContent?.trim() || '',
      inPanelOrViewbar: !!(panel?.contains(el) || viewbar?.contains(el)),
      overlapsStage,
    };
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
  firstView.stripMask = await readStripMask();
  firstView.roNote = await readRoNote();
  if (m.screenshots?.viewFirst) await maskedScreenshot(page, m.screenshots.viewFirst);
  const firstSample = await sampleStageHash();
  await page.evaluate(() => { const c = document.querySelector('.lcard'); if (c) c.click(); });
  await sleep(300);

  const rendersBeforeSecond = out.requests.renders.length;
  await page.click('.viewbar [aria-label="半格视图"] button:nth-child(3)');
  await waitFor(() => out.requests.renders.slice(rendersBeforeSecond).some((r) => r.view === 'second' && r.status === 200), 45000, 'view=second render');
  await sleep(400);
  const secondStripMask = await readStripMask();
  const secondRoNote = await readRoNote();
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
  backToDiptych.stripMask = await readStripMask();
  backToDiptych.roNote = await readRoNote();

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
      secondStripMask,
      secondRoNote,
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
  // P3-3: switch 范围 to <date> 的全部 -> title must update to 导出; switch back -> 导出 1 项（半格一对）
  await page.evaluate(() => {
    const scopeRow = [...document.querySelectorAll('.dialog .row.form')].find((r) => r.querySelector('.lab')?.textContent?.trim() === '范围');
    const btns = scopeRow ? [...scopeRow.querySelectorAll('button')] : [];
    if (btns[1]) btns[1].click();
  });
  await sleep(200);
  const exportScopeDateTitle = await page.evaluate(() => document.querySelector('.dialog .dh h2')?.textContent?.trim() || '');
  await page.evaluate(() => {
    const scopeRow = [...document.querySelectorAll('.dialog .row.form')].find((r) => r.querySelector('.lab')?.textContent?.trim() === '范围');
    const btns = scopeRow ? [...scopeRow.querySelectorAll('button')] : [];
    if (btns[0]) btns[0].click();
  });
  await sleep(200);
  const exportScopeBackTitle = await page.evaluate(() => document.querySelector('.dialog .dh h2')?.textContent?.trim() || '');

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
    exportScopeDateTitle,
    exportScopeBackTitle,
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
  const halfMeta = await page.evaluate(async () => {
    const { currentView } = await import('/assets/viewer.js');
    const v = currentView();
    return {
      ratioBtnText: document.querySelector('button.select[aria-label="比例"]')?.textContent?.trim() || '',
      orientation: v?.photo?.orientation ?? 1,
      hasRaw: !!v?.photo?.has_raw,
      edLabel: v?.ed?.label || null,
    };
  });
  out.steps.cropHalfFirst = {
    xpanHasHalfCrop,
    cropPressed: halfMeta.ratioBtnText === '取半格',
    ratioBtnText: halfMeta.ratioBtnText,
    orientation: halfMeta.orientation,
    hasRaw: halfMeta.hasRaw,
    edLabel: halfMeta.edLabel,
    crop: cropState.body,
  };
}

async function runFramedPair(page) {
  const renders = [];
  page.on('response', (res) => {
    const u = new URL(res.url());
    if (u.pathname.endsWith('/render')) {
      renders.push({ status: res.status(), view: u.searchParams.get('view') || null });
    }
  });

  await page.goto(`${m.base}/?token=${encodeURIComponent(m.token)}#/photos`, { waitUntil: 'domcontentloaded' });
  await waitFor(() => page.evaluate(() => document.querySelectorAll('.grid .cell').length > 0), 30000, 'framed_pair gallery');

  const putRes = await page.evaluate(async (id) => {
    const cur = await (await fetch(`/api/photos/${id}/edit`)).json();
    const r = await fetch(`/api/photos/${id}/edit`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        rev: cur.rev,
        frame: { style: 'gallery' },
        stamp: { enabled: true },
      }),
    });
    return { status: r.status, body: await r.json() };
  }, m.pairSecondId);
  if (putRes.status !== 200) throw new Error(`PUT /edit failed: ${putRes.status}`);

  const inspectStage = () => page.evaluate(async () => {
    const { halfFirstCropRect, partnerOf } = await import('/assets/halfframe.js');
    const { currentView } = await import('/assets/viewer.js');
    const { state } = await import('/assets/state.js');
    const v = currentView();
    const pv = document.querySelector('.stage-area .pv');
    const img = pv?.querySelector('.pv-img');
    const deco = pv?.querySelector('.pv-deco');
    const mask = pv?.querySelector('.pair-strip-mask');
    const note = document.querySelector('.pair-ro-note');
    const st = document.querySelector('.stage-area')?.getBoundingClientRect();
    const pr = pv?.getBoundingClientRect();
    const ir = img?.getBoundingClientRect();
    const mr = mask?.getBoundingClientRect();
    const nr = note && !note.hidden ? note.getBoundingClientRect() : null;
    let maskAlign = null;
    if (mr && ir && img && img.naturalWidth > 0) {
      const p1 = v?.pairView === 'first' ? partnerOf(v.photo, state.photos, state.partners) : null;
      const target = v?.pairView === 'first' ? (p1 || v.photo) : v?.photo;
      const raw = !!(target ? target.has_raw : true);
      const ratio = halfFirstCropRect(img.naturalWidth, img.naturalHeight, { raw, orientation: target?.orientation });
      const expLeft = ir.left + ratio.x * ir.width;
      const expTop = ir.top + ratio.y * ir.height;
      const expRight = expLeft + ratio.w * ir.width;
      const expBottom = expTop + ratio.h * ir.height;
      const maxErrPx = Math.max(
        Math.abs(mr.left - expLeft),
        Math.abs(mr.top - expTop),
        Math.abs(mr.right - expRight),
        Math.abs(mr.bottom - expBottom),
      );
      maskAlign = {
        present: true,
        maxErrPx: Number(maxErrPx.toFixed(3)),
        maskRect: [mr.left, mr.top, mr.width, mr.height].map((x) => Number(x.toFixed(2))),
        expRect: [expLeft, expTop, expRight - expLeft, expBottom - expTop].map((x) => Number(x.toFixed(2))),
      };
    }
    const pvImgDiff = pr && ir
      ? Math.max(Math.abs(pr.left - ir.left), Math.abs(pr.top - ir.top), Math.abs(pr.right - ir.right), Math.abs(pr.bottom - ir.bottom))
      : null;
    return {
      framed: !!pv?.classList.contains('framed'),
      hasDeco: !!deco && deco.width > 0 && deco.height > 0,
      pvBiggerThanImg: !!(pr && ir && (pr.width > ir.width + 10 || pr.height > ir.height + 10)),
      pvMatchesImg: pvImgDiff !== null && pvImgDiff <= 1.0,
      pvImgDiff: pvImgDiff !== null ? Number(pvImgDiff.toFixed(2)) : null,
      pvBg: pv ? getComputedStyle(pv).backgroundColor : null,
      stripMaskCount: document.querySelectorAll('.pair-strip-mask').length,
      maskAlign,
      roNote: nr
        ? {
            present: nr.width > 0 && nr.height > 0,
            text: note.textContent?.trim() || '',
            inPanelOrViewbar: !!(document.querySelector('.panel')?.contains(note) || document.querySelector('.viewbar')?.contains(note)),
            overlapsStage: !!st && nr.right > st.left && nr.left < st.right && nr.bottom > st.top && nr.top < st.bottom,
          }
        : { present: false },
    };
  });

  await page.goto(`${m.base}/?token=${encodeURIComponent(m.token)}#/photo/${m.pairSecondId}`, { waitUntil: 'domcontentloaded' });
  await waitFor(() => page.evaluate(() => {
    const pv = document.querySelector('.stage-area .pv');
    const img = pv?.querySelector('.pv-img');
    const deco = pv?.querySelector('.pv-deco');
    return !!pv?.classList.contains('framed') && !!img && img.complete && img.naturalWidth > 0 && !!deco && deco.width > 0;
  }), 60000, 'framed diptych loaded');
  await sleep(400);
  const diptych = await inspectStage();

  const r1 = renders.length;
  await page.click('.viewbar [aria-label="半格视图"] button:nth-child(2)');
  await waitFor(() => renders.slice(r1).some((r) => r.view === 'first' && r.status === 200), 45000, 'framed view=first render');
  await waitFor(() => page.evaluate(() => {
    const img = document.querySelector('.stage-area .pv-img');
    return !!img && img.complete && img.naturalWidth > 0 && !!document.querySelector('.pair-strip-mask');
  }), 15000, 'framed view=first painted');
  await sleep(400);
  const first = await inspectStage();
  if (m.screenshots?.framedFirst) await maskedScreenshot(page, m.screenshots.framedFirst);

  const r2 = renders.length;
  await page.click('.viewbar [aria-label="半格视图"] button:nth-child(3)');
  await waitFor(() => renders.slice(r2).some((r) => r.view === 'second' && r.status === 200), 45000, 'framed view=second render');
  await waitFor(() => page.evaluate(() => {
    const img = document.querySelector('.stage-area .pv-img');
    return !!img && img.complete && img.naturalWidth > 0 && !!document.querySelector('.pair-strip-mask');
  }), 15000, 'framed view=second painted');
  await sleep(400);
  const second = await inspectStage();

  const r3 = renders.length;
  await page.click('.viewbar [aria-label="半格视图"] button:nth-child(1)');
  await waitFor(() => renders.slice(r3).some((r) => !r.view && r.status === 200), 45000, 'framed back to diptych render');
  await waitFor(() => page.evaluate(() => {
    const pv = document.querySelector('.stage-area .pv');
    const deco = pv?.querySelector('.pv-deco');
    return !!pv?.classList.contains('framed') && !!deco && deco.width > 0;
  }), 15000, 'framed diptych restored');
  await sleep(300);
  const backToDiptych = await inspectStage();

  out.steps.framedPair = { diptych, first, second, backToDiptych };
}

try {
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
  page.on('pageerror', (e) => out.pageErrors.push(line(e)));
  if (m.mode === 'snapshot') await runSnapshot(page);
  else if (m.mode === 'editor_snapshot') await runEditorSnapshot(page);
  else if (m.mode === 'jpgonly_p1') await runJpgOnlyP1(page);
  else if (m.mode === 'framed_pair') await runFramedPair(page);
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
