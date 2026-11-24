// 配方卡小图（photo.looks）的全局上限：按字节记，最近最少用的先放掉，正在打开的那张照片的卡先不放。
// 每张卡只留现在用的那一张：动旋钮装上新的，旧的立刻关掉。
// 换相机配方文件或校准数据时全部清掉（旧的烘焙表画出来的小图不能再用）。
//
// 上限怎么定的（HOW.md：页面约 2 GiB 就会被系统刷新，一次只处理一张全尺寸 DNG 加一张相邻的）：
// 一张小图长边 192，约 192×128×4 = 96 KiB。32 MiB 约 340 张，够最近 30 来张照片各自的内置 8 个加用户配方，
// 只占页面预算的 1.6%。其余留给两份 DNG（各约 18 MB）、显卡源纹理、烘焙表（JS 16 张 + 显卡 24 张约 150 MiB）、
// 四个 WASM 堆和缩略图网格。

import type { Adjust, Engine, Geometry } from '../../../frontend/src/index';
import type { Photo } from './model';

export const LOOK_CACHE_BYTES = 32 << 20;

interface Entry {
  photo: Photo;
  key: string;
  /** 哪张卡的小图（同一张卡换了旋钮，旧的那张就关掉）。 */
  card: string | null;
  bitmap: ImageBitmap;
  bytes: number;
}

function sizeOf(bitmap: ImageBitmap): number {
  return Math.max(1, bitmap.width) * Math.max(1, bitmap.height) * 4;
}

export class LookCache {
  /** 插入顺序就是最近使用的顺序：最久没用的在最前面。 */
  private readonly entries = new Map<string, Entry>();
  private total = 0;
  private open: Photo | null = null;
  /** 每张照片每张卡现在用的那一张（键）。动旋钮装上新的，旧的立刻关掉，历史档位不会越攒越多（Sol r3）。 */
  private readonly current = new Map<string, string>();
  private clears = 0;

  private readonly limit: number;

  constructor(limit = LOOK_CACHE_BYTES) {
    this.limit = limit;
  }

  private id(photo: Photo, key: string): string {
    return `${photo.id}\u0000${key}`;
  }

  /** 编辑器打开了哪张（null：没有打开的）。这张的卡不淘汰。 */
  setOpen(photo: Photo | null): void {
    this.open = photo;
    this.trim();
  }

  /** 这张照片这个版本的小图；拿到就算用过一次。 */
  get(photo: Photo, key: string): ImageBitmap | null {
    const look = photo.looks.get(key);
    if (!look || look.version !== photo.version) return null;
    const id = this.id(photo, key);
    const entry = this.entries.get(id);
    if (entry) {
      this.entries.delete(id);
      this.entries.set(id, entry);
    }
    return look.bitmap;
  }

  put(photo: Photo, key: string, version: number, bitmap: ImageBitmap, card: string | null = null): void {
    this.remove(photo, key);
    if (card !== null) {
      const slot = this.id(photo, card);
      const old = this.current.get(slot);
      if (old !== undefined && old !== key) this.remove(photo, old);
      this.current.set(slot, key);
    }
    photo.looks.set(key, { version, bitmap });
    const entry: Entry = { photo, key, card, bitmap, bytes: sizeOf(bitmap) };
    this.entries.set(this.id(photo, key), entry);
    this.total += entry.bytes;
    this.trim();
  }

  private remove(photo: Photo, key: string): void {
    const id = this.id(photo, key);
    const entry = this.entries.get(id);
    if (entry) {
      this.entries.delete(id);
      this.total -= entry.bytes;
      if (entry.card !== null && this.current.get(this.id(photo, entry.card)) === key) this.current.delete(this.id(photo, entry.card));
    }
    const look = photo.looks.get(key);
    if (look) {
      look.bitmap.close();
      photo.looks.delete(key);
    }
  }

  /** 放掉一张照片的全部小图（换版本、去掉、清空时）。 */
  forget(photo: Photo): void {
    for (const key of [...photo.looks.keys()]) this.remove(photo, key);
  }

  /** 换了相机配方文件或校准数据：全部放掉。 */
  clear(): void {
    for (const entry of [...this.entries.values()]) this.remove(entry.photo, entry.key);
    this.entries.clear();
    this.current.clear();
    this.total = 0;
    this.clears += 1;
  }

  /**
   * 先放别的照片最久没用的；还超就连打开那张也放（每张卡最多一张，正常远到不了上限）。上限任何时候都成立。
   */
  private trim(): void {
    if (this.total <= this.limit) return;
    for (const entry of [...this.entries.values()]) {
      if (this.total <= this.limit) return;
      if (this.open && entry.photo === this.open) continue;
      this.remove(entry.photo, entry.key);
    }
    for (const entry of [...this.entries.values()]) {
      if (this.total <= this.limit) return;
      this.remove(entry.photo, entry.key);
    }
  }

  stats(): { entries: number; bytes: number; limit: number; clears: number } {
    return { entries: this.entries.size, bytes: this.total, limit: this.limit, clears: this.clears };
  }
}

/** 整个页面一份。 */
export const lookCache = new LookCache();

export interface BakeJob<T> {
  id: string;
  item: T;
}

export interface CardBakeHooks<T> {
  isAlive(): boolean;
  isBusy(): boolean;
  isPriority?(id: string): boolean;
  run(item: T): Promise<void>;
}

interface IdleWindow {
  requestIdleCallback?: (cb: () => void, opts?: { timeout?: number }) => number;
  cancelIdleCallback?: (id: number) => void;
}

/**
 * 配方卡可见优先与点击抢占调度器：
 * - 舞台烘焙进行中 / 拖动 / 导出时暂停派发，用户点选立即抢占；
 * - 视口内可见卡片优先，并发上限 2（留出空闲 Worker 给用户即时点击）；
 * - 离屏卡片等浏览器空闲再按并发 1 逐张烘焙。
 */
export class CardBakeController<T> {
  private readonly visibleLimit: number;
  private readonly idleLimit: number;
  private gen = 0;
  private pending: Array<BakeJob<T>> = [];
  private hooks: CardBakeHooks<T> | null = null;
  private inFlight = 0;
  private idleHandle: { kind: 'idle' | 'timer'; id: number } | null = null;
  private waitVisTimer = 0;
  private busyTimer = 0;
  private visFallback = false;
  private stageWant = '';
  private stageWaiters: Array<() => void> = [];
  private readonly visibleIds = new Set<string>();
  private visObserver: IntersectionObserver | null = null;
  private ioReady = false;

  constructor(visibleLimit = 2, idleLimit = 1) {
    this.visibleLimit = visibleLimit;
    this.idleLimit = idleLimit;
  }

  observe(nodes: Iterable<HTMLElement>): void {
    this.clearObserver();
    this.ioReady = false;
    const list = [...nodes];
    if (typeof IntersectionObserver !== 'function') {
      this.ioReady = true;
      for (let i = 0; i < Math.min(6, list.length); i += 1) {
        const id = list[i].dataset?.card;
        if (id) this.visibleIds.add(id);
      }
      return;
    }
    this.visObserver = new IntersectionObserver(
      (entries) => {
        this.ioReady = true;
        let changed = false;
        for (const entry of entries) {
          const id = (entry.target as HTMLElement).dataset?.card;
          if (!id) continue;
          if (entry.isIntersecting) {
            if (!this.visibleIds.has(id)) {
              this.visibleIds.add(id);
              changed = true;
            }
          } else if (this.visibleIds.delete(id)) {
            changed = true;
          }
        }
        if (changed || entries.length > 0) this.wake();
      },
      { root: null },
    );
    for (const node of list) this.visObserver.observe(node);
  }

  clearObserver(): void {
    this.visObserver?.disconnect();
    this.visObserver = null;
    this.visibleIds.clear();
  }

  beginStage(want: string): void {
    this.stageWant = want;
  }

  endStage(want: string): void {
    if (this.stageWant !== want) return;
    this.stageWant = '';
    this.flushStageWaiters();
    this.wake();
  }

  clearStage(): void {
    if (!this.stageWant) return;
    this.stageWant = '';
    this.flushStageWaiters();
    this.wake();
  }

  async waitUntilReady(alive: () => boolean, isBusy: () => boolean): Promise<void> {
    while (alive() && (this.stageWant !== '' || isBusy())) {
      if (this.stageWant !== '' && !isBusy()) {
        await new Promise<void>((done) => this.stageWaiters.push(done));
      } else {
        await new Promise((done) => setTimeout(done, 120));
      }
    }
  }

  start(jobs: Array<BakeJob<T>>, hooks: CardBakeHooks<T>): void {
    this.cancel();
    this.pending = jobs.slice();
    this.hooks = hooks;
    this.visFallback = false;
    this.pump();
  }

  wake(): void {
    if (!this.hooks || !this.pending.length) return;
    if (this.waitVisTimer && this.ioReady) {
      clearTimeout(this.waitVisTimer);
      this.waitVisTimer = 0;
    }
    this.pump();
  }

  cancel(): void {
    this.gen += 1;
    this.pending = [];
    this.hooks = null;
    this.inFlight = 0;
    this.visFallback = false;
    this.clearIdle();
    if (this.waitVisTimer) {
      clearTimeout(this.waitVisTimer);
      this.waitVisTimer = 0;
    }
    if (this.busyTimer) {
      clearTimeout(this.busyTimer);
      this.busyTimer = 0;
    }
  }

  destroy(): void {
    this.stageWant = '';
    this.flushStageWaiters();
    this.cancel();
    this.clearObserver();
  }

  private flushStageWaiters(): void {
    const waiters = this.stageWaiters;
    this.stageWaiters = [];
    for (const wake of waiters) wake();
  }

  private clearIdle(): void {
    if (!this.idleHandle) return;
    const h = this.idleHandle;
    this.idleHandle = null;
    const w = globalThis as unknown as IdleWindow;
    if (h.kind === 'idle' && typeof w.cancelIdleCallback === 'function') w.cancelIdleCallback(h.id);
    else clearTimeout(h.id);
  }

  private scheduleIdle(): void {
    if (this.idleHandle) return;
    const gen = this.gen;
    const run = () => {
      this.idleHandle = null;
      if (gen !== this.gen) return;
      this.pumpIdle();
    };
    const w = globalThis as unknown as IdleWindow;
    if (typeof w.requestIdleCallback === 'function') {
      this.idleHandle = { kind: 'idle', id: w.requestIdleCallback(run, { timeout: 600 }) };
    } else {
      this.idleHandle = { kind: 'timer', id: setTimeout(run, 80) as unknown as number };
    }
  }

  private scheduleBusyRetry(): void {
    if (this.busyTimer) return;
    const gen = this.gen;
    this.busyTimer = setTimeout(() => {
      this.busyTimer = 0;
      if (gen !== this.gen) return;
      this.pump();
    }, 150) as unknown as number;
  }

  private paused(hooks: CardBakeHooks<T>): boolean {
    if (this.stageWant !== '') return true;
    if (hooks.isBusy()) {
      this.scheduleBusyRetry();
      return true;
    }
    return false;
  }

  private takeNext(visibleOnly: boolean): BakeJob<T> | null {
    const hooks = this.hooks;
    if (!hooks || !this.pending.length) return null;
    if (hooks.isPriority) {
      const prio = this.pending.findIndex((job) => hooks.isPriority!(job.id) && (!visibleOnly || this.visibleIds.has(job.id)));
      if (prio >= 0) return this.pending.splice(prio, 1)[0];
    }
    if (visibleOnly) {
      const idx = this.pending.findIndex((job) => this.visibleIds.has(job.id));
      return idx >= 0 ? this.pending.splice(idx, 1)[0] : null;
    }
    return this.pending.shift() ?? null;
  }

  private launch(job: BakeJob<T>): void {
    const hooks = this.hooks;
    if (!hooks) return;
    const gen = this.gen;
    this.inFlight += 1;
    void hooks
      .run(job.item)
      .catch(() => undefined)
      .finally(() => {
        if (gen !== this.gen) return;
        this.inFlight = Math.max(0, this.inFlight - 1);
        this.pump();
      });
  }

  private pump(): void {
    const hooks = this.hooks;
    if (!hooks || !this.pending.length) return;
    if (!hooks.isAlive()) {
      this.cancel();
      return;
    }
    if (this.paused(hooks)) return;
    if (!this.ioReady && !this.visFallback) {
      if (!this.waitVisTimer) {
        const gen = this.gen;
        this.waitVisTimer = setTimeout(() => {
          this.waitVisTimer = 0;
          if (gen !== this.gen) return;
          this.visFallback = true;
          this.pump();
        }, 32) as unknown as number;
      }
      return;
    }
    while (this.inFlight < this.visibleLimit) {
      if (!hooks.isAlive() || this.paused(hooks)) return;
      const next = this.takeNext(true);
      if (!next) break;
      this.clearIdle();
      this.launch(next);
    }
    if (this.pending.length > 0 && this.inFlight === 0) {
      this.scheduleIdle();
    }
  }

  private pumpIdle(): void {
    const hooks = this.hooks;
    if (!hooks || !this.pending.length) return;
    if (!hooks.isAlive()) {
      this.cancel();
      return;
    }
    if (this.paused(hooks)) return;
    if (this.pending.some((job) => this.visibleIds.has(job.id))) {
      this.pump();
      return;
    }
    while (this.inFlight < this.idleLimit) {
      if (!hooks.isAlive() || this.paused(hooks)) return;
      const next = this.takeNext(false);
      if (!next) break;
      this.launch(next);
    }
  }
}

export interface CardThumbRunnerOpts {
  engine: Engine;
  source: string;
  photo: Photo;
  version: number;
  cardId: string;
  geometry: Geometry;
  adjust: Adjust;
  alive(): boolean;
  isBusy(): boolean;
  bakeCtrl: CardBakeController<unknown>;
  bakeKey(): string | null;
  ready(key: string): boolean;
  gpuKey(key: string): string | null;
  bake(): Promise<string>;
  onError(err: unknown): void;
  onPainted(): void;
}

/** 单张配方卡：复用已烘焙键或按需烘焙，等舞台/拖动空闲后渲染 192px 小图并落入 lookCache。 */
export async function renderCardThumb(opts: CardThumbRunnerOpts): Promise<void> {
  const existing = opts.bakeKey();
  if (existing && lookCache.get(opts.photo, existing)) return;
  let key: string;
  if (existing && opts.ready(existing) && opts.gpuKey(existing)) {
    key = existing;
  } else {
    try {
      key = await opts.bake();
    } catch (err) {
      if (opts.alive()) opts.onError(err);
      return;
    }
  }
  await opts.bakeCtrl.waitUntilReady(opts.alive, opts.isBusy);
  if (!opts.alive()) return;
  let gpu = opts.gpuKey(key);
  if (!gpu) {
    try {
      await opts.bake();
    } catch {
      return;
    }
    if (!opts.alive()) return;
    gpu = opts.gpuKey(key);
    if (!gpu) return;
  }
  const out = await opts.engine.thumb({
    source: opts.source,
    look: { recipe: gpu, lut: null, lutStrength: 1, curve: null, adjust: opts.adjust },
    geometry: opts.geometry,
    aspect: null,
    maxEdge: 192,
  });
  if (!out || !opts.alive()) return;
  const bitmap = await createImageBitmap(new ImageData(new Uint8ClampedArray(out.data), out.w, out.h));
  if (!opts.alive() || lookCache.get(opts.photo, key)) {
    bitmap.close();
    return;
  }
  lookCache.put(opts.photo, key, opts.version, bitmap, opts.cardId);
  opts.onPainted();
}
