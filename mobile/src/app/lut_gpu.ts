// GPU 3D LUT 纹理调度器：
// 1. 一次只开一个短命 `workers/lut_table.ts` Worker 生成 `lut_table`，拿到后立刻 `engine.addLut(token, meta, data)`、清 JS 引用并 `worker.terminate()` 回收 WASM 线性内存。
// 2. 不超过引擎的 3 槽上限（FIFO），记录 GPU / JS / Worker WASM 内存高水位，供调试与测试核对。
// 3. 后台小图队列只在非拖动、非导出时跑；主舞台切换若遇到尚未就绪或被挤掉的 LUT，优先重建上传。

import type { Engine, LutMeta } from '../../../frontend/src/index';
import { readLutBlob } from '../core/api';
import { lutRenderToken, lutTargetSpec, type LutRecord } from '../lib/lut';
import type { LutTableReply } from '../workers/lut_table';

export const MAX_GPU_LUT_SLOTS = 3;

export interface LutGpuStats {
  alive: number;
  spawned: number;
  terminated: number;
  pending: number;
  gpuSlots: number;
  gpuBytes: number;
  peakGpuBytes: number;
  peakJsBytes: number;
  peakWorkerWasmBytes: number;
}

export interface LutGpuHost {
  engine(): Engine | null;
  readBlob(cubeId: string): Promise<ArrayBuffer | null>;
}

const defaultHost = (getEngine: () => Engine | null): LutGpuHost => ({
  engine: getEngine,
  readBlob: readLutBlob,
});

function tableGpuBytes(meta: LutMeta): number {
  const n = Math.max(1, Number(meta.n) || 1);
  return n * n * n * (meta.table8 ? 4 : 16);
}

function buildOneInShortWorker(cube: ArrayBuffer, spec: string): Promise<LutTableReply> {
  return new Promise((resolve) => {
    const worker = new Worker(new URL('../workers/lut_table.ts', import.meta.url), { type: 'module' });
    let finished = false;
    const finish = (reply: LutTableReply) => {
      if (finished) return;
      finished = true;
      try {
        worker.terminate();
      } catch {
        /* 已退出 */
      }
      resolve(reply);
    };
    worker.onmessage = (event: MessageEvent<LutTableReply>) => finish(event.data);
    worker.onerror = (event) =>
      finish({ ok: false, reason: 'worker', message: event.message || 'LUT Worker 出错', wasmBytes: 0 });
    worker.onmessageerror = () => finish({ ok: false, reason: 'worker', message: 'LUT 消息损坏', wasmBytes: 0 });
    try {
      worker.postMessage({ cube, spec }, [cube]);
    } catch (err) {
      finish({ ok: false, reason: 'worker', message: err instanceof Error ? err.message : String(err), wasmBytes: 0 });
    }
  });
}

export class LutGpu {
  private readonly inflight = new Map<string, Promise<{ ok: true; token: string } | { ok: false; reason: string; message: string }>>();
  private tail: Promise<unknown> = Promise.resolve();
  /** 与 `Engine.luts` 的 FIFO 3 槽对齐：前端镜像记录每槽字节数以统计准确峰值。 */
  private readonly gpuSlots = new Map<string, number>();
  private aliveCount = 0;
  private spawnedCount = 0;
  private terminatedCount = 0;
  private pendingCount = 0;
  private peakGpuBytes = 0;
  private peakJsBytes = 0;
  private peakWorkerWasmBytes = 0;
  private host: LutGpuHost;

  constructor(getEngine: () => Engine | null = () => null, host?: LutGpuHost) {
    this.host = host ?? defaultHost(getEngine);
  }

  attach(getEngine: () => Engine | null): void {
    this.host = { ...this.host, engine: getEngine };
  }

  stats(): LutGpuStats {
    this.syncSlots();
    let gpuBytes = 0;
    for (const b of this.gpuSlots.values()) gpuBytes += b;
    return {
      alive: this.aliveCount,
      spawned: this.spawnedCount,
      terminated: this.terminatedCount,
      pending: this.pendingCount,
      gpuSlots: this.gpuSlots.size,
      gpuBytes,
      peakGpuBytes: this.peakGpuBytes,
      peakJsBytes: this.peakJsBytes,
      peakWorkerWasmBytes: this.peakWorkerWasmBytes,
    };
  }

  has(token: string): boolean {
    const engine = this.host.engine();
    return Boolean(engine && engine.hasLut(token));
  }

  /** 确保某个 LUT 的渲染令牌已装进显卡（如已装直接返回；否则串行排入短命 Worker）。 */
  ensure(record: LutRecord): Promise<{ ok: true; token: string } | { ok: false; reason: string; message: string }> {
    const spec = lutTargetSpec(record);
    if (!spec) return Promise.resolve({ ok: false, reason: 'space_required', message: '请选择输入色彩空间' });
    const token = lutRenderToken(record);
    const engine = this.host.engine();
    if (!engine) return Promise.resolve({ ok: false, reason: 'no_gpu', message: '没有显卡' });
    if (engine.hasLut(token)) return Promise.resolve({ ok: true, token });

    const existing = this.inflight.get(token);
    if (existing) return existing;

    this.pendingCount += 1;
    const job = this.tail
      .catch(() => undefined)
      .then(async (): Promise<{ ok: true; token: string } | { ok: false; reason: string; message: string }> => {
        const eng = this.host.engine();
        if (!eng) return { ok: false, reason: 'no_gpu', message: '没有显卡' };
        if (eng.hasLut(token)) return { ok: true, token };

        const blob = await this.host.readBlob(record.cube_id);
        if (!blob) return { ok: false, reason: 'not_found', message: '找不到这个 LUT。请重新导入。' };

        this.peakJsBytes = Math.max(this.peakJsBytes, blob.byteLength);
        this.spawnedCount += 1;
        this.aliveCount += 1;
        let reply: LutTableReply;
        try {
          reply = await buildOneInShortWorker(blob, JSON.stringify(spec));
        } finally {
          this.aliveCount = Math.max(0, this.aliveCount - 1);
          this.terminatedCount += 1;
        }
        if (reply.wasmBytes > 0) this.peakWorkerWasmBytes = Math.max(this.peakWorkerWasmBytes, reply.wasmBytes);
        if (!reply.ok) {
          return { ok: false, reason: reply.reason, message: reply.message };
        }

        let data: ArrayBuffer | null = reply.data;
        this.peakJsBytes = Math.max(this.peakJsBytes, data.byteLength);
        try {
          const meta = JSON.parse(reply.metaJson) as LutMeta;
          const engNow = this.host.engine();
          if (!engNow) return { ok: false, reason: 'no_gpu', message: '没有显卡' };
          engNow.addLut(token, meta, data);
          this.recordSlot(token, tableGpuBytes(meta));
          return { ok: true, token };
        } finally {
          // 立刻放掉 JS 里的表字节，不留在主线程内存里。
          data = null;
        }
      })
      .finally(() => {
        this.pendingCount = Math.max(0, this.pendingCount - 1);
        if (this.inflight.get(token) === job) this.inflight.delete(token);
      });

    this.inflight.set(token, job);
    this.tail = job.catch(() => undefined);
    return job;
  }

  private recordSlot(token: string, bytes: number): void {
    this.gpuSlots.delete(token);
    this.gpuSlots.set(token, bytes);
    while (this.gpuSlots.size > MAX_GPU_LUT_SLOTS) {
      const oldest = this.gpuSlots.keys().next().value;
      if (oldest === undefined) break;
      this.gpuSlots.delete(oldest);
    }
    this.syncSlots();
    let total = 0;
    for (const b of this.gpuSlots.values()) total += b;
    this.peakGpuBytes = Math.max(this.peakGpuBytes, total);
  }

  private syncSlots(): void {
    const eng = this.host.engine();
    if (!eng) return;
    for (const token of [...this.gpuSlots.keys()]) {
      if (!eng.hasLut(token)) this.gpuSlots.delete(token);
    }
  }
}
