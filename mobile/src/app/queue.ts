// 同时最多两个 Worker 在读。滚动和点按留在主线程。

import type { ThumbFail, ThumbOk, ThumbRequest } from '../workers/thumb';

type Reply = ThumbOk | ThumbFail;

interface Job {
  file: File;
  role: 'jpg' | 'dng';
  width: number;
  resolve: (reply: Reply) => void;
}

export class ThumbQueue {
  private readonly idle: Worker[] = [];
  private readonly jobs: Job[] = [];
  private readonly pending = new Map<number, { worker: Worker; resolve: (reply: Reply) => void }>();
  private seq = 1;
  private closed = false;

  constructor(limit = 2) {
    for (let i = 0; i < limit; i++) this.idle.push(this.make());
  }

  run(file: File, role: 'jpg' | 'dng', width = 360): Promise<Reply> {
    return new Promise((resolve) => {
      if (this.closed) {
        resolve({ id: 0, ok: false, gone: false });
        return;
      }
      this.jobs.push({ file, role, width, resolve });
      this.pump();
    });
  }

  /** 还没开始的丢掉。已经在读的那一两个让它读完。 */
  cancel(): void {
    const queued = this.jobs.splice(0);
    for (const job of queued) job.resolve({ id: 0, ok: false, gone: true });
  }

  stop(): void {
    this.closed = true;
    this.cancel();
    for (const worker of this.idle) worker.terminate();
    this.idle.length = 0;
    for (const pending of this.pending.values()) pending.worker.terminate();
    this.pending.clear();
  }

  private make(): Worker {
    const worker = new Worker(new URL('../workers/thumb.ts', import.meta.url), { type: 'module' });
    worker.onmessage = (event: MessageEvent<Reply>) => {
      const pending = this.pending.get(event.data.id);
      if (!pending) return;
      this.pending.delete(event.data.id);
      this.idle.push(pending.worker);
      pending.resolve(event.data);
      this.pump();
    };
    worker.onerror = () => {
      for (const [id, pending] of this.pending) {
        if (pending.worker !== worker) continue;
        this.pending.delete(id);
        pending.resolve({ id, ok: false, gone: false });
      }
      if (!this.closed && !this.idle.includes(worker)) this.idle.push(worker);
      this.pump();
    };
    return worker;
  }

  private pump(): void {
    while (!this.closed && this.idle.length && this.jobs.length) {
      const worker = this.idle.pop();
      const job = this.jobs.shift();
      if (!worker || !job) return;
      const id = this.seq++;
      this.pending.set(id, { worker, resolve: job.resolve });
      const message: ThumbRequest = { id, file: job.file, role: job.role, width: job.width };
      try {
        worker.postMessage(message);
      } catch {
        this.pending.delete(id);
        this.idle.push(worker);
        job.resolve({ id, ok: false, gone: false });
      }
    }
  }
}
