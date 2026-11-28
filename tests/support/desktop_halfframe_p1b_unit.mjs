// Node unit tests for halfframe-p1b-ui pure JS helpers (folding, selection, badges, 取半格 crop, seam-aware histogram).
import assert from 'node:assert/strict';
import { pathToFileURL } from 'node:url';
import path from 'node:path';

const assets = path.resolve(process.argv[2]);
const load = (name) => import(pathToFileURL(path.join(assets, name)).href);

const hf = await load('halfframe.js');
const crop = await load('crop.js');
const hmath = await load('histogram_math.js');

// 1. Gallery folding + cross-page pair + ordering by photo 2 capture time + counts
{
  const p1 = { id: 's1', name: 'P1', captured_at: '2026-11-18T10:00:01', date: '2026-11-18', half_first: true, pair: { role: 'first', partner_id: 's2' } };
  const p2 = { id: 's2', name: 'P2', captured_at: '2026-11-18T10:00:05', date: '2026-11-18', diptych: true, pair: { role: 'second', partner_id: 's1' } };
  const splitFirst = { id: 's3', name: 'P3', captured_at: '2026-11-18T10:01:00', date: '2026-11-18', half_first: true, pair: { role: 'first', partner_id: 's4' } };
  const splitSecond = { id: 's4', name: 'P4', captured_at: '2026-11-18T10:01:05', date: '2026-11-18', diptych: true, pair: { role: 'second', partner_id: 's3' } };
  const loneFirst = { id: 's5', name: 'P5', captured_at: '2026-11-18T10:02:00', date: '2026-11-18', half_first: true, pair: null };
  const loneDip = { id: 's6', name: 'P6', captured_at: '2026-11-18T10:03:00', date: '2026-11-18', diptych: true, pair: null };
  const plain = { id: 's7', name: 'P7', captured_at: '2026-11-18T10:04:00', date: '2026-11-18' };

  // Page 1 contains s1, s2, s3 (partner s4 is on page 2, fetched into partners map), s5, s6, s7.
  const page1 = [plain, loneDip, loneFirst, splitFirst, p2, p1];
  const partners = new Map([['s4', splitSecond]]);
  const visible = hf.foldPhotos(page1, { partners });
  assert.deepEqual(visible.map((x) => x.id), ['s7', 's6', 's5', 's4', 's2']);
  assert.equal(hf.pairCount(page1, { partners }), 2);

  // Filter: half-frame pairs only
  const pairsOnly = hf.foldPhotos(page1, { partners, halfframeOnly: true });
  assert.deepEqual(pairsOnly.map((x) => x.id), ['s4', 's2']);

  // Badges
  assert.equal(hf.badgeKind(p2), 'pair');
  assert.equal(hf.badgeKind(loneFirst), 'half_first');
  assert.equal(hf.badgeKind(loneDip), 'diptych');
  assert.equal(hf.badgeKind(plain), null);

  // Selection expansion & dedupe
  const sel = new Set();
  hf.togglePairSelection(sel, p2, true);
  assert.deepEqual([...sel].sort(), ['s1', 's2']);
  assert.equal(hf.isPhotoSelected(sel, p2), true);
  const targets = hf.dedupePairIds([...sel, 's7'], page1, partners);
  assert.deepEqual(targets, ['s2', 's7']);
  hf.togglePairSelection(sel, p2, false);
  assert.equal(sel.size, 0);
}

// 2. Crop preset 「取半格」 on half_first (landscape and portrait)
{
  const halfRectLand = hf.halfFirstCropRect(4288, 2848);
  assert.ok(Math.abs(halfRectLand.x - 1100 / 4288) < 1e-6);
  assert.equal(halfRectLand.y, 0);
  assert.ok(Math.abs(halfRectLand.w - 2088 / 4288) < 1e-6);
  assert.equal(halfRectLand.h, 1);
  assert.equal(Math.round(halfRectLand.w * 4288), 2088);
  assert.equal(Math.round(halfRectLand.h * 2848), 2848);

  const halfRectPort = hf.halfFirstCropRect(2848, 4288);
  assert.equal(halfRectPort.x, 0);
  assert.ok(Math.abs(halfRectPort.y - 1100 / 4288) < 1e-6);
  assert.equal(halfRectPort.w, 1);
  assert.ok(Math.abs(halfRectPort.h - 2088 / 4288) < 1e-6);
  assert.equal(Math.round(halfRectPort.w * 2848), 2848);
  assert.equal(Math.round(halfRectPort.h * 4288), 2088);

  const viewLand = { photo: { half_first: true, pair: null }, canvas: { w: 4288, h: 2848 }, crop: { preset: 'free', rect: halfRectLand } };
  assert.equal(crop.presetFor(viewLand).id, 'halfframe');
  const viewPlain = { photo: { half_first: false, pair: null }, canvas: { w: 4288, h: 2848 }, crop: { preset: 'free', rect: halfRectLand } };
  assert.notEqual(crop.presetFor(viewPlain).id, 'halfframe');
}

// 3. Histogram & clip overlay ignore black seam columns on diptych
{
  const w = 200;
  const h = 100;
  const rgba = new Uint8ClampedArray(w * h * 4);
  for (let y = 0; y < h; y++) {
    for (let x = 0; x < w; x++) {
      const i = (y * w + x) * 4;
      const isSeam = x >= 97 && x <= 102;
      const v = isSeam ? 0 : 128;
      rgba[i] = v;
      rgba[i + 1] = v;
      rgba[i + 2] = v;
      rgba[i + 3] = 255;
    }
  }
  const seam = hf.seamExclusionRect({ diptych: true, pair: { role: 'second', partner_id: 's1' } }, w, h, null);
  assert.ok(seam && seam[0] <= 97 && seam[2] >= 103, JSON.stringify(seam));

  const withoutEx = hmath.summarize(hmath.countRGBA(rgba, w, h, { stride: 1 }), 'server');
  assert.equal(withoutEx.shadowWarn, true, 'expected raw black seam to trigger shadowWarn without exclusion');

  const withEx = hmath.summarize(hmath.countRGBA(rgba, w, h, { stride: 1, exclude: seam }), 'server');
  assert.equal(withEx.shadowWarn, false, 'expected shadowWarn to be false when seam is excluded');
  assert.equal(withEx.highlightWarn, false);

  const overlay = new Uint8ClampedArray(w * h * 4);
  const res = hmath.markClipping(rgba, overlay, { shadow: true, highlight: false, width: w, exclude: seam });
  assert.equal(res.crushed, 0, `expected 0 crushed pixels when seam excluded, got ${res.crushed}`);
}

// 4. P3-6: Midnight-crossing pair is always grouped by photo 2's date
{
  const p1 = { id: 'm1', name: 'M1', captured_at: '2026-11-01T23:59:58', date: '2026-11-01', half_first: true, pair: { role: 'first', partner_id: 'm2' } };
  const p2 = { id: 'm2', name: 'M2', captured_at: '2026-11-02T00:00:05', date: '2026-11-02', diptych: true, pair: { role: 'second', partner_id: 'm1' } };
  const partners = new Map([['m2', p2], ['m1', p1]]);

  // Viewing day D (2026-11-01): photo 1 is folded away and photo 2 (day D+1) must NOT be promoted into day D
  const day1 = hf.foldPhotos([p1], { partners, activeDate: '2026-11-01' });
  assert.deepEqual(day1.map((x) => x.id), [], 'midnight pair must not appear in photo 1 date view');
  assert.equal(hf.pairCount([p1], { partners, activeDate: '2026-11-01' }), 0);

  // Viewing day D+1 (2026-11-02): photo 2 appears and counts as 1 pair
  const day2 = hf.foldPhotos([p2], { partners, activeDate: '2026-11-02' });
  assert.deepEqual(day2.map((x) => x.id), ['m2']);
  assert.equal(hf.pairCount([p2], { partners, activeDate: '2026-11-02' }), 1);

  // Viewing ALL: photo 2 is kept with date 2026-11-02
  const all = hf.foldPhotos([p2, p1], { partners, activeDate: null });
  assert.deepEqual(all.map((x) => [x.id, x.date]), [['m2', '2026-11-02']]);
}

// 5. P3-4: 「取半格」 uses DNG column 1099 on RAW render base and 1100 on camera-JPG base (oriented)
{
  assert.equal(typeof hf.usesRawRenderBase, 'function');
  assert.equal(hf.usesRawRenderBase({ has_raw: true, has_jpg: true }, { label: 'prediction', recipe: { slot: 1 } }), true);
  assert.equal(hf.usesRawRenderBase({ has_raw: true, has_jpg: true }, { label: 'camera-jpg', recipe: null }), false);
  assert.equal(hf.usesRawRenderBase({ has_raw: false, has_jpg: true }, { label: 'camera-jpg', recipe: null }), false);
  assert.equal(hf.usesRawRenderBase({ has_raw: true, has_jpg: false }, { label: 'neutral', recipe: null }), true);

  const rawOri1 = hf.halfFirstCropRect(4288, 2848, { raw: true, orientation: 1 });
  assert.equal(Math.round(rawOri1.x * 4288), 1099);
  assert.equal(Math.round(rawOri1.w * 4288), 2088);

  const jpgOri1 = hf.halfFirstCropRect(4288, 2848, { raw: false, orientation: 1 });
  assert.equal(Math.round(jpgOri1.x * 4288), 1100);
  assert.equal(Math.round(jpgOri1.w * 4288), 2088);

  const rawOri6 = hf.halfFirstCropRect(2848, 4288, { raw: true, orientation: 6 });
  assert.equal(Math.round(rawOri6.y * 4288), 1099);
  assert.equal(Math.round(rawOri6.h * 4288), 2088);

  const rawOri8 = hf.halfFirstCropRect(2848, 4288, { raw: true, orientation: 8 });
  assert.equal(Math.round(rawOri8.y * 4288), 1101);
  assert.equal(Math.round(rawOri8.h * 4288), 2088);

  const jpgOri8 = hf.halfFirstCropRect(2848, 4288, { raw: false, orientation: 8 });
  assert.equal(Math.round(jpgOri8.y * 4288), 1100);
  assert.equal(Math.round(jpgOri8.h * 4288), 2088);
}

// 6. P3-5: selectionNoun unit consistency
{
  assert.equal(typeof hf.selectionNoun, 'function');
  assert.equal(hf.selectionNoun({ items: 1, pairs: 1 }), '1 对半格');
  assert.equal(hf.selectionNoun({ items: 3, pairs: 3 }), '3 对半格');
  assert.equal(hf.selectionNoun({ items: 2, pairs: 1 }), '2 项');
  assert.equal(hf.selectionNoun({ items: 2, pairs: 0 }), '2 张');
}

// 7. P3-2: batch export summary never claims pairs were exported when all items fail
{
  assert.equal(typeof hf.batchExportHeadline, 'function');
  const pairPhoto = { id: 'p2', diptych: true, pair: { role: 'second', partner_id: 'p1' } };
  const xpanPhoto = { id: 'x1' };

  const allFail = hf.batchExportHeadline(
    { pairs: 1, results: [{ id: 'p2', error: 'dest_unwritable' }, { id: 'x1', error: 'dest_unwritable' }] },
    [pairPhoto, xpanPhoto],
    'diptych',
  );
  assert.equal(allFail, '已导出 0 张');

  const pairFailedOnly = hf.batchExportHeadline(
    { pairs: 1, results: [{ id: 'p2', error: 'dest_unwritable' }, { id: 'x1', path: '/tmp/x1.jpg' }] },
    [pairPhoto, xpanPhoto],
    'diptych',
  );
  assert.equal(pairFailedOnly, '已导出 1 张');

  const pairOk = hf.batchExportHeadline(
    { pairs: 1, results: [{ id: 'p2', path: '/tmp/p2.jpg' }, { id: 'x1', path: '/tmp/x1.jpg' }] },
    [pairPhoto, xpanPhoto],
    'diptych',
  );
  assert.equal(pairOk, '导出 2 张，其中 1 对半格按双联导出');
}

// 8. P3-6: patchDayGrid reuses surviving cell elements without recreating them
{
  const makeEl = (tag, cls = '') => {
    const el = {
      tag,
      className: cls,
      dataset: {},
      children: [],
      textContent: '',
      parent: null,
      classList: { contains: (c) => el.className.split(/\s+/).includes(c) },
      get lastElementChild() { return el.children[el.children.length - 1] || null; },
      append(...nodes) { for (const n of nodes) el.insertBefore(n, null); },
      insertBefore(node, ref) {
        if (node.parent) node.remove();
        node.parent = el;
        const idx = ref ? el.children.indexOf(ref) : -1;
        if (idx >= 0) el.children.splice(idx, 0, node);
        else el.children.push(node);
      },
      remove() {
        if (!el.parent) return;
        const i = el.parent.children.indexOf(el);
        if (i >= 0) el.parent.children.splice(i, 1);
        el.parent = null;
      },
      querySelector(sel) {
        if (sel === 'h3 > span') return el.children.find((c) => c.tag === 'h3')?.children.find((c) => c.tag === 'span') || null;
        return null;
      },
    };
    return el;
  };
  const hStub = (tag, attrs = {}, ...kids) => {
    const el = makeEl(tag, attrs.class || '');
    for (const k of kids) {
      if (typeof k === 'string') el.textContent += k;
      else if (k) el.append(k);
    }
    return el;
  };
  const wrap = makeEl('div', 'grid');
  const created = [];
  const createCell = (p) => {
    const c = makeEl('div', 'cell');
    c.dataset.id = p.id;
    created.push(p.id);
    return c;
  };
  const a = { id: 'a', date: '2026-11-18' };
  const p1 = { id: 'p1', date: '2026-11-18' };
  const p2 = { id: 'p2', date: '2026-11-18' };
  const b = { id: 'b', date: '2026-11-18' };
  const opts = { h: hStub, createCell, syncCell: () => {}, fmtDate: (d) => d, countOn: () => 2, unit: '张' };

  hf.patchDayGrid(wrap, [a, p1], opts);
  const cellA = wrap.children[0].lastElementChild.children[0];
  assert.deepEqual(created, ['a', 'p1']);

  // Page 2 brings p2 (folding out p1) and b: cellA must be reused by identity, p1 removed, p2 & b added
  hf.patchDayGrid(wrap, [a, p2, b], opts);
  const cells = wrap.children[0].lastElementChild.children;
  assert.deepEqual(cells.map((c) => c.dataset.id), ['a', 'p2', 'b']);
  assert.equal(cells[0], cellA, 'surviving cell node must be reused without recreation');
  assert.deepEqual(created, ['a', 'p1', 'p2', 'b']);
}

// Import P3-3：没配上的半格双联在图库和编辑器顶栏（共用 pairTag）写「半格」，悬停和读屏说没有 HDR；
// 真正只有 JPG 的、半格一对都不变。放在最后：要装 mini_dom 的 document。
{
  const { installDom } = await import('./mini_dom.mjs');
  installDom();
  const gallery = await load('gallery.js');
  const note = '半格：按相机拼好的双联图调整，没有 HDR';
  const lone = gallery.pairTag({ id: 'd', has_raw: false, has_jpg: true, diptych: true, pair: null });
  assert.equal(lone.textContent, '半格');
  assert.equal(lone.getAttribute('title'), note);
  assert.equal(lone.getAttribute('aria-label'), note);
  const jpgOnly = gallery.pairTag({ id: 'j', has_raw: false, has_jpg: true });
  assert.equal(jpgOnly.textContent, '只有 JPG');
  assert.equal(jpgOnly.getAttribute('title'), null);
  const pairSecond = gallery.pairTag({ id: 'p', has_raw: false, has_jpg: true, diptych: true, pair: { role: 'second', partner_id: 'q' } });
  assert.equal(pairSecond.textContent, '只有 JPG');
}

console.log('OK');
