// 网格空闲预烘焙（mobile/src/app/prebake.ts）：真实 Book（假 io）和真实 Pool（假 Worker）。
// 1. 网格静下来以后，编辑器第一次要默认卡和第一屏的卡时表已经在缓存里，不再发新的烘焙请求；表的参数和编辑器自己烘焙的一样。
// 2. 预烘焙在跑时点卡：前台的 resolve 和 bake 立刻交给空着的 Worker，不排在预烘焙后面；点了以后冷却期内不开始新的预烘焙。
// 3. 换校准、换相机配方文件（目录）以后按新版本重新烘焙，旧版本晚到的表不算数也不进缓存；清空照片作废没开始的。
// 4. 上限：每个版本最多 PREBAKE_MAX 张，同时最多 workers - 2 张，两个 Worker 以下不做；池子有别人的活、导出在跑时不开始。
// GXR_TEST_CHECKOUT 换成旧版本的检出目录做负对照。

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
const mod = (rel) => import(new URL(rel, checkout).href);
const { Pool } = await mod('mobile/src/core/pool.ts');
const { Book, KEEP_BAKES } = await mod('mobile/src/app/book.ts');
const { setExportPaused } = await mod('mobile/src/app/export_pause.ts');
const { shelves } = await mod('mobile/src/lib/recipes.ts');
/** 旧版本没有 prebake.ts：每个用例自己去拿，失败记在用例里。 */
const prebakeModule = () => mod('mobile/src/app/prebake.ts');

const tick = () => new Promise((done) => setImmediate(done));
const sleep = (ms) => new Promise((done) => setTimeout(done, ms));
async function until(condition, what, ms = 3000) {
  const end = Date.now() + ms;
  while (Date.now() < end) {
    if (condition()) return;
    await sleep(2);
  }
  throw new Error(`timed out waiting for ${what}`);
}

const CONTROL = { name: 'amount', label: '强度', min: 1, max: 9, default: 5, cap_lower: false, cap_upper: false, amount_step: null, levels: [1, 5, 9] };
const UNITS = ['jlaunch', 'qlaunch', 'l08firm'];
const LISTING = { units: UNITS.map((id) => ({ id, recipes: Array.from({ length: 8 }, (_, i) => ({ slot: i + 1, controls: [CONTROL] })) })) };
const LENS = { jlaunch: 'GR LENS A12 50mm F2.5 MACRO', qlaunch: 'GR LENS A12 28mm F2.5', l08firm: 'RICOH LENS MOUNT A12' };
const card = (unit, slot) => shelves(LISTING, unit).builtin[slot - 1];

function photo(id, unit, iso) {
  return {
    id, stem: id, version: 1, dng: { name: `${id}.DNG` }, jpg: null, diptych: false, lastModified: 0, captureMs: 0,
    status: 'ready', gone: false, cached: null, jpgMeta: { lens: LENS[unit], iso, copyright: null }, dngMeta: null,
    choice: null, recipeOff: false, levels: new Map(), memory: new Map(), looks: new Map(),
  };
}

function calibRead(tag) {
  return {
    ok: true, saved: true, savedAt: 1, name: tag, units: UNITS.map((unit) => ({ unit })), manifestSha256: tag,
    files: new Map([['manifest.json', new Uint8Array([0])], ['ctx', new Uint8Array([1])], ['ring', new Uint8Array([2])], ['ctx16', new Uint8Array([16])]]),
  };
}

const datRead = (tag) => ({
  summary: JSON.stringify({ generation: 250 }), listing: JSON.stringify(LISTING), entries: new Uint8Array(24 * 552),
  bytes: new TextEncoder().encode(tag).buffer, library: null,
});

/**
 * 假 io：resolve 按（单元、位置、档位）给条目 sha；bake 记下参数。`hold`：bake 先不回，测试手动放。
 * `pool`：给了就真的交给 Pool（frameMeta 是标签，假 Worker 看得到）。
 */
function makeIo({ hold = false, pool = null } = {}) {
  const io = {
    bakes: [], resolves: [], held: [], maxHeld: 0, calibs: 0,
    readFile: async (file) => new TextEncoder().encode(file.name).buffer,
    readDat: async (bytes) => datRead(new TextDecoder().decode(bytes)),
    restoreCalib: async () => calibRead('first'),
    importCalib: async () => calibRead('second'),
    watermark: async () => 0,
    deletePack: async () => true,
    calibCheck: async ({ iso }) => {
      io.calibs += 1;
      return { model_iso: iso, ctx: iso === 1600 ? 'ctx16' : 'ctx', ring: 'ring' };
    },
    resolve: async (_dat, selection, _library, background) => {
      const sel = JSON.parse(selection);
      const tag = `resolve-${sel.unit}-${sel.slot}`;
      io.resolves.push({ tag, background: Boolean(background) });
      if (pool) await pool.request('resolve', null, [], null, { frameMeta: tag }, background);
      return { entrySha256: `${sel.unit}-${sel.slot}-${JSON.stringify(sel.levels)}`, entry: new Uint8Array([UNITS.indexOf(sel.unit), sel.slot]) };
    },
    bake: async (args, background) => {
      const [unit, slot] = new Uint8Array(args.entry);
      const tag = `bake-${UNITS[unit]}-${slot}${new Uint8Array(args.ctx)[0] === 16 ? '-1600' : ''}`;
      const rec = { tag, background: Boolean(background), args };
      io.bakes.push(rec);
      if (pool) await pool.request('bake', null, [], null, { frameMeta: tag }, background);
      else if (hold) {
        await new Promise((done) => {
          io.held.push({ tag, done });
          io.maxHeld = Math.max(io.maxHeld, io.held.length);
        });
      }
      return { meta: { nx: 1, ny: 1, origin: [0, 0], step: 0.375 }, data: new ArrayBuffer(16), fullMeta: {} };
    },
  };
  return io;
}

/** 一个装好校准和相机配方文件的 Book（经过真的 restore / setDat，版本会加）。 */
async function readyBook(io) {
  const book = new Book(io);
  await book.restore();
  await book.setDat(new File(['first'], 'GXRCE.DAT'), 1, null);
  assert.ok(book.dat, 'fixture DAT loaded');
  return book;
}

function makeEngine() {
  const slots = new Set();
  return { hasBake: (slot) => slots.has(slot), addBake: (slot) => slots.add(slot) };
}

function host(book, photos, extra = {}) {
  const states = [];
  return {
    states,
    book: () => book,
    photos: () => photos,
    ready: () => true,
    firstRow: () => 3,
    report: (state) => states.push(state),
    ...extra,
  };
}

const done = (h) => h.states.at(-1) === 'done';

test('1. after the grid is idle the default card and the first-row cards are already baked: no new bake on the first switch', async () => {
  const { Prebaker } = await prebakeModule();
  const io = makeIo();
  const book = await readyBook(io);
  book.attach(makeEngine());
  const photos = [photo('a', 'jlaunch', 200), photo('b', 'jlaunch', 400)];
  const h = host(book, photos);
  const pre = new Prebaker(h, 4, () => 0);
  pre.start(null);
  await until(() => done(h), 'pre-bake done');
  // 默认卡（builtin:4）先，然后横条第一屏的 1–3。
  assert.deepEqual(io.bakes.map((b) => b.tag), ['bake-jlaunch-4', 'bake-jlaunch-1', 'bake-jlaunch-2', 'bake-jlaunch-3']);
  assert.ok(io.bakes.every((b) => b.background), 'pre-bakes use the background lane');
  const before = io.bakes.length;
  // 编辑器打开：applyLook 先看 bakeKey + ready（同步换上），点第一排的卡一样；再走一次 Book.bake（前台）也只拿缓存。
  for (const slot of [4, 2, 1, 3]) {
    const key = book.bakeKey(card('jlaunch', slot), 'jlaunch', 200, undefined);
    assert.ok(key, `builtin:${slot} key known before the editor asks`);
    assert.ok(book.ready(key), `builtin:${slot} is ready for the stage without a bake`);
    assert.equal(await book.bake(card('jlaunch', slot), 'jlaunch', 200, undefined), key);
  }
  assert.equal(io.bakes.length, before, 'the first switches sent no new bake request');
  // 同样的参数：和一个全新的 Book 在编辑器里自己烘焙的这一张一样（像素和导出不变）。
  const io2 = makeIo();
  const fresh = await readyBook(io2);
  fresh.unitState('jlaunch');
  await until(() => fresh.unitState('jlaunch').state === 'ready', 'fresh calibration check');
  await fresh.bake(card('jlaunch', 2), 'jlaunch', 200, undefined);
  const pre2 = io.bakes.find((b) => b.tag === 'bake-jlaunch-2').args;
  for (const k of ['entry', 'stock', 'ctx', 'ring']) assert.deepEqual(new Uint8Array(pre2[k]), new Uint8Array(io2.bakes[0].args[k]), `bake arg ${k}`);
  assert.equal(pre2.params, io2.bakes[0].args.params);
  assert.equal(pre2.menu, io2.bakes[0].args.menu);
  // 一个版本里不重复：再叫醒也不再烘焙。
  pre.wake();
  await sleep(20);
  assert.equal(io.bakes.length, before);
  pre.stop();
});

/** 假 Worker：记下每个请求交出去时这个 Worker 前面还有什么。 */
function controlledWorkers(size) {
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
      log.push({ tag: msg.frameMeta, slot: this.slot, ahead: this.queue.map((m) => m.frameMeta) });
      this.queue.push(msg);
    }
    terminate() {}
  };
  const reply = (tag) => {
    const worker = workers.find((w) => w.queue[0]?.frameMeta === tag);
    assert.ok(worker, `${tag} is not running on any worker: ${JSON.stringify(workers.map((w) => w.queue.map((m) => m.frameMeta)))}`);
    const msg = worker.queue.shift();
    worker.onmessage?.({ data: { id: msg.id, ok: true, json: '{}', buffers: [] } });
  };
  const running = (prefix) => workers.flatMap((w) => w.queue.map((m) => m.frameMeta)).filter((t) => t?.startsWith(prefix));
  const restore = () => {
    if (origNav) Object.defineProperty(globalThis, 'navigator', origNav);
    globalThis.Worker = origWorker;
    globalThis.fetch = origFetch;
  };
  return { workers, log, reply, running, restore };
}

for (const size of [3, 4]) {
  test(`2. a tap during a pre-bake is not delayed by it (${size} workers)`, async () => {
    const { Prebaker } = await prebakeModule();
    const w = controlledWorkers(size);
    const pool = new Pool();
    try {
      const io = makeIo({ pool });
      const book = await readyBook(io);
      book.attach(makeEngine());
      const h = host(book, [photo('a', 'jlaunch', 200)]);
      const pre = new Prebaker(h, size);
      pre.start(null);
      const cap = size - 2;
      // 预烘焙的 resolve 回了，bake 在 Worker 上跑着、不回。
      await until(() => w.running('resolve-').length === cap, 'pre-bake resolves on workers');
      for (const tag of w.running('resolve-')) w.reply(tag);
      await until(() => w.running('bake-').length === cap, 'pre-bakes running');
      assert.equal(w.workers.length >= cap, true);
      // 用户点了第 7 张（不在第一屏）：前台 resolve 立刻交给空的 Worker，前面没有东西；bake 也一样。
      pre.noteInput();
      const tap = book.bake(card('jlaunch', 7), 'jlaunch', 200, undefined);
      // 另一个前台请求同时来（比如舞台和第二次点击重叠）：也不等预烘焙。
      const tap2 = book.bake(card('jlaunch', 8), 'jlaunch', 200, undefined);
      await until(() => w.log.some((r) => r.tag === 'resolve-jlaunch-7') && w.log.some((r) => r.tag === 'resolve-jlaunch-8'), 'tap resolves posted');
      for (const tag of ['resolve-jlaunch-7', 'resolve-jlaunch-8']) {
        const row = w.log.find((r) => r.tag === tag);
        assert.deepEqual(row.ahead, [], `${tag} queued behind ${row.ahead} on worker ${row.slot}`);
        w.reply(tag);
      }
      await until(() => w.log.some((r) => r.tag === 'bake-jlaunch-7') && w.log.some((r) => r.tag === 'bake-jlaunch-8'), 'tap bakes posted');
      for (const tag of ['bake-jlaunch-7', 'bake-jlaunch-8']) {
        const row = w.log.find((r) => r.tag === tag);
        assert.deepEqual(row.ahead, [], `${tag} queued behind ${row.ahead} on worker ${row.slot}`);
      }
      assert.equal(w.running('bake-').filter((t) => !t.endsWith('-7') && !t.endsWith('-8')).length, cap, 'pre-bakes were still running when the taps started');
      w.reply('bake-jlaunch-7');
      w.reply('bake-jlaunch-8');
      await tap;
      await tap2;
      // 预烘焙那几张回来以后，冷却期内不开始新的。
      const posted = w.log.length;
      for (const tag of w.running('bake-')) w.reply(tag);
      await sleep(60);
      assert.equal(w.log.length, posted, 'no new pre-bake work during the input cooldown');
      pre.stop();
    } finally {
      pool.stop();
      w.restore();
    }
  });
}

test('3. a calibration or catalog change re-bakes for the new generation; late old tables are dropped; clear cancels', async () => {
  const { Prebaker } = await prebakeModule();
  const io = makeIo({ hold: true });
  const book = await readyBook(io);
  const h = host(book, [photo('a', 'jlaunch', 200)]);
  const pre = new Prebaker(h, 4, () => 0);
  pre.start(null);
  await until(() => io.held.length === 2, 'two pre-bakes in flight');
  const g1 = book.generationId();
  // 换校准：两张旧的还在路上。
  const oldHeld = io.held.splice(0);
  assert.equal(await book.importPack(new File(['zip'], 'GXR 校准数据.zip')), true);
  assert.notEqual(book.generationId(), g1);
  for (const job of oldHeld) job.done();
  await until(() => io.held.length === 2, 'new-generation pre-bakes started');
  for (const slot of [4, 1]) {
    const stale = `bake:${g1}:jlaunch:200:jlaunch-${slot}-{"amount":5}`;
    assert.equal(book.cached(stale), false, `late table from the old calibration was not kept (${slot})`);
  }
  // 换相机配方文件（目录）：再按新版本来一遍。
  const g2 = book.generationId();
  const held2 = io.held.splice(0);
  await book.setDat(new File(['second'], 'GXRCE.DAT'), 1, null);
  assert.notEqual(book.generationId(), g2);
  for (const job of held2) job.done();
  await until(() => io.held.length === 2, 'pre-bakes started after the catalog change');
  const g3 = book.generationId();
  while (!done(h)) {
    for (const job of io.held.splice(0)) job.done();
    await sleep(2);
  }
  for (const slot of [4, 1, 2, 3]) {
    const key = book.bakeKey(card('jlaunch', slot), 'jlaunch', 200, undefined);
    assert.ok(key?.startsWith(`bake:${g3}:`) && book.cached(key), `builtin:${slot} re-baked for the new catalog`);
  }
  // 清空照片：没开始的作废。
  const h2 = host(book, [photo('c', 'qlaunch', 200)]);
  const pre2 = new Prebaker(h2, 4, () => 0);
  pre2.start(null);
  await until(() => io.held.length === 2, 'two qlaunch pre-bakes in flight');
  const before = io.bakes.length;
  pre2.cancel();
  h2.photos = () => [];
  for (const job of io.held.splice(0)) job.done();
  await sleep(40);
  assert.equal(io.bakes.length, before, 'nothing new started after clear');
  pre.stop();
  pre2.stop();
});

test('4. the cap: at most PREBAKE_MAX tables, at most workers - 2 at once, none on 2 workers, none while busy', async () => {
  const { Prebaker, PREBAKE_MAX } = await prebakeModule();
  assert.equal(PREBAKE_MAX, KEEP_BAKES / 2);
  // 三个单元、两个 ISO 模型：候选远多于上限。
  const library = () => [photo('a', 'jlaunch', 200), photo('b', 'jlaunch', 1600), photo('c', 'qlaunch', 200), photo('d', 'l08firm', 200), photo('e', 'jlaunch', 3200)];
  for (const workers of [3, 4]) {
    const io = makeIo({ hold: true });
    const book = await readyBook(io);
    const h = host(book, library());
    const pre = new Prebaker(h, workers, () => 0);
    pre.start(null);
    while (!done(h)) {
      await sleep(5);
      io.held.shift()?.done();
    }
    const peak = io.maxHeld;
    assert.equal(peak, workers - 2, `${workers} workers: at most ${workers - 2} pre-bakes at once (states ${h.states}, bakes ${io.bakes.map((b) => b.tag)})`);
    assert.equal(io.bakes.length, PREBAKE_MAX, `${workers} workers: exactly PREBAKE_MAX tables`);
    assert.ok(book.done.size <= KEEP_BAKES);
    // 第一张的默认卡最先，然后它第一屏的卡，再是别的（单元、ISO 模型）的默认卡。
    assert.deepEqual(io.bakes.map((b) => b.tag), [
      'bake-jlaunch-4', 'bake-jlaunch-1', 'bake-jlaunch-2', 'bake-jlaunch-3', // 第一张：默认卡和第一屏
      'bake-jlaunch-4-1600', 'bake-qlaunch-4', 'bake-l08firm-4', // 别的（单元、ISO 模型）打开时的默认卡
      'bake-jlaunch-1-1600', // 然后才是它们的第一屏，到上限为止
    ]);
    pre.stop();
  }
  // 两个 Worker：不做。
  {
    const io = makeIo();
    const book = await readyBook(io);
    const pre = new Prebaker(host(book, library()), 2, () => 0);
    pre.start(null);
    await sleep(40);
    assert.equal(io.bakes.length, 0, 'no pre-bake with 2 workers');
    pre.stop();
  }
  // 池子里有别人的活、导出在跑、刚点过：都不开始；放开以后才开始。
  {
    const io = makeIo();
    const book = await readyBook(io);
    let other = 1;
    const h = host(book, library());
    const pre = new Prebaker(h, 4, () => other);
    pre.start(null);
    await sleep(40);
    assert.equal(io.bakes.length, 0, 'pool busy with other work: wait');
    setExportPaused(true);
    other = 0;
    await sleep(300);
    assert.equal(io.bakes.length, 0, 'export running: wait');
    pre.noteInput(800);
    setExportPaused(false);
    await sleep(150);
    assert.equal(io.bakes.length, 0, 'within the input cooldown: wait');
    await until(() => io.bakes.length > 0, 'starts after the cooldown', 4000);
    pre.stop();
  }
});

/** 最小的小端 TIFF：IFD0 里只有 ISO（0x8827）和 UniqueCameraModel（0xC614）。 */
function tiffHead(unique, iso) {
  const text = new TextEncoder().encode(`${unique}\0`);
  const buf = new Uint8Array(38 + text.length);
  const dv = new DataView(buf.buffer);
  buf[0] = 0x49;
  buf[1] = 0x49;
  dv.setUint16(2, 42, true);
  dv.setUint32(4, 8, true);
  dv.setUint16(8, 2, true);
  dv.setUint16(10, 0x8827, true);
  dv.setUint16(12, 3, true);
  dv.setUint32(14, 1, true);
  dv.setUint16(18, iso, true);
  dv.setUint16(22, 0xc614, true);
  dv.setUint16(24, 2, true);
  dv.setUint32(26, text.length, true);
  dv.setUint32(30, 38, true);
  dv.setUint32(34, 0, true);
  buf.set(text, 38);
  return buf.buffer;
}

test('5. a MOUNT photo whose JPG has no lens name: the DNG head names the unit, once, without touching the photo', async () => {
  const { Prebaker } = await prebakeModule();
  const io = makeIo();
  const book = await readyBook(io);
  // 相机 JPG 里没有镜头名和 ISO（MOUNT 单元）：编辑器读了 DNG 才知道是 l08firm、ISO 1600。
  const mount = { ...photo('m', 'l08firm', null), jpgMeta: { lens: null, iso: null, copyright: null } };
  const heads = [];
  const h = host(book, [mount], {
    readHead: async (file) => {
      heads.push(file.name);
      return tiffHead('RICOH GXR MOUNT A12', 1600);
    },
  });
  const pre = new Prebaker(h, 4, () => 0);
  pre.start(null);
  await until(() => done(h), 'pre-bake done');
  assert.deepEqual(io.bakes.map((b) => b.tag), ['bake-l08firm-4-1600', 'bake-l08firm-1-1600', 'bake-l08firm-2-1600', 'bake-l08firm-3-1600']);
  assert.deepEqual(heads, ['m.DNG'], 'the DNG head is read once');
  assert.equal(mount.dngMeta, null, 'the head is only used for planning; the photo is not changed');
  pre.wake();
  await sleep(20);
  assert.deepEqual(heads, ['m.DNG'], 'not read again');
  pre.stop();
  // 读不了开头（或者没有读的办法）：不知道单元就不猜，一张也不烘焙。
  const io2 = makeIo();
  const book2 = await readyBook(io2);
  const blind = { ...photo('n', 'l08firm', null), jpgMeta: { lens: null, iso: null, copyright: null } };
  for (const extra of [{ readHead: async () => null }, {}]) {
    const h2 = host(book2, [blind], extra);
    const pre2 = new Prebaker(h2, 4, () => 0);
    pre2.start(null);
    await until(() => done(h2), 'pre-bake done without a unit');
    assert.equal(io2.bakes.length, 0, 'no guessed-unit bakes');
    pre2.stop();
  }
});
