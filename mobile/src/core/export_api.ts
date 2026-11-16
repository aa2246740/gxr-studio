// 主线程这边的导出 Worker：只有一个，一次一张。一批开始时建，闲 30 秒或停止就关掉，WASM 的内存跟着还回去。
// Worker 陷阱、RangeError 或 error 事件：关掉它，这张算失败（内存不够那一句），下一张用新的 Worker 接着做。

import type { ExportJob, ExportMemory, ExportOut, ExportSetup, FailKind, SizesJob } from './export_protocol';

export const IDLE_MS = 30_000;

export type RunResult =
  | { ok: true; file: ArrayBuffer; result: Record<string, unknown>; shot: Record<string, unknown>; ms: number }
  | { ok: false; kind: FailKind; code: string; message: string };

/** 一批里的内存高水位（字节）。`?perf=1` 显示，测试读它。 */
export interface MemoryMarks {
  wasm: number;
  js: number;
  workers: number;
}

type Pending = { kind: 'run'; done: (r: RunResult) => void } | { kind: 'sizes'; done: (r: { ok: boolean; json?: string }) => void } | { kind: 'decode'; done: (r: ExportOut) => void };

/** 可替换的 Worker 工厂：测试用假的。 */
export type WorkerFactory = () => Worker;

const defaultFactory: WorkerFactory = () => new Worker(new URL('./export_worker.ts', import.meta.url), { type: 'module' });

export class ExportCore {
  private worker: Worker | null = null;
  private setup: ExportSetup | null = null;
  private seq = 1;
  private readonly pending = new Map<number, Pending>();
  private idle = 0;
  readonly marks: MemoryMarks = { wasm: 0, js: 0, workers: 0 };
  onMemory: ((mem: ExportMemory) => void) | null = null;

  constructor(private readonly factory: WorkerFactory = defaultFactory) {}

  /** 这一批的相机配方文件和校准包。Worker 重建时再交一次（交的是拷贝，这边留着原件）。 */
  begin(setup: ExportSetup): void {
    this.setup = setup;
    this.marks.wasm = 0;
    this.marks.js = 0;
    this.marks.workers = 0;
    if (this.worker) this.sendSetup(this.worker);
  }

  alive(): boolean {
    return this.worker !== null;
  }

  run(job: Omit<ExportJob, 'id'>): Promise<RunResult> {
    const worker = this.ensure();
    const id = this.seq++;
    return new Promise((resolve) => {
      this.pending.set(id, { kind: 'run', done: resolve });
      worker.postMessage({ op: 'run', job: { ...job, id } });
    });
  }

  sizes(job: Omit<SizesJob, 'id'>): Promise<{ ok: boolean; json?: string }> {
    const worker = this.ensure();
    const id = this.seq++;
    return new Promise((resolve) => {
      this.pending.set(id, { kind: 'sizes', done: resolve });
      worker.postMessage({ op: 'sizes', job: { ...job, id } });
    });
  }

  /** 只给开发服务器的解码门禁用：照导出的办法解一张 JPG，拿回 RGB。 */
  decode(file: File): Promise<ExportOut> {
    const worker = this.ensure();
    const id = this.seq++;
    return new Promise((resolve) => {
      this.pending.set(id, { kind: 'decode', done: resolve });
      worker.postMessage({ op: 'decode', id, file });
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

  private ensure(): Worker {
    window.clearTimeout(this.idle);
    this.idle = 0;
    if (this.worker) return this.worker;
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
    this.sendSetup(worker);
    return worker;
  }

  private sendSetup(worker: Worker): void {
    const s = this.setup ?? { dat: null, calibIndex: '', calibBlob: null };
    worker.postMessage({ op: 'setup', setup: { dat: s.dat ? s.dat.slice(0) : null, calibIndex: s.calibIndex, calibBlob: s.calibBlob ? s.calibBlob.slice(0) : null } });
  }

  private finish(worker: Worker, msg: ExportOut): void {
    if (worker !== this.worker) return;
    if (msg.op === 'mem') {
      this.marks.wasm = Math.max(this.marks.wasm, msg.mem.wasm);
      this.marks.js = Math.max(this.marks.js, msg.mem.js);
      this.onMemory?.(msg.mem);
      return;
    }
    const pending = this.pending.get(msg.id);
    if (!pending) return;
    this.pending.delete(msg.id);
    if (msg.op === 'done' && pending.kind === 'run') {
      if (msg.ok) pending.done({ ok: true, file: msg.file, result: msg.result, shot: msg.shot, ms: msg.ms });
      else {
        pending.done({ ok: false, kind: msg.kind, code: msg.code, message: msg.message });
        // 陷阱以后这份 WASM 不能再用：关掉，下一张用新的。
        if (msg.kind === 'trap') this.stop('trap');
      }
    } else if (msg.op === 'sizes' && pending.kind === 'sizes') pending.done(msg.ok ? { ok: true, json: msg.json } : { ok: false });
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
