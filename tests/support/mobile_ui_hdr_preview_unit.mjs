// HDR 预览的条件、晚到结果、拖动时不发拟合、关掉时释放纹理。合成数据，不读照片。

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

const { previewWanted, debugForceHdrDisplay, hdrDisplayCapable } = await import('../../mobile/src/core/hdr_display.ts');
const { HdrPreviewController, HDR_REFIT_MS, ticketMatches } = await import('../../mobile/src/core/hdr_preview.ts');
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
