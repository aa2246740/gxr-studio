// 同时最多两个 Worker 在读。滚动和点按留在主线程。整份 DNG 也在 Worker 里读（role 'full'）。

import type { FullOk, ThumbFail, ThumbOk, ThumbRequest, WorkerReply } from '../workers/thumb';
import { currentGridThumbWidth, GRID_THUMB_WIDTH } from './layout';

/**
 * 大屏网格缩略图比 360 解得大的那部分，位图总字节的上限（机主 2026-11-28：照片多时设内存上限）。
 * 2560×1440、2 倍屏一格约 462×308×4 ≈ 570 KB，128 MiB 约 230 张；超过以后新读的照片照旧按 360 解，
 * 所以 1,000+ 张时总量最多比今天多这 128 MiB。小版式永远是 360，不进这本账。
 */
export const BIG_THUMB_BYTES = 128 << 20;

/** 按宽度估一张缩略图最多多大：竖拍 2:3 时高是宽的 1.5 倍。 */
function thumbBytes(width: number): number {
  return width * Math.ceil(width * 1.5) * 4;
}

/** 发出去的大缩略图（弱引用，不拦回收）；close() 以后宽高变 0，就不再算。 */
export class BigThumbBudget {
  private readonly live = new Set<WeakRef<ImageBitmap>>();
  private reserved = 0;
  private readonly limit: number;

  constructor(limit = BIG_THUMB_BYTES) {
    this.limit = limit;
  }

  /** 还活着的大缩略图字节数，顺手丢掉已经关掉或回收的。 */
  bytes(): number {
    let total = 0;
    for (const ref of this.live) {
      const bitmap = ref.deref();
      if (!bitmap || bitmap.width <= 0 || bitmap.height <= 0) {
        this.live.delete(ref);
        continue;
      }
      total += bitmap.width * bitmap.height * 4;
    }
    return total;
  }

  /** 想按 `want` 解一张：放得下就先占上并返回 `want`，放不下返回 360。 */
  take(want: number): number {
    if (want <= GRID_THUMB_WIDTH) return GRID_THUMB_WIDTH;
    const need = thumbBytes(want);
    if (this.bytes() + this.reserved + need > this.limit) return GRID_THUMB_WIDTH;
    this.reserved += need;
    return want;
  }

  /** 那张读回来了（或没读成）：放掉预占，真拿到大位图就记上。 */
  settle(width: number, bitmap: ImageBitmap | null): void {
    if (width <= GRID_THUMB_WIDTH) return;
    this.reserved = Math.max(0, this.reserved - thumbBytes(width));
    if (bitmap && bitmap.width > GRID_THUMB_WIDTH) this.live.add(new WeakRef(bitmap));
  }

  stats(): { bytes: number; reserved: number; limit: number; count: number } {
    const bytes = this.bytes();
    return { bytes, reserved: this.reserved, limit: this.limit, count: this.live.size };
  }
}

type Reply = WorkerReply;

interface Job {
  file: File;
  role: ThumbRequest['role'];
  width: number;
  resolve: (reply: Reply) => void;
}

export class ThumbQueue {
  private readonly idle: Worker[] = [];
  private readonly jobs: Job[] = [];
  private readonly pending = new Map<number, { worker: Worker; job: Job }>();
  private seq = 1;
  private closed = false;
  readonly budget = new BigThumbBudget();

  constructor(limit = 2) {
    for (let i = 0; i < limit; i++) this.idle.push(this.make());
  }

  /**
   * `urgent` 插到队首：编辑器要的大图不排在缩略图后面。
   * 不给宽度就是网格缩略图：小版式 360；大屏按格宽 × 设备像素比解，受 `BIG_THUMB_BYTES` 管着。
   */
  run(file: File, role: 'jpg' | 'dng', width?: number, urgent = false): Promise<ThumbOk | ThumbFail> {
    if (width !== undefined) return this.send(file, role, width, urgent) as Promise<ThumbOk | ThumbFail>;
    const grid = this.budget.take(currentGridThumbWidth());
    const run = this.send(file, role, grid, urgent) as Promise<ThumbOk | ThumbFail>;
    return run.then((reply) => {
      this.budget.settle(grid, reply.ok ? reply.bitmap : null);
      return reply;
    });
  }

  /** 整份文件，Worker 里读完转移过来。 */
  read(file: File): Promise<FullOk | ThumbFail> {
    return this.send(file, 'full', 0, true) as Promise<FullOk | ThumbFail>;
  }

  private send(file: File, role: ThumbRequest['role'], width: number, urgent: boolean): Promise<Reply> {
    const kind = role === 'full' ? 'full' : 'thumb';
    return new Promise((resolve) => {
      if (this.closed) {
        resolve({ kind, name: file.name, id: 0, ok: false, gone: false });
        return;
      }
      const job = { file, role, width, resolve };
      if (urgent) this.jobs.unshift(job);
      else this.jobs.push(job);
      this.pump();
    });
  }

  /** 还没开始的丢掉。已经在读的那一两个让它读完。 */
  cancel(): void {
    const queued = this.jobs.splice(0);
    for (const job of queued) job.resolve({ kind: job.role === 'full' ? 'full' : 'thumb', name: job.file.name, id: 0, ok: false, gone: true });
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
      pending.job.resolve(event.data);
      this.pump();
    };
    worker.onerror = () => {
      for (const [id, pending] of this.pending) {
        if (pending.worker !== worker) continue;
        this.pending.delete(id);
        pending.job.resolve({ kind: pending.job.role === 'full' ? 'full' : 'thumb', name: pending.job.file.name, id, ok: false, gone: false });
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
      this.pending.set(id, { worker, job });
      const message: ThumbRequest = { id, file: job.file, role: job.role, width: job.width };
      try {
        worker.postMessage(message);
      } catch {
        this.pending.delete(id);
        this.idle.push(worker);
        job.resolve({ kind: job.role === 'full' ? 'full' : 'thumb', name: job.file.name, id, ok: false, gone: false });
      }
    }
  }
}

/** 不用这次读的结果了：放掉里面的位图。 */
export function dropReply(reply: ThumbOk | ThumbFail): void {
  if (reply.ok && reply.bitmap) reply.bitmap.close();
}
