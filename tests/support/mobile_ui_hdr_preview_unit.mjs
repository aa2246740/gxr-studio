// HDR 预览的条件、晚到结果、拖动时不发拟合、关掉时释放纹理。合成数据，不读照片。

import assert from 'node:assert/strict';
import { existsSync, mkdtempSync, readFileSync, renameSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
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

const { previewWanted, debugForceHdrDisplay, hdrDisplayCapable } = await import('../../mobile/src/core/hdr_display.ts');
const { HdrPreviewController, HDR_REFIT_MS, ticketMatches } = await import('../../mobile/src/core/hdr_preview.ts');
const { HdrFitHost } = await import('../../mobile/src/core/hdr_fit_host.ts');
const { moveReportFile } = await import('./move_report_file.mjs');
const { rgbaToRgb, downscaleRgba } = await import('../../mobile/src/core/hdr_grid.ts');
const { gainWeight } = await import('../../frontend/src/hdr_math.ts');

test('conditions: display, DNG and the export switch', () => {
  assert.equal(previewWanted(true, true, true), true);
  assert.equal(previewWanted(false, true, true), false);
  assert.equal(previewWanted(true, false, true), false);
  assert.equal(previewWanted(true, true, false), false);
});

test('headless probe stays false unless forced', () => {
  debugForceHdrDisplay(null);
  assert.equal(hdrDisplayCapable(null), false);
  debugForceHdrDisplay(true);
  assert.equal(hdrDisplayCapable(null), true);
  debugForceHdrDisplay(false);
  assert.equal(hdrDisplayCapable({}), false);
  debugForceHdrDisplay(null);
});

test('weight uses cap_max when the display has no headroom', () => {
  const meta = { gain_min: 0, gain_max: 1.5, gamma: 1, offset_sdr: 1 / 64, offset_hdr: 1 / 64, cap_min: 0, cap_max: 1.5 };
  assert.equal(gainWeight(meta, null), 1);
  assert.equal(gainWeight(meta, 2), 1 / 1.5);
});

test('rgba drops alpha and the preview scale stays small', () => {
  const rgba = new Uint8Array([1, 2, 3, 255, 4, 5, 6, 255]);
  assert.deepEqual([...rgbaToRgb(rgba, 2, 1)], [1, 2, 3, 4, 5, 6]);
  const big = new Uint8Array(800 * 600 * 4);
  const small = downscaleRgba(big, 800, 600, 512);
  assert.ok(Math.max(small.width, small.height) <= 512);
  assert.equal(small.rgba.length, small.width * small.height * 4);
  assert.ok(small.width * small.height < 1024 * 1024);
});

function harness() {
  const modes = [];
  let photo = { id: 'p', version: 1, rev: 0, dng: {}, jpg: null };
  let dragging = false;
  let holding = false;
  let exportOn = true;
  let display = true;
  const timers = new Map();
  let seq = 1;
  const engine = {
    setOutput(mode) { modes.push(mode); },
    async readback() { return { w: 4, h: 2, data: new Uint8Array(4 * 2 * 4) }; },
    hdrGainCount() { return modes.at(-1)?.hdr ? 1 : 0; },
  };
  const fits = [];
  let gate = null;
  const clock = {
    set(fn, ms) {
      const id = seq++;
      timers.set(id, { fn, ms });
      return id;
    },
    clear(id) { timers.delete(id); },
  };
  const ctl = new HdrPreviewController({
    photo: () => photo,
    session: () => 1,
    dragging: () => dragging,
    holding: () => holding,
    exportOn: () => exportOn,
    display: () => display,
    engine: () => engine,
    readDng: async () => new ArrayBuffer(8),
    fit: async (req) => {
      fits.push(req.ticket.photoId);
      if (gate) await gate;
      return { ok: true, width: 2, height: 1, codes: new Uint8Array([req.ticket.rev + 1]), meta: { gain_min: 0, gain_max: 1, gamma: 1, offset_sdr: 0, offset_hdr: 0, cap_min: 0, cap_max: 1 } };
    },
    clock,
  });
  async function fire() {
    const pending = [...timers.entries()];
    timers.clear();
    for (const [, timer] of pending) {
      assert.equal(timer.ms, HDR_REFIT_MS);
      timer.fn();
    }
    await new Promise((resolve) => setTimeout(resolve, 0));
    await new Promise((resolve) => setTimeout(resolve, 0));
  }
  return {
    ctl, modes, fits, timers, fire,
    hold() { let go; gate = new Promise((resolve) => { go = resolve; }); return () => { gate = null; go(); }; },
    set: (patch) => { if ('dragging' in patch) dragging = patch.dragging; if ('holding' in patch) holding = patch.holding; if ('exportOn' in patch) exportOn = patch.exportOn; if ('display' in patch) display = patch.display; if ('photo' in patch) photo = patch.photo; },
  };
}

test('a late grid after a photo switch is dropped and close releases the texture', async () => {
  const h = harness();
  const release = h.hold();
  h.ctl.sync();
  const started = h.fire();
  await new Promise((resolve) => setTimeout(resolve, 0));
  assert.equal(h.ctl.requests, 1);
  h.set({ photo: { id: 'q', version: 1, rev: 0, dng: {}, jpg: null } });
  h.ctl.sync();
  release();
  await started;
  h.ctl.close();
  assert.equal(h.modes.some((mode) => mode.hdr === true), false);
  assert.equal(h.ctl.showing, false);
  assert.equal(ticketMatches({ session: 1, photoId: 'q', version: 1, rev: 0 }, { session: 1, photoId: 'q', version: 2, rev: 0 }), false);
});

test('dragging does not start a fit and a settled edit replaces the grid', async () => {
  const h = harness();
  h.ctl.sync();
  await h.fire();
  assert.equal(h.ctl.requests, 1);
  h.set({ dragging: true });
  for (let i = 0; i < 30; i++) h.ctl.sync();
  assert.equal(h.timers.size, 0);
  assert.equal(h.ctl.requests, 1);
  h.set({ dragging: false, photo: { id: 'p', version: 1, rev: 1, dng: {}, jpg: null } });
  h.ctl.sync();
  await h.fire();
  assert.equal(h.ctl.requests, 2);
  assert.equal(h.modes.at(-1)?.gain?.codes?.[0], 2);
});

test('switch off and holding stay on the SDR output', async () => {
  const h = harness();
  h.ctl.sync();
  await h.fire();
  h.set({ exportOn: false });
  h.ctl.sync();
  assert.equal(h.modes.at(-1)?.hdr, false);
  h.set({ exportOn: true });
  h.ctl.sync();
  await h.fire();
  h.set({ holding: true });
  h.ctl.sync();
  assert.equal(h.modes.at(-1)?.hdr, false);
  assert.equal(h.ctl.showing, false);
});

function clocked() {
  const timers = new Map();
  let seq = 1;
  const modes = [];
  let photo = { id: 'p', version: 1, rev: 0, dng: {}, jpg: null, border: { style: 'none' } };
  const engine = {
    setOutput(mode) { modes.push(mode); },
    async readback() { return { w: 4, h: 2, data: new Uint8Array(4 * 2 * 4) }; },
    hdrGainCount() { return modes.at(-1)?.hdr ? 1 : 0; },
  };
  let readDng = async () => new ArrayBuffer(8);
  let fitImpl = async (req) => ({ ok: true, width: 1, height: 1, codes: new Uint8Array([req.ticket.rev + 1]), meta: { gain_min: 0, gain_max: 1, gamma: 1, offset_sdr: 0, offset_hdr: 0, cap_min: 0, cap_max: 1 } });
  let labels = 0;
  const ctl = new HdrPreviewController({
    photo: () => photo,
    session: () => 1,
    dragging: () => false,
    holding: () => false,
    exportOn: () => true,
    display: () => true,
    engine: () => engine,
    readDng: (item) => readDng(item),
    fit: (req) => fitImpl(req),
    clock: {
      set(fn, ms) {
        const id = seq++;
        timers.set(id, { fn, ms });
        return id;
      },
      clear(id) { timers.delete(id); },
    },
    onShowing() { labels = ctl.showing ? 1 : 0; },
    frameLayout: () => (photo.border?.style === 'blur_fill' ? '{"width":2,"height":2,"items":[],"bgfx":{"kind":"blur"}}' : null),
  });
  async function fire() {
    const pending = [...timers.entries()];
    timers.clear();
    for (const [, timer] of pending) timer.fn();
    await new Promise((resolve) => setTimeout(resolve, 0));
    await new Promise((resolve) => setTimeout(resolve, 0));
  }
  return {
    ctl, modes, engine, fire,
    labels: () => labels,
    setPhoto(next) { photo = next; },
    photo: () => photo,
    setReadDng(fn) { readDng = fn; },
    setFit(fn) { fitImpl = fn; },
  };
}

test('a failed read and a rejected fit clear HDR and do not reject', async () => {
  const leaks = [];
  const onReject = (err) => leaks.push(err);
  process.on('unhandledRejection', onReject);
  try {
    const h = clocked();
    h.ctl.sync();
    await h.fire();
    assert.equal(h.modes.at(-1)?.hdr, true);
    assert.equal(h.labels(), 1);
    h.photo().rev = 1;
    h.setReadDng(async () => null);
    h.ctl.sync();
    await h.fire();
    assert.equal(h.modes.at(-1)?.hdr, false);
    assert.equal(h.engine.hdrGainCount(), 0);
    assert.equal(h.labels(), 0);
    h.photo().rev = 2;
    h.setReadDng(async () => new ArrayBuffer(8));
    h.setFit(async () => { throw new Error('worker down'); });
    h.ctl.sync();
    await h.fire();
    await new Promise((resolve) => setTimeout(resolve, 0));
    assert.equal(h.modes.at(-1)?.hdr, false);
    assert.equal(leaks.length, 0);
  } finally {
    process.off('unhandledRejection', onReject);
  }
});

test('a stale failure does not clear the picture that replaced it', async () => {
  const h = clocked();
  h.ctl.sync();
  await h.fire();
  assert.equal(h.modes.at(-1)?.hdr, true);
  let release;
  const gate = new Promise((resolve) => { release = resolve; });
  h.photo().rev = 1;
  h.setFit(async () => gate.then(() => { throw new Error('old fit'); }));
  h.ctl.sync();
  const started = h.fire();
  await new Promise((resolve) => setTimeout(resolve, 0));
  h.setPhoto({ id: 'q', version: 1, rev: 0, dng: {}, jpg: null, border: { style: 'none' } });
  h.setFit(async () => ({ ok: true, width: 1, height: 1, codes: new Uint8Array([9]), meta: { gain_min: 0, gain_max: 1, gamma: 1, offset_sdr: 0, offset_hdr: 0, cap_min: 0, cap_max: 1 } }));
  h.ctl.sync();
  await h.fire();
  assert.equal(h.modes.at(-1)?.hdr, true);
  assert.equal(h.modes.at(-1)?.gain?.codes?.[0], 9);
  release();
  await started;
  assert.equal(h.modes.at(-1)?.gain?.codes?.[0], 9);
  assert.equal(h.engine.hdrGainCount(), 1);
});

test('blur_fill drops the in-flight reply and stays SDR', async () => {
  const h = clocked();
  let release;
  const gate = new Promise((resolve) => { release = resolve; });
  h.setFit(async () => gate.then(() => ({ ok: true, width: 1, height: 1, codes: new Uint8Array([1]), meta: { gain_min: 0, gain_max: 1, gamma: 1, offset_sdr: 0, offset_hdr: 0, cap_min: 0, cap_max: 1 } })));
  h.ctl.sync();
  const started = h.fire();
  await new Promise((resolve) => setTimeout(resolve, 0));
  h.photo().rev = 1;
  h.photo().border = { style: 'blur_fill' };
  h.setFit(async (req) => {
    assert.match(req.frameLayoutJson, /blur/);
    return { ok: false, reason: 'frame_layout' };
  });
  h.ctl.invalidate();
  await h.fire();
  release();
  await started;
  assert.equal(h.modes.some((mode) => mode.hdr === true), false);
  assert.equal(h.engine.hdrGainCount(), 0);
  assert.equal(h.ctl.showing, false);
  assert.equal(h.labels(), 0);
});

test('holding the original clears the HDR label', async () => {
  const host = {
    nodes: [],
    querySelector() { return this.nodes[0] ?? null; },
    append(node) { this.nodes = [node]; },
  };
  const real = globalThis.document;
  globalThis.document = { createElement() { return { dataset: {}, className: '', textContent: '', remove() { host.nodes = []; } }; } };
  try {
    const h = clocked();
    h.ctl.sync();
    await h.fire();
    h.ctl.paintLabel(host);
    assert.equal(host.nodes.length, 1);
    h.ctl.showing = false;
    h.ctl.paintLabel(host);
    assert.equal(host.nodes.length, 0);
  } finally {
    globalThis.document = real;
  }
});

test('the fit host keeps one worker, one in flight, and only the latest queued', async () => {
  const made = [];
  function port() {
    const worker = {
      dead: false,
      posts: [],
      onmessage: null,
      onerror: null,
      postMessage(message) { this.posts.push(message); },
      terminate() { this.dead = true; },
    };
    made.push(worker);
    return worker;
  }
  const host = new HdrFitHost(port);
  const job = (n) => ({
    sdr: new Uint8Array([n]),
    width: 1,
    height: 1,
    dng: new ArrayBuffer(1),
    jpg: null,
    base: null,
    baseWidth: 1,
    baseHeight: 1,
    geometryJson: null,
    cropJson: null,
    box: null,
    frameLayoutJson: null,
    factor: 4,
  });
  const first = host.request(job(1));
  const dropped = host.request(job(2));
  const latest = host.request(job(3));
  assert.equal((await dropped).reason, 'stale');
  assert.equal(made.length, 1);
  assert.equal(made[0].posts.length, 1);
  made[0].onmessage({ data: { id: made[0].posts[0].id, ok: true, json: JSON.stringify({ ok: true, width: 1, height: 1, meta: { gain_min: 0, gain_max: 1, gamma: 1, offset_sdr: 0, offset_hdr: 0, cap_min: 0, cap_max: 1 } }), buffers: [new Uint8Array([7]).buffer] } });
  assert.equal((await first).ok, true);
  assert.equal(made[0].posts.length, 2);
  host.stop();
  assert.equal((await latest).reason, 'stale');
  assert.equal(made[0].dead, true);
  assert.equal(made.length, 1);
});

test('moveReportFile copies when rename throws EXDEV and renames when it does not', () => {
  const dir = mkdtempSync(tmpdir() + '/gxr-move-');
  const src = join(dir, 'src.bin');
  const dst = join(dir, 'dst.bin');
  const sameSrc = join(dir, 'same-src.bin');
  const sameDst = join(dir, 'same-dst.bin');
  try {
    writeFileSync(src, 'cross-volume');
    let sawExdev = false;
    moveReportFile(src, dst, () => {
      sawExdev = true;
      const err = new Error('EXDEV');
      err.code = 'EXDEV';
      throw err;
    });
    assert.equal(sawExdev, true);
    assert.equal(existsSync(src), false);
    assert.equal(readFileSync(dst, 'utf8'), 'cross-volume');
    writeFileSync(sameSrc, 'same-volume');
    let renameCalls = 0;
    moveReportFile(sameSrc, sameDst, (from, to) => {
      renameCalls += 1;
      renameSync(from, to);
    });
    assert.equal(renameCalls, 1);
    assert.equal(existsSync(sameSrc), false);
    assert.equal(readFileSync(sameDst, 'utf8'), 'same-volume');
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});
