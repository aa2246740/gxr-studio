// UI-M3-HDR 的纯逻辑：开关能不能开、混着只有 JPG 的那一行、记住的选择、请求里的 hdr、结果那一行和逐张说明、
// HDR 批次的待存上限。Node 自带的测试直接跑 TypeScript。全是合成数据，不读照片。

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

const hdr = await import('../../mobile/src/lib/hdr_text.ts');
const prefs = await import('../../mobile/src/lib/prefs.ts');
const req = await import('../../mobile/src/lib/export_request.ts');
const { ExportBatch, STAGE_CAP_BYTES, STAGE_CAP_HDR_BYTES } = await import('../../mobile/src/app/export_batch.ts');

const ZERO = { exposure_ev: 0, contrast: 0, highlight: 0, shadow: 0, black_point: 0, warm: 0, tint: 0, saturation: 0, vibrance: 0, vignette: 0 };
const photo = (over = {}) => ({
  stem: 'R0000001',
  dngName: 'R0000001.DNG',
  jpgName: 'R0000001.JPG',
  lens: 'RICOH A12 50mm F2.5 MACRO',
  iso: 200,
  choice: null,
  levels: new Map(),
  knobs: { ...ZERO },
  sourceModified: Date.UTC(2026, 10, 16, 1, 2, 3),
  ...over,
});

test('switch: available when any photo has a DNG (desktop hdrAvailable)', () => {
  assert.equal(hdr.hdrAvailable([{ dng: {} }, { dng: null }]), true);
  assert.equal(hdr.hdrAvailable([{ dng: null }, { dng: null }]), false);
  assert.equal(hdr.hdrAvailable([]), false);
  assert.equal(hdr.HDR_NO_RAW, 'HDR 需要 RAW 文件');
  assert.equal('HDR_LINE' in hdr, false);
  assert.equal(hdr.hdrLine(), '在 iPhone 上看，亮处会更亮，更接近你当时看到的；别的设备照常显示。');
  assert.equal(
    hdr.hdrLine({ userAgent: 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)', platform: 'MacIntel', maxTouchPoints: 5 }),
    '在 iPad 上看，亮处会更亮，更接近你当时看到的；别的设备照常显示。',
  );
});

test('05a mix line only when some, not all, photos are JPG-only', () => {
  assert.equal(hdr.hdrMixLine([{ dng: {} }, { dng: {} }, { dng: null }]), 'HDR 需要 RAW 文件：1 张只有 JPG，会存普通版本。');
  assert.equal(hdr.hdrMixLine([{ dng: {} }, { dng: null }, { dng: null }]), 'HDR 需要 RAW 文件：2 张只有 JPG，会存普通版本。');
  assert.equal(hdr.hdrMixLine([{ dng: {} }]), null);
  assert.equal(hdr.hdrMixLine([{ dng: null }]), null);
});

test('05a frame warning lists RAW-capable photos with blur_fill', () => {
  assert.equal(hdr.hdrFrameLine([{ dng: {}, border: { style: 'blur_fill' } }]), '1 张用的边框样式不能存 HDR，会存普通版本。');
  assert.equal(
    hdr.hdrFrameLine([
      { dng: {}, border: { style: 'blur_fill' } },
      { dng: {}, border: { style: 'gallery' } },
      { dng: null, border: { style: 'blur_fill' } },
    ]),
    '1 张用的边框样式不能存 HDR，会存普通版本。',
  );
  assert.equal(hdr.hdrFrameLine([{ dng: {}, border: { style: 'gallery' } }, { dng: null, border: { style: 'blur_fill' } }]), null);
});

test('remembered choice: on by default, gxr.mobile.export_hdr, storage failures ignored', () => {
  const data = new Map();
  globalThis.localStorage = {
    getItem: (k) => (data.has(k) ? data.get(k) : null),
    setItem: (k, v) => data.set(k, String(v)),
  };
  try {
    assert.equal(prefs.loadExportHdr(), true);
    prefs.saveExportHdr(false);
    assert.equal(data.get('gxr.mobile.export_hdr'), '0');
    assert.equal(prefs.loadExportHdr(), false);
    prefs.saveExportHdr(true);
    assert.equal(data.get('gxr.mobile.export_hdr'), '1');
    assert.equal(prefs.loadExportHdr(), true);
    globalThis.localStorage = { getItem: () => null, setItem: () => { throw new Error('quota'); } };
    assert.doesNotThrow(() => prefs.saveExportHdr(true));
    assert.equal(prefs.loadExportHdr(), true);
  } finally {
    delete globalThis.localStorage;
  }
});

test('request: hdr true for every photo when on (the core decides), false by default', () => {
  const opts = { size: 'original', quality: 'high' };
  assert.equal(req.buildRequest(photo(), [], opts, 'b').hdr, false);
  assert.equal(req.buildRequest(photo(), [], { ...opts, hdr: false }, 'b').hdr, false);
  assert.equal(req.buildRequest(photo(), [], { ...opts, hdr: true }, 'b').hdr, true);
  assert.equal(req.buildRequest(photo({ dngName: null }), [], { ...opts, hdr: true }, 'b').hdr, true);
  // HDR changes nothing else in the request.
  const off = req.buildRequest(photo(), [], opts, 'b');
  const on = req.buildRequest(photo(), [], { ...opts, hdr: true }, 'b');
  assert.deepEqual({ ...on, hdr: false }, off);
});

test('notes per reason are the desktop HDR_NOTES, anything else HDR_UNSUPPORTED', () => {
  assert.equal(hdr.hdrNote('no_raw'), 'HDR 需要 RAW 文件');
  assert.equal(hdr.hdrNote('capture_mismatch'), 'RAW 与 JPG 不是同一次拍摄');
  assert.equal(hdr.hdrNote('not_jpeg'), 'HDR 需要 JPEG 格式');
  assert.equal(hdr.hdrNote('frame_layout'), '这个边框样式不能存 HDR');
  // desktop-accept F10：RAW 和相机 JPG 对不齐的一对有自己的一句，不再是笼统的「不支持」。
  assert.equal(hdr.hdrNote('not_aligned'), 'RAW 和相机 JPG 画面对不上，这张存普通版本');
  assert.equal(hdr.hdrNote('no_curve'), '这张照片不支持 HDR');
  assert.equal(hdr.hdrNote(null), '这张照片不支持 HDR');
  assert.equal(hdr.hdrNote('toString'), '这张照片不支持 HDR');
  assert.equal(hdr.hdrSummary(2), '2 张未生成 HDR');
});

test('05c / 05e line: design wording, other reasons counted with hdrSummary and listed', () => {
  const r = (h, reason) => ({ hdr: h, ...(reason ? { hdr_reason: reason } : {}) });
  const rows = [
    { stem: 'A', result: r(true) },
    { stem: 'B', result: r(true) },
    { stem: 'C', result: r(true) },
    { stem: 'D', result: r(true) },
    { stem: 'E', result: r(false, 'no_raw') },
  ];
  assert.deepEqual(hdr.hdrResultText(rows), { line: '4 张 HDR · 1 张只有 JPG，存普通版本', notes: [] });
  const mixed = [
    ...rows,
    { stem: 'F', result: r(false, 'capture_mismatch') },
    { stem: 'G', result: r(false, 'raw_unreadable') },
    { stem: 'H', result: r(false, 'frame_layout') },
  ];
  assert.deepEqual(hdr.hdrResultText(mixed), {
    line: '4 张 HDR · 1 张只有 JPG，存普通版本 · 3 张未生成 HDR',
    notes: ['F：RAW 与 JPG 不是同一次拍摄', 'G：这张照片不支持 HDR', 'H：这个边框样式不能存 HDR'],
  });
  assert.deepEqual(hdr.hdrResultText([{ stem: 'A', result: r(true) }]), { line: '1 张 HDR', notes: [] });
  assert.deepEqual(hdr.hdrResultText([{ stem: 'A', result: undefined }, { stem: 'B', result: {} }]), { line: '', notes: [] });
});

test('HDR batches stage at most the lower cap; the batch knows it is HDR', async () => {
  assert.ok(STAGE_CAP_HDR_BYTES < STAGE_CAP_BYTES);
  const host = {
    pending: [],
    run(item) { return new Promise((resolve) => this.pending.push({ item, resolve })); },
    stop() {},
    idle() {},
    session: () => 1,
    photo: () => ({ version: 1, dng: null, jpg: null }),
    markGone() {},
    busy() {},
    changed() {},
  };
  const list = [0, 1].map((i) => ({ id: `p${i}`, stem: `R${i}`, version: 1, dng: null, jpg: null, req: { hdr: true } }));
  const batch = new ExportBatch(list, host, STAGE_CAP_HDR_BYTES);
  assert.equal(batch.hdr, true);
  assert.equal(batch.cap, STAGE_CAP_HDR_BYTES);
  assert.equal(new ExportBatch([{ ...list[0], req: { hdr: false } }], host).hdr, false);
  batch.start();
  await new Promise((done) => setTimeout(done, 0));
  const mk = (size, name) => ({ ok: true, file: new ArrayBuffer(size), result: { file_name: name, hdr: true }, shot: {}, ms: 1 });
  host.pending.shift().resolve(mk(STAGE_CAP_HDR_BYTES - 10, 'R0_gxr_hdr.jpg'));
  await new Promise((done) => setTimeout(done, 0));
  host.pending.shift().resolve(mk(20, 'R1_gxr_hdr.jpg'));
  await new Promise((done) => setTimeout(done, 0));
  assert.equal(batch.phase, 'held');
  assert.ok(batch.stagedHigh <= STAGE_CAP_HDR_BYTES);
  assert.deepEqual(batch.staged.map((s) => s.file.name), ['R0_gxr_hdr.jpg']);
  batch.cancel();
});
