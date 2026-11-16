// 一批导出：点「存到相册」那一刻把照片、文件句柄、版本和请求定下来（桌面 N01），之后改动不影响这一批。
// 一次一张交给导出 Worker；做好的文件留在内存里等用户点「存到相册」交给系统共享面板。不碰 DOM，测试直接驱动。

import type { RunResult } from '../core/export_api';
import type { FailKind } from '../core/export_protocol';
import type { ExportRequest } from '../lib/export_request';

/** 做好、还没交出去的文件最多占这么多字节。下一张会超过时先停在「做好了」那一栏，交出去、点「好」以后再接着做。 */
export const STAGE_CAP_BYTES = 384 * 1024 * 1024;
/**
 * 开了 HDR 的一批用的上限。HDR 让导出 Worker 的 WASM 峰值从约 260 MiB 升到约 470 MiB，Worker 里还多拿着一份数据层，
 * 384 MiB 的待存文件加上它们会超过整页 1.2 GiB 的估算，所以 HDR 批次把待存文件压到这里（理由和数字见 ui-m3-hdr 的 REPORT.md）。
 */
export const STAGE_CAP_HDR_BYTES = 256 * 1024 * 1024;

export const TEXT = {
  read: '存储卡上的文件读不了',
  memory: '内存不够，这张没做成。可以重试，或改用小一点的尺寸。',
  fallback: '导出失败。请重试。',
};

export interface BatchItem {
  id: string;
  stem: string;
  version: number;
  dng: File | null;
  jpg: File | null;
  req: ExportRequest;
}

/** `saved`：已经交给共享面板（点了「好」）。 */
export type ItemState = 'wait' | 'run' | 'ok' | 'fail' | 'saved';

export interface Staged {
  id: string;
  file: File;
}

export interface Failure {
  id: string;
  stem: string;
  kind: FailKind | 'stale';
  message: string;
}

/** 批次碰到的外面：导出 Worker、照片的现状、M1 的「读不了」、暂停配方卡、通知界面。 */
export interface BatchHost {
  run(item: BatchItem, index: number): Promise<RunResult>;
  /** 马上关掉导出 Worker。 */
  stop(): void;
  /** 一批不再跑了：让 Worker 闲 30 秒后关掉。 */
  idle(): void;
  session(): number;
  /** 这张照片现在的版本和文件句柄；去掉了是 null。 */
  photo(id: string): { version: number; dng: File | null; jpg: File | null } | null;
  /** 文件读不了：走 M1 的 gone（拔卡）路径。 */
  markGone(id: string, version: number): void;
  /** 跑着的时候 true：配方卡小图和烘焙先停，申请屏幕常亮。 */
  busy(on: boolean): void;
  changed(): void;
}

export type Phase = 'running' | 'held' | 'ready' | 'stopped' | 'closed';

/** 一行失败原因（`<文件名>：<原因>` 的后半句）。核心的拒绝照桌面 followBatch 原样显示 message。 */
export function failText(kind: FailKind | 'stale', code: string, message: string): string {
  if (kind === 'read') return TEXT.read;
  if (kind === 'trap') return TEXT.memory;
  if (kind === 'core' && code !== 'unknown' && message) return message;
  return TEXT.fallback;
}

let nextBatch = 1;

export class ExportBatch {
  readonly id = nextBatch++;
  readonly session: number;
  readonly states = new Map<string, ItemState>();
  readonly failures = new Map<string, Failure>();
  readonly results = new Map<string, Record<string, unknown>>();
  /** 交给 export_prepare 的 shot（开发服务器的测试读）。 */
  readonly shots = new Map<string, Record<string, unknown>>();
  /** 每张 `export_ink` 返回的 `exact` 标记。 */
  readonly exacts = new Map<string, boolean>();
  staged: Staged[] = [];
  stagedBytes = 0;
  /** 留着待交的文件最多同时占了多少字节（报告用；任何时候都不超过上限）。 */
  stagedHigh = 0;
  /** 每张从交给 Worker 到拿回文件的毫秒数。 */
  readonly times: number[] = [];
  phase: Phase = 'running';
  /** 还没做的照片（按网格顺序）。停在上限时留着，交出去以后接着做（Sol r1 P2-02）。 */
  private queue: string[] = [];
  private loop: Promise<void> | null = null;
  private token = 0;

  readonly items: BatchItem[];
  private readonly host: BatchHost;
  readonly cap: number;

  // 不用参数属性：Node 的 strip-types 跑单元测试时不认。
  constructor(items: BatchItem[], host: BatchHost, cap = STAGE_CAP_BYTES) {
    this.items = items;
    this.host = host;
    this.cap = cap;
    this.session = host.session();
    for (const item of items) this.states.set(item.id, 'wait');
  }

  /** 这一批开了 HDR（点「存到相册」时定下来的）。 */
  get hdr(): boolean {
    return this.items.some((it) => it.req.hdr);
  }

  item(id: string): BatchItem | undefined {
    return this.items.find((it) => it.id === id);
  }

  /** 第几张在做（从 1 起），一共几张。交出去的也算做完了。 */
  progress(): { done: number; total: number; current: number } {
    let done = 0;
    for (const s of this.states.values()) if (s === 'ok' || s === 'fail' || s === 'saved') done += 1;
    return { done, total: this.items.length, current: Math.min(this.items.length, done + 1) };
  }

  /** 做好、还没交出去的张数。 */
  okCount(): number {
    return [...this.states.values()].filter((s) => s === 'ok').length;
  }

  /** 已经交给共享面板的张数。 */
  savedCount(): number {
    return [...this.states.values()].filter((s) => s === 'saved').length;
  }

  /** 还排着、没做的张数（停在上限时交出去以后接着做）。 */
  pending(): number {
    return this.queue.length;
  }

  start(): void {
    this.queue = this.items.map((it) => it.id);
    this.kick();
  }

  /** 「停止」：马上关掉 Worker，做好的留着，没做的不算失败。 */
  stop(): void {
    if (this.phase !== 'running') return;
    this.token += 1;
    this.queue = [];
    for (const [id, s] of this.states) if (s === 'run') this.states.set(id, 'wait');
    this.host.stop();
    this.phase = 'stopped';
    this.host.busy(false);
    this.host.changed();
  }

  /**
   * 「重试这张 / 这 N 张」：只重做失败的，用同一份请求；重新选过的照片用新的 File。失败的照片按网格顺序并进还没做的
   * 队列（不覆盖它）。停在上限时不马上开始：先交出现在的文件，点「好」以后连同剩下的一起接着做（Sol r1 P2-02）。
   */
  retry(): void {
    if (this.phase === 'running' || this.phase === 'closed') return;
    const failed = [...this.failures.keys()];
    if (!failed.length) return;
    for (const id of failed) {
      const item = this.item(id);
      const now = this.host.photo(id);
      if (item && now && now.version !== item.version) {
        item.version = now.version;
        item.dng = now.dng;
        item.jpg = now.jpg;
      }
      this.failures.delete(id);
      this.states.set(id, 'wait');
    }
    const order = (id: string) => this.items.findIndex((it) => it.id === id);
    this.queue = [...new Set([...this.queue, ...failed])].sort((a, b) => order(a) - order(b));
    if (this.phase === 'held') {
      this.host.changed();
      return;
    }
    this.phase = 'running';
    this.kick();
  }

  /**
   * 共享面板关了、用户点了「好」：放掉交出去的文件（标成已交出）。还有排着的就接着做；还有失败的就留在 06f，
   * 重试一直在，直到它们做好或者清空（Sol r1 P2-01）；都没有了这一批才结束。
   */
  release(): void {
    if (this.phase === 'running' || this.phase === 'closed') return;
    for (const s of this.staged) this.states.set(s.id, 'saved');
    this.staged = [];
    this.stagedBytes = 0;
    if (this.queue.length && this.phase === 'held') {
      this.phase = 'running';
      this.kick();
      return;
    }
    if (this.failures.size) {
      this.phase = 'ready';
      this.host.changed();
      return;
    }
    this.close();
  }

  /** 清空照片（或换了新的一批）：停下，丢掉所有文件，晚到的结果也丢。 */
  cancel(): void {
    this.token += 1;
    this.queue = [];
    if (this.phase === 'running') this.host.stop();
    this.close();
  }

  private close(): void {
    this.staged = [];
    this.stagedBytes = 0;
    if (this.phase !== 'closed') {
      this.phase = 'closed';
      this.host.busy(false);
      this.host.idle();
      this.host.changed();
    }
  }

  private kick(): void {
    this.host.busy(true);
    this.host.changed();
    const token = this.token;
    // drain 自己结算每一张；万一还有没想到的异常，也不能让这一批一直停在 running（Sol r1 P2-04）。
    this.loop = this.drain(token).catch(() => this.settle(token));
  }

  private live(token: number): boolean {
    return token === this.token && this.host.session() === this.session && this.phase === 'running';
  }

  private async drain(token: number): Promise<void> {
    while (this.live(token) && this.queue.length) {
      const id = this.queue.shift() as string;
      const item = this.item(id);
      if (!item) continue;
      this.states.set(id, 'run');
      this.host.changed();
      const index = this.items.indexOf(item);
      let result: RunResult;
      try {
        result = await this.host.run(item, index);
      } catch (err) {
        // 交给 Worker 这一步本身抛了（建 Worker、拷贝、发消息时 RangeError 之类）：照运行时的陷阱算，关掉 Worker，
        // 这张失败，接着做下一张（Sol r1 P2-04）。
        if (token === this.token) this.host.stop();
        result = { ok: false, kind: 'trap', code: 'trap', message: err instanceof Error ? err.name : '' };
      }
      if (!this.live(token)) return;
      this.commit(item, result);
      if (this.phase === 'held') return;
    }
    this.settle(token);
  }

  /** 队列跑完：有待交的文件或失败就停在「做好了」那一栏，否则这一批结束。 */
  private settle(token: number): void {
    if (!this.live(token)) return;
    if (!this.staged.length && !this.failures.size) {
      this.close();
      return;
    }
    this.phase = 'ready';
    this.host.busy(false);
    this.host.idle();
    this.host.changed();
  }

  private commit(item: BatchItem, result: RunResult): void {
    const now = this.host.photo(item.id);
    if (!now || now.version !== item.version) {
      // 做的时候这张被去掉或重新选过：这份结果作废，算失败，重试时用新的 File。
      this.fail(item, 'stale', 'stale', '');
      return;
    }
    if (!result.ok) {
      if (result.kind === 'read') this.host.markGone(item.id, item.version);
      this.fail(item, result.kind, result.code, result.message);
      return;
    }
    const size = result.file.byteLength;
    if (this.stagedBytes + size > this.cap) {
      if (!this.staged.length) {
        // 一张就超过上限：按内存不够算（不能因为手里没别的文件就越过上限，Sol r1 P2-05）。
        this.fail(item, 'trap', 'staged_cap', '');
        return;
      }
      // 留着会超过上限：这份结果放掉，这张排回队首，先停在「做好了」那一栏。交出去、点「好」以后再做它。
      this.states.set(item.id, 'wait');
      this.queue.unshift(item.id);
      this.phase = 'held';
      this.host.busy(false);
      this.host.changed();
      return;
    }
    const name = typeof result.result.file_name === 'string' ? result.result.file_name : `${item.stem}_gxr.jpg`;
    const file = new File([result.file], name, { type: 'image/jpeg' });
    this.states.set(item.id, 'ok');
    this.results.set(item.id, result.result);
    this.shots.set(item.id, result.shot);
    this.exacts.set(item.id, result.exact !== false);
    this.times.push(result.ms);
    this.staged.push({ id: item.id, file });
    this.stagedBytes += file.size;
    this.stagedHigh = Math.max(this.stagedHigh, this.stagedBytes);
    this.host.changed();
  }

  private fail(item: BatchItem, kind: FailKind | 'stale', code: string, message: string): void {
    this.states.set(item.id, 'fail');
    this.failures.set(item.id, { id: item.id, stem: item.stem, kind, message: failText(kind, code, message) });
    this.host.changed();
  }

  /** 测试用：等这一轮跑完。 */
  settled(): Promise<void> {
    return this.loop ?? Promise.resolve();
  }
}
