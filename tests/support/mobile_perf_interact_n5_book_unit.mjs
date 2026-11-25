// N5 同一张卡的换图回归：真实 Book、Pool 和 CardBakeController。照片 A 的后台烘焙占满 workers - 1 个槽，
// 换到 B，B 的配方卡先按后台在池子里排队（Book 缓存了这些 resolve 的 Promise），再点这几张卡。
// 点击命中还在排队的后台项时要升成前台：resolve 马上进保留槽，随后的 bake 也按前台发，不等 A 的烘焙回来。
// 1、2、3 次点击（重叠或依次），2、3、4 个 Worker。GXR_TEST_CHECKOUT 换成旧版本的检出目录做负对照。

import assert from 'node:assert/strict';
import { registerHooks } from 'node:module';
import test from 'node:test';
import { pathToFileURL } from 'node:url';

registerHooks({
  resolve(spec, ctx, next) {
    try {
      return next(spec, ctx);
    } catch (err) {
      if (spec.startsWith('.') && !/\.[a-z]+$/.test(spec)) return next(`${spec}.ts`, ctx);
      throw err;
    }
  },
  load(url, ctx, next) {
    const loaded = next(url, ctx);
    if (!url.endsWith('/book.ts')) return loaded;
    return { ...loaded, source: Buffer.from(loaded.source).toString().replaceAll('import.meta.env.DEV', 'false') };
  },
});

const checkout = process.env.GXR_TEST_CHECKOUT ? pathToFileURL(`${process.env.GXR_TEST_CHECKOUT}/`) : new URL('../../', import.meta.url);
const { Pool } = await import(new URL('mobile/src/core/pool.ts', checkout).href);
const { Book } = await import(new URL('mobile/src/app/book.ts', checkout).href);
const { CardBakeController } = await import(new URL('mobile/src/app/looks.ts', checkout).href);

const tick = () => new Promise((done) => setImmediate(done));
async function until(condition, what) {
  for (let i = 0; i < 2000; i += 1) {
    if (condition()) return;
    await tick();
  }
  throw new Error(`timed out waiting for ${what}`);
}

async function scenario(size, taps, overlap) {
  const label = `size=${size} taps=${taps} ${overlap ? 'overlap' : 'back-to-back'}`;
  const origNav = Object.getOwnPropertyDescriptor(globalThis, 'navigator');
  const origWorker = globalThis.Worker;
  const origFetch = globalThis.fetch;
  Object.defineProperty(globalThis, 'navigator', { configurable: true, value: { hardwareConcurrency: size + 1 } });
  globalThis.fetch = async () => new Response(Uint8Array.of(0, 97, 115, 109, 1, 0, 0, 0), { headers: { 'content-type': 'application/wasm' } });
  const workers = [];
  const log = [];
  globalThis.Worker = class ControlledWorker {
    constructor() {
      this.slot = workers.length;
      this.queue = [];
      workers.push(this);
    }
    postMessage(msg) {
      if (!msg?.op) return;
      log.push({ slot: this.slot, tag: msg.frameMeta, ahead: this.queue.map((m) => m.frameMeta) });
      this.queue.push(msg);
    }
    terminate() {}
  };
  const reply = (tag, json = '{}') => {
    const worker = workers.find((w) => w.queue[0]?.frameMeta === tag);
    assert.ok(worker, `${label}: ${tag} is not at the head of any worker (${JSON.stringify(workers.map((w) => w.queue.map((m) => m.frameMeta)))})`);
    const msg = worker.queue.shift();
    worker.onmessage?.({ data: { id: msg.id, ok: true, json, buffers: [] } });
  };
  const pool = new Pool();
  const a = new CardBakeController(size);
  const b = new CardBakeController(size);
  const book = new Book({
    restoreCalib: async () => ({
      ok: true, savedAt: 1, name: 'fixture', units: [{ unit: 'jlaunch' }], manifestSha256: 'fixture',
      files: new Map([['manifest.json', new Uint8Array([0])], ['ctx', new Uint8Array([1])], ['ring', new Uint8Array([2])]]),
    }),
    calibCheck: async ({ iso }) => ({ model_iso: iso, ctx: 'ctx', ring: 'ring' }),
    resolve: async (_dat, selection, _library, background) => {
      const slot = JSON.parse(selection).slot;
      const { json } = await pool.request('resolve', null, [], null, { frameMeta: `resolve-${slot}` }, background);
      return { entrySha256: json, entry: new Uint8Array([slot]) };
    },
    bake: async (args, background) => {
      await pool.request('bake', null, [], null, { frameMeta: `bake-${new Uint8Array(args.entry)[0]}` }, background);
      return { meta: { nx: 1, ny: 1, origin: [0, 0], step: 0.375 }, data: new ArrayBuffer(16), fullMeta: {} };
    },
  });
  const isBg = (tag) => typeof tag === 'string' && tag.startsWith('A');
  const assertClear = (tag) => {
    const row = log.findLast((r) => r.tag === tag);
    assert.ok(row, `${label}: ${tag} never reached a worker while A's bakes were in flight (${JSON.stringify(log.map((r) => r.tag))})`);
    const behind = row.ahead.filter(isBg);
    assert.deepEqual(behind, [], `${label}: ${tag} queued behind A's background bake ${JSON.stringify(behind)} on slot ${row.slot}`);
  };
  try {
    await book.restore();
    book.dat = { name: 'fixture', read: { bytes: new ArrayBuffer(16), entries: new Uint8Array(24 * 552) }, library: null };
    await until(() => book.unitState('jlaunch').state !== 'checking', 'calibration');
    assert.equal(book.unitState('jlaunch').state, 'ready');
    // 照片 A：后台烘焙占满 workers - 1 个槽（不同的配方表，不回包）。
    const aJobs = Array.from({ length: size - 1 }, (_, i) => ({ id: `A${i}`, item: `A${i}` }));
    a.observe(aJobs.map((job) => ({ dataset: { card: job.id } })));
    a.start(aJobs, { isAlive: () => true, isBusy: () => false, run: (tag) => pool.request('bake', null, [], null, { frameMeta: tag }, true) });
    await until(() => log.filter((r) => isBg(r.tag)).length === size - 1, "A's bakes");
    a.destroy();
    // 换到照片 B：这几张卡先按后台排进池子（Book 缓存了 resolve 的 Promise）。
    const cards = Array.from({ length: taps }, (_, i) => ({ id: `B${i}`, kind: 'builtin', slot: 10 + i, controls: [{ name: 'amount', default: 5, levels: [1, 5, 9], min: 1, max: 9 }] }));
    b.observe(cards.map((card) => ({ dataset: { card: card.id } })));
    b.start(cards.map((card) => ({ id: card.id, item: card })), { isAlive: () => true, isBusy: () => false, run: (card) => book.bake(card, 'jlaunch', 200, undefined, true) });
    await tick();
    await tick();
    assert.ok(!log.some((r) => r.tag.startsWith('resolve-')), `${label}: B's background resolves must wait while A holds workers - 1 slots`);
    // 点这几张卡（和卡片小图命中同一个 Book 缓存项）。
    const done = [];
    for (let i = 0; i < taps; i += 1) {
      const card = cards[i];
      b.beginStage(`tap-${card.id}`, card.id, true);
      done.push(book.bake(card, 'jlaunch', 200).then(() => b.endStage(`tap-${card.id}`, card.id)));
      await until(() => log.some((r) => r.tag === `resolve-${card.slot}`), `${card.id}'s resolve`);
      assertClear(`resolve-${card.slot}`);
      if (overlap) continue;
      reply(`resolve-${card.slot}`, `sha-${card.id}`);
      await until(() => log.some((r) => r.tag === `bake-${card.slot}`), `${card.id}'s bake`);
      assertClear(`bake-${card.slot}`);
      reply(`bake-${card.slot}`);
      await done.at(-1);
    }
    if (overlap) {
      for (const card of cards) {
        reply(`resolve-${card.slot}`, `sha-${card.id}`);
        await until(() => log.some((r) => r.tag === `bake-${card.slot}`), `${card.id}'s bake`);
        assertClear(`bake-${card.slot}`);
      }
      for (const card of cards) reply(`bake-${card.slot}`);
      await Promise.all(done);
    }
    // A 的烘焙一直没回来：点击全程没等它们。
    assert.equal(log.filter((r) => isBg(r.tag)).length, size - 1);
    assert.ok(workers.every((w) => w.queue.every((m) => isBg(m.frameMeta) || !String(m.frameMeta).startsWith('resolve-'))), `${label}: a tapped resolve is still waiting`);
  } finally {
    a.destroy();
    b.destroy();
    pool.stop();
    globalThis.Worker = origWorker;
    globalThis.fetch = origFetch;
    if (origNav) Object.defineProperty(globalThis, 'navigator', origNav);
    else delete globalThis.navigator;
  }
}

for (const overlap of [true, false]) {
  test(`N5 same card after a photo switch: tapping B's queued background cards (real Book, Pool, CardBakeController) promotes resolve and bake, 1, 2 and 3 ${overlap ? 'overlapping' : 'back-to-back'} taps on 2, 3 and 4 workers`, async () => {
    for (const size of [2, 3, 4]) for (const taps of [1, 2, 3]) await scenario(size, taps, overlap);
  });
}
