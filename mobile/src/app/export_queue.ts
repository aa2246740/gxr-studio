// 导出队列、暂存成片与失败保留状态机（不碰 DOM，供 ExportFlow 与单元测试共用）。
// 覆盖：双击去重、批次结束后自动推进排队、失败保留与重试、已共享分批不重复提供、运行中保存已做好成片。

import type { QualityTier, SizeTier } from '../lib/export_request';
import { halfframeFileCount, normalizedHalfframeMode, type HalfframeMode } from '../lib/halfframe';
import type { Failure, ItemState, Phase } from './export_batch';
import type { ReadyEntry } from './export_ready';

export interface ExportOptions {
  size: SizeTier;
  quality: QualityTier;
  hdr: boolean;
  halfframe?: HalfframeMode;
}

export interface QueuedJob<TPhoto extends { id: string; diptych?: boolean }> {
  photos: TPhoto[];
  options: ExportOptions;
  /** 保留失败重试时精确限定要重做的条目 id（如 `<photoId>#half:2`），绝不重做同双联里已成功的文件。 */
  onlyIds?: string[];
}

export interface CarriedFailure<TPhoto extends { id: string; diptych?: boolean }> {
  failure: Failure;
  photo: TPhoto;
  options: ExportOptions;
}

export interface ActiveBatchView {
  phase: Phase;
  states: ReadonlyMap<string, ItemState>;
  failures: ReadonlyMap<string, Failure>;
  staged: ReadonlyArray<{ id: string; file: File }>;
  savedCount(): number;
}

const HALFFRAME_ENTRY_SUFFIX = /#half:[12]$/;

/** 拆出单格后的批次条目 id（`<photoId>#half:1` / `#half:2`）还原为原照片 id；普通照片（即使含 `#`）原样返回。 */
export function basePhotoId(entryId: string): string {
  return entryId.replace(HALFFRAME_ENTRY_SUFFIX, '');
}

function optionsKey(o: ExportOptions): string {
  return `${o.size}|${o.quality}|${o.hdr ? '1' : '0'}|${normalizedHalfframeMode(o.halfframe)}`;
}

export class ExportQueueState<TPhoto extends { id: string; diptych?: boolean }> {
  /** 前一批已完成、还没存进相册的成片（绝不含已经共享过的分批）。 */
  carryover: ReadyEntry[] = [];
  /** 当前批次在跑（或停在待存上限）时排队的导出任务。 */
  pendingQueue: QueuedJob<TPhoto>[] = [];
  /** 前一批保留下来的失败项及当时的照片/选项，用于展示失败原因与再次重试。 */
  readonly carriedFailures = new Map<string, CarriedFailure<TPhoto>>();
  /** 当前就绪条多批共享里已经成功交出的条目 id，切到新导出时绝不再拷进 carryover。 */
  readonly sharedIds = new Set<string>();

  private currentOptions: ExportOptions | null = null;
  private currentPhotos = new Map<string, TPhoto>();
  private carriedSaved = 0;

  queuedCount(): number {
    return this.pendingQueue.reduce(
      (sum, job) => sum + (job.onlyIds ? job.onlyIds.length : halfframeFileCount(job.photos, job.options.halfframe)),
      0,
    );
  }

  carryoverBytes(): number {
    return this.carryover.reduce((sum, item) => sum + item.file.size, 0);
  }

  savedCount(batch: ActiveBatchView | null): number {
    const inBatch = batch ? batch.savedCount() : 0;
    return this.carriedSaved + inBatch + this.sharedIds.size;
  }

  /** 判断某张照片是否正在导出、已排队，或已做好但还没存进相册。 */
  isQuickBlocked(id: string, batch: ActiveBatchView | null): boolean {
    if (this.carryover.some((entry) => !this.sharedIds.has(entry.id) && basePhotoId(entry.id) === id)) return true;
    for (const job of this.pendingQueue) {
      if (job.photos.some((p) => p.id === id)) return true;
    }
    if (batch && batch.phase !== 'closed') {
      // 只有批次还在跑（或停在待存上限）时，没做完的照片才算「正在导出」；停止以后可以重新点「存到相册」
      const live = batch.phase === 'running' || batch.phase === 'held';
      for (const [entryId, state] of batch.states) {
        if (basePhotoId(entryId) !== id) continue;
        if (live && (state === 'wait' || state === 'run')) return true;
        if (state === 'ok' && !this.sharedIds.has(entryId)) return true;
      }
    }
    return false;
  }

  /** 就绪条要交出的全部成片：同一张照片在新批次里又做好了，就只交新的（旧的留着备用，直到新的做好）。 */
  readyEntries(carried: readonly ReadyEntry[], current: readonly ReadyEntry[]): ReadyEntry[] {
    const fresh = new Set(current.map((e) => e.id));
    return [...carried.filter((e) => !fresh.has(e.id)), ...current];
  }

  /** 快捷「存到相册」去重：忽略已在运行、排队或待存中的照片，同时去除本次重复 id。 */
  filterQuickPhotos(photos: readonly TPhoto[], batch: ActiveBatchView | null): TPhoto[] {
    const seen = new Set<string>();
    const out: TPhoto[] = [];
    for (const photo of photos) {
      if (seen.has(photo.id)) continue;
      seen.add(photo.id);
      if (this.isQuickBlocked(photo.id, batch)) continue;
      out.push(photo);
    }
    return out;
  }

  private clearCarriedFailuresFor(photos: readonly TPhoto[], onlyIds?: readonly string[]): void {
    if (onlyIds) {
      for (const entryId of onlyIds) this.carriedFailures.delete(entryId);
      return;
    }
    const photoIds = new Set(photos.map((p) => p.id));
    for (const entryId of [...this.carriedFailures.keys()]) {
      if (photoIds.has(basePhotoId(entryId))) this.carriedFailures.delete(entryId);
    }
  }

  beginBatch(photos: readonly TPhoto[], options: ExportOptions, onlyIds?: readonly string[]): void {
    this.sharedIds.clear();
    this.currentOptions = { ...options };
    this.currentPhotos = new Map(photos.map((p) => [p.id, p]));
    this.clearCarriedFailuresFor(photos, onlyIds);
  }

  /** 把旧批次里「尚未共享」的成片与全部失败保留下来；已经共享过的分批绝不拷回 carryover。 */
  preserveBatch(batch: ActiveBatchView, entries: readonly ReadyEntry[]): void {
    // 先从现有 carryover 里移除已经共享过的条目（切到新导出时不再重复提供）。
    if (this.sharedIds.size) {
      let removedCarry = 0;
      this.carryover = this.carryover.filter((entry) => {
        if (!this.sharedIds.has(entry.id)) return true;
        removedCarry += 1;
        return false;
      });
      // 把本批次里已共享但尚未调用 release() 标成 saved 的条目计入 carriedSaved。
      const batchShared = entries.filter((e) => this.sharedIds.has(e.id)).length;
      this.carriedSaved += removedCarry + batchShared;
    }
    this.carriedSaved += batch.savedCount();

    for (const entry of entries) {
      if (this.sharedIds.has(entry.id)) continue;
      const idx = this.carryover.findIndex((c) => c.id === entry.id);
      if (idx >= 0) this.carryover[idx] = entry;
      else this.carryover.push(entry);
    }

    if (this.currentOptions) {
      for (const [id, failure] of batch.failures) {
        const photo = this.currentPhotos.get(basePhotoId(id));
        if (photo) {
          this.carriedFailures.set(id, {
            failure,
            photo,
            options: { ...this.currentOptions },
          });
        }
      }
    }
    this.sharedIds.clear();
  }

  /** 开始或排队新导出；返回 'queued' 表示已入队，'launch' 表示调用方可立即启动新批次。 */
  enqueueOrPrepareStart(
    photos: TPhoto[],
    options: ExportOptions,
    batch: ActiveBatchView | null,
    batchEntries: (b: ActiveBatchView) => ReadyEntry[],
  ): 'queued' | 'launch' {
    this.clearCarriedFailuresFor(photos);
    if (batch && (batch.phase === 'running' || batch.phase === 'held')) {
      this.pendingQueue.push({ photos, options: { ...options } });
      return 'queued';
    }
    if (batch && batch.phase !== 'closed') {
      this.preserveBatch(batch, batchEntries(batch));
    } else if (!this.carryover.length) {
      this.carriedSaved = 0; // 上一轮都存完了：这是新的一轮，「已存」从 0 数
    }
    this.beginBatch(photos, options);
    return 'launch';
  }

  /**
   * 批次进入任意非运行、非 held 状态（含失败后 ready、closed、stopped）且队列里还有任务时，
   * 保留旧批次未共享的成片和失败项，并取出下一项立即执行。
   */
  takeNextQueued(
    batch: ActiveBatchView | null,
    batchEntries: (b: ActiveBatchView) => ReadyEntry[],
  ): QueuedJob<TPhoto> | null {
    if (!this.pendingQueue.length) return null;
    if (batch && (batch.phase === 'running' || batch.phase === 'held')) return null;
    const next = this.pendingQueue.shift() ?? null;
    if (!next) return null;
    if (batch && batch.phase !== 'closed') {
      this.preserveBatch(batch, batchEntries(batch));
    } else if (!this.carryover.length) {
      this.carriedSaved = 0;
    }
    this.beginBatch(next.photos, next.options, next.onlyIds);
    return next;
  }

  /** 多批共享中刚完成一批（还不是最后一批）：记下已共享 id，避免切到新导出时再次提供。 */
  markChunkShared(ids: readonly string[]): void {
    for (const id of ids) this.sharedIds.add(id);
  }

  /** 运行中直接保存 carryover 成片（UX #11）：立刻移出已共享的 carryover 条目，不碰运行中的批次。 */
  consumeCarryover(ids: readonly string[]): void {
    const done = new Set(ids);
    const before = this.carryover.length;
    this.carryover = this.carryover.filter((entry) => !done.has(entry.id));
    this.carriedSaved += before - this.carryover.length;
  }

  /** 最后一批交出后：清空 carryover 与分批共享标记；保留 carriedFailures 以便用户继续重试。 */
  finishShare(): void {
    const carryCount = this.carryover.length;
    this.carryover = [];
    this.sharedIds.clear();
    // carryover 里的条目不在当前 batch.savedCount() 中，计入 carriedSaved。
    this.carriedSaved += carryCount;
  }

  /** 汇总当前保留失败与活动批次失败（同一 id 以当前批次为准）。 */
  allFailures(batch: ActiveBatchView | null): Failure[] {
    const merged = new Map<string, Failure>();
    for (const [id, carried] of this.carriedFailures) merged.set(id, carried.failure);
    if (batch && batch.phase !== 'closed') {
      for (const [id, failure] of batch.failures) merged.set(id, failure);
    }
    return [...merged.values()];
  }

  /** 取出全部保留的失败项，按导出选项分组成可立即启动或入队的重试任务（精确记录失败条目 id，不重做已成功的文件）。 */
  takeCarriedRetryJobs(resolvePhoto?: (photo: TPhoto) => TPhoto): QueuedJob<TPhoto>[] {
    if (!this.carriedFailures.size) return [];
    const grouped = new Map<string, { job: QueuedJob<TPhoto>; seen: Set<string>; onlyIds: Set<string> }>();
    for (const [entryId, item] of this.carriedFailures) {
      const key = optionsKey(item.options);
      let bucket = grouped.get(key);
      if (!bucket) {
        bucket = { job: { photos: [], options: { ...item.options } }, seen: new Set(), onlyIds: new Set() };
        grouped.set(key, bucket);
      }
      const photo = resolvePhoto ? resolvePhoto(item.photo) : item.photo;
      if (!bucket.seen.has(photo.id)) {
        bucket.seen.add(photo.id);
        bucket.job.photos.push(photo);
      }
      bucket.onlyIds.add(entryId);
    }
    this.carriedFailures.clear();
    return [...grouped.values()].map((b) => ({ ...b.job, onlyIds: [...b.onlyIds] }));
  }

  /** 点「停止」：清空排队任务（不留幽灵任务），但保留已做好成片与已有失败。 */
  stop(): void {
    this.pendingQueue = [];
  }

  /** 点「关闭」：清空排队任务、待存成片与失败记录。 */
  dismiss(): void {
    this.pendingQueue = [];
    this.carryover = [];
    this.carriedFailures.clear();
    this.sharedIds.clear();
    this.currentOptions = null;
    this.currentPhotos.clear();
    this.carriedSaved = 0;
  }

  /** 清空照片：全部重置。 */
  clear(): void {
    this.dismiss();
  }
}
