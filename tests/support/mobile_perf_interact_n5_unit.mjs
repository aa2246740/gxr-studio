// 点击调度回归单测（原 N5，现在按 docs/mobile.md 的 G1–G4 断言）：直接驱动真实的 core/pool.ts 和 looks.ts 的
// CardBakeController。2、3、4 个 Worker 上，离屏静默烘焙或可见卡烘焙正在跑的时候，连着点 1、2、3 张没烘过的配方卡
// （冷切：先 resolve 再 bake）。每一步之后：一个 Worker 最多一个计算请求（G1，所以到了 Worker 上的点击前面什么都没有）；
// 点击在池子里等时每个 Worker 都有活、至少一个是前台（G2）；至少一个 Worker 没有后台请求（G3）；前台在等或在跑时
// 不发后台请求（G4）。两种点法都测：重叠（上一张还没回来就点下一张）和依次（上一张落到舞台再点）。
// 用 GXR_TEST_POOL_PATH / GXR_TEST_LOOKS_PATH 换成旧版本文件做负对照。

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
  const waiting = new Set(); // 交给池子、还没到 Worker 上的前台请求
  const g4 = [];
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
      waiting.delete(msg.frameMeta);
      const fg = [...waiting, ...spawned.flatMap((w) => w.queue.map((m) => m.frameMeta)).filter((t) => !isBg(t))];
      if (isBg(msg.frameMeta) && fg.length) g4.push(`${msg.frameMeta} posted while foreground ${fg} waited or ran`);
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
  const request = (op, tag, background = false) => {
    if (!background) waiting.add(tag);
    return pool.request(op, null, [], null, { frameMeta: tag }, background);
  };
  const inFlight = () => spawned.flatMap((w) => w.queue.map((m) => m.frameMeta));
  const slotOf = (tag) => spawned.find((w) => w.queue.some((m) => m.frameMeta === tag));
  const settle = async (count) => {
    for (let i = 0; i < 200 && log.length < count; i += 1) await new Promise((done) => setTimeout(done, 1));
  };
  /** G1–G4，每一步之后看。 */
  const assertSched = (label) => {
    for (const w of spawned) assert.ok(w.queue.length <= 1, `${label}: G1 slot ${w.slot} holds ${w.queue.map((m) => m.frameMeta)}`);
    if (waiting.size) {
      const busy = spawned.filter((w) => w.queue.length).length;
      assert.equal(busy, size, `${label}: G2 ${[...waiting]} waits while ${size - busy} worker(s) are free`);
      assert.ok(spawned.some((w) => w.queue.some((m) => !isBg(m.frameMeta))), `${label}: G2 ${[...waiting]} waits while every worker runs background`);
    }
    if (size >= 2) assert.ok(spawned.some((w) => !w.queue.some((m) => isBg(m.frameMeta))), `${label}: G3 every worker holds background`);
    assert.deepEqual(g4, [], `${label}: G4`);
  };
  /** 在 Worker 上最早发出的前台请求。 */
  const oldestForeground = () => log.find((e) => !isBg(e.tag) && inFlight().includes(e.tag))?.tag;
  /** 做完 tag（G1 之下它就在所在槽的头一个）。 */
  const finishThrough = async (tag) => {
    const worker = slotOf(tag);
    assert.ok(worker, `${tag} is not in flight`);
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
    await run({ controller, request, inFlight, finishThrough, assertSched, oldestForeground, log, idle, spawned });
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

/** 依次点：别的前台都做完了，点击马上上 Worker，而且前面什么都没有。 */
function assertStartedAlone(fx, tag, label) {
  const entry = fx.log.findLast((item) => item.tag === tag);
  assert.ok(entry, `${label}: ${tag} was never dispatched`);
  assert.deepEqual(entry.ahead, [], `${label}: ${tag} queued behind ${JSON.stringify(entry.ahead)} on slot ${entry.slot}`);
}

async function runTaps(fx, label, taps, overlap) {
  const jobs = [];
  let landed = 0;
  for (let i = 1; i <= taps; i += 1) {
    const tag = `tap${i}`;
    jobs.push(beginTap(fx, tag).then(() => { landed += 1; }));
    await tick();
    fx.assertSched(label);
    if (overlap) continue;
    assertStartedAlone(fx, `${tag}/resolve`, label);
    await fx.finishThrough(`${tag}/resolve`);
    fx.assertSched(label);
    assertStartedAlone(fx, `${tag}/bake`, label);
    await fx.finishThrough(`${tag}/bake`);
    fx.assertSched(label);
    await jobs.at(-1);
  }
  if (!overlap) return;
  // 重叠：Worker 不够时后来的点击在池子里等（G2 允许）。每次做完最早上 Worker 的前台请求，直到都落地。
  for (let guard = 0; landed < taps; guard += 1) {
    assert.ok(guard < 4 * taps, `${label}: taps never landed`);
    const head = fx.oldestForeground();
    assert.ok(head, `${label}: ${taps - landed} tap(s) pending but no foreground job on any worker`);
    await fx.finishThrough(head);
    fx.assertSched(label);
  }
  await Promise.all(jobs);
}

for (const kind of ['quiet', 'visible']) {
  for (const overlap of [true, false]) {
    test(`N5 → G1–G4: 1, 2 and 3 ${overlap ? 'overlapping' : 'back-to-back'} cold taps on 2, 3 and 4 workers while ${kind} card bakes run`, async () => {
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

// 换照片：每次打开照片都新建 RecipeView 和 CardBakeController。旧照片 A 的小图烘焙还在 Worker 里，销毁 A、挂上 B、
// B 也开始烘焙，再点 B 上没烘过的卡。后台占用按整个池子算（不按控制器），A 的在途烘焙一直占着槽直到回来，
// 所以 1、2、3 次点击（重叠或依次）都不排在任何一张照片的后台烘焙后面；A 的烘焙回来后 B 的照常派发。
for (const aKind of ['visible', 'quiet']) {
  for (const overlap of [true, false]) {
    test(`N5 → G1–G4 photo switch: A's ${aKind} bakes in flight, B mounted, 1, 2 and 3 ${overlap ? 'overlapping' : 'back-to-back'} cold taps on B on 2, 3 and 4 workers`, async () => {
      for (const size of [2, 3, 4]) {
        for (const taps of [1, 2, 3]) {
          await withPool(size, async (fx) => {
            const label = `switch A=${aKind} size=${size} taps=${taps} ${overlap ? 'overlap' : 'back-to-back'}`;
            const fromA = await startBackground(fx, size, aKind);
            fx.controller.destroy();
            const b = new CardBakeController(size);
            try {
              const jobs = Array.from({ length: size + 6 }, (_, i) => ({ id: `bg-B-${i}`, item: `bg-B-${i}` }));
              b.observe(jobs.map((job) => ({ dataset: { card: job.id } })));
              b.start(jobs, { isAlive: () => true, isBusy: () => false, run: (tag) => fx.request('bake', tag, true) });
              await tick();
              const inWorkers = fx.inFlight().filter(isBg);
              await runTaps({ ...fx, controller: b }, label, taps, overlap);
              assert.ok(inWorkers.length <= Math.max(1, size - 1), `${label}: ${inWorkers.length} background requests in workers: ${JSON.stringify(inWorkers)}`);
              // A 的烘焙回来以后，B 在池子里等着的烘焙补进去（不会饿死）。
              for (const tag of fromA) await fx.finishThrough(tag);
              await tick();
              if (size > 1) assert.ok(fx.inFlight().some((tag) => tag.startsWith('bg-B-')), `${label}: B's bakes never reached a worker after A's returned`);
            } finally {
              b.destroy();
            }
          });
        }
      }
    });
  }
}
