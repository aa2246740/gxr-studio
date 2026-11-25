// N5 回归单测：直接驱动真实的 core/pool.ts（pickSlot）和 looks.ts 的 CardBakeController。
// 2、3、4 个 Worker 上，离屏静默烘焙或可见卡烘焙正在跑的时候，连着点 1、2、3 张没烘过的配方卡（冷切：先 resolve
// 再 bake），每一个 resolve 和 bake 都不排在后台请求后面。两种点法都测：重叠（上一张还没回来就点下一张）和依次
// （上一张落到舞台再点）。用 GXR_TEST_POOL_PATH / GXR_TEST_LOOKS_PATH 换成旧版本文件做负对照。

import assert from 'node:assert/strict';
import { registerHooks } from 'node:module';
import test from 'node:test';
import { pathToFileURL } from 'node:url';

registerHooks({
  resolve(specifier, context, next) {
    return next(specifier === './protocol' ? './protocol.ts' : specifier, context);
  },
});

const looksUrl = process.env.GXR_TEST_LOOKS_PATH
  ? pathToFileURL(process.env.GXR_TEST_LOOKS_PATH).href
  : new URL('../../mobile/src/app/looks.ts', import.meta.url).href;
const poolUrl = process.env.GXR_TEST_POOL_PATH
  ? pathToFileURL(process.env.GXR_TEST_POOL_PATH).href
  : new URL('../../mobile/src/core/pool.ts', import.meta.url).href;

const { Pool } = await import(poolUrl);
const { CardBakeController } = await import(looksUrl);

const tick = () => new Promise((done) => setImmediate(done));
const isBg = (tag) => typeof tag === 'string' && tag.startsWith('bg-');

async function withPool(size, run) {
  const origNav = Object.getOwnPropertyDescriptor(globalThis, 'navigator');
  const origWorker = globalThis.Worker;
  const origFetch = globalThis.fetch;
  const origIdle = globalThis.requestIdleCallback;
  const origCancelIdle = globalThis.cancelIdleCallback;
  const log = [];
  const spawned = [];
  const idle = [];
  Object.defineProperty(globalThis, 'navigator', { configurable: true, value: { hardwareConcurrency: size + 1 } });
  globalThis.fetch = async () =>
    new Response(Uint8Array.from([0, 97, 115, 109, 1, 0, 0, 0]), { headers: { 'content-type': 'application/wasm' } });
  globalThis.Worker = class ControlledWorker {
    constructor() {
      this.slot = spawned.length;
      this.queue = [];
      spawned.push(this);
    }
    postMessage(msg) {
      if (!msg?.op) return;
      log.push({ slot: this.slot, op: msg.op, tag: msg.frameMeta, ahead: this.queue.map((q) => q.frameMeta) });
      this.queue.push(msg);
    }
    finish(tag) {
      const head = this.queue.shift();
      assert.equal(head?.frameMeta, tag, `expected ${tag} at the head of slot ${this.slot}, got ${head?.frameMeta}`);
      this.onmessage?.({ data: { id: head.id, ok: true, json: '{}', buffers: [] } });
    }
    terminate() {}
  };
  globalThis.requestIdleCallback = (fn) => idle.push(fn);
  globalThis.cancelIdleCallback = (id) => {
    idle[id - 1] = null;
  };
  const pool = new Pool();
  const controller = new CardBakeController(size);
  const request = (op, tag, background = false) => pool.request(op, null, [], null, { frameMeta: tag }, background);
  const inFlight = () => spawned.flatMap((w) => w.queue.map((m) => m.frameMeta));
  const slotOf = (tag) => spawned.find((w) => w.queue.some((m) => m.frameMeta === tag));
  const settle = async (count) => {
    for (let i = 0; i < 200 && log.length < count; i += 1) await new Promise((done) => setTimeout(done, 1));
  };
  /** 把 tag 所在槽里排在它前面的前台请求依次做完，再做完它自己。前面要是有后台请求，说明点击排在了后台后面。 */
  const finishThrough = async (tag) => {
    const worker = slotOf(tag);
    assert.ok(worker, `${tag} is not in flight`);
    while (worker.queue[0]?.frameMeta !== tag) {
      const head = worker.queue[0].frameMeta;
      assert.ok(!isBg(head), `${tag} waits behind background ${head} on slot ${worker.slot}`);
      worker.finish(head);
      await tick();
    }
    worker.finish(tag);
    await tick();
    // 浏览器这时候空着：排着的空闲回调都跑一遍（冷却期内它们不该派发新的离屏烘焙）。
    while (idle.length) idle.shift()?.();
    await tick();
  };
  try {
    // 先把每个槽都建好并装上 WASM（和打开编辑器之后一样），后台烘焙从空闲的池开始。
    const warm = Array.from({ length: size }, (_, i) => request('bake', `warm-${i}`));
    await settle(size);
    for (const w of spawned) w.finish(w.queue[0].frameMeta);
    await Promise.all(warm);
    log.length = 0;
    await run({ controller, request, inFlight, finishThrough, log, idle, spawned });
  } finally {
    controller.destroy();
    pool.stop();
    globalThis.Worker = origWorker;
    globalThis.fetch = origFetch;
    globalThis.requestIdleCallback = origIdle;
    globalThis.cancelIdleCallback = origCancelIdle;
    if (origNav) Object.defineProperty(globalThis, 'navigator', origNav);
    else delete globalThis.navigator;
  }
}

/** 静默离屏烘焙（quiet）或可见卡烘焙（visible）在跑：返回点击那一刻在跑的后台请求。 */
async function startBackground(fx, size, kind) {
  const jobs = Array.from({ length: size + 6 }, (_, i) => ({ id: `bg-${i}`, item: `bg-${i}` }));
  // 没有 IntersectionObserver 时前 6 张算可见；quiet 不给节点，全部算离屏。
  const nodes = kind === 'visible' ? jobs.map((job) => ({ dataset: { card: job.id } })) : [];
  fx.controller.observe(nodes);
  fx.controller.start(jobs, { isAlive: () => true, isBusy: () => false, run: (tag) => fx.request('bake', tag, true) });
  await tick();
  if (kind === 'quiet') {
    const cb = fx.idle.shift();
    assert.ok(cb, `size=${size}: quiet idle callback must be scheduled`);
    cb();
    await tick();
  }
  return fx.inFlight().filter(isBg);
}

function beginTap(fx, tag) {
  fx.controller.beginStage(tag, tag, true);
  const job = fx
    .request('resolve', `${tag}/resolve`)
    .then(() => fx.request('bake', `${tag}/bake`))
    .finally(() => fx.controller.endStage(tag, tag));
  job.catch(() => undefined);
  return job;
}

function assertNotBehindBackground(fx, tag, label) {
  const entry = fx.log.findLast((item) => item.tag === tag);
  assert.ok(entry, `${label}: ${tag} was never dispatched`);
  const behind = entry.ahead.filter(isBg);
  assert.deepEqual(behind, [], `${label}: ${tag} queued behind background ${JSON.stringify(behind)} on slot ${entry.slot}`);
}

async function runTaps(fx, label, taps, overlap) {
  const jobs = [];
  for (let i = 1; i <= taps; i += 1) {
    const tag = `tap${i}`;
    jobs.push(beginTap(fx, tag));
    await tick();
    assertNotBehindBackground(fx, `${tag}/resolve`, label);
    if (overlap) continue;
    await fx.finishThrough(`${tag}/resolve`);
    assertNotBehindBackground(fx, `${tag}/bake`, label);
    await fx.finishThrough(`${tag}/bake`);
    await jobs.at(-1);
  }
  if (!overlap) return;
  for (let i = 1; i <= taps; i += 1) {
    await fx.finishThrough(`tap${i}/resolve`);
    assertNotBehindBackground(fx, `tap${i}/bake`, label);
  }
  for (let i = 1; i <= taps; i += 1) await fx.finishThrough(`tap${i}/bake`);
  await Promise.all(jobs);
}

for (const kind of ['quiet', 'visible']) {
  for (const overlap of [true, false]) {
    test(`N5 real pickSlot: 1, 2 and 3 ${overlap ? 'overlapping' : 'back-to-back'} cold taps on 2, 3 and 4 workers never queue behind ${kind} card bakes`, async () => {
      for (const size of [2, 3, 4]) {
        for (const taps of [1, 2, 3]) {
          await withPool(size, async (fx) => {
            const running = await startBackground(fx, size, kind);
            const label = `${kind} size=${size} taps=${taps} ${overlap ? 'overlap' : 'back-to-back'}`;
            // 静默离屏最多 workers - 2 个（最少 0），可见卡最多 workers - 1 个：总有不带后台请求的槽。
            assert.equal(running.length, kind === 'quiet' ? Math.max(0, size - 2) : size - 1, `${label}: background in flight ${JSON.stringify(running)}`);
            await runTaps(fx, label, taps, overlap);
            // 点击期间（冷却结束前）没有派发新的离屏烘焙。
            if (kind === 'quiet') {
              const started = fx.log.filter((item) => isBg(item.tag) && !running.includes(item.tag));
              assert.deepEqual(started.map((item) => item.tag), [], `${label}: new off-screen bakes started during the tap cool-down`);
            }
          });
        }
      }
    });
  }
}
