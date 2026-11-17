// Sol final-r2 P2-05: the page budget is checked before each photo starts, with the real ExportBatch.
// Synthetic results only: no photos, no Worker. Each fake run reports the same-stage peak it was planned
// to reach, and the host gives the batch that plan as its estimate before the run, as export_budget does
// with export_output_sizes. Every run records how much was staged at that moment.

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

const batchMod = await import('../../mobile/src/app/export_batch.ts');
const { estimatePeak } = await import('../../mobile/src/app/export_budget.ts');
const { CEILING_CANVAS_PX, CEILING_SOURCE_PX, ExportBatch, PAGE_BUDGET_BYTES, STAGE_CAP_BYTES, STAGE_CAP_HDR_BYTES, WASM_CEILING_BYTES, WASM_CEILING_HDR_BYTES, stagePeakEstimate } = batchMod;
const MIB = 1024 * 1024;

/**
 * `plan[i]` = { size, peak, estimate? }. `estimate` defaults to `peak` (an exact estimate); `null` means the
 * host has no estimate for it; `'throw'` makes the estimate fail. Runs resolve on their own after a tick.
 */
function harness(plan, { cap = STAGE_CAP_BYTES, estimates = true } = {}) {
  const runs = [];
  const invariant = [];
  let observed = 0;
  let batch = null;
  const host = {
    session: () => 1,
    photo: () => ({ version: 1, dng: null, jpg: null }),
    stop() {},
    idle() {},
    markGone() {},
    busy() {},
    changed() {
      if (batch) invariant.push({ staged: batch.stagedBytes, cap: batch.cap, phase: batch.phase });
    },
    memoryPeak: () => observed,
    async run(item, index) {
      const step = plan[index];
      runs.push({ index, stagedAtStart: batch.stagedBytes, peak: step.peak });
      await new Promise((done) => setTimeout(done, 0));
      observed = Math.max(observed, step.peak);
      return { ok: true, file: new ArrayBuffer(step.size), result: { file_name: `S${index}_gxr.jpg` }, shot: {}, ms: 1 };
    },
  };
  if (estimates) {
    host.peakEstimate = async (item) => {
      const step = plan[Number(item.id.slice(1))];
      if (step.estimate === 'throw') throw new Error('sizes');
      return step.estimate === undefined ? step.peak : step.estimate ?? 0;
    };
  }
  const items = plan.map((_, i) => ({ id: `p${i}`, stem: `S${i}`, version: 1, dng: null, jpg: null, req: { hdr: false } }));
  batch = new ExportBatch(items, host, cap);
  return { batch, runs, invariant };
}

/** Peak + what was staged when each run started never passes the page budget. */
function budgetHeld(runs) {
  for (const run of runs) assert.ok(run.peak + run.stagedAtStart <= PAGE_BUDGET_BYTES, `run ${run.index} started over the page budget`);
}

/** stagedBytes never exceeds the cap, at every notification. */
function capHeld(invariant) {
  for (const row of invariant) assert.ok(row.staged <= row.cap, `staged ${row.staged} over cap ${row.cap} (${row.phase})`);
}

test("Sol's case: 16 MiB staged, then a photo peaking at budget - 8 MiB is held before it is computed", async () => {
  const { batch, runs, invariant } = harness([
    { size: 16 * MIB, peak: 512 * MIB },
    { size: 1, peak: PAGE_BUDGET_BYTES - 8 * MIB },
  ]);
  batch.start();
  await batch.settled();
  assert.equal(batch.phase, 'held');
  assert.equal(runs.length, 1, 'the second photo did not start');
  assert.equal(batch.pending(), 1);
  assert.equal(batch.stagedBytes, 16 * MIB);
  assert.equal(batch.estimates.get('p1'), PAGE_BUDGET_BYTES - 8 * MIB);
  assert.ok(batch.estimates.get('p1') + batch.stagedBytes > PAGE_BUDGET_BYTES);
  // The user shares the staged file: the photo runs with nothing staged, and the batch finishes.
  batch.release();
  await batch.settled();
  assert.equal(runs.length, 2);
  assert.equal(runs[1].stagedAtStart, 0);
  assert.equal(batch.phase, 'ready');
  assert.equal(batch.stagedBytes, 1);
  budgetHeld(runs);
  capHeld(invariant);
});

test('rising peaks with files already staged hold before the photo that would not fit', async () => {
  const plan = [
    { size: 100 * MIB, peak: 500 * MIB },
    { size: 100 * MIB, peak: 800 * MIB },
    { size: 10 * MIB, peak: 1100 * MIB },
    { size: 10 * MIB, peak: 600 * MIB },
  ];
  const { batch, runs, invariant } = harness(plan);
  batch.start();
  await batch.settled();
  assert.equal(batch.phase, 'held');
  assert.deepEqual(runs.map((r) => r.index), [0, 1]);
  assert.equal(batch.stagedBytes, 200 * MIB);
  assert.equal(batch.cap, STAGE_CAP_BYTES, '800 MiB + 384 MiB still fits, so the cap is not lowered');
  batch.release();
  await batch.settled();
  assert.deepEqual(runs.map((r) => r.index), [0, 1, 2, 3]);
  assert.equal(runs[2].stagedAtStart, 0);
  // After the 1100 MiB photo the cap drops to what the budget leaves (128.8 MiB), never below what is staged.
  assert.equal(batch.cap, PAGE_BUDGET_BYTES - 1100 * MIB);
  assert.equal(batch.stagedBytes, 20 * MIB);
  budgetHeld(runs);
  capHeld(invariant);
});

test('a cap that would shrink below the staged files holds first, and keeps stagedBytes <= cap', async () => {
  // No estimates: the peak rise is only seen after the photo. The cap would fall to 128.8 MiB with 200 MiB
  // staged, so the batch holds (dropping that result), and lowers the cap only once the files are released.
  const { batch, runs, invariant } = harness(
    [
      { size: 200 * MIB, peak: 400 * MIB },
      { size: 50 * MIB, peak: 1100 * MIB },
      { size: 50 * MIB, peak: 1100 * MIB },
    ],
    { estimates: false },
  );
  batch.start();
  await batch.settled();
  assert.equal(batch.phase, 'held');
  assert.equal(runs.length, 2);
  assert.equal(batch.stagedBytes, 200 * MIB);
  assert.equal(batch.cap, 200 * MIB, 'the cap stops at what is staged');
  assert.equal(batch.pending(), 2, 'the photo whose result was dropped is queued again');
  batch.release();
  assert.equal(batch.cap, PAGE_BUDGET_BYTES - 1100 * MIB, 'released: the cap drops to the budget');
  await batch.settled();
  assert.deepEqual(runs.map((r) => r.index), [0, 1, 1, 2]);
  assert.equal(batch.stagedBytes, 100 * MIB);
  capHeld(invariant);
});

test('an estimate that fails counts as the whole budget: the batch holds while anything is staged', async () => {
  const { batch, runs, invariant } = harness([
    { size: 10 * MIB, peak: 400 * MIB },
    { size: 10 * MIB, peak: 400 * MIB, estimate: 'throw' },
  ]);
  batch.start();
  await batch.settled();
  assert.equal(batch.phase, 'held');
  assert.equal(runs.length, 1);
  assert.equal(batch.estimates.get('p1'), PAGE_BUDGET_BYTES);
  batch.release();
  await batch.settled();
  assert.equal(runs.length, 2);
  assert.equal(runs[1].stagedAtStart, 0);
  capHeld(invariant);
});

test('the first photo never waits: with nothing staged there is nothing to hand over', async () => {
  const { batch, runs } = harness([{ size: 1, peak: PAGE_BUDGET_BYTES + MIB }]);
  batch.start();
  await batch.settled();
  assert.equal(runs.length, 1);
  assert.equal(batch.estimates.get('p0'), PAGE_BUDGET_BYTES + MIB);
});

test('stop and cancel during the estimate leave nothing running', async () => {
  for (const end of ['stop', 'cancel']) {
    const { batch, runs } = harness([{ size: 1, peak: 1 }, { size: 1, peak: 1 }]);
    batch.start();
    batch[end]();
    await batch.settled();
    assert.equal(runs.length, 0, end);
  }
});

test('stagePeakEstimate: ink-text WASM ceiling plus what the Worker holds in JS, scaled up for bigger photos', () => {
  const source = 4288 * 2848;
  const canvas = 4875 * 6500;
  const file = 25 * MIB;
  const sdr = stagePeakEstimate({ source, photo: source, canvas, fileBytes: file, hdr: false });
  assert.equal(sdr, WASM_CEILING_BYTES + canvas * 3 + source + file);
  const hdr = stagePeakEstimate({ source, photo: source, canvas, fileBytes: file, hdr: true });
  assert.equal(hdr, WASM_CEILING_HDR_BYTES + canvas * 3 + source * 9 + file);
  // Twice the canvas of the measured case doubles the WASM term.
  const big = stagePeakEstimate({ source, photo: source, canvas: canvas * 2, fileBytes: file, hdr: false });
  assert.equal(big, WASM_CEILING_BYTES * 2 + canvas * 6 + source + file);
  // The measured high-water marks of item 9 (WASM 372 + JS 99 MiB SDR, 389 + 209 MiB HDR) sit under it.
  assert.ok(472 * MIB < sdr && 599 * MIB < hdr);
  // A full SDR staging cap still fits next to the SDR estimate; HDR holds earlier than its 256 MiB cap.
  assert.ok(sdr + STAGE_CAP_BYTES <= PAGE_BUDGET_BYTES);
  assert.ok(hdr + STAGE_CAP_HDR_BYTES > PAGE_BUDGET_BYTES);
});

test('estimatePeak: sizes from the file heads without sending the batch setup; a failed probe falls back to the measured case', async () => {
  const file = (size) => ({ size });
  const item = { id: 'p0', stem: 'S0', version: 1, dng: file(20 * MIB), jpg: file(5 * MIB), req: { side: {}, layout: {}, needsJpg: true, mtimeMs: 0, tzOffsetMin: 0, hdr: false } };
  const fonts = { fontIndex: '', fontPack: null };
  const calls = [];
  const sized = { photo: [4000, 3000], sizes: { original: [5000, 6000], small: [1600, 1920] } };
  const core = {
    async sizes(job, withSetup) {
      calls.push({ job, withSetup });
      return { ok: true, json: JSON.stringify(sized), source: 4288 * 2848 };
    },
  };
  const small = await estimatePeak(core, item, 'small', fonts);
  assert.equal(calls[0].withSetup, false, 'the setup is left for run()');
  assert.equal(calls[0].job.useJpg, true);
  const canvas = 1600 * 1920;
  const photo = 4000 * 3000 * (canvas / (5000 * 6000));
  assert.equal(small, stagePeakEstimate({ source: 4288 * 2848, photo, canvas, fileBytes: 20 * MIB, hdr: false }));
  const failing = { async sizes() { return { ok: false }; } };
  const fallback = await estimatePeak(failing, { ...item, req: { ...item.req, hdr: true } }, 'original', fonts);
  assert.equal(fallback, stagePeakEstimate({ source: CEILING_SOURCE_PX, photo: CEILING_SOURCE_PX, canvas: CEILING_CANVAS_PX, fileBytes: 20 * MIB, hdr: true }));
});
