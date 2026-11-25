// 点击和后台配方卡烘焙的调度检查（G1–G5，见 docs/mobile.md）。真实的 Pool、Book 和 CardBakeController，
// 假 Worker 只记下 postMessage、按日程回包；假时钟管冷却和空闲回调。所有断言只看假 Worker 收到了什么，不看池子内部。
//
// 日程从这些事件里抽：控制器发起的后台卡、直接发起的后台卡、点卡（beginStage、Book.bake、endStage）、别的前台计算、
// 非计算请求（ping）、回包（成功或出错）、Worker onerror、换照片（销毁控制器，在另一个 ISO 上挂新的：resolve 共用、
// bake 不共用）、新的相机配方文件或校准（generation 变）、导出暂停开关、时间流逝（冷却、空闲回调）、
// 下一个 Worker 构造抛错、可转移对象发出前已经交出去、中途 stop()。Worker 数 1、2、3、4。
//
// 「前台」：没带 Lane 的计算请求，或 Lane 已升级的；对一次点击，是它在等的 resolve，之后是 bake。
//   need(T) := T 的 resolve，直到这次 resolve 的成功回包送到；之后是 T 的 bake（模型、条目）
//   fg(m)   := m 是别的前台计算请求，或某个还没结束的点击 T 的 need(T)
// 每个事件、冲刷微任务之后，对每个没结束的点击 T、J = need(T)：
//   J 在 Worker w 上     -> w 上它前面没有计算请求                                       G1
//   J 不在任何 Worker 上 -> 每个 Worker 都有计算请求，且有一个 Worker 上全是前台          G2
// 一直成立：没有 Worker 同时拿着两个计算请求（G1）；2 个以上 Worker 时总有一个 Worker 上没有后台计算（G3）；
//   同一个 resolve 或 bake 不会同时在两个 Worker 上。
// 后台计算请求交给 Worker 的那一刻：没有前台计算在等，也没有前台计算在 Worker 上（G4）。
// 回包都到了、stop() 以后、Worker 坏掉以后：没有还没结算的请求（G5）。
//
// 例外（docs/mobile.md「不保证」）：先记下点击再调 Book.bake（它的请求可能在调用里就发出）；resolve 回包正在送的
// 那一轮，点了这张卡的还没要 bake，不算在等；1 个 Worker 时点击可以等一个在跑的后台请求（G2 后半）；导出暂停时
// bake 等导出（还没要的不算在等）。
//
// 环境变量：GXR_SCHED_SEEDS（每种 Worker 数的随机日程数，默认 375）、GXR_SCHED_FROM、GXR_SCHED_STEPS（默认 40）、
// GXR_SCHED_SIZES（默认 1,2,3,4）、GXR_SCHED_CHECKOUT（换成别的检出目录做负对照）、GXR_SCHED_PATCH（目录里的 pool.ts、
// book.ts、protocol.ts 换掉检出里的，做变异）、GXR_SCHED_ONLY=fixed|random、GXR_SCHED_SHOW（每类违例打印几个例子）。

import assert from 'node:assert/strict';
import { existsSync, readFileSync } from 'node:fs';
import { registerHooks } from 'node:module';
import test from 'node:test';
import { pathToFileURL } from 'node:url';

const env = process.env;
const SEEDS = Number(env.GXR_SCHED_SEEDS ?? 375);
const FROM = Number(env.GXR_SCHED_FROM ?? 1);
const STEPS = Number(env.GXR_SCHED_STEPS ?? 40);
const SIZES = String(env.GXR_SCHED_SIZES ?? '1,2,3,4').split(',').map(Number);
const PATCH = env.GXR_SCHED_PATCH || null;
const SHOW = Number(env.GXR_SCHED_SHOW ?? 2);
const ONLY = env.GXR_SCHED_ONLY || '';
const ROOT = env.GXR_SCHED_CHECKOUT ? pathToFileURL(`${env.GXR_SCHED_CHECKOUT}/`) : new URL('../../', import.meta.url);

registerHooks({
  resolve(spec, ctx, next) {
    if (/\.(css|ttf|woff2?|png|svg|txt)(\?.*)?$/.test(spec)) return { url: 'data:text/javascript,export default "";', shortCircuit: true };
    try {
      return next(spec, ctx);
    } catch (err) {
      if (spec.startsWith('.') && !/\.[a-z]+$/.test(spec)) return next(`${spec}.ts`, ctx);
      throw err;
    }
  },
  load(url, ctx, next) {
    const loaded = next(url, ctx);
    if (!url.includes('/mobile/src/')) return loaded;
    const base = url.slice(url.lastIndexOf('/') + 1);
    let source = Buffer.from(loaded.source ?? '').toString();
    if (PATCH && ['pool.ts', 'book.ts', 'protocol.ts'].includes(base) && existsSync(`${PATCH}/${base}`)) source = readFileSync(`${PATCH}/${base}`, 'utf8');
    return { ...loaded, source: source.replaceAll('import.meta.env.DEV', 'false') };
  },
});

const { Pool } = await import(new URL('mobile/src/core/pool.ts', ROOT).href);
const { Book } = await import(new URL('mobile/src/app/book.ts', ROOT).href);
const { CardBakeController } = await import(new URL('mobile/src/app/looks.ts', ROOT).href);
const { setExportPaused } = await import(new URL('mobile/src/app/export_pause.ts', ROOT).href);

const realImmediate = setImmediate;
const flush = async () => {
  for (let i = 0; i < 3; i += 1) await new Promise((done) => realImmediate(done));
};

function rng(seed) {
  let a = seed >>> 0;
  return () => {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

/** 假时钟：setTimeout、requestIdleCallback 和 Date.now 只在日程推进时间时走。 */
function installClock() {
  const saved = { setTimeout: globalThis.setTimeout, clearTimeout: globalThis.clearTimeout, ric: globalThis.requestIdleCallback, cic: globalThis.cancelIdleCallback, now: Date.now };
  let now = 0;
  let seq = 0;
  const timers = new Map();
  globalThis.setTimeout = (fn, ms = 0, ...rest) => {
    seq += 1;
    timers.set(seq, { at: now + Math.max(0, Number(ms) || 0), fn: () => fn(...rest) });
    return seq;
  };
  globalThis.clearTimeout = (id) => timers.delete(id);
  globalThis.requestIdleCallback = (fn) => globalThis.setTimeout(() => fn({ didTimeout: false, timeRemaining: () => 10 }), 1);
  globalThis.cancelIdleCallback = (id) => timers.delete(id);
  Date.now = () => 1_800_000_000_000 + now;
  return {
    async advance(ms) {
      const end = now + ms;
      for (;;) {
        let due = null;
        for (const [id, t] of timers) if (t.at <= end && (!due || t.at < due[1].at || (t.at === due[1].at && id < due[0]))) due = [id, t];
        if (!due) break;
        timers.delete(due[0]);
        now = Math.max(now, due[1].at);
        due[1].fn();
        await flush();
      }
      now = end;
    },
    restore() {
      Object.assign(globalThis, { setTimeout: saved.setTimeout, clearTimeout: saved.clearTimeout, requestIdleCallback: saved.ric, cancelIdleCallback: saved.cic });
      Date.now = saved.now;
    },
  };
}

// 四张卡。2 号和 4 号 resolve 到同一个条目（两张卡共用一张表）；每张卡在 200 和 1600 两个模型上各有一张表。
const SHA = { 1: 'e1', 2: 'e2', 3: 'e3', 4: 'e3' };
const CARDS = [1, 2, 3, 4].map((slot) => ({ id: `C${slot}`, kind: 'builtin', slot, controls: [{ name: 'amount', default: 5, levels: [1, 5, 9], min: 1, max: 9 }] }));
const ISOS = [200, 1600];

class Violation extends Error {}

/**
 * 跑一个日程。`next(ctx)` 返回下一个事件（随机或固定脚本），null 结束。返回 null（通过）或违例。
 * 事件：{bg, card, iso} {tap, card} {fg} {ping} {reply|error, tag?} {crash, tag?} {mount, iso, visible} {input}
 *       {time, ms} {pause, on} {gen} {ctorThrow} {detached, background} {stop} {raw}
 */
async function runSchedule(size, label, next) {
  Object.defineProperty(globalThis, 'navigator', { configurable: true, value: { hardwareConcurrency: size + 1 } });
  globalThis.fetch = async () => new Response(Uint8Array.of(0, 97, 115, 109, 1, 0, 0, 0), { headers: { 'content-type': 'application/wasm' } });
  const clock = installClock();
  const workers = [];
  const trace = [];
  const asked = new Set(); // 计算请求：已经交给池子、还没到 Worker 上的 tag
  const calls = []; // 日程发起的每个 Promise
  let gen = 0; // 和 Book 的 generation 同步（每次 bump 加一），tag 里带上，免得新旧两轮的同一张表混在一起
  const sha = new Map(); // `${gen}|${slot}` -> 条目，成功的 resolve 回包送到以后
  let transit = null; // 正在送 resolve 回包的 `${gen}|${slot}`
  let paused = false;
  let failCtor = false;
  let lateBackground = null;
  let n = 0;
  globalThis.Worker = class FakeWorker {
    constructor() {
      if (failCtor) {
        failCtor = false;
        trace.push('ctor throws');
        throw new DOMException('injected Worker constructor failure', 'QuotaExceededError');
      }
      this.n = workers.length;
      this.q = [];
      this.dead = false;
      workers.push(this);
    }
    postMessage(msg, transfer) {
      if (!msg?.op) return;
      const { wasmModule: _drop, ...plain } = msg;
      structuredClone(plain, { transfer: transfer ?? [] });
      if (this.dead) return;
      const tag = msg.op === 'ping' ? 'ping' : (msg.frameMeta ?? msg.op);
      this.q.push({ id: msg.id, op: msg.op, tag });
      trace.push(`post w${this.n} ${tag} ahead=[${this.q.slice(0, -1).map((m) => m.tag)}]`);
      asked.delete(tag);
      if (msg.op === 'ping' || lateBackground) return;
      // G4，就在交出去的这一刻：后台计算请求不能在前台计算等着或在跑的时候开始。
      if (isFg(tag)) return;
      const running = live().flatMap((w) => w.q).filter((m) => m.op !== 'ping' && m.tag !== tag && isFg(m.tag, true)).map((m) => m.tag);
      if (running.length) lateBackground = `${tag} posted to w${this.n} while foreground ${running} was running`;
      // 「前台在等」等一个微任务再看：同一次 pump() 里刚刚因为同步抛错被拒掉的前台请求已经不在等了，
      // 它的拒绝在这之前排进了微任务队列（asked 在那时删掉）。
      // 只看这一刻已经要了的：之后才要的前台请求（比如导出结束时排在后面的点击的 bake）不算。
      const where = this.n;
      const fgAsked = [...asked].filter((t) => isFg(t, true));
      queueMicrotask(() => {
        if (lateBackground) return;
        const waiting = fgAsked.filter((t) => asked.has(t));
        if (waiting.length) lateBackground = `${tag} posted to w${where} while foreground ${waiting} waited`;
      });
    }
    terminate() {
      this.dead = true;
      this.q = [];
    }
  };
  const live = () => workers.filter((w) => !w.dead);
  const pendingTaps = () => calls.filter((c) => c.kind === 'tap' && !c.done);
  const key = (tap) => `${tap.gen}|${tap.slot}`;
  /** 点击现在在等的计算请求。 */
  const need = (tap) => (sha.has(key(tap)) ? `B${tap.iso}:${sha.get(key(tap))}g${tap.gen}` : `R${tap.slot}g${tap.gen}`);
  /** 前台：F/D 开头的前台计算请求，或某个没结束的点击在等的。`settledOnly`：送 resolve 回包那一轮的点击还不算。 */
  function isFg(tag, settledOnly = false) {
    if (tag.startsWith('F') || tag.startsWith('D')) return true;
    return pendingTaps().some((t) => (!settledOnly || key(t) !== transit) && need(t) === tag);
  }
  const pool = new Pool();
  const book = new Book({
    restoreCalib: async () => ({
      ok: true, savedAt: 1, name: 'fixture', units: [{ unit: 'jlaunch' }], manifestSha256: 'fixture',
      files: new Map([['manifest.json', new Uint8Array([0])], ['ctx-200', new Uint8Array([2])], ['ctx-1600', new Uint8Array([16])], ['ring', new Uint8Array([1])]]),
    }),
    calibCheck: async ({ iso }) => ({ model_iso: iso, ctx: `ctx-${iso}`, ring: 'ring' }),
    resolve: async (_dat, selection, _library, background) => {
      const { json } = await ask(`R${JSON.parse(selection).slot}g${gen}`, 'resolve', background);
      return { entrySha256: json, entry: Uint8Array.of(Number(json.slice(1))) };
    },
    bake: async (args, background) => {
      await ask(`B${new Uint8Array(args.ctx)[0] === 16 ? 1600 : 200}:e${new Uint8Array(args.entry)[0]}g${gen}`, 'bake', background);
      return { meta: { nx: 1, ny: 1, origin: [0, 0], step: 0.375 }, data: new ArrayBuffer(16), fullMeta: {} };
    },
  });
  function ask(tag, op, background = false, buffer = null) {
    asked.add(tag);
    const done = pool.request(op, buffer, buffer ? [buffer] : [], null, { frameMeta: tag }, background);
    done.then(() => asked.delete(tag), () => asked.delete(tag));
    return done;
  }
  /** 先记下这一行再调 `start`：调用里就发出的请求已经算这一行的。 */
  function track(kind, text, start, extra = {}) {
    const row = { kind, text, done: false, ...extra };
    calls.push(row);
    let p;
    try {
      p = Promise.resolve(start());
    } catch (err) {
      p = Promise.reject(err);
    }
    row.promise = p;
    p.then(() => { row.done = true; }, () => { row.done = true; });
    return row;
  }
  let ctrl = null;
  function mount(iso, visible) {
    if (ctrl) {
      ctrl.alive = false;
      ctrl.c.destroy();
    }
    const c = new CardBakeController(size);
    const me = { c, iso, alive: true };
    ctrl = me;
    const jobs = CARDS.map((card) => ({ id: card.id, item: card }));
    c.observe(jobs.filter((_, i) => visible[i]).map((job) => ({ dataset: { card: job.id } })));
    c.start(jobs, { isAlive: () => me.alive, isBusy: () => false, run: (card) => track('bg', `ctrl ${card.id}@${iso}`, () => book.bake(card, 'jlaunch', iso, undefined, true)).promise });
  }
  const fail = (kind, detail, step) => {
    throw new Violation(JSON.stringify({ kind, size, label, step, detail, queues: live().map((w) => `w${w.n}:[${w.q.map((m) => m.tag)}]`), waiting: pendingTaps().map((t) => `${t.text}->${need(t)}`) }));
  };
  function check(step) {
    transit = null;
    if (lateBackground) fail('G4-background-started-during-foreground', lateBackground, step);
    const compute = (w) => w.q.filter((m) => m.op !== 'ping');
    const over = live().find((w) => compute(w).length > 1);
    if (over) fail('G1-two-compute-jobs-on-one-worker', `w${over.n}: ${compute(over).map((m) => m.tag)}`, step);
    for (const tap of pendingTaps()) {
      const job = need(tap);
      const at = live().find((w) => w.q.some((m) => m.tag === job));
      if (at) {
        const ahead = at.q.slice(0, at.q.findIndex((m) => m.tag === job)).filter((m) => m.op !== 'ping');
        if (ahead.length) fail('G1-tap-behind-compute', `${tap.text} needs ${job} on w${at.n}, behind ${ahead.map((m) => m.tag)}`, step);
      } else if (asked.has(job)) {
        const busy = live().filter((w) => compute(w).length > 0).length;
        if (busy < size) fail('G2-tap-waits-with-free-worker', `${tap.text} needs ${job}, ${size - busy} worker(s) without compute`, step);
        if (size >= 2 && !live().some((w) => compute(w).every((m) => isFg(m.tag)))) fail('G2-tap-waits-all-workers-background', `${tap.text} needs ${job}`, step);
      } else if (!paused) {
        fail('tap-job-never-requested', `${tap.text} needs ${job}, not asked of the pool`, step);
      }
    }
    if (size >= 2 && live().filter((w) => compute(w).some((m) => !isFg(m.tag))).length >= size) fail('G3-no-worker-clear-of-background', 'every worker holds background compute', step);
    const tags = live().flatMap((w) => w.q.map((m) => m.tag)).filter((t) => /^[RB]/.test(t));
    const dup = tags.find((t, i) => tags.indexOf(t) !== i);
    if (dup) fail('duplicate-in-flight', dup, step);
  }
  const settleAll = async (why, step) => {
    // 导出总会结束：bake 等导出是「不保证」里的，不算没结算。
    paused = false;
    setExportPaused(false);
    await flush();
    await clock.advance(5000);
    await flush();
    const stuck = calls.filter((c) => !c.done);
    if (stuck.length) fail('G5-never-settled', `${why}: ${stuck.map((c) => c.text)}`, step);
  };
  /** 回包、出错或让 Worker 坏掉。`tag`：指定那个请求（固定日程用）；没有就挑一个有活的 Worker 的头一个。 */
  function headOf(tag, pick) {
    const withWork = live().filter((w) => w.q.length);
    if (!withWork.length) return null;
    if (tag) {
      const w = withWork.find((x) => x.q[0].tag === tag);
      if (!w) throw new Error(`fixed schedule: ${tag} is not at the head of any worker (${withWork.map((x) => x.q.map((m) => m.tag))})`);
      return w;
    }
    return pick(withWork);
  }
  /** Worker 事件交给池子。池子的回调同步抛错（r2 P2）在浏览器里成了没人接的异常，后面该发的请求就停在那里：算 G5。 */
  function deliver(what, fn) {
    try {
      fn();
    } catch (err) {
      if (err instanceof Violation) throw err;
      fail('G5-pool-handler-throws', `${what}: ${err?.name}: ${err?.message}`, 'handler');
    }
  }
  function answer(w, ok) {
    const msg = w.q.shift();
    trace.push(`${ok ? 'reply' : 'error'} w${w.n} ${msg.tag}`);
    if (!ok) return deliver(`error ${msg.tag}`, () => w.onmessage?.({ data: { id: msg.id, ok: false, message: 'injected' } }));
    let json = '{}';
    const m = /^R(\d)g(\d+)$/.exec(msg.tag);
    if (m) {
      json = SHA[Number(m[1])];
      transit = `${m[2]}|${m[1]}`;
      sha.set(transit, json);
    }
    deliver(`reply ${msg.tag}`, () => w.onmessage?.({ data: { id: msg.id, ok: true, json, buffers: [] } }));
  }

  try {
    await book.restore();
    book.dat = { name: 'fixture', read: { bytes: new ArrayBuffer(16), entries: new Uint8Array(24 * 552) }, library: null };
    for (let i = 0; i < 200 && book.unitState('jlaunch').state !== 'ready'; i += 1) {
      await flush();
      await clock.advance(10);
    }
    assert.equal(book.unitState('jlaunch').state, 'ready', 'fixture calibration never became ready');
    const warm = ask('Fwarm', 'describe');
    for (let i = 0; i < 2000 && !workers[0]?.q.length; i += 1) await flush();
    answer(workers[0], true);
    await warm;
    await flush();
    trace.length = 0;
    let fgN = 0;
    const ctx = { size, workers: () => live(), ctrlIso: () => ctrl?.iso ?? 200 };
    for (let step = 0; ; step += 1) {
      const ev = next(ctx, step);
      if (!ev) break;
      book.unitState('jlaunch');
      switch (ev.t) {
        case 'reply':
        case 'error': {
          const w = headOf(ev.tag, ev.pick);
          if (w) answer(w, ev.t === 'reply');
          break;
        }
        case 'crash': {
          const w = headOf(ev.tag, ev.pick);
          if (w) {
            trace.push(`crash w${w.n}`);
            deliver(`crash w${w.n}`, () => w.onerror?.({ message: 'crashed' }));
          }
          break;
        }
        case 'bg':
          trace.push(`bg ${ev.card.id}@${ev.iso}`);
          track('bg', `bg ${ev.card.id}@${ev.iso}`, () => book.bake(ev.card, 'jlaunch', ev.iso, undefined, true));
          break;
        case 'tap': {
          const iso = ctrl?.iso ?? 200;
          trace.push(`tap ${ev.card.id}@${iso}`);
          ctrl?.c.beginStage(`tap-${step}`, ev.card.id, true);
          const owner = ctrl;
          track('tap', `tap ${ev.card.id}@${iso}`, () => book.bake(ev.card, 'jlaunch', iso).finally(() => owner?.c.endStage(`tap-${step}`, ev.card.id)), { slot: ev.card.slot, iso, gen });
          break;
        }
        case 'fg':
          fgN += 1;
          trace.push(`fg F${fgN}`);
          track('fg', `F${fgN}`, () => ask(`F${fgN}`, 'describe'));
          break;
        case 'raw':
          fgN += 1;
          trace.push(`bg raw X${fgN}`);
          track('bg', `raw X${fgN}`, () => ask(`X${fgN}`, 'bake', true));
          break;
        case 'ping':
          trace.push('ping');
          track('ping', 'ping', () => pool.request('ping', null, []));
          break;
        case 'mount':
          trace.push(`mount @${ev.iso} visible=${ev.visible.map(Number).join('')}`);
          mount(ev.iso, ev.visible);
          break;
        case 'input':
          trace.push('input');
          ctrl?.c.noteInput();
          break;
        case 'time':
          trace.push(`time +${ev.ms}`);
          await clock.advance(ev.ms);
          break;
        case 'pause':
          trace.push(`export pause ${ev.on ? 'on' : 'off'}`);
          paused = ev.on;
          setExportPaused(ev.on);
          break;
        case 'gen':
          trace.push('generation change');
          book.bump();
          gen += 1;
          book.unitState('jlaunch');
          break;
        case 'ctorThrow':
          trace.push('next Worker constructor throws');
          failCtor = true;
          break;
        case 'detached': {
          fgN += 1;
          const tag = ev.background ? `X${fgN}` : `D${fgN}`;
          const buf = new ArrayBuffer(8);
          structuredClone(buf, { transfer: [buf] });
          trace.push(`detached ${tag}`);
          track(ev.background ? 'bg' : 'fg', tag, () => ask(tag, ev.background ? 'bake' : 'describe', Boolean(ev.background), buf));
          break;
        }
        case 'stop':
          trace.push('stop');
          pool.stop();
          await settleAll('after stop()', step);
          return null;
        default:
          throw new Error(`unknown event ${ev.t}`);
      }
      await flush();
      check(step);
    }
    // 收尾：导出停了，回完所有请求，时间走完（控制器空闲时还会要卡，也照样回）；什么都不能留着没结算。
    paused = false;
    setExportPaused(false);
    let quiet = 0;
    // 连着 20 轮（6 秒，长过冷却和空闲回调）没有新请求才算完。
    for (let i = 0; i < 4000 && quiet < 20; i += 1) {
      await flush();
      await clock.advance(300);
      const w = live().find((x) => x.q.length);
      if (w) {
        quiet = 0;
        answer(w, true);
        await flush();
        check(`drain${i}`);
      } else quiet = asked.size || calls.some((c) => !c.done) ? 0 : quiet + 1;
    }
    await settleAll('after every reply', 'end');
    return null;
  } catch (err) {
    if (!(err instanceof Violation)) throw err;
    return { ...JSON.parse(err.message), trace: trace.slice(-24) };
  } finally {
    if (ctrl) ctrl.c.destroy();
    setExportPaused(false);
    pool.stop();
    clock.restore();
  }
}

/** 随机日程。 */
function randomSchedule(seed, size) {
  const rand = rng(seed * 7919 + size * 104729);
  const pick = (list) => list[Math.floor(rand() * list.length)];
  return (ctx, step) => {
    if (step >= STEPS) return null;
    if (step === 0) return { t: 'mount', iso: pick(ISOS), visible: CARDS.map(() => rand() < 0.5) };
    const r = rand();
    const withPick = (t) => ({ t, pick });
    if (r < 0.30) return withPick('reply');
    if (r < 0.33) return withPick('error');
    if (r < 0.345) return withPick('crash');
    if (r < 0.46) return { t: 'tap', card: pick(CARDS) };
    if (r < 0.56) return { t: 'bg', card: pick(CARDS), iso: pick(ISOS) };
    if (r < 0.61) return { t: 'fg' };
    if (r < 0.64) return { t: 'ping' };
    if (r < 0.67) return { t: 'raw' };
    if (r < 0.71) return { t: 'mount', iso: pick(ISOS), visible: CARDS.map(() => rand() < 0.5) };
    if (r < 0.75) return { t: 'input' };
    if (r < 0.83) return { t: 'time', ms: pick([30, 200, 700, 1300, 2500]) };
    if (r < 0.86) return { t: 'pause', on: rand() < 0.5 };
    if (r < 0.88) return { t: 'gen' };
    if (r < 0.90) return { t: 'ctorThrow' };
    if (r < 0.93) return { t: 'detached', background: rand() < 0.5 };
    if (r < 0.935) return { t: 'stop' };
    return withPick('reply');
  };
}

/** 固定日程：每个以前的发现一个。`events` 里的函数按当时状态生成事件。 */
function script(events) {
  return (ctx, step) => {
    const ev = events[step];
    if (ev === undefined) return null;
    return typeof ev === 'function' ? ev(ctx) : ev;
  };
}
const C = Object.fromEntries(CARDS.map((card) => [card.id, card]));
const fillWithBackground = (size) => Array.from({ length: size }, (_, i) => ({ t: 'raw' }));
const FIXED = {
  // r1 P1：照片 A 的后台烘焙在路上，换到 B（控制器重建），B 也开始烘焙，点 B 上没烘过的卡。
  'r1-P1-photo-switch': (size) => [
    { t: 'mount', iso: 200, visible: [true, true, true, true] },
    { t: 'time', ms: 50 },
    { t: 'mount', iso: 1600, visible: [true, true, true, true] },
    { t: 'time', ms: 50 },
    { t: 'tap', card: C.C1 },
    { t: 'tap', card: C.C2 },
    { t: 'tap', card: C.C3 },
  ],
  // r2 P1：B 的卡先按后台排进池子（Book 缓存了它的 resolve），再点这张卡。
  'r2-P1-tap-hits-queued-background': (size) => [
    ...fillWithBackground(size),
    { t: 'bg', card: C.C2, iso: 200 },
    { t: 'tap', card: C.C2 },
    { t: 'fg' },
  ],
  // r2 P2：补位时下一个 Worker 构造抛错。
  'r2-P2-ctor-throws-on-refill': (size) => [
    ...fillWithBackground(size),
    { t: 'raw' },
    { t: 'raw' },
    { t: 'ctorThrow' },
    { t: 'crash', pick: (ws) => ws[0] },
    { t: 'tap', card: C.C1 },
    { t: 'stop' },
  ],
  // r2 P2：排队的请求的可转移对象发出前已经交出去（DataCloneError），之前的回包要先落地。
  'r2-P2-detached-transferable': (size) => [
    ...fillWithBackground(size),
    { t: 'detached', background: true },
    { t: 'raw' },
    { t: 'reply', pick: (ws) => ws[0] },
    { t: 'detached', background: false },
    { t: 'reply', pick: (ws) => ws[0] },
  ],
  // r3 P1：共用的 resolve。ISO 200 的后台卡和 ISO 1600 的照片共用 R2；切到 1600 后点这张卡，旧 ISO 的 bake 不能升成前台。
  'r3-P1-shared-resolve': (size) => [
    ...fillWithBackground(size - 1),
    { t: 'bg', card: C.C2, iso: 200 },
    { t: 'mount', iso: 1600, visible: [false, false, false, false] },
    { t: 'bg', card: C.C2, iso: 1600 },
    { t: 'tap', card: C.C2 },
    { t: 'reply', pick: (ws) => ws.find((w) => /^R2/.test(w.q[0]?.tag)) ?? ws[0] },
    { t: 'reply', pick: (ws) => ws.find((w) => /^X/.test(w.q[0]?.tag)) ?? ws[0] },
    { t: 'reply', pick: (ws) => ws[0] },
  ],
  // r3 P1：一个 Worker 在做旧的后台 bake、别的 Worker 各有前台请求时要了当前照片的卡的 resolve，再点同一张卡。
  'r3-P1-background-posted-to-busy-worker': (size) => [
    { t: 'raw' },
    ...Array.from({ length: size - 1 }, () => ({ t: 'fg' })),
    { t: 'bg', card: C.C3, iso: 200 },
    { t: 'tap', card: C.C3 },
    ...Array.from({ length: size - 1 }, () => ({ t: 'reply', pick: (ws) => ws.find((w) => /^F/.test(w.q[0]?.tag)) ?? ws[0] })),
  ],
};

const violations = new Map();
function record(bad) {
  const row = violations.get(bad.kind) ?? { count: 0, sizes: new Set(), examples: [] };
  row.count += 1;
  row.sizes.add(bad.size);
  if (row.examples.length < SHOW) row.examples.push(bad);
  violations.set(bad.kind, row);
}
function report(name, schedules, started) {
  const summary = { name, root: ROOT.pathname, patch: PATCH, schedules, steps: STEPS, ms: Date.now() - started, violations: Object.fromEntries([...violations].map(([k, r]) => [k, { count: r.count, sizes: [...r.sizes] }])) };
  console.log(`SCHED ${JSON.stringify(summary)}`);
  for (const [kind, row] of violations) for (const ex of row.examples) console.log(`\n# ${kind}\n${JSON.stringify(ex, null, 1)}`);
}

if (ONLY !== 'random') {
  test('scheduling G1–G5: fixed schedules for every past finding (r1 P1, r2 P1, r2 P2 ×2, r3 P1 ×2) on 1–4 workers', async () => {
    violations.clear();
    const started = Date.now();
    let schedules = 0;
    for (const [name, build] of Object.entries(FIXED)) {
      for (const size of SIZES) {
        schedules += 1;
        const bad = await runSchedule(size, name, script(build(size)));
        if (bad) record(bad);
      }
    }
    report('fixed', schedules, started);
    assert.equal(violations.size, 0, `violations: ${[...violations.keys()].join(', ')}`);
  });
}

if (ONLY !== 'fixed') {
  test(`scheduling G1–G5: ${SEEDS} random schedules per worker count (${SIZES.join(', ')} workers, seeds from ${FROM}, ${STEPS} steps)`, async () => {
    violations.clear();
    const started = Date.now();
    let schedules = 0;
    let failing = 0;
    for (const size of SIZES) {
      for (let seed = FROM; seed < FROM + SEEDS; seed += 1) {
        schedules += 1;
        const bad = await runSchedule(size, `seed ${seed}`, randomSchedule(seed, size));
        if (bad) {
          failing += 1;
          record(bad);
        }
      }
    }
    report(`random (${failing} failing)`, schedules, started);
    assert.equal(failing, 0, `${failing} of ${schedules} schedules violate G1–G5: ${[...violations.keys()].join(', ')}`);
  });
}
