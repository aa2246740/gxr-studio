// 一批导出：点「存到相册」那一刻把照片、文件句柄、版本和请求定下来（桌面 N01），之后改动不影响这一批。
// 一次一张交给导出 Worker；做好的文件留在内存里等用户点「存到相册」交给系统共享面板。不碰 DOM，测试直接驱动。

import type { RunResult } from '../core/export_api';
import type { FailKind } from '../core/export_protocol';
import type { ExportRequest } from '../lib/export_request';

/** 做好、还没交出去的文件最多占这么多字节。下一张会超过时先停在「做好了」那一栏，交出去、点「好」以后再接着做。 */
export const STAGE_CAP_BYTES = 384 * 1024 * 1024;
/** 整页预算：同一阶段的 WASM+JS 峰值，加上待存估算，不超过 1.2 GiB。 */
export const PAGE_BUDGET_BYTES = Math.trunc(1.2 * 1024 * 1024 * 1024);

/** 待存估算（上限）加上同一阶段的峰值超过整页预算时，把上限降到刚好放得下，可以低于默认值。 */
export function stageCapFor(sameStagePeak: number, requestedCap: number): number {
  const room = PAGE_BUDGET_BYTES - Math.max(0, Math.trunc(sameStagePeak));
  if (room >= requestedCap) return requestedCap;
  return Math.max(0, room);
}

/** ink-text 量出的导出 Worker WASM 上限：4288×2848 的照片做全尺寸 blur_fill + 时间戳（画布 4875×6500）。 */
export const WASM_CEILING_BYTES = 512 * 1024 * 1024;
export const WASM_CEILING_HDR_BYTES = 768 * 1024 * 1024;
const CEILING_SOURCE_PX = 4288 * 2848;
const CEILING_CANVAS_PX = 4875 * 6500;

/** 估一张导出要的内存用到的尺寸（像素数）和文件大小，全部在开始算之前就知道。 */
export interface PeakInputs {
  /** 源图（DNG 或相机 JPG，取大的）的像素数。 */
  source: number;
  /** 成品里照片部分的像素数。 */
  photo: number;
  /** 成品画布（含边框）的像素数。 */
  canvas: number;
  /** 最大的源文件字节数（Worker 留着它给 export_complete 写 EXIF）。 */
  fileBytes: number;
  hdr: boolean;
}

/**
 * 一张导出在同一阶段里 WASM 与 JS 加起来的保守上界，开始算这一张之前估。
 * - WASM：ink-text 的上限（SDR 512 MiB、HDR 768 MiB），源图或画布比量上限时的大，就按比例放大。
 * - JS：Worker 报内存时手里同时拿着的东西：成品画布 RGB、日期戳掩码（照片大小）、HDR 数据层（照片大小 × 2 个 float32）和源文件。
 */
export function stagePeakEstimate(p: PeakInputs): number {
  const scale = Math.max(1, p.source / CEILING_SOURCE_PX, p.canvas / CEILING_CANVAS_PX);
  const wasm = (p.hdr ? WASM_CEILING_HDR_BYTES : WASM_CEILING_BYTES) * scale;
  const js = p.canvas * 3 + p.photo * (p.hdr ? 9 : 1) + p.fileBytes;
  return Math.ceil(wasm + js);
}
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
  cameraRecipeNote?: string;
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
  /** 到目前为止，同一阶段里 WASM 与 JS 加在一起的峰值。没有就当 0。 */
  memoryPeak?: () => number;
  /**
   * 开始算这一张之前：它同一阶段 WASM+JS 峰值的保守上界（`stagePeakEstimate`）。估不出来就抛错，按整页预算算。
   * 这张的文件读不了就抛 `FileUnreadable`：不去算它，按 `read` 结算。
   */
  peakEstimate?: (item: BatchItem) => Promise<number> | number;
}

/** 开始算之前就知道这张的文件读不了（卡拔了、文件删了或变了）。Worker 里整个读文件时也会是 `read`，所以不必去算它。 */
export class FileUnreadable extends Error {
  constructor() {
    super('read');
    this.name = 'FileUnreadable';
  }
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
  /** 每张开始前估的同阶段峰值上界（已观察到的峰值和 `peakEstimate` 取大）。 */
  readonly estimates = new Map<string, number>();
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
  /** 这一批点下去时的待存上限（SDR 384 MiB，HDR 256 MiB）。预算不够时 `cap` 可以再低。 */
  readonly requestedCap: number;
  cap: number;

  // 不用参数属性：Node 的 strip-types 跑单元测试时不认。
  constructor(items: BatchItem[], host: BatchHost, cap = STAGE_CAP_BYTES) {
    this.items = items;
    this.host = host;
    this.requestedCap = cap;
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
    // 交出去以后才把上限降到预算要的那么低（之前它不能低于已待存的字节）。
    this.fitCap();
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
      const id = this.queue[0];
      const item = this.item(id);
      if (!item) {
        this.queue.shift();
        continue;
      }
      // 开始算之前：这一张的峰值上界加上已待存的放不进整页预算，就先停在「做好了」那一栏，交出去再算（Sol final-r2 P2-05）。
      const room = await this.preflight(item);
      if (!this.live(token)) return;
      if (room === 'hold') {
        this.hold();
        return;
      }
      this.queue.shift();
      if (room === 'read') {
        // 文件读不了：不交给 Worker，和它在 Worker 里读不了一样结算（06f 那句「读不了」、M1 的 gone），接着做下一张。
        this.commit(item, { ok: false, kind: 'read', code: 'read', message: '' });
        continue;
      }
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
    const capFits = this.fitCap();
    const size = result.file.byteLength;
    if (!capFits || this.stagedBytes + size > this.cap) {
      if (!this.staged.length) {
        // 一张就超过上限：按内存不够算（不能因为手里没别的文件就越过上限，Sol r1 P2-05）。
        this.fail(item, 'trap', 'staged_cap', '');
        return;
      }
      // 留着会超过上限（或上限要降到已待存的以下）：这份结果放掉，这张排回队首，先停在「做好了」那一栏。交出去、点「好」以后再做它。
      this.states.set(item.id, 'wait');
      this.queue.unshift(item.id);
      this.hold();
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

  /**
   * 同一阶段的峰值加上待存上限超过整页预算时，把上限降下来。只降不升，而且任何时候都不低于已待存的字节：
   * 预算要的上限比已待存的还低时返回 false，调用方先停在 held，交出去以后 release() 再降到位。
   */
  private fitCap(): boolean {
    const fitted = stageCapFor(this.host.memoryPeak?.() ?? 0, this.requestedCap);
    if (fitted < this.cap) this.cap = Math.max(fitted, this.stagedBytes);
    return fitted >= this.stagedBytes;
  }

  /**
   * 开始算 `item` 之前：上限要降到已待存的以下，或者它的峰值上界（已观察到的峰值和 host 的估算取大）加上已待存的
   * 超过整页预算，就是 `hold`。手里没有待存的文件时不停：停下来也没有可以交出去的。估算失败按整页预算算；
   * 文件读不了（`FileUnreadable`）是 `read`，这张不算。
   */
  private async preflight(item: BatchItem): Promise<'run' | 'hold' | 'read'> {
    if (!this.fitCap()) return 'hold';
    let estimate = 0;
    if (this.host.peakEstimate) {
      try {
        estimate = await this.host.peakEstimate(item);
      } catch (err) {
        if (err instanceof FileUnreadable) return 'read';
        estimate = PAGE_BUDGET_BYTES;
      }
    }
    const bound = Math.max(this.host.memoryPeak?.() ?? 0, Number.isFinite(estimate) ? estimate : PAGE_BUDGET_BYTES);
    this.estimates.set(item.id, bound);
    return this.stagedBytes === 0 || bound + this.stagedBytes <= PAGE_BUDGET_BYTES ? 'run' : 'hold';
  }

  /** 停在「做好了」那一栏，等用户交出去（「存到相册」）、点「好」以后接着做。 */
  private hold(): void {
    this.phase = 'held';
    this.host.busy(false);
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
