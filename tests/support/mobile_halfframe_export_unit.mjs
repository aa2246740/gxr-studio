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
  STRIP_X0_JPG,
  extractMpfSecondRange,
  findFirstHalfNeighbor,
  formatShootingRecipeTag,
  halfframeFileCount,
  isFirstLikeRgba,
  neighborNames,
  normalizedHalfframeMode,
  pairExportSizeText,
  parseRecipeTag,
  singleHalfSize,
  sliceDiptychHalfRgb,
  sliceFirstHalfStripRgb,
  stripExifCopyrightFromJpeg,
} = await import('../../mobile/src/lib/halfframe.ts');
const { loadExportHalfframe, saveExportHalfframe } = await import('../../mobile/src/lib/prefs.ts');
const { tagAmountLevels } = await import('../../mobile/src/lib/recipes.ts');
const { resolveCatalogPosition } = await import('../../mobile/src/lib/recipe_tags.ts');
const { basePhotoId, ExportQueueState } = await import('../../mobile/src/app/export_queue.ts');
const { readyPositionsSummary } = await import('../../mobile/src/app/export_ready.ts');
const { applyHalfframeFirstNeighbors, buildBatchItems, singleHalfRequestPhoto } = await import(
  '../../mobile/src/app/export_request_prep.ts'
);

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

function buildSyntheticExifJpeg(le, entries) {
  const count = entries.length;
  const ifdLen = 2 + count * 12 + 4;
  const tiff = new Uint8Array(8 + ifdLen + 128);
  const dv = new DataView(tiff.buffer);
  tiff[0] = le ? 0x49 : 0x4d;
  tiff[1] = le ? 0x49 : 0x4d;
  dv.setUint16(2, 42, le);
  dv.setUint32(4, 8, le);
  dv.setUint16(8, count, le);
  let dataCursor = 8 + ifdLen;
  entries.forEach((entry, idx) => {
    const pos = 10 + idx * 12;
    dv.setUint16(pos, entry.tag, le);
    dv.setUint16(pos + 2, entry.type, le);
    dv.setUint32(pos + 4, entry.count, le);
    if (entry.bytes) {
      if (entry.bytes.length <= 4) {
        tiff.set(entry.bytes, pos + 8);
      } else {
        dv.setUint32(pos + 8, dataCursor, le);
        tiff.set(entry.bytes, dataCursor);
        dataCursor += entry.bytes.length;
      }
    } else {
      dv.setUint32(pos + 8, entry.val ?? 0, le);
    }
  });
  const payload = new Uint8Array(6 + dataCursor);
  payload.set([0x45, 0x78, 0x69, 0x66, 0x00, 0x00], 0);
  payload.set(tiff.subarray(0, dataCursor), 6);
  const segLen = payload.length + 2;
  const jpg = new Uint8Array(2 + 4 + payload.length + 2);
  jpg[0] = 0xff;
  jpg[1] = 0xd8;
  jpg[2] = 0xff;
  jpg[3] = 0xe1;
  jpg[4] = (segLen >> 8) & 0xff;
  jpg[5] = segLen & 0xff;
  jpg.set(payload, 6);
  jpg[jpg.length - 2] = 0xff;
  jpg[jpg.length - 1] = 0xd9;
  return jpg;
}

function readIfd0Tags(jpg) {
  const payloadStart = 6;
  const tiffBase = payloadStart + 6;
  const le = jpg[tiffBase] === 0x49;
  const dv = new DataView(jpg.buffer, jpg.byteOffset + tiffBase);
  const ifdOff = dv.getUint32(4, le);
  const count = dv.getUint16(ifdOff, le);
  const tags = [];
  for (let i = 0; i < count; i += 1) {
    tags.push(dv.getUint16(ifdOff + 2 + i * 12, le));
  }
  return tags;
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

test('sliceDiptychHalfRgb and sliceFirstHalfStripRgb match desktop geometry', () => {
  assert.equal(HALF_WIDTH, 2088);
  assert.equal(SEAM_X0, 2088);
  assert.equal(SEAM_X1, 2199);
  assert.equal(RIGHT_X0, 2200);
  assert.equal(STRIP_X0_JPG, 1100);

  // Landscape 4288x2848 diptych: left half R=10, seam R=255, right half R=200.
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

  // Landscape 4288x2848 photo 1 center strip (1100..3187).
  const firstLand = new Uint8Array(FULL_WIDTH * FULL_HEIGHT * 3);
  for (let y = 0; y < FULL_HEIGHT; y += 1) {
    const row = y * FULL_WIDTH * 3;
    for (let x = 0; x < STRIP_X0_JPG; x += 1) firstLand[row + x * 3] = 5;
    for (let x = STRIP_X0_JPG; x < STRIP_X0_JPG + HALF_WIDTH; x += 1) firstLand[row + x * 3] = 140;
    for (let x = STRIP_X0_JPG + HALF_WIDTH; x < FULL_WIDTH; x += 1) firstLand[row + x * 3] = 250;
  }
  const sLand = sliceFirstHalfStripRgb(firstLand, FULL_WIDTH, FULL_HEIGHT);
  assert.equal(sLand.width, HALF_WIDTH);
  assert.equal(sLand.height, FULL_HEIGHT);
  for (let i = 0; i < sLand.data.length; i += 3) assert.equal(sLand.data[i], 140);

  // Portrait 2848x4288 diptych & photo 1 center strip.
  const port = new Uint8Array(FULL_HEIGHT * FULL_WIDTH * 3);
  const firstPort = new Uint8Array(FULL_HEIGHT * FULL_WIDTH * 3);
  for (let y = 0; y < FULL_WIDTH; y += 1) {
    const val = y < HALF_WIDTH ? 30 : y >= RIGHT_X0 ? 180 : 255;
    const fVal = y < STRIP_X0_JPG ? 7 : y < STRIP_X0_JPG + HALF_WIDTH ? 160 : 245;
    const row = y * FULL_HEIGHT * 3;
    for (let x = 0; x < FULL_HEIGHT; x += 1) {
      port[row + x * 3 + 1] = val;
      firstPort[row + x * 3 + 1] = fVal;
    }
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
  const sPort = sliceFirstHalfStripRgb(firstPort, FULL_HEIGHT, FULL_WIDTH);
  assert.equal(sPort.width, FULL_HEIGHT);
  assert.equal(sPort.height, HALF_WIDTH);
  for (let i = 1; i < sPort.data.length; i += 3) assert.equal(sPort.data[i], 160);

  assert.throws(() => sliceDiptychHalfRgb(new Uint8Array(12), 2, 2, 'first'));
  assert.throws(() => sliceFirstHalfStripRgb(new Uint8Array(12), 2, 2));
});

test('stripExifCopyrightFromJpeg drops 0x8298 in LE/BE without touching other tags or input', () => {
  const enc = new TextEncoder();
  const make = enc.encode('RICOH\0');
  const copy = enc.encode('GXR1 6adf847602 50 03 -99999999-\0');
  for (const le of [true, false]) {
    const jpg = buildSyntheticExifJpeg(le, [
      { tag: 0x010f, type: 2, count: make.length, bytes: make },
      { tag: 0x0112, type: 3, count: 1, val: 1 },
      { tag: 0x8298, type: 2, count: copy.length, bytes: copy },
      { tag: 0x8769, type: 4, count: 1, val: 120 },
    ]);
    const before = jpg.slice();
    const stripped = stripExifCopyrightFromJpeg(jpg);
    assert.deepEqual(jpg, before, 'input bytes must not be mutated');
    assert.deepEqual(readIfd0Tags(stripped), [0x010f, 0x0112, 0x8769]);
  }
  const noCopy = buildSyntheticExifJpeg(true, [{ tag: 0x010f, type: 2, count: make.length, bytes: make }]);
  assert.deepEqual(readIfd0Tags(stripExifCopyrightFromJpeg(noCopy)), [0x010f]);
});

test('neighborNames, isFirstLikeRgba, extractMpfSecondRange, and findFirstHalfNeighbor orientation check', async () => {
  assert.deepEqual(neighborNames('R0000100', -1), ['R0000099', 'R99']);
  assert.deepEqual(neighborNames('R0000099', +1), ['R0000100', 'R100']);
  assert.deepEqual(neighborNames('R0000100_1a2b3c', -1), ['R0000099', 'R99']);
  assert.deepEqual(neighborNames('R0000000', -1), []);
  assert.deepEqual(neighborNames('NO_DIGITS', -1), []);

  // Orientation matching rule (Item 6): candOri === dipOri && dipOri in {1, 3, 6, 8}
  const fakeJpg = new Blob([new Uint8Array([0xff, 0xd8, 0xff, 0xd9])]);
  const dip1 = { id: 'd2', stem: 'R0000101', diptych: true, jpg: fakeJpg, frame: { orientation: 1 } };
  const candMatch = { id: 'p1', stem: 'R0000100', diptych: false, jpg: fakeJpg, frame: { orientation: 1 } };
  const candMismatch = { id: 'p1m', stem: 'R0000100', diptych: false, jpg: fakeJpg, frame: { orientation: 6 } };
  const dipInvalidOri = { id: 'd2b', stem: 'R0000101', diptych: true, jpg: fakeJpg, frame: { orientation: 2 } };
  const candInvalidOri = { id: 'p1b', stem: 'R0000100', diptych: false, jpg: fakeJpg, frame: { orientation: 2 } };

  assert.equal(await findFirstHalfNeighbor(dip1, [candMatch, dip1], async () => true), candMatch);
  assert.equal(await findFirstHalfNeighbor(dip1, [candMismatch, dip1], async () => true), null);
  assert.equal(await findFirstHalfNeighbor(dipInvalidOri, [candInvalidOri, dipInvalidOri], async () => true), null);

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

  const fullRgba = new Uint8ClampedArray(w * h * 4).fill(140);
  assert.equal(isFirstLikeRgba(fullRgba, w, h), false);

  // Minimal valid little-endian APP2 MPF segment pointing to 2nd image (offset 0x1000, size 0x400).
  const segPayload = new Uint8Array(4 + 8 + 2 + 12 + 4 + 32);
  segPayload.set([0x4d, 0x50, 0x46, 0x00], 0);
  const dv = new DataView(segPayload.buffer, 4);
  dv.setUint16(0, 0x4949, false);
  dv.setUint16(2, 42, true);
  dv.setUint32(4, 8, true);
  dv.setUint16(8, 1, true);
  dv.setUint16(10, 0xb002, true);
  dv.setUint16(12, 7, true);
  dv.setUint32(14, 32, true);
  dv.setUint32(18, 26, true);
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

test('formatShootingRecipeTag and tagAmountLevels resolve synthetic amount levels and show 强度未知 without matching DAT', () => {
  const mkAmount = (levels) => ({
    name: 'amount',
    label: '强度',
    min: levels[0],
    max: levels[levels.length - 1],
    default: 100,
    cap_lower: false,
    cap_upper: false,
    amount_step: null,
    levels,
  });
  const listing178 = {
    units: [
      {
        id: 'jlaunch',
        recipes: [
          { kind: 'builtin', slot: 1, number: '01', controls: [mkAmount([0, 25, 50, 75, 100, 125, 150])] },
          { kind: 'record', id: 'k-kino', name: 'K-KINO', slot: 6, number: '26', controls: [mkAmount([0, 10, 20, 30, 40, 50, 60, 70, 80, 90, 100, 110, 120, 130, 140, 150])] },
          { kind: 'record', id: 'k64', name: 'K64', slot: 6, number: '12', controls: [mkAmount([0, 10, 50, 80, 100, 150])] },
        ],
      },
      {
        id: 'qlaunch',
        recipes: [
          { kind: 'record', id: 'k64', name: 'K64', slot: 6, number: '12', controls: [mkAmount([0, 10, 50, 80, 100, 150])] },
        ],
      },
    ],
  };
  const dat262 = { generation: 262, listing: listing178 };
  const dat176 = { generation: 176, listing: listing178 };

  const kino80 = 'GXR1 0ccc86c19b 50 68 879999999-';
  const kino0 = 'GXR1 0ccc86c19b 50 68 079999999-';
  const kino100 = 'GXR1 0ccc86c19b 50 68 A99999999-';
  const grpos75 = 'GXR1 0ccc86c19b 50 01 799999999-';
  assert.equal(formatShootingRecipeTag(kino80, tagAmountLevels(parseRecipeTag(kino80), dat262)), 'K-KINO（50mm · 68） · 强度 80% · 对比度 -2');
  assert.equal(formatShootingRecipeTag(kino0, tagAmountLevels(parseRecipeTag(kino0), dat262)), 'K-KINO（50mm · 68） · 强度 0% · 对比度 -2');
  assert.equal(formatShootingRecipeTag(kino100, tagAmountLevels(parseRecipeTag(kino100), dat262)), 'K-KINO（50mm · 68）');
  assert.equal(formatShootingRecipeTag(grpos75, tagAmountLevels(parseRecipeTag(grpos75), dat262)), 'GRPos（50mm · 01） · 强度 75%');
  assert.equal(formatShootingRecipeTag(kino80, tagAmountLevels(parseRecipeTag(kino80), null)), 'K-KINO（50mm · 68） · 对比度 -2 · 强度未知');
  assert.equal(formatShootingRecipeTag(kino80, tagAmountLevels(parseRecipeTag(kino80), dat176)), 'K-KINO（50mm · 68） · 对比度 -2 · 强度未知');
  assert.equal(formatShootingRecipeTag(kino100), 'K-KINO（50mm · 68） · 强度未知');

  // lib-178 回退到旧基底槽 5（28 05 K64）时必须按 builtin slot=5 查表（目录无槽位 5 → null → 强度未知），不借用同名 Look 记录。
  const fallbackK64 = 'GXR1 0ccc86c19b 28 05 899999999-';
  assert.deepEqual(resolveCatalogPosition('0ccc86c19b', '28', 5), { kind: 'builtin', slot: 5, effect: null, name: 'K64' });
  assert.equal(tagAmountLevels(parseRecipeTag(fallbackK64), dat262), null);
  assert.equal(formatShootingRecipeTag(fallbackK64, tagAmountLevels(parseRecipeTag(fallbackK64), dat262)), 'K64（28mm · 05） · 强度未知');
});

test('basePhotoId preserves # in ordinary stems and strips only #half:[12]', () => {
  assert.equal(basePhotoId('p1#half:1'), 'p1');
  assert.equal(basePhotoId('p1#half:2'), 'p1');
  assert.equal(basePhotoId('p1'), 'p1');
  assert.equal(basePhotoId('trip#1'), 'trip#1');
  assert.equal(basePhotoId('photo#2026#final'), 'photo#2026#final');
  assert.equal(basePhotoId('photo#2026#half:2'), 'photo#2026');
  assert.equal(basePhotoId('p1#half:3'), 'p1#half:3');
});

test('ExportQueueState carried retry remakes ONLY failed entry IDs, not already-succeeded halves', () => {
  assert.equal(readyPositionsSummary([1, 1]), '第 1 张（2 个文件）');
  assert.equal(readyPositionsSummary([1, 1, 2]), '第 1 张、第 2 张（共 3 个文件）');
  assert.equal(readyPositionsSummary([1, 3]), '第 1 张、第 3 张');

  const q = new ExportQueueState();
  const dipPhoto = { id: 'dip1', diptych: true };
  const normPhoto = { id: 'p2', diptych: false };
  const optsSingles = { size: 'original', quality: 'high', hdr: false, halfframe: 'singles' };
  q.beginBatch([dipPhoto], optsSingles);
  const partialFailedBatch = {
    phase: 'ready',
    staged: [{ id: 'dip1#half:1', file: { size: 1000 } }],
    failures: new Map([['dip1#half:2', { id: 'dip1#half:2', stem: 'R0000101', kind: 'trap', code: 'trap', message: '' }]]),
    states: new Map([
      ['dip1#half:1', 'ok'],
      ['dip1#half:2', 'fail'],
    ]),
    savedCount: () => 0,
  };
  assert.equal(q.isQuickBlocked('dip1', partialFailedBatch), true);
  assert.equal(q.isQuickBlocked('other', partialFailedBatch), false);

  // Preserve batch (as when another export or dismiss/re-launch occurs), then retry carried failures.
  q.preserveBatch(partialFailedBatch, [{ id: 'dip1#half:1', stem: 'R0000100', file: { size: 1000 }, position: 1, hdrRequested: false }]);
  const jobs = q.takeCarriedRetryJobs((p) => p);
  assert.equal(jobs.length, 1);
  assert.deepEqual(jobs[0].onlyIds, ['dip1#half:2']);
  assert.deepEqual(jobs[0].photos, [dipPhoto]);

  // Queued count uses onlyIds length (1 file), not full diptych count (2 files).
  q.pendingQueue.push(jobs[0]);
  assert.equal(q.queuedCount(), 1);
  q.pendingQueue.length = 0;

  // Normal multi-file queue counting still works.
  const runningBatch = { ...partialFailedBatch, phase: 'running' };
  q.enqueueOrPrepareStart([dipPhoto, normPhoto], { size: 'original', quality: 'high', hdr: false, halfframe: 'both' }, runningBatch, () => []);
  assert.equal(q.queuedCount(), 4);
});

test('singleHalfRequestPhoto, buildBatchItems, and applyHalfframeFirstNeighbors match desktop naming and source rules', () => {
  const reqPhoto = {
    stem: 'R0000101',
    dngName: 'R0000101.DNG',
    jpgName: 'R0000101.JPG',
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
      vignette: 25,
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

  // Unpaired naming: first -> <photo2>_1, second -> <photo2> (NOT _2).
  const hLog = singleHalfRequestPhoto(reqPhoto, 'first');
  assert.equal(hLog.stem, 'R0000101_1');
  assert.equal(hLog.dngName, null);
  assert.equal(hLog.choice, null);
  assert.equal(hLog.lut, null);
  assert.equal(hLog.crop, null);
  assert.equal(hLog.geometry, undefined);
  assert.equal(hLog.border, null);
  assert.equal(hLog.stamp, null);
  assert.equal(hLog.knobs.exposure_ev, 0.5);
  assert.equal(hLog.knobs.vignette, 25);
  assert.ok(hLog.curve);

  const hDisp = singleHalfRequestPhoto({ ...reqPhoto, lut: 'cube-srgb', lutInputSpace: 'srgb' }, 'second');
  assert.equal(hDisp.stem, 'R0000101');
  assert.equal(hDisp.lut, 'cube-srgb');
  assert.equal(hDisp.lutInputSpace, 'srgb');

  // Paired naming: first -> <photo1>.
  const firstFile = { name: 'R0000100.JPG', lastModified: 1699999999000 };
  const hPaired = singleHalfRequestPhoto(reqPhoto, 'first', { stem: 'R0000100', jpg: firstFile });
  assert.equal(hPaired.stem, 'R0000100');
  assert.equal(hPaired.jpgName, 'R0000100.JPG');
  assert.equal(hPaired.sourceModified, 1699999999000);

  const secondFile = { name: 'R0000101.JPG', lastModified: 1700000000000 };
  const snap = {
    id: 'dip1',
    stem: 'R0000101',
    version: 3,
    diptych: true,
    dng: null,
    jpg: secondFile,
    reqPhoto: { ...reqPhoto, dngName: null, choice: null, lut: null, lutInputSpace: null },
    cards: [],
    explicitChoice: false,
    recipeOff: false,
  };
  const dipOnly = buildBatchItems([snap], { size: 'original', quality: 'high', hdr: false, halfframe: 'diptych' }, 'test');
  assert.equal(dipOnly.length, 1);
  assert.equal(dipOnly[0].halfframeRole, undefined);
  assert.equal(dipOnly[0].halfframeSource, undefined);

  const singles = buildBatchItems([snap], { size: 'original', quality: 'high', hdr: true, halfframe: 'singles' }, 'test');
  assert.equal(singles.length, 2);
  assert.deepEqual(
    singles.map((it) => [it.id, it.photoId, it.halfframeRole, it.halfframeSource, it.stripCopyright, it.stem, it.req.hdr]),
    [
      ['dip1#half:1', 'dip1', 'first', 'diptych-first', true, 'R0000101_1', false],
      ['dip1#half:2', 'dip1', 'second', 'diptych-second', false, 'R0000101', false],
    ],
  );
  assert.equal(singles[0].req.side.crop, undefined);
  assert.equal(singles[0].req.side.frame, undefined);
  assert.equal(singles[0].req.side.stamp, undefined);
  assert.ok(singles[0].req.side.adjust);
  assert.ok(singles[0].req.side.curve);

  // Applying a matched first-half neighbor switches first item to first-strip with photo 1's file, stem, and full EXIF.
  const neighbors = new Map([['dip1', { id: 'p1', stem: 'R0000100', version: 2, jpg: firstFile }]]);
  applyHalfframeFirstNeighbors([snap], singles, neighbors, { size: 'original', quality: 'high', hdr: false, halfframe: 'singles' }, 'test');
  assert.equal(singles[0].halfframeSource, 'first-strip');
  assert.equal(singles[0].stripCopyright, false);
  assert.equal(singles[0].sourcePhotoId, 'p1');
  assert.equal(singles[0].sourceVersion, 2);
  assert.equal(singles[0].stem, 'R0000100');
  assert.equal(singles[0].jpg, firstFile);
  assert.equal(singles[1].halfframeSource, 'diptych-second');
  assert.equal(singles[1].stem, 'R0000101');
  assert.equal(singles[1].jpg, secondFile);

  // onlyIds filter (per-file carried retry) emits ONLY the failed entry ID.
  const retrySecondOnly = buildBatchItems(
    [snap],
    { size: 'original', quality: 'high', hdr: false, halfframe: 'both' },
    'test',
    undefined,
    ['dip1#half:2'],
  );
  assert.deepEqual(
    retrySecondOnly.map((it) => it.id),
    ['dip1#half:2'],
  );

  const both = buildBatchItems([snap], { size: 'original', quality: 'high', hdr: false, halfframe: 'both' }, 'test');
  assert.equal(both.length, 3);
  assert.deepEqual(
    both.map((it) => it.halfframeRole ?? 'diptych'),
    ['diptych', 'first', 'second'],
  );
});
