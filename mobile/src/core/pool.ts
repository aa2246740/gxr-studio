// 按需启动的核心 Worker 池（上限不变）。WASM 只拉取并编译一次，再把编译好的 Module 发给真正需要它的 Worker。
// 页面不能用 SharedArrayBuffer（GitHub Pages 没有 COOP/COEP）。

// 使用标准 new URL(..., import.meta.url) 而不是 Vite `?url` 静态导入，保证 Node --experimental-strip-types 单测加载 api/pool 时不触发原生 WASM 模块导入。
import { CoreError, Lane, type BakeArgs, type CoreOp, type CoreReply, type CoreRequest, type CoreResult } from './protocol';

function wasmAssetUrl(): string {
  return new URL('../wasm-pkg/gxr_wasm_bg.wasm', import.meta.url).href;
}

interface Pending {
  slot: number;
  worker: Worker;
  /** 计算请求（EAGER_WASM_OPS）。一个 Worker 同一时间最多拿一个。 */
  compute: boolean;
  /** 后台计算请求的 Lane；null 是前台。升过级的 Lane 也算前台，已经在 Worker 上跑的也一样。 */
  lane: Lane | null;
  resolve: (result: CoreResult) => void;
  reject: (err: Error) => void;
}

type PoolRequest = CoreRequest & {
  wasmManaged?: boolean;
  wasmModule?: WebAssembly.Module;
};

type PoolWorkerMessage = CoreReply | { needWasm: true };

/** 一个请求，计算请求在交给 Worker 之前先在池子的两张单子里等。 */
interface Job {
  op: CoreOp;
  buffer: ArrayBuffer | null;
  transfer: Transferable[];
  args: BakeArgs | null;
  extra: RequestExtra;
  lane: Lane | null;
  resolve: (result: CoreResult) => void;
  reject: (err: Error) => void;
}

function asError(err: unknown): Error {
  return err instanceof Error ? err : new Error(String(err));
}

type RequestExtra = Pick<
  CoreRequest,
  | 'file'
  | 'meta'
  | 'defaultCalib'
  | 'resolve'
  | 'cameraRecipe'
  | 'check'
  | 'frameMeta'
  | 'frameLayout'
  | 'logoSave'
  | 'geom'
  | 'finalBox'
  | 'sizes'
  | 'lutImport'
  | 'lutPatch'
  | 'lutDelete'
  | 'lutBlob'
  | 'lutSnapshot'
  | 'hdrGrid'
>;

/** 必然调用 WASM 导出的请求：由池先确保单次编译完成后再一并送出。 */
const EAGER_WASM_OPS: ReadonlySet<CoreOp> = new Set([
  'describe',
  'dat',
  'bake',
  'resolve',
  'camera-recipe',
  'calib-check',
  'frame-meta',
  'frame-layout',
  'geom-crop-write',
  'geom-geometry-write',
  'geom-final-box',
  'export-output-sizes',
  'hdr-preview-grid',
]);

/** 仅在通过前缀/IDB 检查后才可能需要 WASM 的请求。 */
const MAYBE_WASM_OPS: ReadonlySet<CoreOp> = new Set([
  'calib-file',
  'calib-restore',
  'calib-default',
  'lut-import',
]);

function workerCount(): number {
  const cores = navigator.hardwareConcurrency || 2;
  return Math.min(4, Math.max(1, cores - 1));
}

async function compileWasmModule(): Promise<WebAssembly.Module> {
  const res = await fetch(wasmAssetUrl(), { credentials: 'same-origin' });
  if (!res.ok) throw new Error(`WASM HTTP ${res.status}`);
  const contentType = (res.headers.get('content-type') || '').toLowerCase();
  if (typeof WebAssembly.compileStreaming === 'function' && contentType.includes('application/wasm')) {
    return WebAssembly.compileStreaming(res);
  }
  return WebAssembly.compile(await res.arrayBuffer());
}

/**
 * 核心 Worker 池。计算请求（EAGER_WASM_OPS：describe、dat、resolve、bake……）的调度全在这里，规则见 docs/mobile.md 的 G1–G5：
 * - 计算请求先进池子的两张单子（前台、后台，各自先来先走），只有 pump() 把它交给 Worker，而且只交给手上没有计算请求的
 *   Worker：一个 Worker 上最多一个没回来的计算请求，前台请求交出去时前面什么都没有。
 * - 前台先走。后台只有在前台单子空、没有前台计算在跑、在跑的后台少于 workers - 1 个时才开始，所以至少一个 Worker
 *   总是空着给前台。
 * - 后台请求带一个 Lane（core/protocol.ts）；用户点了同一项工作时升成前台：还在单子里的挪到前台单子末尾。
 * - 共享的 WASM 模块还没编好时，计算请求都留在单子里，不先占 Worker；编好以后 pump() 按那一刻的单子重新挑（前台先）。
 *   编译失败时单子里的计算请求都以这个错误结束，下一个请求重新下载。
 * - 状态都从 pending 现算，没有要对齐的计数。
 * 其余请求（LUT、标志、校准导入、ping）照旧马上发出、严格轮询（LUT 写入屏障依赖这一点）。
 */
export class Pool {
  private readonly cap: number;
  private readonly workers: Array<Worker | null>;
  /** 同时在跑的后台计算请求上限：workers - 1（只有一个 Worker 时是 1，那时点击可能等一个后台请求）。 */
  private readonly bgMax: number;
  /** 还没交给 Worker 的计算请求，前台、后台各一张单子，先来先走。 */
  private readonly fgWaiting: Job[] = [];
  private readonly bgWaiting: Job[] = [];
  private next = 0;
  private seq = 1;
  private stopped = false;
  /** 已经在等共享模块编好（只挂一次）。 */
  private moduleWait = false;
  private readonly pending = new Map<number, Pending>();
  private wasmPromise: Promise<WebAssembly.Module> | null = null;
  private wasmResolved: WebAssembly.Module | null = null;
  private canPostModule = true;
  private readonly wasmSent = new WeakSet<Worker>();

  constructor() {
    this.cap = workerCount();
    this.workers = Array.from({ length: this.cap }, () => null);
    this.bgMax = Math.max(1, this.cap - 1);
  }

  /**
   * 把 `buffer` 交给一个 Worker。`transfer` 里的东西发给对方之后这边就不能再用。
   * `background`：后台计算请求（配方卡小图）。`true`，或者一个只给这一个请求用的 Lane（以后可以升成前台）。
   */
  request(op: CoreOp, buffer: ArrayBuffer | null, transfer: Transferable[], args: BakeArgs | null = null, extra: RequestExtra = {}, background: boolean | Lane = false): Promise<CoreResult> {
    if (this.stopped) return Promise.reject(new Error('Worker 已停止'));
    const lane = typeof background === 'object' ? background : background ? new Lane() : null;
    lane?.claim();
    return new Promise((resolve, reject) => {
      const job: Job = { op, buffer, transfer, args, extra, lane, resolve, reject };
      if (!EAGER_WASM_OPS.has(op)) {
        const slot = this.next % this.cap;
        this.next += 1;
        this.start(job, slot, false);
        return;
      }
      if (lane?.background) {
        this.bgWaiting.push(job);
        lane.onPromote(() => {
          const at = this.bgWaiting.indexOf(job);
          if (at < 0) return;
          this.bgWaiting.splice(at, 1);
          this.fgWaiting.push(job);
          this.pump();
        });
      } else this.fgWaiting.push(job);
      this.pump();
    });
  }

  stop(): void {
    this.stopped = true;
    for (const job of [...this.fgWaiting.splice(0), ...this.bgWaiting.splice(0)]) {
      job.lane?.onPromote(null);
      job.reject(new Error('Worker 已停止'));
    }
    for (const [id, pending] of this.pending) {
      this.pending.delete(id);
      pending.reject(new Error('Worker 已停止'));
    }
    for (let i = 0; i < this.workers.length; i += 1) {
      this.workers[i]?.terminate();
      this.workers[i] = null;
    }
    this.wasmPromise = null;
    this.wasmResolved = null;
    this.moduleWait = false;
  }

  /** 唯一把计算请求交给 Worker 的地方。新请求、升级、回包、请求失败、Worker 坏掉之后都调一次。 */
  private pump(): void {
    while (!this.stopped) {
      const fg = this.fgWaiting.length > 0;
      if (!fg && !this.mayStartBackground()) return;
      // 模块没编好时先等它，不先挑 Worker（freeSlot() 会推进轮询位置；编好以前也没有在跑的计算请求）。
      if (this.canPostModule && !this.wasmResolved) {
        this.awaitModule();
        return;
      }
      const slot = this.freeSlot();
      if (slot < 0) return;
      const job = (fg ? this.fgWaiting : this.bgWaiting).shift() as Job;
      job.lane?.onPromote(null);
      this.start(job, slot, true);
    }
  }

  /** 共享模块编好（或编译失败）以后再 pump() 一次。失败时单子里的计算请求都以这个错误结束。 */
  private awaitModule(): void {
    if (this.moduleWait) return;
    this.moduleWait = true;
    this.getWasmModule().then(
      () => {
        this.moduleWait = false;
        this.pump();
      },
      (err: unknown) => {
        this.moduleWait = false;
        if (this.stopped) return;
        for (const job of [...this.fgWaiting.splice(0), ...this.bgWaiting.splice(0)]) {
          job.lane?.onPromote(null);
          job.reject(asError(err));
        }
      },
    );
  }

  /** 后台可以开始：前台单子空（调用方查过）、没有前台计算在跑、在跑的后台少于 workers - 1 个。 */
  private mayStartBackground(): boolean {
    if (!this.bgWaiting.length) return false;
    let running = 0;
    for (const pending of this.live()) {
      if (!pending.compute) continue;
      if (!pending.lane?.background) return false;
      running += 1;
    }
    return running < this.bgMax;
  }

  /** 还在活着的 Worker 上的请求（坏掉换掉的 Worker 上的不算）。 */
  private *live(): Generator<Pending> {
    for (const pending of this.pending.values()) if (this.workers[pending.slot] === pending.worker) yield pending;
  }

  private load(slot: number): { all: number; compute: number } {
    let all = 0;
    let compute = 0;
    for (const pending of this.live()) {
      if (pending.slot !== slot) continue;
      all += 1;
      if (pending.compute) compute += 1;
    }
    return { all, compute };
  }

  /**
   * 手上没有计算请求的 Worker；都有就是 -1，请求留在单子里。顺序和以前一样：装好 WASM 的空闲 Worker、别的空闲 Worker、
   * 还没建的 Worker，最后是手上只有非计算请求的 Worker（挑在途最少的）。
   */
  private freeSlot(): number {
    for (let i = 0; i < this.cap; i += 1) {
      const w = this.workers[i];
      if (w && this.load(i).all === 0 && this.wasmSent.has(w)) return i;
    }
    for (let i = 0; i < this.cap; i += 1) {
      const idx = (this.next + i) % this.cap;
      if (this.workers[idx] && this.load(idx).all === 0) {
        this.next = idx + 1;
        return idx;
      }
    }
    for (let i = 0; i < this.cap; i += 1) {
      const idx = (this.next + i) % this.cap;
      if (!this.workers[idx]) {
        this.next = idx + 1;
        return idx;
      }
    }
    let best = -1;
    let bestLoad = Infinity;
    for (let i = 0; i < this.cap; i += 1) {
      const idx = (this.next + i) % this.cap;
      const load = this.load(idx);
      if (load.compute > 0 || load.all >= bestLoad) continue;
      best = idx;
      bestLoad = load.all;
    }
    if (best >= 0) this.next = best + 1;
    return best;
  }

  /** 把一个请求交给 `slot` 上的 Worker。新建 Worker 或 postMessage 同步抛错（比如可转移对象已经交出去了）时只结算这一个。 */
  private start(job: Job, slot: number, compute: boolean): void {
    const { op, buffer, transfer, args, extra, lane, resolve, reject } = job;
    const id = this.seq++;
    try {
      const worker = this.workerAt(slot);
      this.pending.set(id, { slot, worker, compute, lane: compute ? lane : null, resolve, reject });
      const base: PoolRequest = { id, op, buffer, args, ...extra, wasmManaged: true };
      // pump() 只在模块编好以后交计算请求，所以这里总是同步发出。没编好的（不会发生）照样发，Worker 会自己要模块。
      if ((compute || MAYBE_WASM_OPS.has(op)) && this.wasmResolved && this.canPostModule && !this.wasmSent.has(worker)) {
        this.postWithModule(worker, base, transfer, this.wasmResolved);
        return;
      }
      worker.postMessage(base, transfer);
    } catch (err) {
      // 只结算这一个；从 pump() 来的，循环接着发下一个（非计算请求不占单子，不用再 pump）。
      this.pending.delete(id);
      reject(asError(err));
    }
  }

  private workerAt(slot: number): Worker {
    const existing = this.workers[slot];
    if (existing) return existing;
    const worker = new Worker(new URL('./worker.ts', import.meta.url), { type: 'module' });
    worker.onmessage = (event: MessageEvent<PoolWorkerMessage>) => {
      if (this.workers[slot] !== worker) return;
      this.onMessage(worker, event.data);
    };
    worker.onerror = (event) => this.failSlot(slot, worker, event.message || 'Worker 出错');
    this.workers[slot] = worker;
    return worker;
  }

  private getWasmModule(): Promise<WebAssembly.Module> {
    if (this.wasmResolved) return Promise.resolve(this.wasmResolved);
    if (!this.wasmPromise) {
      const task = compileWasmModule().then(
        (mod) => {
          this.wasmResolved = mod;
          return mod;
        },
        (err: unknown) => {
          if (this.wasmPromise === task) this.wasmPromise = null;
          throw err;
        },
      );
      this.wasmPromise = task;
    }
    return this.wasmPromise;
  }

  private postWithModule(worker: Worker, msg: PoolRequest, transfer: Transferable[], mod: WebAssembly.Module): void {
    try {
      worker.postMessage({ ...msg, wasmModule: mod }, transfer);
      this.wasmSent.add(worker);
    } catch {
      // 不带模块再发一次；这次也抛（比如可转移对象已经交出去了），问题不在模块，原样抛给调用方。
      worker.postMessage(msg, transfer);
      this.canPostModule = false;
    }
  }

  private deliverWasm(worker: Worker): void {
    if (this.stopped || !this.workers.includes(worker)) return;
    if (!this.canPostModule) {
      worker.postMessage({ wasmFallback: true });
      return;
    }
    void this.getWasmModule().then(
      (mod) => {
        if (this.stopped || !this.workers.includes(worker)) return;
        try {
          worker.postMessage({ wasmModule: mod });
          this.wasmSent.add(worker);
        } catch {
          this.canPostModule = false;
          worker.postMessage({ wasmFallback: true });
        }
      },
      (err: unknown) => {
        if (this.stopped || !this.workers.includes(worker)) return;
        worker.postMessage({ wasmError: err instanceof Error ? err.message : String(err) });
      },
    );
  }

  private onMessage(worker: Worker, data: PoolWorkerMessage): void {
    if (data && typeof data === 'object' && 'needWasm' in data && data.needWasm) {
      this.deliverWasm(worker);
      return;
    }
    this.finish(data as CoreReply);
  }

  /** 回包先落地，再让单子里的请求补上。 */
  private finish(reply: CoreReply): void {
    const pending = this.pending.get(reply.id);
    if (!pending) return;
    this.pending.delete(reply.id);
    if (reply.ok) pending.resolve({ json: reply.json, buffers: reply.buffers });
    else pending.reject(reply.code ? new CoreError(reply.code, reply.message) : new Error(reply.message));
    this.pump();
  }

  /** 一个 Worker 坏了：只拒绝它手上的请求（最多一个计算请求），丢掉它，下次要用时新建。单子里的请求不受影响。 */
  private failSlot(slot: number, worker: Worker, message: string): void {
    if (this.workers[slot] === worker) this.workers[slot] = null;
    this.wasmSent.delete(worker);
    try {
      worker.terminate();
    } catch {
      /* 已经失效的 Worker 忽略 terminate 异常。 */
    }
    for (const [id, pending] of this.pending) {
      if (pending.slot !== slot || pending.worker !== worker) continue;
      this.pending.delete(id);
      pending.reject(new Error(message));
    }
    this.pump();
  }
}
