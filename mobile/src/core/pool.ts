// 按需启动的核心 Worker 池（上限不变）。WASM 只拉取并编译一次，再把编译好的 Module 发给真正需要它的 Worker。
// 页面不能用 SharedArrayBuffer（GitHub Pages 没有 COOP/COEP）。

import wasmUrl from '../wasm-pkg/gxr_wasm_bg.wasm?url';
import { CoreError, type BakeArgs, type CoreOp, type CoreReply, type CoreRequest, type CoreResult } from './protocol';

interface Pending {
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
  const res = await fetch(wasmUrl, { credentials: 'same-origin' });
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
  }

  /** 把 `buffer` 交给一个 Worker。`transfer` 里的东西发给对方之后这边就不能再用。 */
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
  ): Promise<CoreResult> {
    if (this.stopped) return Promise.reject(new Error('Worker 已停止'));
    const id = this.seq++;
    const slot = this.next % this.cap;
    this.next += 1;
    const worker = this.workerAt(slot);
    return new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject });
      const base: PoolRequest = { id, op, buffer, args, ...extra, wasmManaged: true };
      const eager = EAGER_WASM_OPS.has(op);
      const maybe = MAYBE_WASM_OPS.has(op);
      if (eager && this.canPostModule && !this.wasmSent.has(worker)) {
        if (this.wasmResolved) {
          this.postWithModule(worker, base, transfer, this.wasmResolved);
          return;
        }
        void this.getWasmModule().then(
          (mod) => {
            if (this.stopped || !this.pending.has(id)) return;
            this.postWithModule(worker, base, transfer, mod);
          },
          (err: unknown) => {
            const p = this.pending.get(id);
            if (!p) return;
            this.pending.delete(id);
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

  private workerAt(slot: number): Worker {
    const existing = this.workers[slot];
    if (existing) return existing;
    const worker = new Worker(new URL('./worker.ts', import.meta.url), { type: 'module' });
    worker.onmessage = (event: MessageEvent<PoolWorkerMessage>) => this.onMessage(worker, event.data);
    worker.onerror = (event) => this.failAll(event.message || 'Worker 出错');
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
    if (this.stopped) return;
    if (!this.canPostModule) {
      worker.postMessage({ wasmFallback: true });
      return;
    }
    void this.getWasmModule().then(
      (mod) => {
        if (this.stopped) return;
        try {
          worker.postMessage({ wasmModule: mod });
          this.wasmSent.add(worker);
        } catch {
          this.canPostModule = false;
          worker.postMessage({ wasmFallback: true });
        }
      },
      (err: unknown) => {
        if (this.stopped) return;
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
    if (reply.ok) pending.resolve({ json: reply.json, buffers: reply.buffers });
    else pending.reject(reply.code ? new CoreError(reply.code, reply.message) : new Error(reply.message));
  }

  private failAll(message: string): void {
    for (const [id, pending] of this.pending) {
      pending.reject(new Error(message));
      this.pending.delete(id);
    }
  }
}
