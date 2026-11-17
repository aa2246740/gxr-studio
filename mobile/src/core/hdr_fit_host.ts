// 编辑器自己的 HDR 拟合 Worker。和四份核心池分开，最多一个在跑，忙的时候只留最新的一个排队。
// 换片、关掉编辑器或关掉 HDR 时 terminate，WASM 线性内存跟着还回去。

import type { HdrChainResult } from './hdr_chain';

export interface HdrFitJob {
  sdr: Uint8Array;
  width: number;
  height: number;
  dng: ArrayBuffer;
  jpg: ArrayBuffer | null;
  base: Uint8Array | null;
  baseWidth: number;
  baseHeight: number;
  geometryJson: string | null;
  cropJson: string | null;
  box: { x: number; y: number; w: number; h: number } | null;
  frameLayoutJson: string | null;
  factor: number;
}

interface FitReply {
  id: number;
  ok: boolean;
  json?: string;
  buffers?: ArrayBuffer[];
  message?: string;
}

export interface FitPort {
  postMessage(message: unknown, transfer: Transferable[]): void;
  terminate(): void;
  onmessage: ((event: MessageEvent<FitReply>) => void) | null;
  onerror: ((event: { message?: string }) => void) | null;
}

interface Pending {
  id: number;
  job: HdrFitJob;
  resolve: (result: HdrChainResult) => void;
  reject: (err: Error) => void;
}

function copyBytes(bytes: Uint8Array): ArrayBuffer {
  const copy = new ArrayBuffer(bytes.byteLength);
  new Uint8Array(copy).set(bytes);
  return copy;
}

function copyBuffer(buffer: ArrayBuffer): ArrayBuffer {
  const copy = new ArrayBuffer(buffer.byteLength);
  new Uint8Array(copy).set(new Uint8Array(buffer));
  return copy;
}

function defaultSpawn(): FitPort {
  return new Worker(new URL('./worker.ts', import.meta.url), { type: 'module' }) as unknown as FitPort;
}

/**
 * 一个专用 Worker。`request` 在已有任务时把上一个排队的改成 stale，只保留最新的。
 * `stop` 终止 Worker，在途和排队的都按 stale 结束。
 */
export class HdrFitHost {
  private worker: FitPort | null = null;
  private readonly spawn: () => FitPort;
  private seq = 1;
  private inflight: Pending | null = null;
  private pending: Pending | null = null;

  constructor(spawn?: () => FitPort) {
    this.spawn = spawn ?? defaultSpawn;
  }

  request(job: HdrFitJob): Promise<HdrChainResult> {
    const id = this.seq++;
    return new Promise((resolve, reject) => {
      const next: Pending = { id, job, resolve, reject };
      if (this.inflight) {
        if (this.pending) this.pending.resolve({ ok: false, reason: 'stale' });
        this.pending = next;
        return;
      }
      this.send(next);
    });
  }

  /** 换片、销毁或关掉 HDR。Worker 被终止，排队的请求以 stale 结束。 */
  stop(): void {
    if (this.pending) {
      this.pending.resolve({ ok: false, reason: 'stale' });
      this.pending = null;
    }
    if (this.inflight) {
      const current = this.inflight;
      this.inflight = null;
      current.resolve({ ok: false, reason: 'stale' });
    }
    if (this.worker) {
      this.worker.onmessage = null;
      this.worker.onerror = null;
      this.worker.terminate();
      this.worker = null;
    }
  }

  private ensure(): FitPort {
    if (this.worker) return this.worker;
    const worker = this.spawn();
    worker.onmessage = (event) => this.finish(event.data);
    worker.onerror = (event) => this.fail(event.message || 'Worker 出错');
    this.worker = worker;
    return worker;
  }

  private send(job: Pending): void {
    this.inflight = job;
    const sdr = copyBytes(job.job.sdr);
    const dng = copyBuffer(job.job.dng);
    const jpg = job.job.jpg ? copyBuffer(job.job.jpg) : null;
    const base = job.job.base ? copyBytes(job.job.base) : null;
    const transfer: ArrayBuffer[] = [sdr, dng];
    if (jpg) transfer.push(jpg);
    if (base) transfer.push(base);
    this.ensure().postMessage(
      {
        id: job.id,
        op: 'hdr-preview-grid',
        buffer: sdr,
        hdrGrid: {
          width: job.job.width,
          height: job.job.height,
          dng,
          jpg,
          base,
          baseWidth: job.job.baseWidth,
          baseHeight: job.job.baseHeight,
          geometryJson: job.job.geometryJson,
          cropJson: job.job.cropJson,
          box: job.job.box,
          frameLayoutJson: job.job.frameLayoutJson,
          factor: job.job.factor,
        },
      },
      transfer,
    );
  }

  private finish(reply: FitReply | undefined): void {
    if (!reply) return;
    const current = this.inflight;
    if (!current || current.id !== reply.id) return;
    this.inflight = null;
    if (!reply.ok) {
      current.reject(new Error(reply.message || 'HDR 拟合失败'));
    } else {
      try {
        const parsed = JSON.parse(reply.json || '{}') as HdrChainResult;
        if (parsed.ok) {
          const codes = new Uint8Array(reply.buffers?.[0] ?? new ArrayBuffer(0));
          current.resolve({ ...parsed, codes });
        } else {
          current.resolve(parsed);
        }
      } catch (err) {
        current.reject(err instanceof Error ? err : new Error(String(err)));
      }
    }
    const next = this.pending;
    this.pending = null;
    if (next) this.send(next);
  }

  private fail(message: string): void {
    const current = this.inflight;
    this.inflight = null;
    current?.reject(new Error(message));
    const next = this.pending;
    this.pending = null;
    if (next) this.send(next);
  }
}
