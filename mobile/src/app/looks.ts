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
  /** 正在舞台烘焙的卡不占用后台并发槽，等舞台就绪后再画小图。 */
  isDeferred?(id: string): boolean;
  isPriority?(id: string): boolean;
  run(item: T): Promise<void>;
}

interface IdleWindow {
  requestIdleCallback?: (cb: () => void, opts?: { timeout?: number }) => number;
  cancelIdleCallback?: (id: number) => void;
}

/** 与 core/pool.ts 保持一致的 Worker 数量推算（不改 pool.ts）。 */
export function coreWorkerCount(cores?: number): number {
  const raw = (cores ?? (typeof navigator !== 'undefined' ? navigator.hardwareConcurrency : 0)) || 2;
  return Math.min(4, Math.max(1, Math.floor(raw) - 1));
}

/**
 * 现行 core/pool.ts 按严格轮询分派 Worker，静默离屏并发上限先钉在 2（保留一半空闲 Worker 避免点击撞车）；
 * 待 perf-startup 合入最闲路由（pickSlot）后再按需调高。
 */
export const QUIET_IDLE_BAKE_CAP = 2;

/**
 * 配方卡可见优先与点击抢占调度器：
 * - 按实际 Worker 数量计算后台并发上限（N >= 2 时始终至少预留 1 个空闲 Worker 给用户点击）；
 * - 跨 cancel() 保持真实的在途 running 计数，只在实际任务结束（resolve/reject）时递减；
 * - 用户主动点击选卡或调旋钮时立即抢占并暂停派发新的后台任务，直到所点配方落到舞台；
 * - 离屏卡片在无用户交互的空闲期按 min(QUIET_IDLE_BAKE_CAP, bgLimit) 并行烘焙，一旦检测到编辑器任意输入立即回落至并发 1。
 */
export class CardBakeController<T> {
  private readonly workers: number;
  private gen = 0;
  private pending: Array<BakeJob<T>> = [];
  private hooks: CardBakeHooks<T> | null = null;
  private running = 0;
  private idleMode = false;
  private idleHandle: { kind: 'idle' | 'timer'; id: number } | null = null;
  private waitVisTimer = 0;
  private busyTimer = 0;
  private quietTimer = 0;
  private quietUntil = 0;
  private visFallback = false;
  private tapWant = '';
  private stageCardId = '';
  private tapWaiters: Array<() => void> = [];
  private readonly visibleIds = new Set<string>();
  private visObserver: IntersectionObserver | null = null;
  private inputTarget: EventTarget | null = null;
  private inputStrip: EventTarget | null = null;
  private inputDetach: (() => void) | null = null;
  private ioReady = false;

  constructor(workerCount?: number) {
    this.workers = Math.max(1, Math.floor(workerCount ?? coreWorkerCount()));
  }

  attachInput(target: EventTarget | null, strip?: EventTarget | null): void {
    const nextStrip = strip ?? null;
    if (this.inputDetach && this.inputTarget === target && this.inputStrip === nextStrip) return;
    this.detachInput();
    const onInput = (): void => this.noteInput();
    const opts = { passive: true } as AddEventListenerOptions;
    const events = ['pointerdown', 'touchstart', 'wheel', 'keydown'];
    if (target) {
      for (const ev of events) target.addEventListener(ev, onInput, opts);
    }
    if (nextStrip) nextStrip.addEventListener('scroll', onInput, opts);
    this.inputTarget = target;
    this.inputStrip = nextStrip;
    this.inputDetach = () => {
      if (target) {
        for (const ev of events) target.removeEventListener(ev, onInput, opts);
      }
      if (nextStrip) nextStrip.removeEventListener('scroll', onInput, opts);
    };
  }

  noteInput(cooldownMs = 1200): void {
    const until = Date.now() + cooldownMs;
    if (until > this.quietUntil) this.quietUntil = until;
    if (this.running >= 1) this.clearIdle();
    if (this.quietTimer) clearTimeout(this.quietTimer);
    this.quietTimer = setTimeout(() => {
      this.quietTimer = 0;
      if (Date.now() >= this.quietUntil) this.wake();
    }, cooldownMs + 20) as unknown as number;
  }

  hasVisible(): boolean {
    return this.visibleIds.size > 0;
  }

  syncVisible(nodes: Iterable<HTMLElement>, container?: HTMLElement | null): void {
    if (!container || !container.isConnected || typeof container.getBoundingClientRect !== 'function') return;
    const sr = container.getBoundingClientRect();
    if (sr.width <= 0 || sr.height <= 0) return;
    let found = 0;
    for (const node of nodes) {
      const id = node.dataset?.card;
      if (!id || typeof node.getBoundingClientRect !== 'function') continue;
      const r = node.getBoundingClientRect();
      if (r.width > 0 && r.height > 0 && r.right > sr.left && r.left < sr.right && r.bottom > sr.top && r.top < sr.bottom) {
        this.visibleIds.add(id);
        found += 1;
      }
    }
    if (found > 0) this.ioReady = true;
  }

  observe(nodes: Iterable<HTMLElement>, container?: HTMLElement | null): void {
    this.clearObserver();
    this.ioReady = false;
    const list = [...nodes];
    this.syncVisible(list, container);
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

  beginStage(want: string, cardId: string, userTap: boolean): void {
    this.stageCardId = cardId;
    if (userTap) {
      this.tapWant = want;
      this.noteInput();
    }
  }

  endStage(want: string, cardId: string): void {
    let wake = false;
    if (this.stageCardId === cardId) {
      this.stageCardId = '';
      wake = true;
    }
    if (this.tapWant === want) {
      this.tapWant = '';
      this.flushTapWaiters();
      wake = true;
    }
    if (wake) this.wake();
  }

  clearStage(): void {
    const had = this.tapWant !== '' || this.stageCardId !== '';
    this.stageCardId = '';
    if (this.tapWant) {
      this.tapWant = '';
      this.flushTapWaiters();
    }
    if (had) this.wake();
  }

  isStageBaking(cardId: string): boolean {
    return this.stageCardId !== '' && this.stageCardId === cardId;
  }

  async waitUntilReady(alive: () => boolean, isBusy: () => boolean): Promise<boolean> {
    let waited = false;
    while (alive() && (this.tapWant !== '' || isBusy())) {
      waited = true;
      if (this.tapWant !== '' && !isBusy()) {
        await new Promise<void>((done) => this.tapWaiters.push(done));
      } else {
        await new Promise((done) => setTimeout(done, 120));
      }
    }
    return waited;
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
    this.idleMode = false;
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
    this.tapWant = '';
    this.stageCardId = '';
    this.flushTapWaiters();
    this.cancel();
    this.clearObserver();
    this.detachInput();
    if (this.quietTimer) {
      clearTimeout(this.quietTimer);
      this.quietTimer = 0;
    }
  }

  private detachInput(): void {
    this.inputDetach?.();
    this.inputDetach = null;
    this.inputTarget = null;
    this.inputStrip = null;
  }

  private bgLimit(): number {
    const stageActive = this.stageCardId !== '' ? 1 : 0;
    const maxTotal = Math.max(1, this.workers - 1);
    return Math.max(0, maxTotal - stageActive);
  }

  private currentIdleLimit(): number {
    const limit = this.bgLimit();
    if (Date.now() < this.quietUntil) return Math.min(1, limit);
    return Math.min(QUIET_IDLE_BAKE_CAP, limit);
  }

  private flushTapWaiters(): void {
    const waiters = this.tapWaiters;
    this.tapWaiters = [];
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
    if (this.tapWant !== '') return true;
    if (hooks.isBusy()) {
      this.scheduleBusyRetry();
      return true;
    }
    return false;
  }

  private takeNext(visibleOnly: boolean): BakeJob<T> | null {
    const hooks = this.hooks;
    if (!hooks || !this.pending.length) return null;
    const eligible = (job: BakeJob<T>): boolean => !(hooks.isDeferred && hooks.isDeferred(job.id));
    if (hooks.isPriority) {
      const prio = this.pending.findIndex((job) => eligible(job) && hooks.isPriority!(job.id) && (!visibleOnly || this.visibleIds.has(job.id)));
      if (prio >= 0) return this.pending.splice(prio, 1)[0];
    }
    if (visibleOnly) {
      const idx = this.pending.findIndex((job) => eligible(job) && this.visibleIds.has(job.id));
      return idx >= 0 ? this.pending.splice(idx, 1)[0] : null;
    }
    const idx = this.pending.findIndex(eligible);
    return idx >= 0 ? this.pending.splice(idx, 1)[0] : null;
  }

  private launch(job: BakeJob<T>): void {
    const hooks = this.hooks;
    if (!hooks) return;
    this.running += 1;
    void hooks
      .run(job.item)
      .catch(() => undefined)
      .finally(() => {
        this.running = Math.max(0, this.running - 1);
        if (this.hooks && this.pending.length > 0) this.pump();
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
    const limit = this.bgLimit();
    while (this.running < limit) {
      if (!hooks.isAlive() || this.paused(hooks)) return;
      const next = this.takeNext(true);
      if (!next) break;
      this.idleMode = false;
      this.clearIdle();
      this.launch(next);
    }
    if (this.pending.length > 0 && this.stageCardId === '' && (this.running === 0 || (this.idleMode && this.running < this.currentIdleLimit()))) {
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
    if (this.paused(hooks) || this.stageCardId !== '') return;
    const eligibleVisible = (job: BakeJob<T>): boolean =>
      !(hooks.isDeferred && hooks.isDeferred(job.id)) && this.visibleIds.has(job.id);
    if (this.pending.some(eligibleVisible)) {
      this.pump();
      return;
    }
    this.idleMode = true;
    const limit = this.currentIdleLimit();
    while (this.running < limit) {
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
  geometry(): Promise<Geometry>;
  adjust: Adjust;
  alive(): boolean;
  isBusy(): boolean;
  bakeCtrl: CardBakeController<unknown>;
  bakeKey(): string | null;
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
  let gpu = existing ? opts.gpuKey(existing) : null;
  if (existing && gpu) {
    key = existing;
  } else {
    try {
      key = await opts.bake();
    } catch (err) {
      if (opts.alive()) opts.onError(err);
      return;
    }
  }
  const waited = await opts.bakeCtrl.waitUntilReady(opts.alive, opts.isBusy);
  if (!opts.alive()) return;
  if (!gpu || waited) gpu = opts.gpuKey(key);
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
  const geometry = await opts.geometry();
  if (!opts.alive()) return;
  const out = await opts.engine.thumb({
    source: opts.source,
    look: { recipe: gpu, lut: null, lutStrength: 1, curve: null, adjust: opts.adjust },
    geometry,
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
