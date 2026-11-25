// core/pool.ts 的失败恢复：后台队列补发时同步抛错（替补 Worker 构造失败、排队的可转移对象已被交出去），
// 每个请求都得落地，补发前先结算已收到的回包，stop() 拒绝所有在途和排队的请求。三种时序来自 Sol v2 r2 的 pool-recovery 探针。
// GXR_TEST_POOL_PATH 换成旧版本的 pool.ts 做负对照。

import assert from 'node:assert/strict';
import { registerHooks } from 'node:module';
import test from 'node:test';
import { pathToFileURL } from 'node:url';

registerHooks({
  resolve(spec, ctx, next) {
    if (spec === './protocol') return next(`${spec}.ts`, ctx);
    return next(spec, ctx);
  },
});
const poolUrl = process.env.GXR_TEST_POOL_PATH ? pathToFileURL(process.env.GXR_TEST_POOL_PATH).href : new URL('../../mobile/src/core/pool.ts', import.meta.url).href;
const { Pool } = await import(poolUrl);
const tick = () => new Promise((done) => setImmediate(done));
const settle = async (n = 20) => {
  for (let i = 0; i < n; i += 1) await tick();
};
async function until(condition, what) {
  for (let i = 0; i < 400 && !condition(); i += 1) await tick();
  assert.ok(condition(), `timed out waiting for ${what}`);
}

/** 2 个 Worker（hardwareConcurrency 3 → 2），后台最多占 1 个；postMessage 走真实 structuredClone，所以交出去的 ArrayBuffer 会抛 DataCloneError。 */
function harness() {
  Object.defineProperty(globalThis, 'navigator', { configurable: true, value: { hardwareConcurrency: 3 } });
  globalThis.fetch = async () => new Response(Uint8Array.of(0, 97, 115, 109, 1, 0, 0, 0), { headers: { 'content-type': 'application/wasm' } });
  const workers = [];
  const sent = [];
  let failConstruction = false;
  globalThis.Worker = class {
    constructor() {
      if (failConstruction) {
        failConstruction = false;
        throw new DOMException('injected Worker constructor rejection', 'QuotaExceededError');
      }
      this.messages = [];
      workers.push(this);
    }
    postMessage(message, transfer) {
      structuredClone(message, { transfer });
      this.messages.push(message);
      sent.push(message.frameMeta);
    }
    terminate() {
      this.terminated = true;
    }
    finish() {
      const { id } = this.messages.shift();
      this.onmessage({ data: { id, ok: true, json: '{}', buffers: [] } });
    }
    die() {
      this.onerror({ message: 'injected worker failure' });
    }
  };
  const pool = new Pool();
  const results = {};
  const post = (tag, background = true, buffer = null) => {
    results[tag] = 'pending';
    void pool.request('bake', buffer, buffer ? [buffer] : [], null, { frameMeta: tag }, background).then(
      () => {
        results[tag] = 'resolved';
      },
      (err) => {
        results[tag] = `rejected: ${err.message}`;
      },
    );
  };
  return { pool, workers, sent, results, post, failNextConstruction: () => { failConstruction = true; } };
}

test('pool recovery: a failed slot is replaced in FIFO order, background waits for the running tap (G4), and stop() rejects in-flight and queued requests', async () => {
  const { pool, workers, sent, results, post } = harness();
  const holder = (tag) => workers.find((w) => !w.terminated && w.messages.some((m) => m.frameMeta === tag));
  post('A');
  post('B');
  post('C');
  await until(() => sent.length === 1, 'A');
  post('tap', false);
  await until(() => sent.length === 2, 'tap');
  assert.deepEqual(sent, ['A', 'tap']);
  workers[0].die();
  await settle();
  assert.equal(workers[0].terminated, true);
  assert.equal(results.A, 'rejected: injected worker failure');
  assert.deepEqual(sent, ['A', 'tap'], 'G4: no background job starts while the tap runs');
  holder('tap').finish();
  await until(() => sent.length === 3, 'B');
  assert.equal(sent[2], 'B');
  holder('B').finish();
  await until(() => sent.length === 4, 'C');
  assert.equal(sent[3], 'C');
  post('D');
  pool.stop();
  await settle();
  assert.deepEqual(results, { A: 'rejected: injected worker failure', B: 'resolved', C: 'rejected: Worker 已停止', D: 'rejected: Worker 已停止', tap: 'resolved' });
});

test('pool recovery: a replacement Worker constructor that throws while the queue refills fails only that request', async () => {
  const { pool, workers, sent, results, post, failNextConstruction } = harness();
  post('A');
  post('B');
  post('C');
  await until(() => sent.length === 1, 'A');
  failNextConstruction();
  let eventError = null;
  try {
    workers[0].die();
  } catch (err) {
    eventError = err.name;
  }
  await settle();
  assert.equal(eventError, null, 'the Worker error handler must not throw');
  assert.equal(results.A, 'rejected: injected worker failure');
  assert.equal(results.B, 'rejected: injected Worker constructor rejection', `B must settle with its own error: ${JSON.stringify(results)}`);
  assert.ok(sent.includes('C'), `C must still be sent: ${JSON.stringify(sent)}`);
  pool.stop();
  await settle();
  assert.equal(results.C, 'rejected: Worker 已停止');
  assert.ok(Object.values(results).every((r) => r !== 'pending'), `every request settles: ${JSON.stringify(results)}`);
});

test('pool recovery: a queued request whose transferable was detached fails alone, after the reply that freed its slot settles', async () => {
  const { pool, workers, sent, results, post } = harness();
  post('A');
  const detached = new ArrayBuffer(4);
  post('B', true, detached);
  post('C');
  await until(() => sent.length === 1, 'A');
  structuredClone(detached, { transfer: [detached] });
  let eventError = null;
  try {
    workers[0].finish();
  } catch (err) {
    eventError = err.name;
  }
  await settle();
  assert.equal(eventError, null, 'the reply handler must not throw');
  assert.equal(results.A, 'resolved', `A's reply must settle: ${JSON.stringify(results)}`);
  assert.match(results.B, /^rejected: /, `B must settle with its DataCloneError: ${JSON.stringify(results)}`);
  assert.ok(sent.includes('C'), `C must still be sent: ${JSON.stringify(sent)}`);
  assert.equal(results.C, 'pending');
  pool.stop();
  await settle();
  assert.equal(results.C, 'rejected: Worker 已停止');
});
