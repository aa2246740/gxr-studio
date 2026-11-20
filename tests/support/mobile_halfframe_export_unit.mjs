import assert from 'node:assert/strict';
import { registerHooks } from 'node:module';
import test from 'node:test';

registerHooks({
  resolve(spec, ctx, next) {
    try {
      return next(spec, ctx);
    } catch (err) {
      if (spec.startsWith('.') && !/\.[a-z]+$/.test(spec)) return next(`${spec}.ts`, ctx);
      throw err;
    }
  },
});

const {
  FULL_HEIGHT,
  FULL_WIDTH,
  HALF_WIDTH,
  HALFFRAME_MODES,
  PAIR_EXPORT_NOTE,
  RIGHT_X0,
  SEAM_X0,
  SEAM_X1,
  extractMpfSecondRange,
  formatShootingRecipeTag,
  halfframeFileCount,
  isFirstLikeRgba,
  neighborNames,
  normalizedHalfframeMode,
  pairExportSizeText,
  parseRecipeTag,
  singleHalfSize,
  sliceDiptychHalfRgb,
} = await import('../../mobile/src/lib/halfframe.ts');
const { loadExportHalfframe, saveExportHalfframe } = await import('../../mobile/src/lib/prefs.ts');
const { basePhotoId, ExportQueueState } = await import('../../mobile/src/app/export_queue.ts');
const { readyPositionsSummary } = await import('../../mobile/src/app/export_ready.ts');
const { buildBatchItems, singleHalfRequestPhoto } = await import('../../mobile/src/app/export_request_prep.ts');

function withMemoryStorage(run) {
  const map = new Map();
  const prev = globalThis.localStorage;
  globalThis.localStorage = {
    getItem: (k) => (map.has(k) ? map.get(k) : null),
    setItem: (k, v) => {
      map.set(k, String(v));
    },
    removeItem: (k) => {
      map.delete(k);
    },
  };
  try {
    run(map);
  } finally {
    if (prev === undefined) delete globalThis.localStorage;
    else globalThis.localStorage = prev;
  }
}

test('export halfframe preference defaults to diptych and remembers singles/both', () => {
  assert.deepEqual(
    HALFFRAME_MODES.map((m) => m[0]),
    ['diptych', 'singles', 'both'],
  );
  assert.ok(PAIR_EXPORT_NOTE.includes('112'));
  assert.equal(normalizedHalfframeMode('unknown'), 'diptych');
  withMemoryStorage((store) => {
    assert.equal(loadExportHalfframe(), 'diptych');
    saveExportHalfframe('singles');
    assert.equal(loadExportHalfframe(), 'singles');
    saveExportHalfframe('both');
    assert.equal(loadExportHalfframe(), 'both');
    store.set('gxr.mobile.export_halfframe', 'invalid');
    assert.equal(loadExportHalfframe(), 'diptych');
  });
});

test('singleHalfSize, pairExportSizeText, and halfframeFileCount match desktop geometry', () => {
  const land = { width: FULL_WIDTH, height: FULL_HEIGHT };
  const port = { width: FULL_HEIGHT, height: FULL_WIDTH };
  assert.deepEqual(singleHalfSize(land, 'original'), [2088, 2848]);
  assert.deepEqual(singleHalfSize(port, 'original'), [2848, 2088]);
  assert.deepEqual(singleHalfSize(land, 'medium'), [1501, 2048]);
  assert.deepEqual(singleHalfSize(port, 'small'), [1600, 1173]);

  assert.equal(pairExportSizeText(land, [4288, 2848], 'original', 'diptych'), '4288 × 2848 像素 · 1 个文件');
  assert.equal(pairExportSizeText(land, [4288, 2848], 'original', 'singles'), '2088 × 2848 像素 · 2 个文件');
  assert.equal(pairExportSizeText(land, [4288, 2848], 'original', 'both'), '4288 × 2848 + 2088 × 2848 像素 · 3 个文件');
  assert.equal(pairExportSizeText(port, [2848, 4288], 'original', 'singles'), '2848 × 2088 像素 · 2 个文件');

  const mixed = [{ diptych: false }, { diptych: true }, { diptych: true }];
  assert.equal(halfframeFileCount(mixed, 'diptych'), 3);
  assert.equal(halfframeFileCount(mixed, 'singles'), 5);
  assert.equal(halfframeFileCount(mixed, 'both'), 7);
});

test('sliceDiptychHalfRgb splits landscape and portrait RGB excluding 112px seam', () => {
  assert.equal(HALF_WIDTH, 2088);
  assert.equal(SEAM_X0, 2088);
  assert.equal(SEAM_X1, 2199);
  assert.equal(RIGHT_X0, 2200);

  // Landscape 4288x2848: paint left half R=10, seam R=255 (sentinel), right half R=200.
  const land = new Uint8Array(FULL_WIDTH * FULL_HEIGHT * 3);
  for (let y = 0; y < FULL_HEIGHT; y += 1) {
    const row = y * FULL_WIDTH * 3;
    for (let x = 0; x < HALF_WIDTH; x += 1) land[row + x * 3] = 10;
    for (let x = SEAM_X0; x <= SEAM_X1; x += 1) land[row + x * 3] = 255;
    for (let x = RIGHT_X0; x < FULL_WIDTH; x += 1) land[row + x * 3] = 200;
  }
  const h1 = sliceDiptychHalfRgb(land, FULL_WIDTH, FULL_HEIGHT, 'first');
  const h2 = sliceDiptychHalfRgb(land, FULL_WIDTH, FULL_HEIGHT, 'second');
  assert.equal(h1.width, HALF_WIDTH);
  assert.equal(h1.height, FULL_HEIGHT);
  assert.equal(h2.width, HALF_WIDTH);
  assert.equal(h2.height, FULL_HEIGHT);
  for (let i = 0; i < h1.data.length; i += 3) {
    assert.equal(h1.data[i], 10);
    assert.equal(h2.data[i], 200);
  }

  // Portrait 2848x4288: top half G=30, seam G=255, bottom half G=180.
  const port = new Uint8Array(FULL_HEIGHT * FULL_WIDTH * 3);
  for (let y = 0; y < FULL_WIDTH; y += 1) {
    const val = y < HALF_WIDTH ? 30 : y >= RIGHT_X0 ? 180 : 255;
    const row = y * FULL_HEIGHT * 3;
    for (let x = 0; x < FULL_HEIGHT; x += 1) port[row + x * 3 + 1] = val;
  }
  const p1 = sliceDiptychHalfRgb(port, FULL_HEIGHT, FULL_WIDTH, 'first');
  const p2 = sliceDiptychHalfRgb(port, FULL_HEIGHT, FULL_WIDTH, 'second');
  assert.equal(p1.width, FULL_HEIGHT);
  assert.equal(p1.height, HALF_WIDTH);
  assert.equal(p2.width, FULL_HEIGHT);
  assert.equal(p2.height, HALF_WIDTH);
  for (let i = 1; i < p1.data.length; i += 3) {
    assert.equal(p1.data[i], 30);
    assert.equal(p2.data[i], 180);
  }

  assert.throws(() => sliceDiptychHalfRgb(new Uint8Array(12), 2, 2, 'first'));
});

test('neighborNames, isFirstLikeRgba, and extractMpfSecondRange match desktop rules', () => {
  assert.deepEqual(neighborNames('R0000100', -1), ['R0000099', 'R99']);
  assert.deepEqual(neighborNames('R0000099', +1), ['R0000100', 'R100']);
  assert.deepEqual(neighborNames('R0000100_1a2b3c', -1), ['R0000099', 'R99']);
  assert.deepEqual(neighborNames('R0000000', -1), []);
  assert.deepEqual(neighborNames('NO_DIGITS', -1), []);

  // Synthetic 640x424 MPF preview: dark left (0..163) & right (476..639), bright center (164..475).
  const w = 640;
  const h = 424;
  const firstRgba = new Uint8ClampedArray(w * h * 4);
  for (let y = 0; y < h; y += 1) {
    for (let x = 0; x < w; x += 1) {
      const off = (y * w + x) * 4;
      const v = x > 163 && x < 476 ? 140 : 2;
      firstRgba[off] = v;
      firstRgba[off + 1] = v;
      firstRgba[off + 2] = v;
      firstRgba[off + 3] = 255;
    }
  }
  assert.equal(isFirstLikeRgba(firstRgba, w, h), true);

  // Ordinary full-frame bright preview should NOT match.
  const fullRgba = new Uint8ClampedArray(w * h * 4).fill(140);
  assert.equal(isFirstLikeRgba(fullRgba, w, h), false);

  // Minimal valid little-endian APP2 MPF segment pointing to 2nd image (offset 0x1000, size 0x400).
  const segPayload = new Uint8Array(4 + 8 + 2 + 12 + 4 + 32);
  segPayload.set([0x4d, 0x50, 0x46, 0x00], 0); // "MPF\0"
  const dv = new DataView(segPayload.buffer, 4);
  dv.setUint16(0, 0x4949, false); // "II"
  dv.setUint16(2, 42, true);
  dv.setUint32(4, 8, true); // IFD0 at offset 8
  dv.setUint16(8, 1, true); // 1 entry
  dv.setUint16(10, 0xb002, true); // MPEntry
  dv.setUint16(12, 7, true); // UNDEFINED
  dv.setUint32(14, 32, true); // 2 entries * 16B
  dv.setUint32(18, 26, true); // value offset from mpfBase
  // Entry 1 (index 1, at offset 26 + 16 = 42): size=0x400, dataOff=0x1000
  dv.setUint32(42 + 4, 0x400, true);
  dv.setUint32(42 + 8, 0x1000, true);
  const jpegHead = new Uint8Array(2 + 4 + segPayload.length);
  jpegHead[0] = 0xff;
  jpegHead[1] = 0xd8;
  jpegHead[2] = 0xff;
  jpegHead[3] = 0xe2;
  const segLen = segPayload.length + 2;
  jpegHead[4] = (segLen >> 8) & 0xff;
  jpegHead[5] = segLen & 0xff;
  jpegHead.set(segPayload, 6);
  const range = extractMpfSecondRange(jpegHead);
  assert.deepEqual(range, { offset: 6 + 4 + 0x1000, size: 0x400 });
});

test('parseRecipeTag and formatShootingRecipeTag decode g258 GXR1 tags without DAT', () => {
  const tag1 = parseRecipeTag('GXR1 6adf847602 50 23 -99999999-');
  assert.ok(tag1);
  assert.equal(tag1.name, 'Leica F1');
  assert.equal(tag1.unitLabel, '50mm');
  assert.equal(tag1.position, 23);
  assert.equal(formatShootingRecipeTag('GXR1 6adf847602 50 23 -99999999-'), 'Leica F1（50mm · 23）');
  assert.equal(formatShootingRecipeTag('GXR1 6adf847602 MT 25 -99999999-'), 'Leica F1（MOUNT · 25）');

  assert.equal(formatShootingRecipeTag('GXR1 6adf847602 50 03 -A9899999-'), 'HC BW（50mm · 03） · 对比度 +1 · 阴影 -1');
  assert.equal(formatShootingRecipeTag('GXR1 6adf847602 28 03'), 'HC BW（28mm · 03） · 微调未知');
  assert.equal(formatShootingRecipeTag('Not a GXR tag'), '未标记');
  assert.equal(formatShootingRecipeTag(null), '未标记');
});

test('ExportQueueState, readyPositionsSummary, and buildBatchItems handle multi-file diptychs cleanly', () => {
  assert.equal(basePhotoId('p1#half:1'), 'p1');
  assert.equal(basePhotoId('p1#half:2'), 'p1');
  assert.equal(basePhotoId('p1'), 'p1');

  assert.equal(readyPositionsSummary([1, 1]), '第 1 张（2 个文件）');
  assert.equal(readyPositionsSummary([1, 1, 2]), '第 1 张、第 2 张（共 3 个文件）');
  assert.equal(readyPositionsSummary([1, 3]), '第 1 张、第 3 张');

  const q = new ExportQueueState();
  const fakeBatch = {
    phase: 'running',
    items: [{ id: 'dip1#half:1' }, { id: 'dip1#half:2' }],
    staged: [{ id: 'dip1#half:1' }],
    failures: new Map([
      ['dip1#half:1', { id: 'dip1#half:1', stem: 'R001_1', kind: 'trap', code: 'trap', message: '' }],
      ['dip1#half:2', { id: 'dip1#half:2', stem: 'R001_2', kind: 'trap', code: 'trap', message: '' }],
    ]),
    states: new Map([
      ['dip1#half:1', 'ok'],
      ['dip1#half:2', 'run'],
    ]),
  };
  assert.equal(q.isQuickBlocked('dip1', fakeBatch), true);
  assert.equal(q.isQuickBlocked('other', fakeBatch), false);

  const dipPhoto = { id: 'dip1', diptych: true };
  const normPhoto = { id: 'p2', diptych: false };
  q.enqueueOrPrepareStart([dipPhoto, normPhoto], { size: 'original', quality: 'high', hdr: false, halfframe: 'both' }, fakeBatch, () => []);
  assert.equal(q.queuedCount(), 4); // 3 for diptych (both) + 1 for normal

  // Single-half request stripping and batch expansion
  const reqPhoto = {
    stem: 'R0000100',
    dngName: 'R0000100.DNG',
    jpgName: 'R0000100.JPG',
    lens: 'GR LENS A12 50mm F2.5',
    iso: 200,
    choice: { id: 'builtin:3', unit: '50' },
    lut: 'cube-log',
    lutInputSpace: 'slog3',
    levels: new Map([['builtin:3', { contrast: 2 }]]),
    memory: new Map(),
    knobs: {
      exposure_ev: 0.5,
      contrast: 10,
      highlight: 0,
      shadow: 0,
      black_point: 0,
      warm: 0,
      tint: 0,
      saturation: 0,
      vibrance: 0,
      vignette: 0,
    },
    curve: { master: [[0, 0], [128, 140], [255, 255]] },
    frame: { width: 4288, height: 2848 },
    geometry: { straighten: 2.5 },
    crop: { preset: '1:1', rect: [0.1, 0.1, 0.8, 0.8], angle_deg: 0 },
    border: { style: 'solid', color: '#ffffff', ratio: 'auto', width: 'medium', caption: 'none', logo: 'none' },
    stamp: { enabled: true, style: 'film', color: 'amber', corner: 'br', format: 'ymd', size: 'medium', opacity: 0.9 },
    logoInfo: null,
    sourceModified: 1700000000000,
  };

  // Log-space LUT stripped on single half, display-space LUT kept.
  const hLog = singleHalfRequestPhoto(reqPhoto, 'first');
  assert.equal(hLog.stem, 'R0000100_1');
  assert.equal(hLog.dngName, null);
  assert.equal(hLog.choice, null);
  assert.equal(hLog.lut, null);
  assert.equal(hLog.crop, null);
  assert.equal(hLog.geometry, undefined);
  assert.equal(hLog.border, null);
  assert.equal(hLog.stamp, null);
  assert.equal(hLog.knobs.exposure_ev, 0.5);
  assert.ok(hLog.curve);

  const hDisp = singleHalfRequestPhoto({ ...reqPhoto, lut: 'cube-srgb', lutInputSpace: 'srgb' }, 'second');
  assert.equal(hDisp.stem, 'R0000100_2');
  assert.equal(hDisp.lut, 'cube-srgb');
  assert.equal(hDisp.lutInputSpace, 'srgb');

  const snap = {
    id: 'dip1',
    stem: 'R0000100',
    version: 1,
    diptych: true,
    dng: null,
    jpg: null,
    reqPhoto: { ...reqPhoto, dngName: null, choice: null, lut: null, lutInputSpace: null },
    cards: [],
    explicitChoice: false,
    recipeOff: false,
  };
  const dipOnly = buildBatchItems([snap], { size: 'original', quality: 'high', hdr: false, halfframe: 'diptych' }, 'test');
  assert.equal(dipOnly.length, 1);
  assert.equal(dipOnly[0].halfframeRole, undefined);

  const singles = buildBatchItems([snap], { size: 'original', quality: 'high', hdr: true, halfframe: 'singles' }, 'test');
  assert.equal(singles.length, 2);
  assert.deepEqual(
    singles.map((it) => [it.id, it.photoId, it.halfframeRole, it.stem, it.req.hdr]),
    [
      ['dip1#half:1', 'dip1', 'first', 'R0000100_1', false],
      ['dip1#half:2', 'dip1', 'second', 'R0000100_2', false],
    ],
  );
  assert.equal(singles[0].req.side.crop, undefined);
  assert.equal(singles[0].req.side.frame, undefined);
  assert.equal(singles[0].req.side.stamp, undefined);
  assert.ok(singles[0].req.side.adjust);
  assert.ok(singles[0].req.side.curve);

  const both = buildBatchItems([snap], { size: 'original', quality: 'high', hdr: false, halfframe: 'both' }, 'test');
  assert.equal(both.length, 3);
  assert.deepEqual(
    both.map((it) => it.halfframeRole ?? 'diptych'),
    ['diptych', 'first', 'second'],
  );
});
