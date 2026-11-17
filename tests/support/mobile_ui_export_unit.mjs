// UI-M3 的纯逻辑：导出请求（shot / side / 选项）、批次状态机（定下快照、停止、重试、过期的会话和版本、待存上限）、
// 共享面板的分批、文件头里的宽高。Node 自带的测试直接跑 TypeScript。全是合成数据，不读照片。

import assert from 'node:assert/strict';
import { registerHooks } from 'node:module';
import test from 'node:test';

// mobile/src 里的 import 不带扩展名（vite 的写法）；Node 找不到时补上 .ts。
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

const req = await import('../../mobile/src/lib/export_request.ts');
const share = await import('../../mobile/src/lib/share_plan.ts');
const sizes = await import('../../mobile/src/lib/photo_size.ts');
const { ExportBatch, PAGE_BUDGET_BYTES, STAGE_CAP_BYTES, TEXT, failText, stageCapFor } = await import('../../mobile/src/app/export_batch.ts');

const ZERO = { exposure_ev: 0, contrast: 0, highlight: 0, shadow: 0, black_point: 0, warm: 0, tint: 0, saturation: 0, vibrance: 0, vignette: 0 };
const LOOK_ID = 'a'.repeat(64);
const control = (name, def, levels) => ({ name, label: name, min: 0, max: 0, default: def, cap_lower: false, cap_upper: false, amount_step: null, levels });
const CARDS = [
  { id: 'builtin:3', kind: 'builtin', name: 'GR 高对比黑白', desc: null, slot: 3, recipeId: null, number: '03', controls: [control('amount', 100, [0, 50, 100]), control('contrast', 0, [-2, -1, 0, 1, 2])] },
  { id: 'builtin:4', kind: 'builtin', name: '原厂标准', desc: null, slot: 4, recipeId: null, number: '04', controls: [control('contrast', 0, [-1, 0, 1])] },
  { id: `look:${LOOK_ID}`, kind: 'look', name: 'Look', desc: null, slot: 6, recipeId: LOOK_ID, number: '', controls: [control('amount', 100, [50, 100]), control('fade', 0, [0, 1, 2]), control('saturation', 0, [-1, 0, 1])] },
];

function photo(over = {}) {
  return {
    stem: 'R0000001',
    dngName: 'R0000001.DNG',
    jpgName: 'R0000001.JPG',
    lens: 'RICOH A12 50mm F2.5 MACRO',
    iso: 200,
    choice: null,
    levels: new Map(),
    memory: new Map(),
    knobs: { ...ZERO },
    sourceModified: Date.UTC(2026, 10, 15, 1, 2, 3, 456),
    ...over,
  };
}

const OPTS = { size: 'original', quality: 'high' };

test('untouched photo: empty sidecar, camera JPG decoded, no entry', () => {
  const r = req.buildRequest(photo(), CARDS, OPTS, 'abc1234');
  assert.deepEqual(r.side, {});
  assert.deepEqual(r.defaults, {});
  assert.deepEqual(r.layout, {});
  assert.equal(r.base, 'auto');
  assert.equal(r.resolve, null);
  assert.equal(r.needsJpg, true);
  assert.equal(r.binding, 'gxr-core abc1234');
  assert.equal(r.quality, 95);
  assert.equal(r.longEdge, undefined);
  assert.equal(r.hdr, false);
  assert.deepEqual(r.shot, { name: 'R0000001', files: { dng: { relpath: 'R0000001.DNG' }, jpg: { relpath: 'R0000001.JPG' } }, lens: 'RICOH A12 50mm F2.5 MACRO', iso: 200 });
  assert.equal(r.mtimeMs, Date.UTC(2026, 10, 15, 1, 2, 3, 456));
  assert.equal(r.tzOffsetMin, -new Date(r.mtimeMs).getTimezoneOffset());
  assert.equal(req.buildRequest(photo(), CARDS, OPTS, 'x', 'desktop pin').binding, 'desktop pin');
});

test('tiers follow the desktop SIZE_TIERS and QUALITY_TIERS', () => {
  const pairs = [['original', undefined], ['large', 3000], ['medium', 2048], ['small', 1600]];
  for (const [size, edge] of pairs) assert.equal(req.buildRequest(photo(), CARDS, { size, quality: 'high' }, 'b').longEdge, edge);
  for (const [quality, q] of [['high', 95], ['medium', 85], ['low', 70]]) assert.equal(req.buildRequest(photo(), CARDS, { size: 'small', quality }, 'b').quality, q);
});

test('recipe only: RecipeSel.to_json with no levels, resolve without memory, no JPG decode', () => {
  const r = req.buildRequest(photo({ choice: { id: 'builtin:3', unit: 'jlaunch' } }), CARDS, OPTS, 'b');
  assert.deepEqual(r.side, { recipe: { kind: 'builtin', unit: 'jlaunch', slot: 3, levels: {} } });
  assert.deepEqual(JSON.parse(r.resolve), { kind: 'builtin', unit: 'jlaunch', slot: 3, levels: {} });
  assert.equal(r.needsJpg, false);
});

test('knobs only: Adjust.to_json shape, zero extras left out, camera JPG branch', () => {
  const r = req.buildRequest(photo({ knobs: { ...ZERO, exposure_ev: 0.35, warm: 12, saturation: -5 } }), CARDS, OPTS, 'b');
  assert.deepEqual(r.side, { adjust: { exposure_ev: 0.35, wb: { warm: 12, tint: 0 }, contrast: 0, highlight: 0, shadow: 0, saturation: -5 } });
  assert.equal(r.resolve, null);
  assert.equal(r.needsJpg, true);
});

test('recipe and knobs together, levels cleaned and sorted, memory keyed like recipeKey', () => {
  const levels = new Map([['builtin:3', { contrast: 2, amount: 50, bogus: 1 }]]);
  // UI-M3-GROUP：memory 是照片自己的 memory（桌面 recipeTarget 记下的），不再从 levels 推。空的一项（第一次选配方时的原厂 04）
  // 和选中配方以前切走时记下的旧档位都照桌面留着；levels 里别的卡不进 memory。
  const memory = new Map([
    ['builtin:4', {}],
    [`look:${LOOK_ID}`, { fade: 2, saturation: 7 }],
    ['builtin:3', { contrast: 1 }],
  ]);
  const p = photo({ choice: { id: 'builtin:3', unit: 'jlaunch' }, levels, memory, knobs: { ...ZERO, contrast: 10 } });
  const side = req.buildSide(p, CARDS);
  assert.deepEqual(side.recipe, {
    kind: 'builtin', unit: 'jlaunch', slot: 3, levels: { amount: 50, contrast: 2 },
    memory: { 'jlaunch/4': {}, [`jlaunch/look/${LOOK_ID}`]: { fade: 2 }, 'jlaunch/3': { contrast: 1 } },
  });
  // memory 是空的时不写这一键（render/edits.write 的 `if memory`）。
  assert.equal('memory' in req.buildSide(photo({ choice: { id: 'builtin:3', unit: 'jlaunch' }, levels }), CARDS).recipe, false);
  assert.deepEqual(Object.keys(side.recipe.levels), ['amount', 'contrast']);
  assert.deepEqual(side.adjust, { exposure_ev: 0, wb: { warm: 0, tint: 0 }, contrast: 10, highlight: 0, shadow: 0 });
  // memory never goes to color_resolve.
  assert.equal(JSON.parse(req.buildRequest(p, CARDS, OPTS, 'b').resolve).memory, undefined);
});

test('a Look with a changed level carries its id', () => {
  const p = photo({ choice: { id: `look:${LOOK_ID}`, unit: 'jlaunch' }, levels: new Map([[`look:${LOOK_ID}`, { fade: 1 }]]) });
  const r = req.buildRequest(p, CARDS, { size: 'small', quality: 'low' }, 'b');
  assert.deepEqual(r.side, { recipe: { kind: 'look', unit: 'jlaunch', slot: 6, levels: { fade: 1 }, id: LOOK_ID } });
  assert.equal(req.recipeKey('qlaunch', CARDS[2]), `qlaunch/look/${LOOK_ID}`);
  assert.equal(req.recipeKey('qlaunch', CARDS[0]), 'qlaunch/3');
});

test('JPG-only: no dng file, decode the JPG; DNG-only without recipe resolves stock 04 of the lens unit', () => {
  const jpgOnly = req.buildRequest(photo({ dngName: null, knobs: { ...ZERO, shadow: 4 } }), CARDS, OPTS, 'b');
  assert.deepEqual(jpgOnly.shot.files, { jpg: { relpath: 'R0000001.JPG' } });
  assert.equal(jpgOnly.needsJpg, true);
  assert.equal(jpgOnly.resolve, null);
  const dngOnly = req.buildRequest(photo({ jpgName: null, lens: 'RICOH A12 28mm F2.5' }), CARDS, OPTS, 'b');
  assert.deepEqual(dngOnly.shot.files, { dng: { relpath: 'R0000001.DNG' } });
  assert.equal(dngOnly.needsJpg, false);
  assert.deepEqual(JSON.parse(dngOnly.resolve), { kind: 'builtin', unit: 'qlaunch', slot: 4, levels: {} });
  const dngRecipe = req.buildRequest(photo({ jpgName: null, choice: { id: 'builtin:3', unit: 'jlaunch' } }), CARDS, OPTS, 'b');
  assert.equal(dngRecipe.needsJpg, false);
  assert.equal(JSON.parse(dngRecipe.resolve).slot, 3);
});

// ---- share chunks and size text

test('share chunks: at most 20 files and 256 MiB per tap, oversize file alone, labels', () => {
  assert.deepEqual(share.shareChunks([1, 1, 1, 1, 1], 4, 100), [[0, 1, 2, 3], [4]]);
  assert.deepEqual(share.shareChunks([60, 50, 10], 20, 100), [[0], [1, 2]]);
  assert.deepEqual(share.shareChunks([300, 5], 20, 100), [[0], [1]]);
  assert.deepEqual(share.shareChunks([]), []);
  assert.equal(share.shareChunks(Array(45).fill(1)).length, 3);
  assert.equal(share.SHARE_MAX_FILES, 20);
  assert.equal(share.SHARE_MAX_BYTES, 256 * 1024 * 1024);
  assert.equal(share.shareLabel(0, 3, '存到相册'), '存到相册（第 1 批，共 3 批）');
  assert.equal(share.shareLabel(0, 1, '存到相册'), '存到相册');
  assert.equal(share.sizeText([4288, 2848], null), '4288 × 2848 像素');
  assert.equal(share.sizeText([3000, 1993], [3, 2]), '3000 × 1993 像素 · 3:2');
  assert.equal(share.ratioText([191, 100]), '1.91:1');
});

// ---- header sizes (synthetic files)

function jpegWith(w, h, orientation) {
  const tiff = [0x49, 0x49, 42, 0, 8, 0, 0, 0, 1, 0, 0x12, 0x01, 3, 0, 1, 0, 0, 0, orientation, 0, 0, 0, 0, 0, 0, 0, 0, 0];
  const app1 = [0x45, 0x78, 0x69, 0x66, 0, 0, ...tiff];
  const len = app1.length + 2;
  const sof = [0xff, 0xc0, 0, 17, 8, h >> 8, h & 255, w >> 8, w & 255, 3, 1, 0x22, 0, 2, 0x11, 1, 3, 0x11, 1];
  return new Uint8Array([0xff, 0xd8, 0xff, 0xe1, len >> 8, len & 255, ...app1, ...sof, 0xff, 0xd9]);
}

test('JPEG size from SOF, turned by the EXIF orientation (meta.read_jpeg_meta)', () => {
  assert.deepEqual(sizes.jpegSize(jpegWith(4288, 2848, 1)), { width: 4288, height: 2848 });
  assert.deepEqual(sizes.jpegSize(jpegWith(4288, 2848, 6)), { width: 2848, height: 4288 });
  assert.deepEqual(sizes.jpegSize(jpegWith(4288, 2848, 9)), { width: 4288, height: 2848 });
  assert.equal(sizes.jpegSize(new Uint8Array([0xff, 0xd8, 0xff])), null);
  assert.equal(sizes.jpegSize(new Uint8Array(0)), null);
});

function le16(v) { return [v & 255, v >> 8]; }
function le32(v) { return [v & 255, (v >> 8) & 255, (v >> 16) & 255, (v >>> 24) & 255]; }
function entry(tag, type, count, value) { return [...le16(tag), ...le16(type), ...le32(count), ...le32(value)]; }

test('DNG size: DefaultCropSize of the full-size SubIFD, then IFD0 orientation (meta.read_dng_meta)', () => {
  // IFD0 at 8: width 160, height 120, orientation 8, SubIFDs -> one IFD at 64.
  const ifd0 = [...le16(4), ...entry(256, 4, 1, 160), ...entry(257, 4, 1, 120), ...entry(274, 3, 1, 8), ...entry(330, 4, 1, 64), ...le32(0)];
  const head = [0x49, 0x49, 42, 0, ...le32(8), ...ifd0];
  while (head.length < 64) head.push(0);
  const sub = [...le16(4), ...entry(254, 4, 1, 0), ...entry(256, 4, 1, 4352), ...entry(257, 4, 1, 2880), ...entry(50720, 3, 2, 4288 | (2848 << 16)), ...le32(0)];
  const bytes = new Uint8Array([...head, ...sub]);
  assert.deepEqual(sizes.dngSize(bytes), { width: 2848, height: 4288 });
  assert.deepEqual(sizes.shotSize(null, bytes), { width: 2848, height: 4288 });
  assert.deepEqual(sizes.shotSize(jpegWith(640, 480, 1), bytes), { width: 640, height: 480 });
  assert.deepEqual(sizes.shotSize(new Uint8Array([1, 2, 3]), bytes), { width: 2848, height: 4288 });
  assert.equal(sizes.dngSize(new Uint8Array([0x49, 0x49, 42, 0, 255, 255, 255, 255])), null);
});

// ---- batch state machine

function fakeHost({ results = {}, versions = {} } = {}) {
  let session = 1;
  const photos = new Map();
  const calls = { run: [], stop: 0, idle: 0, gone: [], busy: [], changed: 0 };
  const pending = [];
  const host = {
    calls,
    pending,
    setSession(v) { session = v; },
    photos,
    run(item, index) {
      calls.run.push(item.id);
      return new Promise((resolve) => pending.push({ item, index, resolve }));
    },
    stop() { calls.stop += 1; },
    idle() { calls.idle += 1; },
    session: () => session,
    photo: (id) => photos.get(id) ?? null,
    markGone(id, version) { calls.gone.push([id, version]); },
    busy(on) { calls.busy.push(on); },
    changed() { calls.changed += 1; },
  };
  void results;
  void versions;
  return host;
}

const tick = () => new Promise((done) => setTimeout(done, 0));

function items(n, host) {
  return Array.from({ length: n }, (_, i) => {
    const id = `p${i}`;
    host.photos.set(id, { version: 1, dng: null, jpg: null });
    return { id, stem: `R${i}`, version: 1, dng: null, jpg: null, req: { quality: 95 } };
  });
}

const ok = (bytes = 10, name = 'x_gxr.jpg') => ({ ok: true, file: new ArrayBuffer(bytes), result: { file_name: name }, shot: {}, ms: 5 });

test('batch: one at a time in the given order, snapshot fixed, files staged, ready at the end', async () => {
  const host = fakeHost();
  const list = items(3, host);
  const batch = new ExportBatch(list, host);
  batch.start();
  await tick();
  assert.deepEqual(host.calls.run, ['p0']);
  assert.equal(batch.progress().current, 1);
  list[0].req.quality = 70; // the snapshot belongs to the batch; nothing outside changes it
  host.pending.shift().resolve(ok(10, 'R0_gxr.jpg'));
  await tick();
  assert.deepEqual(host.calls.run, ['p0', 'p1']);
  host.pending.shift().resolve(ok(20, 'R1_gxr.jpg'));
  await tick();
  host.pending.shift().resolve(ok(30, 'R2_gxr.jpg'));
  await batch.settled();
  assert.equal(batch.phase, 'ready');
  assert.deepEqual(batch.staged.map((s) => [s.file.name, s.file.type, s.file.size]), [['R0_gxr.jpg', 'image/jpeg', 10], ['R1_gxr.jpg', 'image/jpeg', 20], ['R2_gxr.jpg', 'image/jpeg', 30]]);
  assert.equal(batch.stagedHigh, 60);
  assert.deepEqual(host.calls.busy, [true, false]);
  assert.equal(host.calls.idle, 1);
  batch.release();
  assert.equal(batch.phase, 'closed');
  assert.equal(batch.staged.length, 0);
});

test('batch: stop terminates at once, keeps finished files, late result dropped', async () => {
  const host = fakeHost();
  const batch = new ExportBatch(items(5, host), host);
  batch.start();
  await tick();
  host.pending.shift().resolve(ok());
  await tick();
  host.pending.shift().resolve(ok());
  await tick();
  const third = host.pending.shift();
  batch.stop();
  assert.equal(host.calls.stop, 1);
  assert.equal(batch.phase, 'stopped');
  third.resolve(ok());
  await tick();
  assert.equal(batch.staged.length, 2);
  assert.equal(batch.states.get('p2'), 'wait');
  assert.equal(batch.failures.size, 0);
  assert.equal(host.calls.run.length, 3);
});

test('batch: failures mapped to one line each; retry runs only the failed ones with the re-picked File', async () => {
  const host = fakeHost();
  const list = items(4, host);
  const batch = new ExportBatch(list, host);
  batch.start();
  await tick();
  host.pending.shift().resolve(ok());
  await tick();
  host.pending.shift().resolve({ ok: false, kind: 'read', code: 'read', message: 'NotReadableError' });
  await tick();
  host.pending.shift().resolve({ ok: false, kind: 'trap', code: 'trap', message: '' });
  await tick();
  host.pending.shift().resolve({ ok: false, kind: 'core', code: 'no_jpg_source', message: '无法读取 RAW 文件。这张照片没有相机 JPG。' });
  await batch.settled();
  assert.equal(batch.phase, 'ready');
  assert.deepEqual(host.calls.gone, [['p1', 1]]);
  assert.deepEqual([...batch.failures.values()].map((f) => f.message), [TEXT.read, TEXT.memory, '无法读取 RAW 文件。这张照片没有相机 JPG。']);
  // p1 was re-picked: new version and File.
  const fresh = new File([new Uint8Array(3)], 'R1.DNG');
  host.photos.set('p1', { version: 2, dng: fresh, jpg: null });
  host.calls.run.length = 0;
  batch.retry();
  await tick();
  assert.deepEqual(host.calls.run, ['p1']);
  assert.equal(host.pending[0].item.dng, fresh);
  assert.equal(host.pending[0].item.version, 2);
  host.pending.shift().resolve(ok());
  await tick();
  host.pending.shift().resolve(ok());
  await tick();
  host.pending.shift().resolve(ok());
  await batch.settled();
  assert.deepEqual(host.calls.run, ['p1', 'p2', 'p3']);
  assert.equal(batch.failures.size, 0);
  assert.equal(batch.staged.length, 4);
});

test('batch: stale session or photo version never commits', async () => {
  const host = fakeHost();
  const batch = new ExportBatch(items(3, host), host);
  batch.start();
  await tick();
  host.photos.set('p0', { version: 9, dng: null, jpg: null });
  host.pending.shift().resolve(ok());
  await tick();
  assert.equal(batch.states.get('p0'), 'fail');
  assert.equal(batch.failures.get('p0').message, TEXT.fallback);
  assert.equal(batch.staged.length, 0);
  host.setSession(2); // 清空照片 started a new session
  host.pending.shift().resolve(ok());
  await tick();
  assert.equal(batch.staged.length, 0);
  assert.equal(host.calls.run.length, 2);
});

test('batch: cancel (清空) drops files and late results', async () => {
  const host = fakeHost();
  const batch = new ExportBatch(items(2, host), host);
  batch.start();
  await tick();
  host.pending.shift().resolve(ok());
  await tick();
  const late = host.pending.shift();
  batch.cancel();
  late.resolve(ok());
  await tick();
  assert.equal(batch.phase, 'closed');
  assert.equal(batch.staged.length, 0);
  assert.equal(host.calls.stop, 1);
});

test('batch: the staging cap is never passed; the over-cap result is dropped and redone after the hand-off (Sol r1 P2-05)', async () => {
  const host = fakeHost();
  const batch = new ExportBatch(items(4, host), host, 100);
  batch.start();
  for (const size of [40, 50]) {
    await tick();
    host.pending.shift().resolve(ok(size));
  }
  await tick();
  host.pending.shift().resolve(ok(30)); // 40 + 50 + 30 > 100: dropped, p2 back to the front
  await tick();
  assert.equal(batch.phase, 'held');
  assert.deepEqual(batch.staged.map((s) => s.file.size), [40, 50]);
  assert.equal(batch.states.get('p2'), 'wait');
  assert.equal(batch.pending(), 2);
  assert.equal(host.pending.length, 0);
  assert.equal(batch.stagedHigh, 90);
  batch.release();
  await tick();
  assert.equal(batch.phase, 'running');
  assert.deepEqual(host.calls.run, ['p0', 'p1', 'p2', 'p2']);
  host.pending.shift().resolve(ok(30));
  await tick();
  host.pending.shift().resolve(ok(25));
  await batch.settled();
  assert.equal(batch.phase, 'ready');
  assert.deepEqual(batch.staged.map((s) => s.file.size), [30, 25]);
  assert.equal(batch.savedCount(), 2);
  assert.ok(batch.stagedHigh <= 100);
  assert.equal(STAGE_CAP_BYTES, 384 * 1024 * 1024);
});

test('batch: one file alone over the cap fails with the memory line', async () => {
  const host = fakeHost();
  const batch = new ExportBatch(items(2, host), host, 100);
  batch.start();
  await tick();
  host.pending.shift().resolve(ok(150));
  await tick();
  host.pending.shift().resolve(ok(20));
  await batch.settled();
  assert.equal(batch.failures.get('p0').message, TEXT.memory);
  assert.deepEqual(batch.staged.map((s) => s.file.size), [20]);
  assert.ok(batch.stagedHigh <= 100);
});

test('batch: after sharing the successes the failures keep their retry (Sol r1 P2-01)', async () => {
  const host = fakeHost();
  const batch = new ExportBatch(items(2, host), host);
  batch.start();
  await tick();
  host.pending.shift().resolve({ ok: false, kind: 'read', code: 'read', message: '' });
  await tick();
  host.pending.shift().resolve(ok());
  await batch.settled();
  batch.release();
  assert.equal(batch.phase, 'ready');
  assert.equal(batch.failures.size, 1);
  assert.equal(batch.staged.length, 0);
  assert.equal(batch.states.get('p1'), 'saved');
  batch.retry();
  await tick();
  host.pending.shift().resolve(ok());
  await batch.settled();
  assert.equal(batch.phase, 'ready');
  assert.equal(batch.failures.size, 0);
  assert.equal(batch.staged.length, 1);
  batch.release();
  assert.equal(batch.phase, 'closed');
});

test('batch: retry while held keeps the queue and starts after the hand-off (Sol r1 P2-02)', async () => {
  const host = fakeHost();
  const batch = new ExportBatch(items(5, host), host, 100);
  batch.start();
  await tick();
  host.pending.shift().resolve({ ok: false, kind: 'read', code: 'read', message: '' });
  for (const size of [60, 60]) {
    await tick();
    host.pending.shift().resolve(ok(size));
  }
  await tick();
  assert.equal(batch.phase, 'held');
  batch.retry();
  await tick();
  assert.equal(batch.phase, 'held');
  assert.equal(host.pending.length, 0);
  assert.equal(batch.pending(), 4); // p0 (retry), p2 (dropped), p3, p4 in grid order
  batch.release();
  for (const size of [1, 60, 10, 10]) {
    await tick();
    host.pending.shift().resolve(ok(size));
  }
  await batch.settled();
  assert.deepEqual(host.calls.run, ['p0', 'p1', 'p2', 'p0', 'p2', 'p3', 'p4']);
  assert.equal(batch.phase, 'ready');
  assert.deepEqual(batch.staged.map((s) => s.file.size), [1, 60, 10, 10]);
  assert.equal(batch.failures.size, 0);
});

test('batch: a run that throws (Worker creation RangeError) settles as a trap and the next photo runs (Sol r1 P2-04)', async () => {
  const host = fakeHost();
  let first = true;
  host.run = (item) => {
    host.calls.run.push(item.id);
    if (first) {
      first = false;
      throw new RangeError('synthetic');
    }
    return Promise.resolve(ok());
  };
  const batch = new ExportBatch(items(2, host), host);
  batch.start();
  await batch.settled();
  assert.equal(batch.phase, 'ready');
  assert.equal(batch.failures.get('p0').message, TEXT.memory);
  assert.equal(batch.staged.length, 1);
  assert.equal(host.calls.stop, 1);
  assert.equal(host.calls.busy.at(-1), false);
});

test('failText: core message as is, unknown kinds use the desktop fallback', () => {
  assert.equal(failText('core', 'bad_adjust', '请求无效。请刷新页面后重试。'), '请求无效。请刷新页面后重试。');
  assert.equal(failText('core', 'unknown', ''), TEXT.fallback);
  assert.equal(failText('core', 'x', ''), TEXT.fallback);
  assert.equal('ink' in TEXT, false);
});

test('border / stamp sidecar, layout logos + GXR_MARK, mapInkError, and packLogoEntries', async () => {
  const { DEFAULT_FRAME, DEFAULT_STAMP } = await import('../../mobile/src/lib/frame_spec.ts');
  const { GXR_MARK, CJK_REFUSE_MESSAGE } = await import('../../mobile/src/lib/fonts.ts');
  const { MISSING_LOGO_LINE, packLogoEntries } = await import('../../mobile/src/lib/logo_key.ts');

  assert.equal(req.borderJson(DEFAULT_FRAME), null);
  assert.equal(req.stampJson(DEFAULT_STAMP), null);

  const logoInfo = { id: 'custom-1', name: 'Studio', w: 2, h: 1, coverage: 0.5, opaque: false, lum: 0.2 };
  const border = { ...DEFAULT_FRAME, style: 'exhibition_label', show_logo: true, logo: 'custom-1', text: 'GXR' };
  const stampPhoto = { ...DEFAULT_STAMP, enabled: true, placement: 'photo' };
  const stampBorder = { ...DEFAULT_STAMP, enabled: true, placement: 'border' };

  assert.equal(req.usedLogoId(border), 'custom-1');
  assert.equal(req.usedLogoId({ ...border, show_logo: false }), null);
  assert.equal(req.usedLogoId({ ...border, logo: 'ricoh' }), null);
  assert.equal(req.usedLogoId({ ...border, style: 'gallery' }), null);

  assert.equal('placement' in req.stampJson(stampPhoto), false);
  assert.equal(req.stampJson(stampBorder).placement, 'border');

  const r = req.buildRequest(
    photo({
      border,
      stamp: stampBorder,
      logoInfo,
      frame: { orientation: 1, width: 4288, height: 2848 },
    }),
    CARDS,
    OPTS,
    'b',
  );
  assert.equal(r.side.border.version, 2);
  assert.equal(r.side.border.style, 'exhibition_label');
  assert.equal(r.side.stamp.placement, 'border');
  assert.deepEqual(r.side.frame, { orientation: 1, width: 4288, height: 2848 });
  assert.deepEqual(r.layout, { logos: [logoInfo], mark: GXR_MARK });

  assert.deepEqual(req.mapInkError(JSON.stringify({ error: { kind: 'missing_cjk', message: 'cjk' } })), {
    kind: 'core',
    code: 'missing_cjk',
    message: CJK_REFUSE_MESSAGE,
  });
  assert.deepEqual(req.mapInkError(JSON.stringify({ error: { kind: 'unknown_logo', message: 'missing' } })), {
    kind: 'core',
    code: 'unknown_logo',
    message: MISSING_LOGO_LINE,
  });
  assert.deepEqual(req.mapInkError(JSON.stringify({ error: { kind: 'bad_state', message: 'x' } })), {
    kind: 'core',
    code: 'ink_error',
    message: req.INK_FALLBACK_MESSAGE,
  });
  assert.deepEqual(req.mapInkError('plain boom'), {
    kind: 'core',
    code: 'ink_error',
    message: req.INK_FALLBACK_MESSAGE,
  });
  assert.equal(req.mapInkError(new WebAssembly.RuntimeError('unreachable')).kind, 'trap');
  assert.equal(req.mapInkError(new RangeError('oom')).kind, 'trap');

  const rgba = new Uint8Array([10, 20, 30, 255, 40, 50, 60, 128]);
  const packed = packLogoEntries([{ id: 'custom-1', w: 2, h: 1, rgba }]);
  assert.deepEqual(JSON.parse(packed.logoIndex), [['custom-1', 2, 1]]);
  assert.deepEqual([...packed.logoBlob], [...rgba]);
});

test('page budget counts same-stage JS and lowers the staging cap', async () => {
  const mib = 1024 * 1024;
  const peak = 512 * mib + 400 * mib;
  const requested = 384 * mib;
  // The old check ignored JS, so 512 + 384 still looked inside 1.2 GiB.
  assert.ok(512 * mib + requested <= PAGE_BUDGET_BYTES);
  assert.ok(peak + requested > PAGE_BUDGET_BYTES);
  const lowered = stageCapFor(peak, requested);
  assert.ok(lowered < requested);
  assert.ok(peak + lowered <= PAGE_BUDGET_BYTES);
  const host = fakeHost();
  host.memoryPeak = () => peak;
  const batch = new ExportBatch(items(1, host), host, requested);
  batch.start();
  await tick();
  host.pending.shift().resolve(ok(32, 'R0_gxr.jpg'));
  await batch.settled();
  assert.equal(batch.cap, lowered);
  assert.equal(batch.requestedCap, requested);
  assert.equal(batch.staged.length, 1);
});
