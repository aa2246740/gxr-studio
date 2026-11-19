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

console.log('OK');
