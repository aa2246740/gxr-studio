// 主线程这边的导出 Worker：只有一个，一次一张。一批开始时建，闲 30 秒或停止就关掉，WASM 的内存跟着还回去。
// Worker 陷阱、RangeError 或 error 事件：关掉它，这张算失败（内存不够那一句），下一张用新的 Worker 接着做。

import type { ExportJob, ExportMemory, ExportOut, ExportSetup, FailKind, SizesJob } from './export_protocol';

export const IDLE_MS = 30_000;

export type RunResult =
  | { ok: true; file: ArrayBuffer; result: Record<string, unknown>; shot: Record<string, unknown>; exact?: boolean; ms: number }
  | { ok: false; kind: FailKind; code: string; message: string };

/** 一批里的内存高水位（字节）。`?perf=1` 显示，测试读它。 */
export interface StageMark {
  wasm: number;
  js: number;
  total: number;
}

export interface MemoryMarks {
  wasm: number;
  js: number;
  total: number;
  workers: number;
  stages: Record<ExportMemory['stage'], StageMark>;
}

function zeroStages(): Record<ExportMemory['stage'], StageMark> {
  return {
    prepare: { wasm: 0, js: 0, total: 0 },
    ink: { wasm: 0, js: 0, total: 0 },
    complete: { wasm: 0, js: 0, total: 0 },
  };
}

type Pending = { kind: 'run'; done: (r: RunResult) => void } | { kind: 'sizes'; done: (r: { ok: boolean; json?: string; source?: number }) => void } | { kind: 'decode'; done: (r: ExportOut) => void };

/** 可替换的 Worker 工厂：测试用假的。 */
export type WorkerFactory = () => Worker;

const defaultFactory: WorkerFactory = () => new Worker(new URL('./export_worker.ts', import.meta.url), { type: 'module' });

export class ExportCore {
  private worker: Worker | null = null;
  private setup: ExportSetup | null = null;
  /** 现在的 Worker 还没收到这一批的设置。只在 ensure() 里发（它总在 run / sizes / decode 的 try 里），发成功才清掉。 */
  private setupPending = true;
  private seq = 1;
  private readonly pending = new Map<number, Pending>();
  private idle = 0;
  readonly marks: MemoryMarks = { wasm: 0, js: 0, total: 0, workers: 0, stages: zeroStages() };
  onMemory: ((mem: ExportMemory) => void) | null = null;

  constructor(private readonly factory: WorkerFactory = defaultFactory) {}

  /**
   * 这一批的相机配方文件和校准包。这里只记下，不发：发给 Worker（含拷贝）在下一次 run() 的 try 里做，
   * 失败（比如分配失败的 RangeError）就关掉 Worker、这张按陷阱结算（Sol r2 P2-04）。Worker 重建时再交一次。
   */
  begin(setup: ExportSetup): void {
    this.setup = setup;
    this.setupPending = true;
    this.marks.wasm = 0;
    this.marks.js = 0;
    this.marks.total = 0;
    this.marks.workers = 0;
    this.marks.stages = zeroStages();
  }

  alive(): boolean {
    return this.worker !== null;
  }

  /**
   * 交一张给 Worker。建 Worker、交设置时的拷贝、发消息本身抛错（比如分配失败的 RangeError）也不往外抛：
   * 关掉 Worker，按陷阱（RangeError / RuntimeError）或内部错误返回，批次照常结算这一张、接着做下一张（Sol r1 P2-04）。
   */
  run(job: Omit<ExportJob, 'id'>): Promise<RunResult> {
    const id = this.seq++;
    return new Promise((resolve) => {
      this.pending.set(id, { kind: 'run', done: resolve });
      try {
        this.ensure().postMessage({ op: 'run', job: { ...job, id } });
      } catch (err) {
        this.pending.delete(id);
        this.stop();
        resolve({ ok: false, kind: isTrap(err) ? 'trap' : 'core', code: isTrap(err) ? 'trap' : 'unknown', message: '' });
      }
    });
  }

  /**
   * 成品尺寸。`withSetup` 为 false 时不顺带发这一批的设置（批次开始算一张之前估内存用）：设置还是由下一次 run()
   * 在它自己的 try 里发，发设置失败照旧按这一张的陷阱结算（Sol r2 P2-04）。
   */
  sizes(job: Omit<SizesJob, 'id'>, withSetup = true): Promise<{ ok: boolean; json?: string; source?: number }> {
    const id = this.seq++;
    return new Promise((resolve) => {
      this.pending.set(id, { kind: 'sizes', done: resolve });
      try {
        this.ensure(withSetup).postMessage({ op: 'sizes', job: { ...job, id } });
      } catch {
        this.pending.delete(id);
        this.stop();
        resolve({ ok: false });
      }
    });
  }

  /** 只给开发服务器的解码门禁用：照导出的办法解一张 JPG，拿回 RGB。 */
  decode(file: File): Promise<ExportOut> {
    const id = this.seq++;
    return new Promise((resolve) => {
      this.pending.set(id, { kind: 'decode', done: resolve });
      try {
        this.ensure().postMessage({ op: 'decode', id, file });
      } catch (err) {
        this.pending.delete(id);
        this.stop();
        resolve({ op: 'decoded', id, ok: false, message: err instanceof Error ? err.name : 'error' });
      }
    });
  }

  /** 马上关掉 Worker（「停止」、清空、批次结束、陷阱之后）。还在等的都按陷阱失败返回。 */
  stop(reason: FailKind = 'trap'): void {
    window.clearTimeout(this.idle);
    this.idle = 0;
    const worker = this.worker;
    this.worker = null;
    worker?.terminate();
    this.failAll(reason);
  }

  /** 一批结束：闲 30 秒还没有新活就关掉。 */
  release(): void {
    window.clearTimeout(this.idle);
    this.idle = window.setTimeout(() => {
      if (!this.pending.size) this.stop();
    }, IDLE_MS);
  }

  /**
   * 拿到（或新建）Worker，并把还没发的设置发过去。只在 run / sizes / decode 的 try 里调用：factory、设置拷贝或
   * postMessage 抛错时，调用方 stop()（关掉已经记下的 Worker）并按陷阱结算。
   */
  private ensure(withSetup = true): Worker {
    window.clearTimeout(this.idle);
    this.idle = 0;
    const worker = this.worker ?? this.create();
    if (withSetup && this.setupPending) {
      this.sendSetup(worker);
      this.setupPending = false;
    }
    return worker;
  }

  private create(): Worker {
    const worker = this.factory();
    this.marks.workers += 1;
    worker.onmessage = (event: MessageEvent<ExportOut>) => this.finish(worker, event.data);
    worker.onerror = (event) => {
      event.preventDefault();
      if (this.worker === worker) this.stop('trap');
    };
    worker.onmessageerror = () => {
      if (this.worker === worker) this.stop('trap');
    };
    this.worker = worker;
    // 新的 Worker 什么设置都没有。
    this.setupPending = true;
    return worker;
  }

  private sendSetup(worker: Worker): void {
    const s = this.setup ?? { dat: null, calibIndex: '', calibBlob: null, luts: {} };
    const luts: Record<string, { cube: ArrayBuffer; space: string }> = {};
    if (s.luts) {
      for (const [id, entry] of Object.entries(s.luts)) {
        luts[id] = { cube: entry.cube.slice(0), space: entry.space };
      }
    }
    worker.postMessage({
      op: 'setup',
      setup: {
        dat: s.dat ? s.dat.slice(0) : null,
        calibIndex: s.calibIndex,
        calibBlob: s.calibBlob ? s.calibBlob.slice(0) : null,
        luts,
        fontIndex: s.fontIndex ?? '',
        fontPack: s.fontPack ? s.fontPack.slice(0) : null,
        logoIndex: s.logoIndex ?? '',
        logoBlob: s.logoBlob ? s.logoBlob.slice(0) : null,
      },
    });
  }

  private finish(worker: Worker, msg: ExportOut): void {
    if (worker !== this.worker) return;
    if (msg.op === 'mem') {
      const total = msg.mem.wasm + msg.mem.js;
      this.marks.wasm = Math.max(this.marks.wasm, msg.mem.wasm);
      this.marks.js = Math.max(this.marks.js, msg.mem.js);
      this.marks.total = Math.max(this.marks.total, total);
      const st = this.marks.stages[msg.mem.stage];
      if (st) {
        st.wasm = Math.max(st.wasm, msg.mem.wasm);
        st.js = Math.max(st.js, msg.mem.js);
        st.total = Math.max(st.total, total);
      }
      this.onMemory?.(msg.mem);
      return;
    }
    const pending = this.pending.get(msg.id);
    if (!pending) return;
    this.pending.delete(msg.id);
    if (msg.op === 'done' && pending.kind === 'run') {
      if (msg.ok) pending.done({ ok: true, file: msg.file, result: msg.result, shot: msg.shot, exact: msg.exact, ms: msg.ms });
      else {
        pending.done({ ok: false, kind: msg.kind, code: msg.code, message: msg.message });
        // 陷阱以后这份 WASM 不能再用：关掉，下一张用新的。
        if (msg.kind === 'trap') this.stop('trap');
      }
    } else if (msg.op === 'sizes' && pending.kind === 'sizes') pending.done(msg.ok ? { ok: true, json: msg.json, source: msg.source } : { ok: false });
    else if (msg.op === 'decoded' && pending.kind === 'decode') pending.done(msg);
  }

  private failAll(kind: FailKind): void {
    for (const [id, pending] of this.pending) {
      this.pending.delete(id);
      if (pending.kind === 'run') pending.done({ ok: false, kind, code: kind, message: '' });
      else if (pending.kind === 'sizes') pending.done({ ok: false });
      else pending.done({ op: 'decoded', id, ok: false, message: 'stopped' });
    }
  }
}

function isTrap(err: unknown): boolean {
  return err instanceof RangeError || err instanceof WebAssembly.RuntimeError;
}

/** 校准包：manifest 和其余文件按名字排好拼成一块，加 `[[名字, 字节数], …]`（export_prepare 的 calib_index）。 */
export function calibPack(files: ReadonlyMap<string, Uint8Array> | null): { calibIndex: string; calibBlob: ArrayBuffer | null } {
  if (!files || !files.size) return { calibIndex: '', calibBlob: null };
  const names = [...files.keys()].sort();
  const index: Array<[string, number]> = [];
  let size = 0;
  for (const name of names) {
    const len = files.get(name)?.length ?? 0;
    index.push([name, len]);
    size += len;
  }
  const blob = new Uint8Array(size);
  let at = 0;
  for (const name of names) {
    const part = files.get(name) ?? new Uint8Array(0);
    blob.set(part, at);
    at += part.length;
  }
  return { calibIndex: JSON.stringify(index), calibBlob: blob.buffer };
}
