// Round 6 (P2 / N5) 回归单测：直接驱动真实 Pool.pickSlot 与 CardBakeController，
// 验证 2、3、4 Worker 下静默离屏期连续两次冷切（含 resolve + bake）均立即落到空闲槽，不排在后台烘焙后。

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
const { CardBakeController, quietIdleBakeCap } = await import(looksUrl);

const tick = () => new Promise((done) => setImmediate(done));

async function withControlledPool(size, run) {
  const origNav = Object.getOwnPropertyDescriptor(globalThis, 'navigator');
  const origWorker = globalThis.Worker;
  const origFetch = globalThis.fetch;
  const origIdle = globalThis.requestIdleCallback;
  const origCancelIdle = globalThis.cancelIdleCallback;

  const log = [];
  const spawned = [];
  const idle = [];
  let fetches = 0;

  Object.defineProperty(globalThis, 'navigator', { configurable: true, value: { hardwareConcurrency: size + 1 } });
  globalThis.fetch = async () => {
    fetches += 1;
    return new Response(Uint8Array.from([0, 97, 115, 109, 1, 0, 0, 0]), {
      headers: { 'content-type': 'application/wasm' },
    });
  };
  globalThis.Worker = class ControlledWorker {
    constructor() {
      this.slot = spawned.length;
      this.queue = [];
      spawned.push(this);
    }
    postMessage(msg) {
      if (!msg?.op) return;
      log.push({
        slot: this.slot,
        op: msg.op,
        tag: msg.frameMeta,
        ahead: this.queue.map((q) => q.frameMeta),
        module: Boolean(msg.wasmModule),
      });
      this.queue.push(msg);
    }
    finish(tag) {
      const head = this.queue.shift();
      assert.equal(head?.frameMeta, tag, `expected ${tag} at head of slot ${this.slot}, got ${head?.frameMeta}`);
      this.onmessage?.({ data: { id: head.id, ok: true, json: '{}', buffers: [] } });
    }
    terminate() {}
  };
  globalThis.requestIdleCallback = (fn) => {
    idle.push(fn);
    return idle.length;
  };
  globalThis.cancelIdleCallback = (id) => {
    idle[id - 1] = null;
  };

  const pool = new Pool();
  const controller = new CardBakeController(size);
  const request = (op, tag) => pool.request(op, null, [], null, { frameMeta: tag });
  const inFlight = () => spawned.flatMap((w) => w.queue.map((m) => m.frameMeta));
  const findDispatch = (tag) => {
    const entry = log.findLast((item) => item.tag === tag);
    assert.ok(entry, `missing dispatch ${tag}`);
    return entry;
  };
  const finish = (tag) => spawned[findDispatch(tag).slot].finish(tag);
  const awaitDispatchCount = async (count) => {
    for (let i = 0; i < 100 && log.length < count; i += 1) {
      await new Promise((done) => setTimeout(done, 1));
    }
    assert.ok(log.length >= count, `expected >= ${count} dispatches, got ${log.length}`);
  };

  try {
    // 1) 预热所有 Worker 并确认非 WASM ping 保持严格轮询
    const warm = Array.from({ length: size }, (_, i) => request('bake', `warm-${i}`));
    await awaitDispatchCount(size);
    for (let i = 0; i < size; i += 1) finish(`warm-${i}`);
    await Promise.all(warm);
    assert.equal(fetches, 1, 'WASM must be fetched once');

    const pings = Array.from({ length: size }, (_, i) => request('ping', `ping-${i}`));
    const pingSlots = log.slice(-size).map((item) => item.slot);
    assert.deepEqual(pingSlots, Array.from({ length: size }, (_, i) => i), 'non-WASM ping must stay strict round-robin');
    for (let i = 0; i < size; i += 1) finish(`ping-${i}`);
    await Promise.all(pings);

    await run({ controller, request, inFlight, findDispatch, finish, idle, spawned });
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

test('N5 real Pool.pickSlot: 3 and 4 workers dispatch two overlapping consecutive cold taps immediately with zero background queueing', async () => {
  for (const size of [3, 4]) {
    await withControlledPool(size, async ({ controller, request, inFlight, findDispatch, finish, idle }) => {
      const bg = Array.from({ length: size + 2 }, (_, i) => ({ id: `bg-${i}`, item: `bg-${i}` }));
      controller.observe([]);
      controller.start(bg, { isAlive: () => true, isBusy: () => false, run: (tag) => request('bake', tag) });
      const cb = idle.shift();
      assert.ok(cb, `size=${size}: quiet idle callback must be scheduled`);
      cb();

      const runningAtTap = inFlight();
      assert.ok(runningAtTap.length >= 1, `size=${size}: quiet idle must start background bakes`);

      const beginTap = (tag) => {
        controller.beginStage(tag, tag, true);
        const job = request('resolve', `${tag}/resolve`)
          .then(() => request('bake', `${tag}/bake`))
          .finally(() => controller.endStage(tag, tag));
        job.catch(() => undefined);
        return job;
      };

      // 第一次冷切：resolve 与 bake 均无排队
      const first = beginTap('tap1');
      assert.deepEqual(findDispatch('tap1/resolve').ahead, [], `size=${size}: tap1/resolve must hit idle slot`);
      finish('tap1/resolve');
      await tick();
      assert.deepEqual(findDispatch('tap1/bake').ahead, [], `size=${size}: tap1/bake must hit idle slot`);

      // 第二次连续冷切（在 tap1/bake 与全部后台烘焙均未返回时立刻点击）：resolve 与 bake 仍直落另一个空闲槽
      const second = beginTap('tap2');
      assert.deepEqual(
        findDispatch('tap2/resolve').ahead,
        [],
        `size=${size}: tap2/resolve queued behind ${JSON.stringify(findDispatch('tap2/resolve').ahead)}`,
      );
      finish('tap2/resolve');
      await tick();
      assert.deepEqual(
        findDispatch('tap2/bake').ahead,
        [],
        `size=${size}: tap2/bake queued behind ${JSON.stringify(findDispatch('tap2/bake').ahead)}`,
      );
      assert.equal(runningAtTap.length, quietIdleBakeCap(size));
      assert.equal(runningAtTap.length, Math.max(1, size - 2), `size=${size}: quiet idle cap must leave 2 free workers`);

      // 两次冷切都在后台烘焙未完成前独立结束
      finish('tap2/bake');
      await second;
      finish('tap1/bake');
      await first;
      assert.deepEqual(
        inFlight().filter((tag) => runningAtTap.includes(tag)),
        runningAtTap,
        `size=${size}: background bakes should still be running independently`,
      );
    });
  }
});

test('N5 real Pool.pickSlot: 2, 3, and 4 workers pause new off-screen bakes after a tap so consecutive cold taps dispatch immediately', async () => {
  for (const size of [2, 3, 4]) {
    await withControlledPool(size, async ({ controller, request, inFlight, findDispatch, finish, idle }) => {
      const bg = Array.from({ length: size + 2 }, (_, i) => ({ id: `bg-${i}`, item: `bg-${i}` }));
      controller.observe([]);
      controller.start(bg, { isAlive: () => true, isBusy: () => false, run: (tag) => request('bake', tag) });
      const cb = idle.shift();
      assert.ok(cb);
      cb();

      const runningAtTap = inFlight();
      assert.ok(runningAtTap.length >= 1);

      const beginTap = (tag) => {
        controller.beginStage(tag, tag, true);
        const job = request('resolve', `${tag}/resolve`)
          .then(() => request('bake', `${tag}/bake`))
          .finally(() => controller.endStage(tag, tag));
        job.catch(() => undefined);
        return job;
      };

      // 第一次冷切立即进入空闲槽并完成
      const first = beginTap('tap1');
      assert.deepEqual(findDispatch('tap1/resolve').ahead, [], `size=${size}: tap1/resolve must hit idle slot`);
      finish('tap1/resolve');
      await tick();
      assert.deepEqual(findDispatch('tap1/bake').ahead, [], `size=${size}: tap1/bake must hit idle slot`);
      finish('tap1/bake');
      await first;

      // tap1 结束后、bg-0 也结束并触发空闲回调：点击后离屏暂停窗口必须阻止新后台任务抢占刚腾出的 Worker（2/3/4 Worker 均适用）
      finish('bg-0');
      await tick();
      while (idle.length) {
        const nextCb = idle.shift();
        if (nextCb) nextCb();
      }
      await tick();
      assert.equal(
        inFlight().some((tag) => tag.startsWith('bg-') && !runningAtTap.includes(tag)),
        false,
        `size=${size}: new background bake must stay paused after tap, inFlight=${JSON.stringify(inFlight())}`,
      );

      // 此时紧接着点第二张冷卡：2/3/4 Worker 上 resolve 与 bake 均零排队，且 2 Worker 上两个槽全部空出，可再叠加第三张冷切
      const second = beginTap('tap2');
      assert.deepEqual(
        findDispatch('tap2/resolve').ahead,
        [],
        `size=${size}: consecutive tap2/resolve queued behind ${JSON.stringify(findDispatch('tap2/resolve').ahead)}`,
      );
      finish('tap2/resolve');
      await tick();
      assert.deepEqual(
        findDispatch('tap2/bake').ahead,
        [],
        `size=${size}: consecutive tap2/bake queued behind ${JSON.stringify(findDispatch('tap2/bake').ahead)}`,
      );

      const third = beginTap('tap3');
      assert.deepEqual(
        findDispatch('tap3/resolve').ahead,
        [],
        `size=${size}: rapid tap3/resolve after tap1 pause queued behind ${JSON.stringify(findDispatch('tap3/resolve').ahead)}`,
      );
      finish('tap3/resolve');
      await tick();
      assert.deepEqual(
        findDispatch('tap3/bake').ahead,
        [],
        `size=${size}: rapid tap3/bake after tap1 pause queued behind ${JSON.stringify(findDispatch('tap3/bake').ahead)}`,
      );

      finish('tap3/bake');
      await third;
      finish('tap2/bake');
      await second;
    });
  }
});
