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

// ExportCore (the real one, with a fake Worker below) clears and sets its idle timer through `window`.
globalThis.window ??= globalThis;
const batchMod = await import('../../mobile/src/app/export_batch.ts');
const { estimatePeak } = await import('../../mobile/src/app/export_budget.ts');
const { ExportCore } = await import('../../mobile/src/core/export_api.ts');
const { ExportBatch, FileUnreadable, PAGE_BUDGET_BYTES, STAGE_CAP_BYTES, STAGE_CAP_HDR_BYTES, TEXT, WASM_CEILING_BYTES, WASM_CEILING_HDR_BYTES, stagePeakEstimate } = batchMod;
const MIB = 1024 * 1024;

/**
 * `plan[i]` = { size, peak, estimate? }. `estimate` defaults to `peak` (an exact estimate); `null` means the
 * host has no estimate for it; `'throw'` makes the estimate fail; `'unreadable'` throws `FileUnreadable`, as
 * export_budget does when a file cannot be read. Runs resolve on their own after a tick.
 */
function harness(plan, { cap = STAGE_CAP_BYTES, estimates = true } = {}) {
  const runs = [];
  const invariant = [];
  const gone = [];
  let observed = 0;
  let batch = null;
  const host = {
    session: () => 1,
    photo: () => ({ version: 1, dng: null, jpg: null }),
    stop() {},
    idle() {},
    markGone(id, version) {
      gone.push([id, version]);
    },
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
      if (step.estimate === 'unreadable') throw new FileUnreadable();
      return step.estimate === undefined ? step.peak : step.estimate ?? 0;
    };
  }
  const items = plan.map((_, i) => ({ id: `p${i}`, stem: `S${i}`, version: 1, dng: null, jpg: null, req: { hdr: false } }));
  batch = new ExportBatch(items, host, cap);
  return { batch, runs, invariant, gone };
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

/** A File stand-in: `size`, and `slice().arrayBuffer()` that rejects like a deleted or changed file when `gone`. */
function file(size, gone = false) {
  return {
    size,
    slice: () => ({
      arrayBuffer: async () => {
        if (gone) throw new DOMException('A requested file or directory could not be found', 'NotFoundError');
        return new ArrayBuffer(1);
      },
    }),
  };
}

function photoItem(dng, jpg, hdr = false) {
  return { id: 'p0', stem: 'S0', version: 1, dng, jpg, req: { side: {}, layout: {}, needsJpg: true, mtimeMs: 0, tzOffsetMin: 0, hdr } };
}

const FONTS = { fontIndex: '', fontPack: null };
const SIZED = { photo: [4000, 3000], sizes: { original: [5000, 6000], small: [1600, 1920] } };

test('estimatePeak: sizes from the file heads, without sending the batch setup', async () => {
  const calls = [];
  const core = {
    async sizes(job, withSetup) {
      calls.push({ job, withSetup });
      return { ok: true, json: JSON.stringify(SIZED), source: 4288 * 2848 };
    },
  };
  const small = await estimatePeak(core, photoItem(file(20 * MIB), file(5 * MIB)), 'small', FONTS);
  assert.equal(calls[0].withSetup, false, 'the setup is left for run()');
  assert.equal(calls[0].job.useJpg, true);
  const canvas = 1600 * 1920;
  const photo = 4000 * 3000 * (canvas / (5000 * 6000));
  assert.equal(small, stagePeakEstimate({ source: 4288 * 2848, photo, canvas, fileBytes: 20 * MIB, hdr: false }));
});

test('estimatePeak: a file that cannot be read is FileUnreadable, even when the probe answers', async () => {
  const calls = [];
  const probe = {
    async sizes() {
      calls.push('sizes');
      return { ok: true, json: JSON.stringify(SIZED), source: 4288 * 2848 };
    },
  };
  for (const item of [photoItem(file(20 * MIB), file(5 * MIB, true)), photoItem(file(20 * MIB, true), file(5 * MIB)), photoItem(null, file(5 * MIB, true))]) {
    await assert.rejects(estimatePeak(probe, item, 'original', FONTS), FileUnreadable);
  }
  assert.equal(calls.length, 3, 'the probe answered each time; the unreadable file still wins');
});

test('estimatePeak: a failed probe on readable files fails the estimate (whole page budget), never a guessed size', async () => {
  // Readable files whose probe fails (Worker stopped, header without a size): the estimate fails, so the batch
  // counts the whole page budget. It must not fall back to a size it did not read.
  for (const reply of [{ ok: false }, { ok: true, json: '{}' }]) {
    const failing = { sizes: async () => reply };
    const err = await estimatePeak(failing, photoItem(file(20 * MIB), file(5 * MIB), true), 'original', FONTS).then(
      (value) => value,
      (e) => e,
    );
    assert.ok(err instanceof Error && err.name !== 'FileUnreadable', `failed probe ${JSON.stringify(reply)} gave ${err}`);
  }
});

test('a photo whose file cannot be read settles as read without being computed, and never holds the batch', async () => {
  // M3 item 9 (a file deleted before its turn): with a file already staged, the unreadable photo fails with the
  // 06f read line and goes the M1 gone way, exactly as when the Worker cannot read it; the next photo still runs.
  const { batch, runs, invariant, gone } = harness([
    { size: 10 * MIB, peak: 400 * MIB },
    { size: 1, peak: 400 * MIB, estimate: 'unreadable' },
    { size: 10 * MIB, peak: 400 * MIB },
  ]);
  batch.start();
  await batch.settled();
  assert.equal(batch.phase, 'ready');
  assert.deepEqual(runs.map((r) => r.index), [0, 2], 'the unreadable photo was never computed');
  assert.deepEqual([...batch.failures.values()].map((f) => [f.id, f.kind, f.message]), [['p1', 'read', TEXT.read]]);
  assert.deepEqual(gone, [['p1', 1]]);
  assert.equal(batch.stagedBytes, 20 * MIB);
  assert.ok(invariant.every((row) => row.phase !== 'held'), 'never held');
  assert.equal(batch.estimates.has('p1'), false);
  budgetHeld(runs);
  capHeld(invariant);
});

test("the preflight probe never sends the batch setup: a setup failure still lands on the photo's run as a trap", async () => {
  // Real ExportCore, fake Worker (Sol r2 P2-04 kept under the per-photo preflight). The probe answers from the
  // file heads; the setup copy goes out with run(), inside its try.
  for (const failSetup of [false, true]) {
    const posts = [];
    const core = new ExportCore(() => ({
      postMessage(msg) {
        posts.push(msg.op);
        if (msg.op === 'setup' && failSetup) throw new RangeError('synthetic setup allocation');
        if (msg.op === 'sizes') queueMicrotask(() => this.onmessage({ data: { op: 'sizes', id: msg.job.id, ok: true, json: JSON.stringify(SIZED), source: 4288 * 2848 } }));
        if (msg.op === 'run') queueMicrotask(() => this.onmessage({ data: { op: 'done', id: msg.job.id, ok: true, file: new ArrayBuffer(4), result: {}, shot: {}, ms: 1 } }));
      },
      terminate() {},
    }));
    core.begin({ dat: new ArrayBuffer(4), calibIndex: '', calibBlob: null });
    const estimate = await estimatePeak(core, photoItem(file(20 * MIB), file(5 * MIB)), 'original', FONTS);
    assert.ok(estimate > 0, 'the probe answered');
    assert.deepEqual(posts, ['sizes'], 'no setup with the probe');
    const run = await core.run({ dng: null, jpg: null, req: {} });
    if (failSetup) {
      assert.equal(run.ok, false);
      assert.equal(run.kind, 'trap', 'the setup failure is this photo\'s trap');
      assert.deepEqual(posts, ['sizes', 'setup']);
      assert.equal(core.alive(), false, 'the Worker is closed');
    } else {
      assert.equal(run.ok, true);
      assert.deepEqual(posts, ['sizes', 'setup', 'run']);
    }
    core.stop();
  }
});

test('stop while the next photo is being estimated leaves no export Worker behind', async () => {
  // M3 item 9 (stop after 2 of 5) with the real ExportBatch and ExportCore: 停止 lands in the same task as the
  // commit that repaints the dock (a microtask, like the page's MutationObserver). The probe for the next photo
  // must already be on the Worker that stop() closes; nothing may create a Worker after the stop.
  let created = 0;
  const core = new ExportCore(() => {
    created += 1;
    return {
      postMessage(msg) {
        if (msg.op === 'sizes') queueMicrotask(() => this.onmessage({ data: { op: 'sizes', id: msg.job.id, ok: true, json: JSON.stringify(SIZED), source: 4288 * 2848 } }));
        if (msg.op === 'run') setTimeout(() => this.onmessage({ data: { op: 'done', id: msg.job.id, ok: true, file: new ArrayBuffer(4), result: {}, shot: {}, ms: 1 } }), 0);
      },
      terminate() {},
    };
  });
  core.begin({ dat: null, calibIndex: '', calibBlob: null });
  let batch = null;
  let stopQueued = false;
  const host = {
    session: () => 1,
    photo: () => ({ version: 1, dng: null, jpg: null }),
    run: (item) => core.run({ dng: item.dng, jpg: item.jpg, req: item.req }),
    stop: () => core.stop(),
    idle: () => core.release(),
    markGone() {},
    busy() {},
    changed() {
      if (batch && !stopQueued && batch.phase === 'running' && batch.okCount() === 1) {
        stopQueued = true;
        queueMicrotask(() => batch.stop());
      }
    },
    memoryPeak: () => core.marks.total,
    peakEstimate: (item) => estimatePeak(core, item, 'original', FONTS),
  };
  const items = [0, 1, 2].map((i) => ({ ...photoItem(file(20 * MIB), file(5 * MIB)), id: `p${i}`, stem: `S${i}` }));
  batch = new ExportBatch(items, host);
  batch.start();
  await batch.settled();
  await new Promise((done) => setTimeout(done, 10));
  assert.equal(batch.phase, 'stopped');
  assert.equal(batch.stagedBytes, 4);
  assert.equal(core.alive(), false, 'stop closed the Worker and none was made after it');
  assert.equal(created, 1);
});
