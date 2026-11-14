// 几个各自持有一份 WASM 的 Worker。页面不能用 SharedArrayBuffer（GitHub Pages 没有 COOP/COEP）。

interface Ok {
  id: number;
  ok: true;
  json: string;
}

interface Fail {
  id: number;
  ok: false;
  message: string;
}

type Reply = Ok | Fail;

interface Pending {
  resolve: (json: string) => void;
  reject: (err: Error) => void;
}

function workerCount(): number {
  const cores = navigator.hardwareConcurrency || 2;
  return Math.min(4, Math.max(1, cores - 1));
}

export class Pool {
  private readonly workers: Worker[];
  private next = 0;
  private seq = 1;
  private readonly pending = new Map<number, Pending>();

  constructor() {
    const count = workerCount();
    this.workers = Array.from({ length: count }, () => {
      const worker = new Worker(new URL('./worker.ts', import.meta.url), { type: 'module' });
      worker.onmessage = (event: MessageEvent<Reply>) => this.finish(event.data);
      worker.onerror = (event) => this.failAll(event.message || 'Worker 出错');
      return worker;
    });
  }

  /** 把 `buffer` 交给一个 Worker。`transfer` 里的东西发给对方之后这边就不能再用。 */
  request(op: 'describe' | 'ping', buffer: ArrayBuffer, transfer: Transferable[]): Promise<string> {
    const id = this.seq++;
    const worker = this.workers[this.next % this.workers.length];
    this.next += 1;
    return new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject });
      worker.postMessage({ id, op, buffer }, transfer);
    });
  }

  stop(): void {
    this.failAll('Worker 已停止');
    for (const worker of this.workers) worker.terminate();
  }

  private finish(reply: Reply): void {
    const pending = this.pending.get(reply.id);
    if (!pending) return;
    this.pending.delete(reply.id);
    if (reply.ok) pending.resolve(reply.json);
    else pending.reject(new Error(reply.message));
  }

  private failAll(message: string): void {
    for (const [id, pending] of this.pending) {
      pending.reject(new Error(message));
      this.pending.delete(id);
    }
  }
}
