// 按需启动的核心 Worker 池（上限不变）。WASM 只拉取并编译一次，再把编译好的 Module 发给真正需要它的 Worker。
// 页面不能用 SharedArrayBuffer（GitHub Pages 没有 COOP/COEP）。

// 使用标准 new URL(..., import.meta.url) 而不是 Vite `?url` 静态导入，保证 Node --experimental-strip-types 单测加载 api/pool 时不触发原生 WASM 模块导入。
import { CoreError, type BakeArgs, type CoreOp, type CoreReply, type CoreRequest, type CoreResult } from './protocol';

function wasmAssetUrl(): string {
  return new URL('../wasm-pkg/gxr_wasm_bg.wasm', import.meta.url).href;
}

interface Pending {
  slot: number;
  worker: Worker;
  background: boolean;
  resolve: (result: CoreResult) => void;
  reject: (err: Error) => void;
}

type PoolRequest = CoreRequest & {
  wasmManaged?: boolean;
  wasmModule?: WebAssembly.Module;
};

type PoolWorkerMessage = CoreReply | { needWasm: true };

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

export class Pool {
  private readonly cap: number;
  private readonly workers: Array<Worker | null>;
  private readonly inFlight: number[];
  /** 每个槽上排着或在跑的后台请求数（配方卡小图的 resolve / bake）。前台请求全忙时不排到这些槽后面。 */
  private readonly bgInFlight: number[];
  private next = 0;
  private seq = 1;
  private stopped = false;
  private readonly pending = new Map<number, Pending>();
  private wasmPromise: Promise<WebAssembly.Module> | null = null;
  private wasmResolved: WebAssembly.Module | null = null;
  private canPostModule = true;
  private readonly wasmSent = new WeakSet<Worker>();

  constructor() {
    this.cap = workerCount();
    this.workers = Array.from({ length: this.cap }, () => null);
    this.inFlight = Array.from({ length: this.cap }, () => 0);
    this.bgInFlight = Array.from({ length: this.cap }, () => 0);
  }

  /**
   * 把 `buffer` 交给一个 Worker。`transfer` 里的东西发给对方之后这边就不能再用。
   * `background` 标出后台请求（配方卡小图）：用户点击发起的前台请求在所有槽都忙时只排到没有后台请求的槽后面。
   */
  request(
    op: CoreOp,
    buffer: ArrayBuffer | null,
    transfer: Transferable[],
    args: BakeArgs | null = null,
    extra: Pick<
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
    > = {},
    background = false,
  ): Promise<CoreResult> {
    if (this.stopped) return Promise.reject(new Error('Worker 已停止'));
    const id = this.seq++;
    const eager = EAGER_WASM_OPS.has(op);
    const maybe = MAYBE_WASM_OPS.has(op);
    const slot = this.pickSlot(eager, background);
    const worker = this.workerAt(slot);
    this.inFlight[slot] += 1;
    if (background) this.bgInFlight[slot] += 1;
    return new Promise((resolve, reject) => {
      this.pending.set(id, { slot, worker, background, resolve, reject });
      const base: PoolRequest = { id, op, buffer, args, ...extra, wasmManaged: true };
      if (eager && this.canPostModule && !this.wasmSent.has(worker)) {
        if (this.wasmResolved) {
          this.postWithModule(worker, base, transfer, this.wasmResolved);
          return;
        }
        void this.getWasmModule().then(
          (mod) => {
            if (this.stopped || !this.pending.has(id) || this.workers[slot] !== worker) return;
            this.postWithModule(worker, base, transfer, mod);
          },
          (err: unknown) => {
            const p = this.pending.get(id);
            if (!p) return;
            this.pending.delete(id);
            this.release(p);
            p.reject(err instanceof Error ? err : new Error(String(err)));
          },
        );
        return;
      }
      if (maybe && this.wasmResolved && this.canPostModule && !this.wasmSent.has(worker)) {
        this.postWithModule(worker, base, transfer, this.wasmResolved);
        return;
      }
      worker.postMessage(base, transfer);
    });
  }

  stop(): void {
    this.stopped = true;
    this.failAll('Worker 已停止');
    for (let i = 0; i < this.workers.length; i += 1) {
      this.workers[i]?.terminate();
      this.workers[i] = null;
    }
    this.wasmPromise = null;
    this.wasmResolved = null;
  }

  /**
   * 必然用 WASM 的请求优先复用已装好 WASM 且空闲的 Worker，其次选任意空闲槽（含未启动冷槽），全忙时选在途最少槽；
   * 全忙时前台请求只在没有后台请求的槽里挑（后台并发最多 workers - 1，总有这样的槽），不会排到配方卡小图后面。
   * 其余请求保持轮询（LUT 写入屏障依赖这一点）。
   */
  private pickSlot(eagerWasm: boolean, background: boolean): number {
    if (eagerWasm) {
      for (let i = 0; i < this.cap; i += 1) {
        const w = this.workers[i];
        if (w && this.inFlight[i] === 0 && this.wasmSent.has(w)) return i;
      }
      for (let i = 0; i < this.cap; i += 1) {
        const idx = (this.next + i) % this.cap;
        if (this.workers[idx] && this.inFlight[idx] === 0) {
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
      const clear = !background && this.bgInFlight.some((n) => n === 0);
      let best = -1;
      for (let i = 0; i < this.cap; i += 1) {
        const idx = (this.next + i) % this.cap;
        if (clear && this.bgInFlight[idx] > 0) continue;
        if (best < 0 || this.inFlight[idx] < this.inFlight[best]) best = idx;
      }
      this.next = best + 1;
      return best;
    }
    const slot = this.next % this.cap;
    this.next += 1;
    return slot;
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
      this.canPostModule = false;
      worker.postMessage(msg, transfer);
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

  private finish(reply: CoreReply): void {
    const pending = this.pending.get(reply.id);
    if (!pending) return;
    this.pending.delete(reply.id);
    this.release(pending);
    if (reply.ok) pending.resolve({ json: reply.json, buffers: reply.buffers });
    else pending.reject(reply.code ? new CoreError(reply.code, reply.message) : new Error(reply.message));
  }

  private release(pending: Pending): void {
    if (this.workers[pending.slot] !== pending.worker) return;
    this.inFlight[pending.slot] = Math.max(0, this.inFlight[pending.slot] - 1);
    if (pending.background) this.bgInFlight[pending.slot] = Math.max(0, this.bgInFlight[pending.slot] - 1);
  }

  /** 单个 Worker 槽位发生永久错误时只拒绝该槽位的在途任务并销毁坏实例，下次请求自动新建。 */
  private failSlot(slot: number, worker: Worker, message: string): void {
    if (this.workers[slot] === worker) {
      this.workers[slot] = null;
      this.inFlight[slot] = 0;
      this.bgInFlight[slot] = 0;
    }
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
  }

  private failAll(message: string): void {
    this.inFlight.fill(0);
    this.bgInFlight.fill(0);
    for (const [id, pending] of this.pending) {
      pending.reject(new Error(message));
      this.pending.delete(id);
    }
  }
}
